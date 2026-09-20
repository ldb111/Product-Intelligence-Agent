"""Repair B 控制 E2E：验证 recommendation → 显式启动 → completed 复用。"""

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
from tests.test_pipeline import ScriptedAdapters, fixture_event


async def main() -> None:
    """用无网络替身证明用户触发边界，不重复验证 Research Agent 自身能力。"""
    ScriptedAdapters.calls = Counter()
    ScriptedAdapters.failures = {}
    ScriptedAdapters.route = "research"
    ScriptedAdapters.verification = "still_pending"
    ScriptedAdapters.versions = ["A", "B"]
    ScriptedAdapters.dedup_ambiguous = False
    ScriptedAdapters.detection_decision = "event"
    ScriptedAdapters.detection_statuses = []
    product = ProductInput(workspace_id="repair-b", competitor_name="Example",
                           official_url="https://example.com")

    with tempfile.TemporaryDirectory() as temp:
        pipeline = ProductPipeline(Path(temp), adapter_type=ScriptedAdapters)
        phase_a_output = await pipeline.run_events(product, [fixture_event()], provenance="controlled-repair-b")
        event_id = phase_a_output["product"]["detected_events"][0]["event_id"]
        phase_a = {
            "pipeline_completed": phase_a_output["product"]["status"] == "complete",
            "triage_recommended_research": phase_a_output["product"]["triage_results"][0]["result"]["decision"] == "research",
            "research_not_started": phase_a_output["product"]["research_actions"][0]["status"] == "not_started",
            "research_agent_calls": ScriptedAdapters.calls["deep_research"] == 0,
        }

        explicit = pipeline.start_research(product, event_id)
        phase_b = {
            "accepted": explicit["product"]["accepted"] is True,
            "completed": explicit["product"]["research"]["status"] == "completed",
            "result_present": bool(explicit["product"]["research"].get("result")),
            "research_agent_called_once": ScriptedAdapters.calls["deep_research"] == 1,
        }

        repeated = ProductPipeline(Path(temp), adapter_type=ScriptedAdapters).start_research(product, event_id)
        phase_c = {
            "completed_reused": repeated["product"]["research"]["status"] == "completed",
            "reported_reuse": repeated["audit"]["reused"] is True,
            "no_second_agent_call": ScriptedAdapters.calls["deep_research"] == 1,
        }

    result = {"phase_a": phase_a, "phase_b": phase_b, "phase_c": phase_c,
              "status": "PASS" if all(phase_a.values()) and all(phase_b.values()) and all(phase_c.values()) else "FAIL"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
