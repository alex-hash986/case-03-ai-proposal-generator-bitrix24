from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Money = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2, allow_inf_nan=False)]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=3, allow_inf_nan=False)]
Percent = Annotated[Decimal, Field(ge=0, le=100, decimal_places=4, allow_inf_nan=False)]
Text = Annotated[str, Field(min_length=1, max_length=500)]
CopyText = Annotated[str, Field(max_length=2000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProposalItemIn(Contract):
    name: Text
    quantity: Quantity
    unit_price: Money
    unit_cost: Money
    vat_pct: Percent | None = None


class ProposalLineOut(Contract):
    name: str
    quantity: Decimal
    unit_price: Decimal
    discount_pct: Decimal
    vat_pct: Decimal
    subtotal: Decimal
    discount: Decimal
    net: Decimal
    vat: Decimal
    total: Decimal


class DealContextInput(Contract):
    deal_id: Annotated[int, Field(gt=0)]
    client_name: Text
    company_name: Text
    company_details: Text | None = None
    segment: Text = "standard"
    pains: list[Text] = Field(default_factory=list, max_length=20)
    agreements: list[Text] = Field(default_factory=list, max_length=20)
    objections: list[Text] = Field(default_factory=list, max_length=20)
    payment_terms: Text | None = None
    implementation_terms: Text | None = None
    items: list[ProposalItemIn] = Field(min_length=1, max_length=200)
    desired_discount_pct: Percent = Decimal(0)
    mode: Literal["single", "three_tiers"] = "single"


class LLMProposalCopy(Contract):
    greeting: CopyText
    value_proposition: CopyText
    objection_handling: CopyText
    payment_terms: CopyText
    implementation_terms: CopyText
    cover_letter: CopyText


class MarginAuditResult(Contract):
    is_approved: bool
    effective_margin_pct: Decimal
    max_allowed_discount_pct: Decimal
    requires_director_approval: bool
    rejection_reasons: list[str]


class ProposalFinancials(Contract):
    label: str
    lines: list[ProposalLineOut]
    subtotal: Decimal
    discount: Decimal
    net: Decimal
    vat: Decimal
    total: Decimal
    cost: Decimal
    margin: Decimal


class ProposalGenerationResponse(Contract):
    cycle_id: str
    idempotency_key: str
    status: Literal["completed", "requires_approval", "reconcile"]
    docx_path: str
    pdf_path: str
    financials: list[ProposalFinancials]
    margin_audit: MarginAuditResult
    proposal_copy: LLMProposalCopy = Field(alias="copy")
    model_config = ConfigDict(serialize_by_alias=True)
    bitrix_sync_status: Literal["completed", "reconcile"]
    bitrix_mode: Literal["mock", "live"] = "mock"
    cost_rub: Decimal
    latency_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    need_human: bool
    mode: Literal["mock", "live", "fallback"]
    fallback_reason: Literal["invalid_copy", "numeric_guard"] | None = None
