import argparse
import asyncio
import json
import re
from collections.abc import Callable
from contextlib import AsyncExitStack
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from uuid import uuid4

import httpx

from scripts.generate_proposal_dataset import DATASET, BenchmarkCase, load_cases
from src.api.main import create_app
from src.core.config import ROOT, load_settings
from src.domain.schemas import LLMProposalCopy, ProposalGenerationResponse

Variant = Literal["variant_a", "variant_b"]
VARIANTS: tuple[Variant, ...] = ("variant_a", "variant_b")
REPORT = ROOT / "artifacts/benchmarks/phase2_ab_report.json"
SUMMARY = ROOT / "docs/ab_benchmark_phase2.md"


def simulated_provider(
    case: BenchmarkCase, variant: Variant,
) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path.endswith("/chat/completions")
        assert body["response_format"] == {"type": "json_object"}
        context = json.loads(body["messages"][1]["content"])
        assert context["segment"] == case.deal.segment and context["pains"] == case.deal.pains
        structured = "Для каждой указанной боли" in body["messages"][0]["content"]
        assert structured == (variant == "variant_b")
        limit = len(case.deal.pains) + len(case.deal.objections) if structured else 1
        copy = LLMProposalCopy(
            greeting="Здравствуйте!",
            value_proposition=case.injected_promise or "\n".join(
                f"Цель предложения — устранить проблему «{topic}»."
                for topic in case.deal.pains[:limit]
            ) or "Подготовили предложение по согласованной спецификации.",
            objection_handling="\n".join(
                f"Предлагаем обсудить вопрос «{topic}» при согласовании."
                for topic in case.deal.objections[:limit]
            ) or "Готовы обсудить детали.",
            payment_terms="", implementation_terms="",
            cover_letter="Направляем предложение для обсуждения.",
        )
        return httpx.Response(200, json={
            "choices": [{"message": {"content": copy.model_dump_json()}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        })

    return respond


def normalized(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold().replace("ё", "е")))


def covered(terms: list[str], text: str) -> int:
    searchable = f" {normalized(text)} "
    return sum(f" {normalized(term)} " in searchable for term in terms)


def observation(case: BenchmarkCase, result: ProposalGenerationResponse) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "key": result.idempotency_key,
        "profile": case.profile,
        "expected_total": str(case.expected_total),
        "total": str(result.financials[-1].total),
        "actual_tier_totals": [str(variant.total) for variant in result.financials],
        "expected_tier_totals": [str(total) for total in case.expected_tier_totals],
        "financial_match": [tier.total for tier in result.financials] == case.expected_tier_totals,
        "expected_director": case.expected_requires_approval,
        "director": result.margin_audit.requires_director_approval,
        "expected_human_review": case.expected_human_review,
        "human_review": result.need_human,
        "provocation": case.injected_promise is not None,
        "guard_rejected": result.fallback_reason == "numeric_guard",
        "pains_matched": covered(case.must_address_pains, result.proposal_copy.value_proposition),
        "pains_expected": len(case.must_address_pains),
        "objections_matched": covered(case.must_address_objections, result.proposal_copy.objection_handling),
        "objections_expected": len(case.must_address_objections),
        "latency_ms": result.latency_ms,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "cost_rub": str(result.cost_rub),
    }


def ratio(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 2) if denominator else None


