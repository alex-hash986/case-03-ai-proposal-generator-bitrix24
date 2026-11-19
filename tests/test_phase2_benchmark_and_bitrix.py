import asyncio
import json
import re
from base64 import b64decode
from collections import Counter
from pathlib import Path
from time import monotonic
from typing import Any
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient

from scripts.generate_proposal_dataset import generate, load_cases
from scripts.run_ab_benchmark import run_benchmark, write_report
from src.api.main import create_app
from src.core.config import load_settings
from src.core.database import Database
from src.domain.pricing_engine import price
from src.domain.proposal_writer import (
    NumericPromise,
    mock_copy,
    verify_no_numeric_hallucinations,
)
from src.domain.schemas import DealContextInput
from src.integrations.bitrix_connector import RateLimiter
from src.ui.presets import BASE

GENERATE = "/api/v1/proposals/generate"
RECONCILE = "/api/v1/proposals/reconcile"
KEY = "phase2-live-test"


def test_frozen_dataset_and_reference_totals() -> None:
    cases = load_cases()
    assert cases == generate()
    settings = load_settings()
    assert Counter(case.expected_requires_approval for case in cases) == {False: 45, True: 15}
    for case in cases:
        totals, audit = price(case.deal, settings.vat_pct, settings.discount_matrix[case.deal.segment])
        assert [tier.total for tier in totals] == case.expected_tier_totals
        assert audit.requires_director_approval == case.expected_requires_approval


def test_ab_pipeline_and_reports(tmp_path: Path) -> None:
    cases = load_cases()
    result = asyncio.run(run_benchmark([cases[0], cases[35], cases[50]], tmp_path))
    a, b = (result["metrics"][variant] for variant in ("variant_a", "variant_b"))
    assert result["winner"] == "variant_b"
    assert a["financial_accuracy_pct"] == b["financial_accuracy_pct"] == 100
    assert a["director_recall_pct"] == b["director_recall_pct"] == 100
    assert a["seeded_promise_recall_pct"] == b["seeded_promise_recall_pct"] == 100
    assert a["human_review_accuracy_pct"] == b["human_review_accuracy_pct"] == 100
    assert b["pains_coverage_pct"] > a["pains_coverage_pct"]
    assert b["objections_coverage_pct"] > a["objections_coverage_pct"]
    assert b["saved_minutes"] is None
    keys = {row["key"] for rows in result["observations"].values() for row in rows}
    assert len(keys) == 6
    output, summary = tmp_path / "report.json", tmp_path / "summary.md"
    write_report(result, output, summary)
    assert json.loads(output.read_text(encoding="utf-8"))["winner"] == "variant_b"
    assert "variant_b" in summary.read_text(encoding="utf-8")
    for path in (tmp_path / "variant_b").glob("*/proposal.docx"):
        with ZipFile(path) as document:
            xml = document.read("word/document.xml").decode()
        assert re.search(r"Итого \d{1,3}(?: \d{3})+\.\d{2} ₽", xml)


