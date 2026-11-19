from decimal import ROUND_HALF_UP, Decimal

from src.core.config import DiscountPolicy
from src.domain.schemas import (
    DealContextInput,
    MarginAuditResult,
    ProposalFinancials,
    ProposalLineOut,
)

ZERO = Decimal(0)
HUNDRED = Decimal(100)


def rounded(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def price(
    context: DealContextInput, default_vat: Decimal, policy: DiscountPolicy,
) -> tuple[list[ProposalFinancials], MarginAuditResult]:
    discount = context.desired_discount_pct
    variants = [("Запрошенный вариант", discount)]
    if context.mode == "three_tiers":
        variants = [("Без скидки", ZERO), ("Промежуточный вариант", discount / 2), *variants]
    financials = []
    reasons = []
    if discount > policy.max_discount_pct:
        reasons.append("Скидка превышает лимит сегмента.")
    cost = sum((rounded(item.quantity * item.unit_cost) for item in context.items), ZERO)
    worst_margin = HUNDRED
    for label, rate in variants:
        lines = []
        for item in context.items:
            vat_rate = default_vat if item.vat_pct is None else item.vat_pct
            subtotal = rounded(item.unit_price * item.quantity)
            reduction = rounded(subtotal * rate / HUNDRED)
            net = subtotal - reduction
            vat = rounded(net * vat_rate / HUNDRED)
            lines.append(ProposalLineOut(
                name=item.name, quantity=item.quantity, unit_price=item.unit_price,
                discount_pct=rate, vat_pct=vat_rate, subtotal=subtotal, discount=reduction,
                net=net, vat=vat, total=net + vat,
            ))
        net_total = sum((line.net for line in lines), ZERO)
        margin = net_total - cost
        margin_pct = margin * HUNDRED / net_total if net_total else ZERO
        worst_margin = min(worst_margin, margin_pct)
        if not net_total or margin_pct < policy.min_margin_pct:
            reasons.append(f"{label}. Маржа ниже порога или выручка отсутствует.")
        financials.append(ProposalFinancials(
            label=label, lines=lines, subtotal=sum((line.subtotal for line in lines), ZERO),
            discount=sum((line.discount for line in lines), ZERO), net=net_total,
            vat=sum((line.vat for line in lines), ZERO),
            total=sum((line.total for line in lines), ZERO), cost=cost, margin=margin,
        ))
    return financials, MarginAuditResult(
        is_approved=not reasons, effective_margin_pct=rounded(worst_margin),
        max_allowed_discount_pct=policy.max_discount_pct,
        requires_director_approval=bool(reasons), rejection_reasons=reasons,
    )