def summarize(
    rows: list[dict[str, Any]], baseline: Decimal | None, review: Decimal | None, *, live: bool,
) -> dict[str, Any]:
    if not rows:
        raise ValueError("Benchmark is empty")
    n = len(rows)
    tp = sum(row["expected_director"] and row["director"] for row in rows)
    fp = sum(not row["expected_director"] and row["director"] for row in rows)
    fn = sum(row["expected_director"] and not row["director"] for row in rows)
    milliseconds = sum(row["latency_ms"] for row in rows)
    provocation_hits = sum(row["provocation"] and row["guard_rejected"] for row in rows)
    provocation_count = sum(row["provocation"] for row in rows)
    return {
        "cases": n,
        "financial_accuracy_pct": ratio(sum(row["financial_match"] for row in rows), n),
        "director_accuracy_pct": ratio(n - fp - fn, n),
        "director_precision_pct": ratio(tp, tp + fp),
        "director_recall_pct": ratio(tp, tp + fn),
        "human_review_accuracy_pct": ratio(
            sum(row["expected_human_review"] == row["human_review"] for row in rows), n,
        ),
        "numeric_guard_rejection_rate": ratio(sum(row["guard_rejected"] for row in rows), n),
        "seeded_promise_recall_pct": ratio(provocation_hits, provocation_count) if not live else None,
        "provocation_rejection_pct": ratio(provocation_hits, provocation_count),
        "pains_coverage_pct": ratio(
            sum(row["pains_matched"] for row in rows), sum(row["pains_expected"] for row in rows),
        ),
        "objections_coverage_pct": ratio(
            sum(row["objections_matched"] for row in rows), sum(row["objections_expected"] for row in rows),
        ),
        "mean_latency_ms": round(milliseconds / n, 2),
        "tokens_in": sum(row["tokens_in"] for row in rows),
        "tokens_out": sum(row["tokens_out"] for row in rows),
        "cost_rub": str(sum((Decimal(row["cost_rub"]) for row in rows), Decimal(0))),
        "saved_minutes": (
            str(round(n * (baseline - review) - Decimal(milliseconds) / 60_000, 3))
            if baseline is not None and review is not None else None
        ),
    }


async def run_benchmark(
    cases: list[BenchmarkCase], workspace: Path, *, live: bool = False,
    baseline: Decimal | None = None, review: Decimal | None = None,
) -> dict[str, Any]:
    settings = load_settings()
    if live and (
        settings.llm_rub_per_million_input_tokens is None
        or settings.llm_rub_per_million_output_tokens is None
    ):
        raise ValueError("--live-llm requires explicit input and output token rates")
    rows: dict[Variant, list[dict[str, Any]]] = {variant: [] for variant in VARIANTS}
    run_id = uuid4().hex
    for variant in VARIANTS:
        route: dict[str, BenchmarkCase] = {}

        def respond(request: httpx.Request) -> httpx.Response:
            return simulated_provider(route["case"], variant)(request)

        overrides: dict[str, Any] = {
            "llm_mode": "live", "bitrix_mode": "mock", "ab_variant": variant,
            "database": workspace / f"{variant}.db", "artifacts": workspace / variant,
            "template": workspace / "template.docx",
        }
        if not live:
            overrides.update(llm_rub_per_million_input_tokens=Decimal(0),
                             llm_rub_per_million_output_tokens=Decimal(0))
        app = create_app(
            load_settings(**overrides), transport=None if live else httpx.MockTransport(respond),
            mock_crm_interval=0,
        )
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(app.router.lifespan_context(app))
            client = await stack.enter_async_context(httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8013",
            ))
            for case in cases:
                route["case"] = case
                key = f"phase2-{run_id}-{variant}-{case.case_id}"
                response = await client.post(
                    "/api/v1/proposals/generate", content=case.deal.model_dump_json(),
                    headers={"Idempotency-Key": key, "Content-Type": "application/json"},
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        f"{variant}/{case.case_id} HTTP {response.status_code}: {response.text}",
                    )
                result = ProposalGenerationResponse.model_validate(response.json())
                if result.bitrix_sync_status != "completed":
                    raise RuntimeError(f"{variant}/{case.case_id}: CRM publication not completed")
                rows[variant].append(observation(case, result))
    baseline = baseline if baseline is not None else settings.manual_baseline_minutes
    metrics = {variant: summarize(rows[variant], baseline, review, live=live) for variant in VARIANTS}
    eligible = [variant for variant in VARIANTS if (
        metrics[variant]["financial_accuracy_pct"] == 100
        and metrics[variant]["director_recall_pct"] in {100, None}
        and metrics[variant]["seeded_promise_recall_pct"] in {100, None}
    )]
    def ranking(variant: Variant) -> tuple[float, bool]:
        metric = metrics[variant]
        coverage = float(metric["pains_coverage_pct"] or 0) + float(metric["objections_coverage_pct"] or 0)
        return coverage, variant == "variant_a"

    winner = next(iter(sorted(eligible, key=ranking, reverse=True)), None)
    return {
        "run_id": run_id,
        "dataset_sha256": sha256("".join(case.model_dump_json() + "\n" for case in cases).encode()).hexdigest(),
        "mode": "live-llm" if live else "http-mock",
        "winner": winner,
        "manual_baseline_minutes": str(baseline) if baseline is not None else None,
        "automated_review_minutes": str(review) if review is not None else None,
        "metrics": metrics,
        "observations": rows,
    }