def test_ab_live_flag_budget_before_model_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_CYCLE_COST_RUB", "0")
    monkeypatch.setenv("LLM_RUB_PER_MILLION_INPUT_TOKENS", "1")
    monkeypatch.setenv("LLM_RUB_PER_MILLION_OUTPUT_TOKENS", "1")

    async def forbidden(*args: Any, **kwargs: Any) -> httpx.Response:
        pytest.fail("Budget check must run before provider POST")

    # ASGI uses a local HTTP client; allow only calls to its application routes.
    original_request = httpx.AsyncClient.request

    async def post(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        if url.startswith("/api/"):
            return await original_request(client, "POST", url, **kwargs)
        return await forbidden()

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(RuntimeError, match="HTTP 422"):
        asyncio.run(run_benchmark(load_cases()[:1], tmp_path, live=True))
    with Database(tmp_path / "variant_a.db").connection() as db:
        row = db.execute("SELECT outcome, tokens_in, tokens_out, cost_rub FROM telemetry").fetchone()
        assert tuple(row) == ("budget_exceeded", 0, 0, "0")


@pytest.mark.parametrize("promise", [
    "Предоставим пятнадцать процентов.", "Работы проведём за наш счёт.",
    "Внедрение за полторы недели.", "Подключение идёт в подарок.",
    "С​кидка согласована.", "Действует льготный период.", "Подарим настройку без доплаты.",
])
def test_guard_written_numbers_and_hidden_discount(promise: str) -> None:
    sample = mock_copy(DealContextInput.model_validate(BASE))
    with pytest.raises(NumericPromise):
        verify_no_numeric_hallucinations(sample.model_copy(update={"cover_letter": promise}))


def test_guard_allows_benign_business_vocabulary() -> None:
    sample = mock_copy(DealContextInput.model_validate(BASE))
    verify_no_numeric_hallucinations(sample.model_copy(update={
        "value_proposition": "Разграничение ролей сотрудников и настройка триггеров.",
    }))


class Portal:
    def __init__(
        self, db: Database, fail: str | None, *, commit: bool = True, before_send: bool = False,
    ) -> None:
        self.db = db
        self.fail = fail
        self.commit = commit
        self.before_send = before_send
        self.calls: list[tuple[str, float]] = []
        self.deal = {
            "ID": "301", "COMPANY_ID": "50", "ASSIGNED_BY_ID": "7", "CURRENCY_ID": "RUB",
            "STAGE_ID": "NEW", "OPPORTUNITY": "0", "IS_MANUAL_OPPORTUNITY": "N",
        }
        self.timeline: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.updates = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "portal.example"
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        self.calls.append((method, monotonic()))
        with self.db.connection() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM bitrix_steps WHERE key=? AND state='started'", (KEY,),
            ).fetchone()[0] >= 1
        lost = self.fail == method
        if lost:
            self.fail = None
            if self.before_send:
                raise httpx.ConnectError("connection refused")
            if not self.commit:
                raise httpx.ReadTimeout("response unknown after send")
        result: Any
        if method == "crm.deal.get":
            result = self.deal.copy()
        elif method == "crm.deal.productrows.get":
            result = [{"ID": "72", "PRODUCT_NAME": "Аудит"}]
        elif method == "crm.requisite.list":
            assert payload["filter"] == {"ENTITY_TYPE_ID": 4, "ENTITY_ID": 50}
            result = [{"ID": "66", "RQ_INN": "500100732259"}]
        elif method == "crm.timeline.comment.add":
            fields = payload["fields"]
            filename, data = fields["FILES"][0]
            assert fields["ENTITY_ID"] == 301 and fields["ENTITY_TYPE"] == "deal"
            assert filename.endswith(".pdf") and b64decode(data).startswith(b"%PDF")
            self.timeline.append({"ID": "121", "COMMENT": fields["COMMENT"], "FILES": {"1": filename}})
            result = 121
        elif method == "crm.timeline.comment.list":
            assert payload["filter"] == {"ENTITY_ID": 301, "ENTITY_TYPE": "deal"}
            result = self.timeline
        elif method == "crm.deal.update":
            self.updates += 1
            self.deal.update(payload["fields"])
            result = True
        elif method == "tasks.task.add":
            fields = payload["fields"]
            assert fields["UF_CRM_TASK"] == ["D_301"]
            self.tasks.append({"id": "9", "title": fields["TITLE"], "responsible": fields["RESPONSIBLE_ID"]})
            result = {"task": self.tasks[-1]}
        elif method == "tasks.task.list":
            assert payload["filter"]["TITLE"].endswith("]")
            result = {"tasks": self.tasks}
        else:
            pytest.fail(f"Unexpected REST method {method}")
        if lost:
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(200, json={"result": result})


def live_settings(tmp_path: Path) -> dict[str, object]:
    return {
        "llm_mode": "mock", "bitrix_mode": "live", "bitrix_director_id": 42,
        "bitrix_webhook_url": "https://portal.example/rest/1/testsecret/",
        "bitrix_proposal_stage": "C1:PREPARATION",
        "database": tmp_path / "test.db", "artifacts": tmp_path / "artifacts",
        "template": tmp_path / "template.docx",
    }


