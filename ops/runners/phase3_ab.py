"""Run the stratified Phase 3 benchmark and retain its SQLite WAL databases."""

import argparse
import asyncio
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from tempfile import mkdtemp

from scripts.generate_proposal_dataset import DATASET, load_cases
from scripts.run_ab_benchmark import (
    VARIANTS,
    measured_minutes,
    run_benchmark,
    write_report,
)
from src.core.config import ROOT, load_settings

# Both offer modes and all three segments in each of the business strata.
# Include every numeric provocation and two other edge cases.
SAMPLE = (1, 8, 17, 20, 27, 32, 36, 39, 40, 46, 47, 50, *range(51, 59))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-llm", action="store_true", required=True)
    parser.add_argument("--session-budget-rub", type=measured_minutes, default=Decimal(10))
    parser.add_argument("--manual-baseline-minutes", type=measured_minutes)
    parser.add_argument("--automated-review-minutes", type=measured_minutes)
    args = parser.parse_args()
    settings = load_settings()
    cases_by_id = {case.case_id: case for case in load_cases(DATASET)}
    cases = [cases_by_id[f"proposal-{number:03d}"] for number in SAMPLE]
    attempts = len(cases) * len(VARIANTS)
    cycle_limit = settings.max_cycle_cost_rub
    if not 0 < cycle_limit <= 5 or not cycle_limit * attempts <= args.session_budget_rub <= 10:
        parser.error(
            f"Require 0 < MAX_CYCLE_COST_RUB <= 5 and {attempts} × cycle cap <= session cap <= 10",
        )
    if any(rate is None for rate in (
        settings.llm_rub_per_million_input_tokens, settings.llm_rub_per_million_output_tokens,
    )):
        parser.error("Configure explicit SMART input/output token rates")
    if (args.manual_baseline_minutes is None) != (args.automated_review_minutes is None):
        parser.error("Pass both measured durations together")

    workspace = Path(mkdtemp(prefix="phase3-ab-", dir=ROOT / "data"))
    print(f"SQLite workspace: {workspace}", flush=True)
    report = asyncio.run(run_benchmark(
        cases, workspace, live=args.live_llm,
        baseline=args.manual_baseline_minutes, review=args.automated_review_minutes,
    ))
    report["planned_cycles"] = attempts
    report["cycle_budget_rub"] = str(cycle_limit)
    report["session_budget_rub"] = str(args.session_budget_rub)
    report["smart_model"] = settings.smart_llm_model
    report["cost_verified"] = {}
    for variant in VARIANTS:
        with closing(sqlite3.connect(workspace / f"{variant}.db")) as database:
            if database.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                raise RuntimeError(f"{variant}: expected WAL storage")
            usage = database.execute(
                "SELECT tokens_in, tokens_out, cost_rub, cost_complete FROM telemetry",
            ).fetchall()
        metric = report["metrics"][variant]
        report["cost_verified"][variant] = (
            len(usage) == metric["cases"]
            and all(row[3] == 1 for row in usage)
            and sum(row[0] for row in usage) == metric["tokens_in"]
            and sum(row[1] for row in usage) == metric["tokens_out"]
            and sum((Decimal(row[2]) for row in usage), Decimal(0)) == Decimal(metric["cost_rub"])
        )
    report["sqlite_wal_verified"] = True
    report["total_cost_rub"] = str(sum(
        Decimal(metric["cost_rub"]) for metric in report["metrics"].values()
    ))
    write_report(
        report, ROOT / "artifacts/benchmarks/phase3_live_llm_report.json",
        workspace / "summary.md",
    )


if __name__ == "__main__":
    main()