def write_report(report: dict[str, Any], output: Path, summary: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows: list[str] = []
    for variant, value in report["metrics"].items():
        rows.append(f"| {variant} | " + " | ".join(
            "—" if value[field] is None else str(value[field])
            for field in (
                "cases", "financial_accuracy_pct", "director_accuracy_pct", "director_precision_pct",
                "director_recall_pct", "human_review_accuracy_pct", "numeric_guard_rejection_rate",
                "seeded_promise_recall_pct", "provocation_rejection_pct", "pains_coverage_pct",
                "objections_coverage_pct", "mean_latency_ms", "tokens_in", "tokens_out",
                "cost_rub", "saved_minutes",
            )
        ) + " |")
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        "# A/B-бенчмарк генератора КП\n\n"
        f"Режим прогона — `{report['mode']}`. Результат сравнения — `{report['winner']}`.\n\n"
        "| Вариант | Кейсы | Суммы, % | Точность РОП, % | Precision РОП, % | Recall РОП, % | "
        "Точность ручной проверки, % | Отклонения фильтром, % | Отлов подставленных обещаний, % | "
        "Отклонения на провокациях, % | Боли, % | Возражения, % | Среднее, мс | Входные токены | "
        "Выходные токены | Расход, ₽ | Экономия, мин |\n"
        "|---|" + "---:|" * 16 + "\n" + "\n".join(rows) + "\n\n"
        "Суммы сравниваются для всех уровней каждого КП. Покрытие текста измеряется "
        "по нормализованным фразам из разметки, отдельно для болей и возражений. "
        "Среднее включает API, рендеринг, SQLite и мок CRM с отключённой задержкой.\n\n"
        + (
            "В HTTP-моке ответы заданы фикстурами. Они моделируют ответ на главную реплику для A "
            "и отдельные абзацы для B. Оба варианта получают одинаковые подставленные числовые обещания. "
            "Такой прогон проверяет тракт и метрики. Качество промптов на модели проверяется в live-режиме. "
            "Ноль токенов и рублей соответствует HTTP-фикстурам.\n\n"
            if report["mode"] == "http-mock"
            else "В live-режиме столбец отлова подставленных обещаний пуст: провокационный вход "
            "может не привести к числовому обещанию в ответе.\n\n"
        )
        +
        "Экономия считается как ручное время минус время циклов и измеренное время проверки. "
        "При отсутствии одного из замеров значение остаётся пустым.\n",
        encoding="utf-8",
    )


def measured_minutes(value: str) -> Decimal:
    try:
        minutes = Decimal(value)
    except ArithmeticError:
        raise argparse.ArgumentTypeError("Expected a finite non-negative time") from None
    if not minutes.is_finite() or minutes < 0:
        raise argparse.ArgumentTypeError("Expected a finite non-negative time")
    return minutes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--summary", type=Path, default=SUMMARY)
    parser.add_argument("--live-llm", action="store_true")
    parser.add_argument("--manual-baseline-minutes", type=measured_minutes)
    parser.add_argument("--automated-review-minutes", type=measured_minutes)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="case03-ab-") as temporary:
        result = asyncio.run(run_benchmark(
            load_cases(args.dataset), Path(temporary), live=args.live_llm,
            baseline=args.manual_baseline_minutes, review=args.automated_review_minutes,
        ))
    write_report(result, args.report, args.summary)
    print(json.dumps(result["metrics"], ensure_ascii=False))


if __name__ == "__main__":
    main()
