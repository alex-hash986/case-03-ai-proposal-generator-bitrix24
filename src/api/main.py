import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, cast

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from filelock import FileLock

from src.core.config import Settings, load_settings
from src.core.database import Conflict, Database, MissingCycle
from src.core.llm_gateway import BudgetExceeded, LLMGateway
from src.domain.doc_renderer import DocumentRenderer
from src.domain.schemas import Contract, DealContextInput, ProposalGenerationResponse
from src.domain.service import ProposalService, UnknownSegment
from src.integrations.bitrix_connector import LiveBitrixConnector, MockBitrixConnector


class ReconcileRequest(Contract):
    idempotency_key: str


def create_app(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
    mock_crm_interval: float = 0.5,
) -> FastAPI:
    config = settings if settings is not None else load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(config.database)
        lock_path = config.database.with_suffix(".publisher.lock")
        with FileLock(str(lock_path), timeout=0):
            database.recover_interrupted_cycles()
            async with httpx.AsyncClient(timeout=30, transport=transport) as client:
                crm = (
                    LiveBitrixConnector(config, database, client) if config.bitrix_mode == "live"
                    else MockBitrixConnector(database, interval=mock_crm_interval)
                )
                app.state.proposals = ProposalService(
                    config, database, LLMGateway(config, client),
                    crm, DocumentRenderer(config),
                )
                yield

    app = FastAPI(title="Case 03 · Генератор КП", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "llm_mode": config.llm_mode, "bitrix_mode": config.bitrix_mode}

    @app.post("/api/v1/proposals/generate")
    async def generate(
        context: DealContextInput, request: Request,
        idempotency_key: Annotated[str, Header(pattern=r"^[a-zA-Z0-9_.:-]{1,160}$")],
        simulate_failure: bool = False,
    ) -> ProposalGenerationResponse:
        service = cast(ProposalService, request.app.state.proposals)
        try:
            return await service.generate(context, idempotency_key, simulate_failure)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except UnknownSegment as exc:
            raise HTTPException(422, str(exc)) from exc
        except BudgetExceeded as exc:
            raise HTTPException(422, "Превышен бюджет модельного цикла") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, "Модельный сервис недоступен") from exc

    @app.post("/api/v1/proposals/reconcile")
    async def reconcile(body: ReconcileRequest, request: Request) -> ProposalGenerationResponse:
        service = cast(ProposalService, request.app.state.proposals)
        try:
            return await service.reconcile(body.idempotency_key)
        except MissingCycle as exc:
            raise HTTPException(404, "Ключ публикации не найден") from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/metrics")
    def metrics(request: Request) -> dict[str, Any]:
        service = cast(ProposalService, request.app.state.proposals)
        return service.database.metrics()

    return app


app = create_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock-llm", action="store_true")
    args = parser.parse_args()
    settings = load_settings(**({"llm_mode": "mock"} if args.mock_llm else {}))
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.api_port)