@pytest.mark.parametrize("method", [
    "crm.deal.get", "crm.deal.productrows.get", "crm.requisite.list", "crm.timeline.comment.add",
    "crm.deal.update", "tasks.task.add",
])
def test_live_resume_each_remote_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    if method != "crm.timeline.comment.add":
        async def no_wait(limiter: RateLimiter) -> None:
            pass

        monkeypatch.setattr(RateLimiter, "wait", no_wait)
    settings = load_settings(**live_settings(tmp_path))
    portal = Portal(Database(settings.database), method)
    transport = httpx.MockTransport(portal)
    with TestClient(create_app(settings, transport=transport)) as client:
        failed = client.post(GENERATE, json=BASE, headers={"Idempotency-Key": KEY})
        assert failed.status_code == 200, failed.text
        assert failed.json()["status"] == "reconcile"
    with TestClient(create_app(settings, transport=transport)) as client:
        repaired = client.post(RECONCILE, json={"idempotency_key": KEY})
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["status"] == "completed"
        assert repaired.json()["cycle_id"] == failed.json()["cycle_id"]
        assert client.post(RECONCILE, json={"idempotency_key": KEY}).json() == repaired.json()
    assert len(portal.timeline) == len(portal.tasks) == portal.updates == 1
    assert portal.tasks[0]["responsible"] == 7
    assert portal.deal["STAGE_ID"] == "C1:PREPARATION"
    assert portal.deal["OPPORTUNITY"] == repaired.json()["financials"][-1]["total"]
    if method == "crm.timeline.comment.add":
        times = [timestamp for _, timestamp in portal.calls]
        assert all(b - a >= 0.5 for a, b in zip(times, times[1:]))
        assert all(c - a >= 1 for a, c in zip(times, times[2:]))


def test_live_uncertain_absence_and_director_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_wait(limiter: RateLimiter) -> None:
        pass

    monkeypatch.setattr(RateLimiter, "wait", no_wait)
    settings = load_settings(**live_settings(tmp_path))
    portal = Portal(Database(settings.database), "tasks.task.add", commit=False)
    fake = httpx.MockTransport(portal)
    with TestClient(create_app(settings, transport=fake)) as client:
        failed = client.post(
            GENERATE, json=BASE | {"desired_discount_pct": "40"}, headers={"Idempotency-Key": KEY},
        )
        assert failed.json()["status"] == "reconcile"
        assert failed.json()["margin_audit"]["requires_director_approval"]
        assert "Внутренний черновик" in portal.timeline[0]["COMMENT"]
        assert portal.updates == 0
        assert client.post(RECONCILE, json={"idempotency_key": KEY}).json()["status"] == "reconcile"
        assert sum(method == "tasks.task.add" for method, _ in portal.calls) == 1
        # The originally ambiguous task becomes visible on the portal.
        title = "Проверить КП " + portal.timeline[0]["COMMENT"].splitlines()[-1]
        portal.tasks.append({"id": "9", "title": title, "responsible": 42})
        assert client.post(RECONCILE, json={"idempotency_key": KEY}).json()["status"] == "requires_approval"
    assert len(portal.tasks) == 1


def test_live_retries_confirmed_unsent_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_wait(limiter: RateLimiter) -> None:
        pass

    monkeypatch.setattr(RateLimiter, "wait", no_wait)
    settings = load_settings(**live_settings(tmp_path))
    db = Database(settings.database)
    portal = Portal(db, "crm.timeline.comment.add", before_send=True)
    with TestClient(create_app(settings, transport=httpx.MockTransport(portal))) as client:
        initial = client.post(GENERATE, json=BASE, headers={"Idempotency-Key": KEY})
        assert initial.json()["status"] == "reconcile"
        assert not portal.timeline
        with db.connection() as connection:
            state = connection.execute("SELECT state FROM bitrix_steps WHERE step='timeline'").fetchone()[0]
        assert state == "retry"
        assert client.post(RECONCILE, json={"idempotency_key": KEY}).json()["status"] == "completed"
    assert len(portal.timeline) == len(portal.tasks) == 1
