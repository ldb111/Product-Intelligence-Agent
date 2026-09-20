"""Repair C 控制 E2E：验证 Initial Backfill、后续声明和 statement dedup。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.orchestrator import ProductPipeline
from pipeline.schemas import ProductInput
from tests.test_pipeline import ScriptedAdapters, statement_candidate


async def main() -> None:
    """在同一 workspace 顺序运行首次、后续新增和重复声明三个无网络场景。"""
    historical_text = "On 2026-09-10 we launched SSO for enterprise plans."
    baseline_only_text = "We launched an older integration without a reliable date."
    new_text = "We released audit log export."
    historical = statement_candidate(
        historical_text, initial=True, statement_date="2026-09-10"
    )
    baseline_only = statement_candidate(baseline_only_text, initial=True)
    new_statement = statement_candidate(new_text, initial=False)

    ScriptedAdapters.calls = Counter()
    ScriptedAdapters.failures = {}
    ScriptedAdapters.route = "research"
    ScriptedAdapters.verification = "still_pending"
    ScriptedAdapters.versions = ["Stable current product state."] * 3
    ScriptedAdapters.dedup_ambiguous = False
    ScriptedAdapters.detection_decision = "event"
    ScriptedAdapters.detection_statuses = []
    ScriptedAdapters.explicit_outcomes = {}
    ScriptedAdapters.explicit_event_times = {historical_text: "2026-09-10"}
    ScriptedAdapters.dedup_same_event = False
    ScriptedAdapters.statement_batches = [
        [historical, baseline_only],
        [{**historical, "initial_backfill": False, "is_new": False},
         {**baseline_only, "initial_backfill": False, "is_new": False}, new_statement],
        [{**historical, "initial_backfill": False, "is_new": False},
         {**baseline_only, "initial_backfill": False, "is_new": False},
         {**new_statement, "is_new": False}],
    ]

    product = ProductInput(
        workspace_id="repair-c", competitor_name="Example",
        official_url="https://example.com",
    )
    with tempfile.TemporaryDirectory() as temp:
        pipeline = ProductPipeline(Path(temp), adapter_type=ScriptedAdapters)

        initial = await pipeline.run(product)
        case_a = {
            "path_a_model_calls_zero": ScriptedAdapters.calls["path_a_detect"] == 0,
            "historical_event_created": len(initial["product"]["historical_events"]) == 1,
            "not_counted_as_new": initial["product"]["detected_events"] == [],
            "undated_baseline_statement_not_processed": ScriptedAdapters.calls["path_b_detect"] == 1,
            "historical_origin": initial["product"]["historical_events"][0]["discovery_origin"]
            == "historical_backfill",
        }

        subsequent = await pipeline.run(product)
        case_b = {
            "path_a_unchanged_was_checked_once": ScriptedAdapters.calls["path_a_detect"] == 1,
            "new_statement_called_path_b": ScriptedAdapters.calls["path_b_detect"] == 2,
            "normal_event_created": len(subsequent["product"]["detected_events"]) == 1,
            "normal_origin": subsequent["product"]["detected_events"][0]["discovery_origin"]
            == "normal_monitoring",
        }

        repeated = await ProductPipeline(
            Path(temp), adapter_type=ScriptedAdapters
        ).run(product)
        case_c = {
            "no_repeated_path_b_call": ScriptedAdapters.calls["path_b_detect"] == 2,
            "no_duplicate_new_event": repeated["product"]["detected_events"] == [],
            "no_duplicate_historical_event": repeated["product"]["historical_events"] == [],
        }

    result = {
        "case_a": case_a,
        "case_b": case_b,
        "case_c": case_c,
        "status": "PASS" if all(case_a.values()) and all(case_b.values()) and all(case_c.values()) else "FAIL",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
