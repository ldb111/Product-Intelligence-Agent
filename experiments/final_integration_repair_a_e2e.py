"""Correctness Repair A 的受控最小集成验收，不访问网络或真实模型。"""

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
from pipeline.store import identity
from tests.test_pipeline import ScriptedAdapters, fixture_event


SOURCE_URL = "https://example.com/product"


def reset_adapter() -> None:
    """恢复控制替身的确定性状态，避免两个验收 Case 互相污染调用记录。"""
    ScriptedAdapters.calls = Counter()
    ScriptedAdapters.failures = {}
    ScriptedAdapters.route = "research"
    ScriptedAdapters.verification = "still_pending"
    ScriptedAdapters.versions = ["Export supports CSV only.", "Export supports CSV and PDF."]
    ScriptedAdapters.dedup_ambiguous = False
    ScriptedAdapters.detection_decision = "event"
    ScriptedAdapters.detection_statuses = []


async def main() -> None:
    """运行故障恢复和 Verification no_event 两条完整编排路径并输出摘要。"""
    product = ProductInput(workspace_id="repair-a", competitor_name="Example",
                           official_url="https://example.com")
    workspace = identity([product.workspace_id, product.competitor_name])
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)

        # Case A：Event 已判定但 Dedup 尚未完成；新 run 必须再次处理 A → B。
        reset_adapter()
        pipeline = ProductPipeline(root / "recovery", adapter_type=ScriptedAdapters)
        await pipeline.run(product)
        baseline = pipeline.store.get("processed_latest:" + workspace, SOURCE_URL)
        ScriptedAdapters.failures["dedup"] = RuntimeError("controlled dedup failure")
        failed = await pipeline.run(product)
        observed_after_failure = pipeline.store.get("observed_latest:" + workspace, SOURCE_URL)
        processed_after_failure = pipeline.store.get("processed_latest:" + workspace, SOURCE_URL)
        del ScriptedAdapters.failures["dedup"]
        recovered = await ProductPipeline(root / "recovery", adapter_type=ScriptedAdapters).run(product)
        processed_after_recovery = pipeline.store.get("processed_latest:" + workspace, SOURCE_URL)
        case_a = {
            "first_attempt_failed": failed["product"]["error"]["type"] == "event_detection_error",
            "observed_b_persisted": observed_after_failure != baseline,
            "processed_remained_a": processed_after_failure == baseline,
            "new_run_reprocessed_a_to_b": ScriptedAdapters.calls["detect"] == 2,
            "event_persisted_after_recovery": len(pipeline.store.items("events:" + workspace)) == 1,
            "processed_advanced_to_b": processed_after_recovery != baseline,
            "recovered": recovered["product"]["status"] == "complete",
        }

        # Case B：Event 已持久化后 Triage 失败，水位保留 B，新 run 恢复下游动作。
        reset_adapter()
        downstream_pipeline = ProductPipeline(root / "downstream", adapter_type=ScriptedAdapters)
        await downstream_pipeline.run(product)
        downstream_baseline = downstream_pipeline.store.get("processed_latest:" + workspace, SOURCE_URL)
        ScriptedAdapters.failures["triage"] = RuntimeError("controlled triage failure")
        downstream_failed = await downstream_pipeline.run(product)
        processed_after_triage_failure = downstream_pipeline.store.get(
            "processed_latest:" + workspace, SOURCE_URL
        )
        del ScriptedAdapters.failures["triage"]
        downstream_recovered = await ProductPipeline(
            root / "downstream", adapter_type=ScriptedAdapters
        ).run(product)
        action = list(downstream_pipeline.store.items("event_actions:" + workspace).values())[0]
        case_b = {
            "triage_failed": downstream_failed["product"]["error"]["type"] == "triage_error",
            "event_was_durable": len(downstream_pipeline.store.items("events:" + workspace)) == 1,
            "processed_remained_b": processed_after_triage_failure != downstream_baseline,
            "new_run_did_not_rededuplicate_event": ScriptedAdapters.calls["dedup"] == 1,
            "new_run_resumed_triage": ScriptedAdapters.calls["triage"] == 2,
            "comparison_after_recovery_was_unchanged": ScriptedAdapters.detection_statuses == ["comparable", "unchanged"],
            "downstream_action_completed": action["status"] == "complete",
            "recovered": downstream_recovered["product"]["status"] == "complete",
        }

        # Case C：Verification 的 no_event 只决定核验方向，Event Core 才能正式解决候选。
        reset_adapter()
        ScriptedAdapters.verification = "no_event"
        ScriptedAdapters.detection_decision = "no_event"
        verification_pipeline = ProductPipeline(root / "verification", adapter_type=ScriptedAdapters)
        candidate = fixture_event(True)
        verified = await verification_pipeline.run_events(product, [candidate], provenance="controlled-repair-a")
        resolution = verification_pipeline.store.get("resolved:" + workspace, candidate.candidate_id)
        case_c = {
            "event_core_called": ScriptedAdapters.calls["detect"] == 1,
            "resolved_by_event_core_no_event": resolution and resolution["status"] == "resolved_no_event",
            "candidate_removed_from_pending": verified["product"]["pending_candidates"] == [],
            "no_confirmed_event_created": verified["product"]["detected_events"] == [],
        }

    result = {"case_a": case_a, "case_b": case_b, "case_c": case_c,
              "status": "PASS" if all(case_a.values()) and all(case_b.values()) and all(case_c.values()) else "FAIL"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
