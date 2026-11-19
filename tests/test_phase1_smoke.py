from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.core.config import load_settings
from src.core.database import Database
from src.core.llm_gateway import (
    BudgetExceeded,
    CycleUsage,
    InvalidCompletion,
    LLMGateway,
)
from src.domain.pricing_engine import price
from src.domain.proposal_writer import mock_copy, verify_no_numeric_hallucinations
from src.domain.schemas import DealContextInput
from src.integrations.bitrix_connector import MockBitrixConnector
from src.ui.presets import BASE

GENERATE = "/api/v1/proposals/generate"
RECONCILE = "/api/v1/proposals/reconcile"


@pytest.fixture
def settings_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    async def no_wait(self: MockBitrixConnector) -> None:
        pass

    monkeypatch.setattr(MockBitrixConnector, "_pace", no_wait)
    return {
        "database": tmp_path / "test.db",
        "artifacts": tmp_path / "artifacts",
        "template": tmp_path / "template.docx",
    }


def test_decimal_rounding() -> None:
    settings = load_settings()
    context = DealContextInput.model_validate(BASE | {
        "desired_discount_pct": "10",
        "items": [{"name": "Товар", "quantity": "1.005", "unit_price": "1.00",
                   "unit_cost": "0.10", "vat_pct": "20"}],
    })
    financials, audit = price(context, settings.vat_pct, settings.discount_matrix["standard"])
    line = financials[0].lines[0]
    assert line.subtotal == Decimal("1.01")
    assert line.discount == Decimal("0.10")
    assert line.vat == Decimal("0.18")
    assert line.total == Decimal("1.09")
    assert audit.is_approved


def test_deal_contract_and_three_tiers() -> None:
    settings = load_settings()
    with pytest.raises(ValueError):
        DealContextInput.model_validate(BASE | {"unknown": True})
    context = DealContextInput.model_validate(BASE | {"mode": "three_tiers"})
    financials, _ = price(context, settings.vat_pct, settings.discount_matrix["standard"])
    assert [variant.lines[0].discount_pct for variant in financials] == [
        Decimal(0), Decimal("2.5"), Decimal(5),
    ]


@pytest.mark.parametrize("text", ["Итого 1 000", "за две недели", "на Ⅳ этапе", "бесплатно"])
def test_guard(text: str) -> None:
    copy = mock_copy(DealContextInput.model_validate(BASE))
    with pytest.raises(InvalidCompletion):
        verify_no_numeric_hallucinations(copy.model_copy(update={"value_proposition": text}))


def test_api_generation_idempotency_and_files(settings_paths: dict[str, Path]) -> None:
    settings = load_settings(llm_mode="mock", **settings_paths)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json()["llm_mode"] == "mock"
        result = client.post(GENERATE, json=BASE, headers={"Idempotency-Key": "abc"})
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["status"] == "completed"
        assert Path(body["pdf_path"]).read_bytes().startswith(b"%PDF")
        with ZipFile(body["docx_path"]) as archive:
            assert "Коммерческое предложение" in archive.read("word/document.xml").decode()
        assert client.post(GENERATE, json=BASE, headers={"Idempotency-Key": "abc"}).json() == body
        changed = client.post(
            GENERATE, json=BASE | {"deal_id": 99}, headers={"Idempotency-Key": "abc"},
        )
        assert changed.status_code == 409
        assert client.get("/api/v1/metrics").json()["by_mode"][0]["attempts"] == 1


def test_director_approval(settings_paths: dict[str, Path]) -> None:
    settings = load_settings(llm_mode="mock", **settings_paths)
    with TestClient(create_app(settings)) as client:
        result = client.post(
            GENERATE, json=BASE | {"desired_discount_pct": "40"},
            headers={"Idempotency-Key": "approval"},
        ).json()
    assert result["status"] == "requires_approval"
    assert result["margin_audit"]["requires_director_approval"]
    with Database(settings.database).connection() as db:
        rows = {row["operation"]: row["payload"] for row in db.execute("SELECT * FROM mock_operations")}
        assert "director" in rows["task"]
        assert "update_deal" not in rows


def test_reconcile(settings_paths: dict[str, Path]) -> None:
    settings = load_settings(llm_mode="mock", **settings_paths)
    with TestClient(create_app(settings)) as client:
        body = client.post(
            GENERATE + "?simulate_failure=true", json=BASE,
            headers={"Idempotency-Key": "reconcile"},
        ).json()
        assert body["status"] == "reconcile"
    with Database(settings.database).connection() as db:
        # Simulate a process exit after it claims reconciliation, before the first CRM call.
        db.execute("UPDATE cycles SET state='pending' WHERE key='reconcile'")
    with TestClient(create_app(settings)) as client:
        assert client.post(RECONCILE, json={"idempotency_key": "missing"}).status_code == 404
        result = client.post(RECONCILE, json={"idempotency_key": "reconcile"}).json()
        assert result["status"] == "completed"
        assert result["cycle_id"] == body["cycle_id"]
        assert client.post(RECONCILE, json={"idempotency_key": "reconcile"}).json() == result
    with Database(settings.database).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM mock_operations").fetchone()[0] == 4
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_budget_stops_before_provider(settings_paths: dict[str, Path]) -> None:
    settings = load_settings(
        llm_mode="live", max_cycle_cost_rub="0", llm_rub_per_million_input_tokens="1",
        llm_rub_per_million_output_tokens="2", **settings_paths,
    )
    with TestClient(create_app(settings)) as client:
        assert client.post(GENERATE, json=BASE, headers={"Idempotency-Key": "budget"}).status_code == 422
    with Database(settings.database).connection() as db:
        assert db.execute("SELECT outcome FROM telemetry").fetchone()[0] == "budget_exceeded"
        assert db.execute("SELECT COUNT(*) FROM mock_operations").fetchone()[0] == 0


def test_provider_contract_and_actual_charge() -> None:
    import asyncio

    async def run() -> None:
        def provider(request: httpx.Request) -> httpx.Response:
            import json

            payload = json.loads(request.content)
            if request.url.path.endswith("/embeddings"):
                assert payload["model"] == "bge-m3"
                return httpx.Response(200, json={
                    "usage": {"prompt_tokens": 5}, "data": [{"embedding": [0.1, 0.2]}],
                })
            assert payload["response_format"] == {"type": "json_object"}
            return httpx.Response(200, json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                "choices": [{"message": {"content": "```json\n{\"message\": \"ok\"}\n```"}}],
            })

        settings = load_settings(
            max_cycle_cost_rub="1", llm_rub_per_million_input_tokens="1",
            llm_rub_per_million_output_tokens="2", embedding_rub_per_million_input_tokens="1",
            embedding_model="bge-m3",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            usage = CycleUsage()
            assert await LLMGateway(settings, client).evaluate("system", "payload", usage) == {
                "message": "ok",
            }
            assert usage.cost_rub == Decimal("0.0002")
            assert await LLMGateway(settings, client).embed("payload", usage) == [0.1, 0.2]
            assert usage.cost_rub == Decimal("0.000205")
            with pytest.raises(BudgetExceeded):
                usage.authorize(Decimal(0), Decimal(0))

    asyncio.run(run())
