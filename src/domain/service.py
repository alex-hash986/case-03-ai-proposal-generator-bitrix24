import asyncio
import hashlib
from datetime import datetime
from decimal import Decimal
from time import perf_counter
from typing import Any
from uuid import uuid4

from src.core.config import Settings
from src.core.database import Conflict, Database
from src.core.llm_gateway import BudgetExceeded, CycleUsage, LLMGateway
from src.domain.doc_renderer import DocumentRenderer
from src.domain.ports import BitrixPort, UncertainWrite
from src.domain.pricing_engine import price
from src.domain.proposal_writer import write_copy
from src.domain.schemas import DealContextInput, ProposalGenerationResponse


class UnknownSegment(ValueError):
    pass


class ProposalService:
    def __init__(
        self, settings: Settings, database: Database, gateway: LLMGateway,
        crm: BitrixPort, renderer: DocumentRenderer,
    ) -> None:
        self.settings = settings
        self.database = database
        self.gateway = gateway
        self.crm = crm
        self.renderer = renderer

    def _telemetry(
        self, started: float, usage: CycleUsage, mode: str, need_human: bool, outcome: str,
    ) -> tuple[Any, ...]:
        now = datetime.now().astimezone()
        elapsed = perf_counter() - started
        baseline = self.settings.manual_baseline_minutes
        saved = (
            float(baseline) - elapsed / 60
            if baseline is not None and outcome == "completed" and not need_human
            else None
        )
        return (
            uuid4().hex, elapsed, usage.tokens_in, usage.tokens_out, str(usage.cost_rub),
            usage.cost_complete, self.settings.ab_variant, mode, f"bitrix24_{self.settings.bitrix_mode}",
            now.weekday() >= 5 or not 9 <= now.hour < 18, need_human, saved, outcome,
        )

    async def generate(
        self, context: DealContextInput, key: str, simulate_failure: bool = False,
    ) -> ProposalGenerationResponse:
        started, usage = perf_counter(), CycleUsage()
        policy = self.settings.discount_matrix.get(context.segment)
        if policy is None:
            raise UnknownSegment("В матрице отсутствует сегмент сделки")
        context_json = context.model_dump_json()
        fingerprint = hashlib.sha256(context_json.encode()).hexdigest()
        cached = self.database.claim(key, fingerprint, context_json)
        if cached is not None:
            return ProposalGenerationResponse.model_validate_json(cached)
        mode = self.settings.llm_mode
        try:
            financials, audit = price(context, self.settings.vat_pct, policy)
            copy, copy_mode, fallback_reason = await write_copy(context, self.gateway, usage)
            cycle_id = uuid4().hex
            needs_review = (
                audit.requires_director_approval or copy_mode == "fallback"
                or not context.payment_terms or not context.implementation_terms
            )
            docx, pdf = self.renderer.render(
                cycle_id, context, copy, financials,
                needs_approval=needs_review,
            )
            result = ProposalGenerationResponse(
                cycle_id=cycle_id, idempotency_key=key, status="reconcile",
                docx_path=str(docx), pdf_path=str(pdf), financials=financials, margin_audit=audit,
                copy=copy, bitrix_sync_status="reconcile", cost_rub=usage.cost_rub, latency_ms=0,
                tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
                need_human=needs_review, mode=copy_mode, fallback_reason=fallback_reason,
                bitrix_mode=self.settings.bitrix_mode,
            )
        except (Exception, asyncio.CancelledError) as exc:
            outcome = "budget_exceeded" if isinstance(exc, BudgetExceeded) else "generation_failed"
            self.database.fail(key, self._telemetry(started, usage, mode, True, outcome), retryable=True)
            raise
        self.database.checkpoint(key, result.model_dump_json())
        return await self._publish(context, result, started, usage, simulate_failure)

    async def reconcile(self, key: str) -> ProposalGenerationResponse:
        started = perf_counter()
        payload, serialized, claimed = self.database.claim_reconcile(key)
        result = ProposalGenerationResponse.model_validate_json(serialized)
        if not claimed:
            return result
        if result.bitrix_mode != self.settings.bitrix_mode:
            self.database.finish(
                key, "reconcile", serialized,
                self._telemetry(started, CycleUsage(), result.mode, True, "crm_mode_conflict"),
            )
            raise Conflict("Сверка требует исходного режима Bitrix24")
        context = DealContextInput.model_validate_json(payload)
        return await self._publish(context, result, started, CycleUsage(), False, reconcile=True)

    async def _publish(
        self, context: DealContextInput, result: ProposalGenerationResponse,
        started: float, usage: CycleUsage, simulate_failure: bool, *, reconcile: bool = False,
    ) -> ProposalGenerationResponse:
        director = result.margin_audit.requires_director_approval
        amount: Decimal = result.financials[-1].total
        common: dict[str, Any] = {"deal_id": context.deal_id}
        try:
            crm_context = await self.crm.read_deal(context.deal_id, key=result.idempotency_key)
            if self.settings.bitrix_mode == "live":
                common["assigned_by"] = crm_context["deal"]["ASSIGNED_BY_ID"]
                common["currency"] = crm_context["deal"]["CURRENCY_ID"]
                common["pdf_path"] = result.pdf_path
                common["internal_draft"] = result.need_human
            operations: list[tuple[str, dict[str, Any]]] = [
                ("upload_pdf", {"path": result.pdf_path, "internal_draft": result.need_human}),
                ("timeline", {"summary": result.proposal_copy.value_proposition, "total_rub": str(amount),
                              "reasons": result.margin_audit.rejection_reasons}),
            ]
            if not result.need_human:
                operations.append(("update_deal", {"stage": "PROPOSAL", "amount": str(amount)}))
            operations.append(("task", {"assignee": "director" if director else "manager",
                                        "action": "review" if result.need_human else "follow_up"}))
            for operation, payload in operations:
                await self.crm.apply(
                    result.idempotency_key, operation, common | payload,
                    simulate_failure=simulate_failure and operation == "upload_pdf",
                )
        except (UncertainWrite, asyncio.CancelledError):
            state = "reconcile"
        except Exception:
            self.database.finish(
                result.idempotency_key, "reconcile", result.model_dump_json(),
                self._telemetry(started, usage, result.mode, True, "publish_failed"),
            )
            raise
        else:
            state = "completed"
            result.bitrix_sync_status = "completed"
            result.status = "requires_approval" if director else "completed"
        result.latency_ms += round((perf_counter() - started) * 1000)
        self.database.finish(
            result.idempotency_key, state, result.model_dump_json(),
            self._telemetry(
                started, usage, "reconcile" if reconcile else result.mode,
                result.need_human or state == "reconcile", result.status,
            ),
        )
        return result
