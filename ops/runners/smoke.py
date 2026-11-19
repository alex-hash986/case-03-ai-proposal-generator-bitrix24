import argparse
import json
from uuid import uuid4

import httpx

from src.core.config import load_settings
from src.ui.presets import PRESETS


def main() -> None:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--live-llm", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    with httpx.Client(base_url=f"http://127.0.0.1:{settings.api_port}", timeout=90) as client:
        health = client.get("/health")
        health.raise_for_status()
        expected = "live" if args.live_llm else "mock"
        if health.json()["llm_mode"] != expected or health.json()["bitrix_mode"] != "mock":
            raise SystemExit(f"Expected LLM_MODE={expected} and BITRIX_MODE=mock")
        for index, (preset, payload) in enumerate(PRESETS.items()):
            key = uuid4().hex
            query = {"simulate_failure": index == 1}
            generated = client.post(
                "/api/v1/proposals/generate", json=payload,
                headers={"Idempotency-Key": key}, params=query,
            )
            generated.raise_for_status()
            result = generated.json()
            if index == 1:
                assert result["status"] == "reconcile"
                reconciled = client.post(
                    "/api/v1/proposals/reconcile", json={"idempotency_key": key},
                )
                reconciled.raise_for_status()
                assert reconciled.json()["cycle_id"] == result["cycle_id"]
                result = reconciled.json()
            assert result["status"] == ("requires_approval" if index == 2 else "completed")
            repeated = client.post(
                "/api/v1/proposals/generate", json=payload, headers={"Idempotency-Key": key},
            )
            repeated.raise_for_status()
            assert repeated.json() == result
            print(json.dumps({
                "preset": preset, "cycle_id": result["cycle_id"], "status": result["status"],
                "latency_ms": result["latency_ms"], "pdf": result["pdf_path"],
                "docx": result["docx_path"],
            }, ensure_ascii=False))
        metrics = client.get("/api/v1/metrics")
        metrics.raise_for_status()
        print(json.dumps(metrics.json(), ensure_ascii=False))


if __name__ == "__main__":
    main()
