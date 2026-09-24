"""集成控制测试：复用正式差异和事件构造器，模型回答用明确的脚本替身。"""

import json
import asyncio
import tempfile
import threading
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from event_core.schemas import DedupResult, EventCandidate, EventDecision, Quote
from event_core.inputs import build_explicit_statement_input
from event_core.evidence import merge_evidence
from llm.config import LLMConfigurationError
from llm.retrieval_models import EmbeddingAPIError
from pipeline.adapters import ModuleAdapters
from pipeline.configuration import ProductConfigurationService
from pipeline.documents import workspace_knowledge_path
from pipeline.orchestrator import ProductPipeline
from pipeline.output import present_artifact, public_text
from pipeline.schemas import ProductInput
from pipeline.statements import (
    INITIAL_BACKFILL_MAX_EVENTS, extract_statement_candidates, select_initial_backfill,
)
from pipeline.store import PipelineStore, identity, product_event_id
from source_discovery.schemas import SourceDiscoveryResult
from tests.test_event_core import data, decision_for, detect_with_mock, snapshot
from triage.schemas import TriageResult


def fixture_event(uncertain=False):
    return detect_with_mock(data(old=None) if uncertain else data())[0].candidates[0]


def second_fixture_event():
    """构造同一竞品下另一条业务事实不同的 confirmed Event。"""
    value = fixture_event().model_dump(mode="json")
    value.update(
        candidate_id=identity("second-event"),
        event_subject="API",
        change_action="增加批量 API",
        change_items=[{
            "item": "API",
            "previous_state": "Only single-item API requests are supported.",
            "current_state": "Batch API requests are supported.",
        }],
    )
    return EventCandidate.model_validate(value)


def statement_candidate(
    text: str, *, initial: bool, statement_date: str | None = None,
    observed_at: str = "2026-09-20T10:00:00+08:00",
    url: str = "https://example.com/changelog",
) -> dict:
    """构造已经过 Integration 结构提取的 Path B 候选，不伪造 Event Core 结论。"""
    value = snapshot(text, observed_at)
    value.update(url=url, final_url=url, title="Example Changelog")
    detection_input = build_explicit_statement_input(
        value, text, competitor="Example", source_type="changelog", source_credibility="official"
    )
    return {
        "statement_id": identity([url, " ".join(text.split()), ""]),
        "source_url": url,
        "statement_date": statement_date,
        "input": detection_input.model_dump(mode="json"),
        "initial_backfill": initial,
        "is_new": True,
        "snapshot_id": identity([url, observed_at]),
    }


def discovery():
    return SourceDiscoveryResult(competitor_name="Example", official_domains=["example.com"], sources=[
        {"url": "https://example.com/product", "title": "Product", "source_type": "product_page",
         "monitoring_reason": "观察导出能力"}], discovery_complete=True, remaining_gaps=[])


class ScriptedAdapters(ModuleAdapters):
    """每次测试独立设置调用记录；只有外部副作用和模型语义被替换。"""
    calls = Counter()
    failures = {}
    route = "research"
    verification = "still_pending"
    versions = ["Export supports CSV only.", "Export supports CSV and PDF."]
    dedup_ambiguous = False
    detection_decision = "event"
    detection_statuses = []
    statement_batches = []
    explicit_outcomes = {}
    explicit_event_times = {}
    dedup_same_event = False
    follow_up_inputs = []

    def hit(self, name):
        self.calls[name] += 1
        if name in self.failures:
            raise self.failures[name]

    async def discover(self):
        self.hit("discover")
        return discovery()

    def scan(self, url):
        self.hit("scan")
        index = min(self.calls["scan"] - 1, len(self.versions) - 1)
        return snapshot(self.versions[index], f"2026-09-{self.calls['scan']:02d}T10:00:00+08:00")

    def detect(self, value):
        self.hit("detect")
        self.detection_statuses.append(value.state_context.comparison_status)
        decision = None
        if value.detection_path == "explicit_statement" and value.source_type != "verified_statement":
            self.calls["path_b_detect"] += 1
            outcome = self.explicit_outcomes.get(value.current_state, "event")
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == "no_event":
                decision = EventDecision(decision="no_event", decision_reason="不是明确变化声明。")
            else:
                decision = decision_for(value)
                event_time = self.explicit_event_times.get(value.current_state)
                if event_time:
                    decision.event_time = event_time
                    decision.citations["event_time"] = [
                        Quote(input_field="current_state", excerpt=value.current_state)
                    ]
        elif self.detection_decision == "no_event":
            if value.detection_path == "state_comparison":
                self.calls["path_a_detect"] += 1
            decision = EventDecision(decision="no_event", decision_reason="正式检测未发现产品状态变化。")
        elif self.detection_decision == "uncertain":
            if value.detection_path == "state_comparison":
                self.calls["path_a_detect"] += 1
            decision = EventDecision(decision="uncertain", decision_reason="正式证据仍不足。",
                                     uncertainty_reason="正式证据仍不足。")
        elif value.detection_path == "state_comparison":
            self.calls["path_a_detect"] += 1
        return detect_with_mock(value, decision)[0]

    def deduplicate(self, candidate, existing):
        self.hit("dedup")
        if self.dedup_ambiguous:
            return DedupResult(decision="UNCERTAIN", reason="同一性待判断", pending_candidate=candidate)
        if self.dedup_same_event and existing:
            merged = existing[0].model_copy(
                update={"evidence": merge_evidence(existing[0].evidence, candidate.evidence)}
            )
            return DedupResult(decision="SAME_EVENT", reason="测试脚本判定为同一事件",
                               matched_event_id=existing[0].candidate_id, event=merged)
        return DedupResult(decision="NEW_EVENT", reason="测试脚本的新事件", event=candidate)

    def explicit_statement_candidates(self, history, source):
        self.calls["statement_extract"] += 1
        index = min(self.calls["scan"] - 1, len(self.statement_batches) - 1)
        return deepcopy(self.statement_batches[index]) if self.statement_batches else []

    def ingest_own(self):
        self.hit("own")
        return {"results": [], "runtime": []}

    def own_product_context_identity(self):
        return []

    def ingest_event(self, event):
        self.hit("admit")
        return {"results": [], "runtime": []}

    def triage(self, event):
        self.hit("triage")
        result = TriageResult(event_id=event.candidate_id, decision=self.route, reason="测试路由",
            focus_relevance="导出能力", own_product_relevance={"status": "insufficient_own_product_context", "claims": []},
            evidence_refs=["event_evidence_001"], missing_context=["insufficient_own_product_context"], uncertainty=[])
        return {"result": result.model_dump(), "context": {"secret_internal_path": "E:\\internal\\store.sqlite3"},
                "citations": [{"result_id": "event_evidence_001", "url": event.evidence[0].source_url,
                               "title": "原始证据", "content": event.evidence[0].relevant_excerpt,
                               "source": "event", "domain": "competitor", "source_reference": "E:\\internal\\data.json"}],
                "audit": {"provider": "private-provider", "raw_tool_trace": "private-trace"}}

    def research(self, event, mode, triage_result, official_hosts):
        self.hit(mode)
        if mode == "verification":
            content = ("PDF export is not available; CSV remains the only export format."
                       if self.verification == "no_event" else "We released PDF export in public beta.")
            citations = [{"result_id": "research_evidence_001", "url": "https://example.com/news", "title": "News",
                          "content": content, "credibility": "official", "source": "page", "domain": "competitor",
                          "observed_at": "2026-09-03T10:00:00+08:00"}]
            result = {"decision": self.verification, "reason": "测试核验结论",
                      "new_evidence_refs": ["research_evidence_001"],
                      "support": [{"result_id": "research_evidence_001", "excerpt": content}],
                      "unresolved_questions": ["待进一步确认"] if self.verification == "still_pending" else []}
        else:
            result = {"event_summary": [], "background": [], "competitor_context": [],
                      "own_product_comparison": {"status": "insufficient_own_product_context", "claims": []},
                      "implications": [], "watch_next": [], "uncertainties": ["缺自身资料"], "evidence_refs": []}
            citations = []
        return {"result": result, "citations": citations, "audit": {"provider": "private-provider"}}

    def extract_preference_candidates(self, text, *, event_id, turn_id):
        self.hit("extract_preference")
        return [{"content": text}] if "长期关注" in text else []

    def follow_up(self, event, triage_result, official_hosts, *, initial_artifact,
                  conversation_history, question):
        self.hit("follow_up")
        self.follow_up_inputs.append({
            "question": question, "history": deepcopy(conversation_history),
            "initial": deepcopy(initial_artifact),
        })
        return {"result": {"answer": [{"text": "继续研究回答", "kind": "recommendation", "support": []}],
                           "uncertainties": [], "evidence_refs": []},
                "citations": [], "audit": {"provider": "private-provider"}}

    def remember_explicit(self, text):
        self.hit("write_memory")
        return {"result": {"content": text}, "runtime": []}


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 10

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.product = ProductInput(workspace_id="w", competitor_name="Example", official_url="https://example.com")
        self.source_url = "https://example.com/product"
        ScriptedAdapters.calls = Counter()
        ScriptedAdapters.failures = {}
        ScriptedAdapters.route = "research"
        ScriptedAdapters.verification = "still_pending"
        ScriptedAdapters.versions = ["Export supports CSV only.", "Export supports CSV and PDF."]
        ScriptedAdapters.dedup_ambiguous = False
        ScriptedAdapters.detection_decision = "event"
        ScriptedAdapters.detection_statuses = []
        ScriptedAdapters.statement_batches = []
        ScriptedAdapters.explicit_outcomes = {}
        ScriptedAdapters.explicit_event_times = {}
        ScriptedAdapters.dedup_same_event = False
        ScriptedAdapters.follow_up_inputs = []
        self.pipeline = ProductPipeline(self.root, adapter_type=ScriptedAdapters)

    async def replay(self, uncertain=False):
        return await self.pipeline.run_events(self.product, [fixture_event(uncertain)], provenance="controlled-test")

    async def recommendation(self):
        """生成一条 confirmed Event + research 推荐，并返回产品级 Event 标识。"""
        output = await self.replay()
        return output, output["product"]["detected_events"][0]["event_id"]

    async def test_first_scan_only_creates_baseline(self):
        result = await self.pipeline.run(self.product)
        self.assertTrue(result["product"]["baseline_created"])
        self.assertEqual(result["product"]["detected_events"], [])
        self.assertEqual(ScriptedAdapters.calls["detect"], 0)
        self.assertEqual(ScriptedAdapters.calls["triage"], 0)

    async def test_sync_browser_scan_runs_outside_asyncio_event_loop(self):
        """回归 FastAPI 下同步 Playwright 因处于 Event Loop 而失败的问题。"""

        class EventLoopSensitiveAdapter(ScriptedAdapters):
            def scan(self, url):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    return super().scan(url)
                raise RuntimeError("同步浏览器采集仍在 asyncio Event Loop 内运行。")

        pipeline = ProductPipeline(self.root, adapter_type=EventLoopSensitiveAdapter)
        result = await pipeline.run(self.product)
        self.assertEqual(result["product"]["status"], "complete")
        self.assertTrue(result["product"]["baseline_created"])

    async def test_first_scan_current_fact_is_only_baseline(self):
        existing = statement_candidate("Supports SSO.", initial=True, statement_date=None)
        genuinely_new = statement_candidate("We launched SCIM.", initial=False)
        ScriptedAdapters.versions = ["Stable product page."] * 3
        ScriptedAdapters.statement_batches = [
            [existing],
            [{**existing, "initial_backfill": False}],
            [{**existing, "initial_backfill": False}, genuinely_new],
        ]
        result = await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        self.assertTrue(result["product"]["baseline_created"])
        self.assertEqual(result["product"]["detected_events"], [])
        self.assertEqual(result["product"]["historical_events"], [])
        self.assertEqual(ScriptedAdapters.calls["path_a_detect"], 0)
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 0)
        observation = self.pipeline.store.get(
            "statement_observations:" + workspace, existing["statement_id"]
        )
        self.assertTrue(observation["seen_at_baseline"])
        self.assertEqual(self.pipeline.store.items("statement_processing:" + workspace), {})

        reopened = ProductPipeline(self.root, adapter_type=ScriptedAdapters)
        unchanged = await reopened.run(self.product)
        self.assertEqual(unchanged["product"]["detected_events"], [])
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 0)
        self.assertIsNotNone(reopened.store.get(
            "statement_observations:" + workspace, existing["statement_id"]
        ))

        added = await reopened.run(self.product)
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 1)
        self.assertEqual(len(added["product"]["detected_events"]), 1)
        self.assertEqual(added["product"]["detected_events"][0]["discovery_origin"], "normal_monitoring")
        self.assertIsNone(added["product"]["detected_events"][0]["event_time"])

    async def test_first_scan_recent_dated_statement_becomes_historical_event(self):
        text = "On 2026-09-10 we launched SSO for enterprise plans."
        ScriptedAdapters.statement_batches = [[
            statement_candidate(text, initial=True, statement_date="2026-09-10")
        ]]
        ScriptedAdapters.explicit_event_times = {text: "2026-09-10"}
        result = await self.pipeline.run(self.product)
        self.assertEqual(ScriptedAdapters.calls["path_a_detect"], 0)
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 1)
        self.assertEqual(result["product"]["detected_events"], [])
        self.assertEqual(len(result["product"]["historical_events"]), 1)
        event = result["product"]["historical_events"][0]
        self.assertEqual(event["discovery_origin"], "historical_backfill")
        self.assertEqual(event["event_time"], "2026-09-10")
        self.assertNotEqual(event["event_time"], "2026-09-20T10:00:00+08:00")
        self.assertNotIn(
            ScriptedAdapters.statement_batches[0][0]["statement_id"], json.dumps(result["product"])
        )

    async def test_first_scan_old_or_undated_statement_is_not_backfilled(self):
        old = statement_candidate(
            "On 2026-05-01 we launched SSO.", initial=True, statement_date="2026-05-01"
        )
        undated = statement_candidate("We launched SCIM.", initial=True, statement_date=None)
        ScriptedAdapters.versions = ["Stable product page."] * 2
        ScriptedAdapters.statement_batches = [
            [old, undated],
            [{**old, "initial_backfill": False}, {**undated, "initial_backfill": False}],
        ]
        result = await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        self.assertEqual(result["product"]["historical_events"], [])
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 0)
        observations = self.pipeline.store.items("statement_observations:" + workspace)
        self.assertEqual(len(observations), 2)
        self.assertTrue(all(item["seen_at_baseline"] for item in observations.values()))
        await ProductPipeline(self.root, adapter_type=ScriptedAdapters).run(self.product)
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 0)

    async def test_initial_backfill_filters_then_sorts_and_caps_at_twenty(self):
        candidates = []
        reference = date(2026, 9, 20)
        for days_ago in range(25):
            statement_date = reference - timedelta(days=days_ago)
            candidates.append(statement_candidate(
                f"On {statement_date.isoformat()} we released capability {days_ago}.",
                initial=True, statement_date=statement_date.isoformat(),
            ))
        candidates.append(statement_candidate(
            "On 2026-01-01 we released an old capability.", initial=True,
            statement_date="2026-01-01",
        ))
        selected = select_initial_backfill(
            list(reversed(candidates)), "2026-09-20T10:00:00+08:00", INITIAL_BACKFILL_MAX_EVENTS
        )
        self.assertEqual(len(selected), 20)
        self.assertEqual(selected[0]["statement_date"], "2026-09-20")
        self.assertEqual(selected[-1]["statement_date"], "2026-09-01")
        self.assertNotIn("2026-01-01", {item["statement_date"] for item in selected})

    async def test_initial_backfill_pipeline_never_calls_more_than_twenty(self):
        reference = date(2026, 9, 20)
        candidates = [
            statement_candidate(
                f"On {(reference - timedelta(days=index)).isoformat()} we released capability {index}.",
                initial=True, statement_date=(reference - timedelta(days=index)).isoformat(),
            )
            for index in range(25)
        ]
        ScriptedAdapters.versions = ["Stable product page."] * 2
        ScriptedAdapters.statement_batches = [
            candidates, [{**item, "initial_backfill": False} for item in candidates]
        ]
        result = await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 20)
        self.assertEqual(len(result["product"]["historical_events"]), 20)
        self.assertEqual(result["product"]["detected_events"], [])
        self.assertEqual(len(self.pipeline.store.items("statement_observations:" + workspace)), 25)
        self.assertEqual(len(self.pipeline.store.items("statement_processing:" + workspace)), 20)

        unchanged = await ProductPipeline(self.root, adapter_type=ScriptedAdapters).run(self.product)
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 20)
        self.assertEqual(unchanged["product"]["detected_events"], [])
        self.assertEqual(unchanged["product"]["historical_events"], [])

    async def test_subsequent_new_undated_statement_runs_path_b_when_path_a_unchanged(self):
        text = "We launched a new audit log export."
        ScriptedAdapters.versions = ["Stable product page.", "Stable product page.", "Stable product page."]
        ScriptedAdapters.statement_batches = [[], [statement_candidate(text, initial=False)], [
            {**statement_candidate(text, initial=False), "is_new": False}
        ]]
        await self.pipeline.run(self.product)
        second = await self.pipeline.run(self.product)
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 1)
        self.assertEqual(len(second["product"]["detected_events"]), 1)
        self.assertEqual(second["product"]["detected_events"][0]["discovery_origin"], "normal_monitoring")
        self.assertIsNone(second["product"]["detected_events"][0]["event_time"])
        observed = self.pipeline.store.items("events:" + identity(
            [self.product.workspace_id, self.product.competitor_name]
        ))
        self.assertEqual(next(iter(observed.values()))["evidence"][0]["observed_at"],
                         "2026-09-20T10:00:00+08:00")

        third = await self.pipeline.run(self.product)
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 1)
        self.assertEqual(third["product"]["detected_events"], [])

    async def test_statement_identity_dedup_is_separate_from_event_dedup(self):
        first = statement_candidate(
            "On 2026-09-10 we announced SSO.", initial=True, statement_date="2026-09-10",
            url="https://example.com/changelog",
        )
        second = statement_candidate(
            "On 2026-09-10 we announced enterprise SSO.", initial=True, statement_date="2026-09-10",
            url="https://example.com/blog",
        )
        ScriptedAdapters.statement_batches = [[first, second]]
        ScriptedAdapters.dedup_same_event = True
        result = await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 2)
        self.assertEqual(len(self.pipeline.store.items("statement_processing:" + workspace)), 2)
        self.assertEqual(len(self.pipeline.store.items("events:" + workspace)), 1)
        self.assertEqual(len(result["product"]["historical_events"]), 1)
        self.assertEqual(len(next(iter(self.pipeline.store.items("events:" + workspace).values()))["evidence"]), 2)

    async def test_announcement_and_later_ga_have_distinct_statement_identity(self):
        announced = statement_candidate(
            "On 2026-09-01 we announced SSO for Q4.", initial=True, statement_date="2026-09-01"
        )
        launched = statement_candidate(
            "On 2026-09-15 SSO became generally available.", initial=True, statement_date="2026-09-15"
        )
        ScriptedAdapters.statement_batches = [[announced, launched]]
        await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        self.assertNotEqual(announced["statement_id"], launched["statement_id"])
        self.assertEqual(len(self.pipeline.store.items("statement_processing:" + workspace)), 2)
        self.assertEqual(len(self.pipeline.store.items("events:" + workspace)), 2)

    async def test_path_b_partial_failure_retries_only_pending_statement_after_reopen(self):
        ScriptedAdapters.versions = ["Stable product page.", "Stable product page."]
        candidates = [
            statement_candidate(
                f"On 2026-09-{index:02d} we released capability {index}.", initial=True,
                statement_date=f"2026-09-{index:02d}",
            )
            for index in range(1, 21)
        ]
        failed = [candidates[5], candidates[13]]
        ScriptedAdapters.statement_batches = [candidates]
        ScriptedAdapters.explicit_outcomes = {
            item["input"]["current_state"]: RuntimeError("controlled Path B failure")
            for item in failed
        }
        first = await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        states = self.pipeline.store.items("statement_processing:" + workspace)
        observations = self.pipeline.store.items("statement_observations:" + workspace)
        self.assertEqual(first["product"]["status"], "failed")
        self.assertEqual(len(observations), 20)
        self.assertTrue(all(item["seen_at_baseline"] for item in observations.values()))
        self.assertEqual(sum(state["status"] == "processed" for state in states.values()), 18)
        self.assertEqual(sum(state["status"] == "pending" for state in states.values()), 2)
        self.assertEqual(len(self.pipeline.store.items("events:" + workspace)), 18)
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 20)
        self.assertIsNotNone(self.pipeline.store.get("processed_latest:" + workspace, self.source_url))

        ScriptedAdapters.explicit_outcomes = {}
        reopened = ProductPipeline(self.root, adapter_type=ScriptedAdapters)
        recovered = await reopened.resume(first["run_id"])
        states = reopened.store.items("statement_processing:" + workspace)
        self.assertEqual(recovered["product"]["status"], "complete")
        self.assertTrue(all(state["status"] == "processed" for state in states.values()))
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 22)
        self.assertEqual(len(reopened.store.items("events:" + workspace)), 20)

        await ProductPipeline(self.root, adapter_type=ScriptedAdapters).run(self.product)
        self.assertEqual(ScriptedAdapters.calls["path_b_detect"], 22)

    def test_structured_candidate_extraction_uses_official_blocks_and_reliable_dates(self):
        recent_text = "On 2026-09-10 we released SSO."
        blocks = [
            {"type": "heading", "level": 2, "text": "2026-09-10", "links": []},
            {"type": "paragraph", "text": recent_text, "links": []},
            {"type": "heading", "level": 2, "text": "Current capabilities", "links": []},
            {"type": "paragraph", "text": "Supports SCIM.", "links": []},
        ]
        content = "## 2026-09-10\n\n" + recent_text + "\n\n## Current capabilities\n\nSupports SCIM."
        current = snapshot(recent_text, "2026-09-20T10:00:00+08:00")
        current.update(blocks=blocks, content=content, final_url="https://example.com/changelog")
        history = {"is_first_scan": True, "current_snapshot": current, "previous_snapshot": None,
                   "snapshot_id": "snapshot-1"}
        source = {"url": "https://example.com/changelog", "source_type": "changelog",
                  "credibility": "official", "official_domains": ["example.com"]}
        candidates = extract_statement_candidates(history, source, competitor="Example")
        self.assertEqual(candidates[0]["statement_date"], "2026-09-10")
        self.assertIsNone(candidates[1]["statement_date"])
        source["credibility"] = "unverified"
        self.assertEqual(extract_statement_candidates(history, source, competitor="Example"), [])

    def test_structured_candidate_extraction_has_per_source_cost_bound(self):
        blocks = [
            {"type": "paragraph", "text": f"Statement block {index}.", "links": []}
            for index in range(65)
        ]
        current = snapshot("placeholder", "2026-09-20T10:00:00+08:00")
        current.update(
            blocks=blocks, content="\n\n".join(block["text"] for block in blocks),
            final_url="https://example.com/changelog",
        )
        history = {"is_first_scan": True, "current_snapshot": current, "previous_snapshot": None,
                   "snapshot_id": "bounded-snapshot"}
        source = {"url": "https://example.com/changelog", "source_type": "changelog",
                  "credibility": "official", "official_domains": ["example.com"]}
        self.assertEqual(len(extract_statement_candidates(history, source, competitor="Example")), 60)

    def test_statement_identity_and_seen_state_survive_store_reopen(self):
        text = "We released audit log export."
        source = {"url": "https://example.com/changelog", "source_type": "changelog",
                  "credibility": "official", "official_domains": ["example.com"]}
        first_snapshot = snapshot(text, "2026-09-20T10:00:00+08:00")
        first_snapshot.update(final_url=source["url"])
        second_snapshot = snapshot(text, "2026-09-21T10:00:00+08:00")
        second_snapshot.update(final_url=source["url"])
        first = extract_statement_candidates(
            {"is_first_scan": True, "current_snapshot": first_snapshot,
             "previous_snapshot": None, "snapshot_id": "first"},
            source, competitor="Example",
        )[0]
        second = extract_statement_candidates(
            {"is_first_scan": False, "current_snapshot": second_snapshot,
             "previous_snapshot": first_snapshot, "snapshot_id": "second"},
            source, competitor="Example",
        )[0]
        self.assertEqual(first["statement_id"], second["statement_id"])

        workspace = "statement-seen-reopen"
        self.assertFalse(self.pipeline.store.observe_statements(workspace, [first])[
            first["statement_id"]
        ]["was_seen"])
        reopened = PipelineStore(self.root)
        self.assertTrue(reopened.observe_statements(workspace, [second])[
            second["statement_id"]
        ]["was_seen"])
        self.assertTrue(reopened.get(
            "statement_observations:" + workspace, first["statement_id"]
        )["seen_at_baseline"])

    async def test_second_scan_enters_event_detection(self):
        await self.pipeline.run(self.product)
        result = await self.pipeline.run(self.product)
        self.assertEqual(result["product"]["status"], "complete")
        self.assertFalse(result["product"]["baseline_created"])
        self.assertEqual(len(result["product"]["detected_events"]), 1)
        self.assertEqual(ScriptedAdapters.calls["detect"], 1)

    async def test_unchanged_scan_gets_deterministic_no_event(self):
        ScriptedAdapters.versions = ["Export supports CSV only."] * 2
        await self.pipeline.run(self.product)
        result = await self.pipeline.run(self.product)
        self.assertFalse(result["product"]["baseline_created"])
        self.assertEqual(ScriptedAdapters.calls["detect"], 1)
        self.assertEqual(result["product"]["detected_events"], [])

    async def test_confirmed_event_routes_through_triage(self):
        output = await self.replay()
        self.assertEqual(len(output["product"]["triage_results"]), 1)
        self.assertEqual(ScriptedAdapters.calls["admit"], 1)

    async def test_research_route_creates_not_started_recommendation_without_agent_call(self):
        output, _ = await self.recommendation()
        self.assertEqual(output["product"]["status"], "complete")
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 0)
        self.assertEqual(ScriptedAdapters.calls["verification"], 0)
        self.assertEqual(output["product"]["research_actions"][0]["status"], "not_started")
        self.assertEqual(output["product"]["research_results"], [])

    async def test_monitor_does_not_research(self):
        ScriptedAdapters.route = "monitor"
        result = await self.replay()
        self.assertEqual(result["product"]["research_results"], [])
        self.assertEqual(result["product"]["research_actions"], [])
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 0)
        event_id = result["product"]["detected_events"][0]["event_id"]
        rejected = self.pipeline.start_research(self.product, event_id)
        self.assertFalse(rejected["product"]["accepted"])
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 0)

    async def test_no_action_does_not_research(self):
        ScriptedAdapters.route = "no_action"
        result = await self.replay()
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 0)
        self.assertEqual(result["product"]["research_actions"], [])
        event_id = result["product"]["detected_events"][0]["event_id"]
        rejected = self.pipeline.start_research(self.product, event_id)
        self.assertEqual(rejected["product"]["reason"], "research_not_recommended")
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 0)

    async def test_uncertain_routes_to_verification_and_remains_pending(self):
        result = await self.replay(True)
        self.assertEqual(result["product"]["status"], "pending")
        self.assertEqual(len(result["product"]["pending_candidates"]), 1)
        self.assertIn("pending_id", result["product"]["pending_candidates"][0]["candidate"])
        self.assertNotIn("candidate_id", json.dumps(result["product"]))
        self.assertEqual(ScriptedAdapters.calls["verification"], 1)
        self.assertEqual(ScriptedAdapters.calls["triage"], 0)
        self.assertEqual(ScriptedAdapters.calls["admit"], 0)

    async def test_verified_event_reenters_formal_detection_and_triage(self):
        ScriptedAdapters.verification = "event"
        result = await self.replay(True)
        self.assertEqual(result["product"]["status"], "complete")
        self.assertEqual(ScriptedAdapters.calls["detect"], 1)
        self.assertEqual(ScriptedAdapters.calls["triage"], 1)
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 0)
        self.assertEqual(result["product"]["pending_candidates"], [])

    async def test_verification_no_event_reenters_event_core_before_resolution(self):
        ScriptedAdapters.verification = "no_event"
        ScriptedAdapters.detection_decision = "no_event"
        result = await self.replay(True)
        self.assertEqual(result["product"]["pending_candidates"], [])
        self.assertEqual(result["product"]["detected_events"], [])
        self.assertEqual(ScriptedAdapters.calls["detect"], 1)
        self.assertEqual(ScriptedAdapters.calls["triage"], 0)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        candidate = fixture_event(True)
        self.assertEqual(self.pipeline.store.get("resolved:" + workspace, candidate.candidate_id)["status"],
                         "resolved_no_event")

    async def test_research_outputs_not_admitted_or_remembered(self):
        _, event_id = await self.recommendation()
        self.pipeline.start_research(self.product, event_id)
        self.assertEqual(ScriptedAdapters.calls["admit"], 1)
        self.assertEqual(ScriptedAdapters.calls["write_memory"], 0)

    async def test_missing_own_does_not_invent_capability(self):
        result = await self.replay()
        own = result["product"]["triage_results"][0]["result"]["own_product_relevance"]
        self.assertEqual(own, {"status": "insufficient_own_product_context", "claims": []})

    async def test_product_and_audit_separated(self):
        result = await self.replay()
        public = json.dumps(result["product"])
        for text in ("candidate_id", "pipeline_stages", "provider", "private-provider", "private-trace",
                     "sqlite", "source_reference", "raw_tool_trace", "E:\\"):
            self.assertNotIn(text, public)
        event = result["product"]["detected_events"][0]
        self.assertTrue(event["event_id"].startswith("evt_"))
        self.assertEqual(result["product"]["triage_results"][0]["result"]["event_id"], event["event_id"])
        self.assertIn("private-provider", json.dumps(result["audit"]))
        self.assertTrue(result["audit"]["steps"])

    async def test_internal_candidate_id_cannot_start_research(self):
        await self.recommendation()
        internal_id = fixture_event().candidate_id
        rejected = self.pipeline.start_research(self.product, internal_id)
        self.assertFalse(rejected["product"]["accepted"])
        self.assertEqual(rejected["product"]["reason"], "confirmed_event_not_found")
        self.assertNotIn(internal_id, json.dumps(rejected["product"]))
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 0)

    async def test_source_discovery_failure_stops_scan(self):
        ScriptedAdapters.failures["discover"] = RuntimeError("private failure")
        result = await self.pipeline.run(self.product)
        self.assertEqual(result["product"]["error"]["type"], "source_discovery_error")
        self.assertEqual(ScriptedAdapters.calls["scan"], 0)

    async def test_scan_failure_stops_event_detection(self):
        ScriptedAdapters.failures["scan"] = RuntimeError("private failure")
        result = await self.pipeline.run(self.product)
        self.assertEqual(result["product"]["error"]["type"], "scan_error")
        self.assertFalse(result["product"]["baseline_created"])

    async def test_detection_failure_preserves_snapshot(self):
        await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        baseline = self.pipeline.store.get("processed_latest:" + workspace, self.source_url)
        ScriptedAdapters.failures["detect"] = RuntimeError("private failure")
        result = await self.pipeline.run(self.product)
        self.assertEqual(result["product"]["error"]["type"], "event_detection_error")
        self.assertEqual(len(self.pipeline.store.items("snapshots")), 2)
        self.assertNotEqual(self.pipeline.store.get("observed_latest:" + workspace, self.source_url), baseline)
        self.assertEqual(self.pipeline.store.get("processed_latest:" + workspace, self.source_url), baseline)
        self.assertEqual(ScriptedAdapters.calls["triage"], 0)

    async def test_successful_detection_advances_processed_baseline(self):
        await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        baseline = self.pipeline.store.get("processed_latest:" + workspace, self.source_url)
        await self.pipeline.run(self.product)
        processed = self.pipeline.store.get("processed_latest:" + workspace, self.source_url)
        self.assertNotEqual(processed, baseline)
        self.assertEqual(processed, self.pipeline.store.get("observed_latest:" + workspace, self.source_url))

    async def test_new_run_recovers_unprocessed_comparison(self):
        await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        baseline = self.pipeline.store.get("processed_latest:" + workspace, self.source_url)
        ScriptedAdapters.failures["detect"] = RuntimeError("fail once")
        failed = await self.pipeline.run(self.product)
        self.assertEqual(failed["product"]["error"]["type"], "event_detection_error")
        del ScriptedAdapters.failures["detect"]
        recovered = await ProductPipeline(self.root, adapter_type=ScriptedAdapters).run(self.product)
        processed = self.pipeline.store.get("processed_latest:" + workspace, self.source_url)
        self.assertEqual(recovered["product"]["status"], "complete")
        self.assertNotEqual(processed, baseline)
        self.assertEqual(ScriptedAdapters.calls["detect"], 2)

    async def test_same_run_resume_reuses_snapshot_and_comparison(self):
        await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        ScriptedAdapters.failures["detect"] = RuntimeError("fail once")
        failed = await self.pipeline.run(self.product)
        snapshot_count = len(self.pipeline.store.items("snapshots"))
        del ScriptedAdapters.failures["detect"]
        recovered = await ProductPipeline(self.root, adapter_type=ScriptedAdapters).resume(failed["run_id"])
        self.assertEqual(recovered["product"]["status"], "complete")
        self.assertEqual(len(self.pipeline.store.items("snapshots")), snapshot_count)
        self.assertEqual(ScriptedAdapters.calls["scan"], 2)
        self.assertEqual(self.pipeline.store.get("processed_latest:" + workspace, self.source_url),
                         self.pipeline.store.get("observed_latest:" + workspace, self.source_url))

    async def test_no_event_advances_processed_baseline(self):
        await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        baseline = self.pipeline.store.get("processed_latest:" + workspace, self.source_url)
        ScriptedAdapters.detection_decision = "no_event"
        await self.pipeline.run(self.product)
        self.assertNotEqual(self.pipeline.store.get("processed_latest:" + workspace, self.source_url), baseline)
        comparison = list(self.pipeline.store.items("comparisons:" + workspace).values())[-1]
        self.assertEqual(comparison["status"], "processed")
        self.assertEqual(comparison["event_core_outcomes"], ["no_event"])

    async def test_uncertain_is_saved_before_processed_baseline_advances(self):
        await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        baseline = self.pipeline.store.get("processed_latest:" + workspace, self.source_url)
        ScriptedAdapters.detection_decision = "uncertain"
        original = self.pipeline.store.mark_comparison_processed
        pending_at_mark = []

        def mark(*args, **kwargs):
            pending_at_mark.append(bool(self.pipeline.store.items("pending:" + workspace)))
            return original(*args, **kwargs)

        with patch.object(self.pipeline.store, "mark_comparison_processed", side_effect=mark):
            result = await self.pipeline.run(self.product)
        self.assertEqual(result["product"]["status"], "pending")
        self.assertTrue(self.pipeline.store.items("pending:" + workspace))
        self.assertEqual(pending_at_mark, [True])
        self.assertNotEqual(self.pipeline.store.get("processed_latest:" + workspace, self.source_url), baseline)

    async def test_event_persistence_failure_keeps_baseline_and_new_run_recovers(self):
        await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        baseline = self.pipeline.store.get("processed_latest:" + workspace, self.source_url)
        ScriptedAdapters.failures["dedup"] = RuntimeError("controlled dedup failure")
        failed = await self.pipeline.run(self.product)
        self.assertEqual(failed["product"]["error"]["type"], "event_detection_error")
        self.assertEqual(self.pipeline.store.get("processed_latest:" + workspace, self.source_url), baseline)
        self.assertEqual(self.pipeline.store.items("events:" + workspace), {})
        del ScriptedAdapters.failures["dedup"]

        recovered = await ProductPipeline(self.root, adapter_type=ScriptedAdapters).run(self.product)
        self.assertEqual(recovered["product"]["status"], "complete")
        self.assertNotEqual(self.pipeline.store.get("processed_latest:" + workspace, self.source_url), baseline)
        self.assertEqual(len(self.pipeline.store.items("events:" + workspace)), 1)
        self.assertEqual(ScriptedAdapters.detection_statuses, ["comparable", "comparable"])

    async def test_triage_failure_after_event_persistence_keeps_watermark_and_new_run_resumes_action(self):
        await self.pipeline.run(self.product)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        baseline = self.pipeline.store.get("processed_latest:" + workspace, self.source_url)
        ScriptedAdapters.failures["triage"] = RuntimeError("controlled triage failure")
        failed = await self.pipeline.run(self.product)
        processed = self.pipeline.store.get("processed_latest:" + workspace, self.source_url)
        self.assertEqual(failed["product"]["error"]["type"], "triage_error")
        self.assertNotEqual(processed, baseline)
        self.assertEqual(len(self.pipeline.store.items("events:" + workspace)), 1)
        action = list(self.pipeline.store.items("event_actions:" + workspace).values())[0]
        self.assertEqual(action["status"], "pending")
        del ScriptedAdapters.failures["triage"]

        recovered = await ProductPipeline(self.root, adapter_type=ScriptedAdapters).run(self.product)
        self.assertEqual(recovered["product"]["status"], "complete")
        self.assertEqual(len(self.pipeline.store.items("events:" + workspace)), 1)
        self.assertEqual(ScriptedAdapters.calls["dedup"], 1)
        self.assertEqual(ScriptedAdapters.calls["triage"], 2)
        self.assertEqual(ScriptedAdapters.detection_statuses, ["comparable", "unchanged"])
        action = list(self.pipeline.store.items("event_actions:" + workspace).values())[0]
        self.assertEqual(action["status"], "complete")

    async def test_next_change_uses_last_processed_snapshot(self):
        ScriptedAdapters.versions = ["A", "B", "C"]
        await self.pipeline.run(self.product)
        await self.pipeline.run(self.product)
        await self.pipeline.run(self.product)
        histories = list(self.pipeline.store.items("snapshots").values())
        self.assertEqual(histories[-1]["previous_snapshot"]["content"], histories[-2]["current_snapshot"]["content"])

    async def test_triage_failure_preserves_event_and_stops_research(self):
        ScriptedAdapters.failures["triage"] = RuntimeError("private failure")
        result = await self.replay()
        self.assertEqual(result["product"]["error"]["type"], "triage_error")
        self.assertEqual(len(result["product"]["detected_events"]), 1)
        self.assertEqual(result["product"]["research_results"], [])

    async def test_retrieval_failure_has_distinct_category(self):
        ScriptedAdapters.failures["triage"] = EmbeddingAPIError("private failure")
        result = await self.replay()
        self.assertEqual(result["product"]["error"]["type"], "retrieval_error")

    async def test_research_failure_is_independent_and_retry_completes(self):
        main, event_id = await self.recommendation()
        ScriptedAdapters.failures["deep_research"] = RuntimeError("private failure")
        failed = self.pipeline.start_research(self.product, event_id)
        self.assertEqual(failed["product"]["research"]["status"], "failed")
        self.assertEqual(main["product"]["status"], "complete")
        self.assertEqual(len(main["product"]["detected_events"]), 1)
        self.assertEqual(len(main["product"]["triage_results"]), 1)
        self.assertNotIn("private failure", json.dumps(failed["product"]))
        del ScriptedAdapters.failures["deep_research"]
        retried = self.pipeline.start_research(self.product, event_id, retry=True)
        self.assertEqual(retried["product"]["research"]["status"], "completed")
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 2)

    async def test_missing_model_config_is_configuration_error(self):
        ScriptedAdapters.failures["triage"] = LLMConfigurationError("missing configuration")
        self.assertEqual((await self.replay())["product"]["error"]["type"], "configuration_error")

    async def test_finished_run_resume_has_no_side_effect(self):
        first = await self.pipeline.run(self.product)
        counts = ScriptedAdapters.calls.copy()
        reopened = ProductPipeline(self.root, adapter_type=ScriptedAdapters)
        self.assertEqual(await reopened.resume(first["run_id"]), first)
        self.assertEqual(counts, ScriptedAdapters.calls)

    async def test_explicit_research_success_persists_across_reopen(self):
        _, event_id = await self.recommendation()
        first = self.pipeline.start_research(self.product, event_id)
        self.assertEqual(first["product"]["research"]["status"], "completed")
        reopened = ProductPipeline(self.root, adapter_type=ScriptedAdapters)
        second = reopened.start_research(self.product, event_id)
        self.assertEqual(second["product"]["research"]["status"], "completed")
        self.assertTrue(second["audit"]["reused"])
        self.assertEqual(ScriptedAdapters.calls["triage"], 1)
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 1)

    async def test_completed_research_is_idempotent_and_retry_cannot_rerun(self):
        _, event_id = await self.recommendation()
        first = self.pipeline.start_research(self.product, event_id)
        second = self.pipeline.start_research(self.product, event_id)
        retry = self.pipeline.start_research(self.product, event_id, retry=True)
        self.assertEqual(first["product"]["research"], second["product"]["research"])
        self.assertEqual(second["product"]["research"], retry["product"]["research"])
        self.assertEqual(ScriptedAdapters.calls["triage"], 1)
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 1)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        self.assertEqual(len(self.pipeline.store.items("research:" + workspace)), 1)

    async def test_follow_up_requires_completed_initial_research_and_duplicate_request_is_idempotent(self):
        _, event_id = await self.recommendation()
        unavailable = self.pipeline.follow_up_research(
            self.product, event_id, request_id="request-123", question="这件事有什么影响？"
        )
        self.assertFalse(unavailable["accepted"])
        self.pipeline.start_research(self.product, event_id)
        first = self.pipeline.follow_up_research(
            self.product, event_id, request_id="request-123", question="我长期关注企业权限，这件事有什么影响？"
        )
        duplicate = self.pipeline.follow_up_research(
            self.product, event_id, request_id="request-123", question="不得覆盖原问题"
        )
        self.assertEqual(first["turn"]["status"], "completed")
        self.assertTrue(duplicate["reused"])
        self.assertEqual(duplicate["turn"]["user_message"], "我长期关注企业权限，这件事有什么影响？")
        self.assertEqual(ScriptedAdapters.calls["follow_up"], 1)
        self.assertEqual(ScriptedAdapters.calls["extract_preference"], 1)
        self.assertEqual(first["turn"]["candidate_count"], 1)

    async def test_follow_up_failure_keeps_turn_and_retry_uses_conversation_history(self):
        _, event_id = await self.recommendation()
        self.pipeline.start_research(self.product, event_id)
        first = self.pipeline.follow_up_research(
            self.product, event_id, request_id="request-first", question="先分析产品影响。"
        )
        self.assertEqual(first["turn"]["status"], "completed")
        ScriptedAdapters.failures["follow_up"] = RuntimeError("private failure")
        failed = self.pipeline.follow_up_research(
            self.product, event_id, request_id="request-second", question="再看竞争回应。"
        )
        self.assertEqual(failed["turn"]["status"], "failed")
        self.assertNotIn("private failure", json.dumps(failed))
        del ScriptedAdapters.failures["follow_up"]
        retried = self.pipeline.follow_up_research(
            self.product, event_id, retry_turn_id=failed["turn"]["turn_id"]
        )
        self.assertEqual(retried["turn"]["status"], "completed")
        self.assertEqual(ScriptedAdapters.follow_up_inputs[-1]["history"][0]["user"], "先分析产品影响。")
        self.assertEqual(ScriptedAdapters.calls["extract_preference"], 2)

    async def test_preference_extraction_failure_does_not_fail_follow_up(self):
        _, event_id = await self.recommendation()
        self.pipeline.start_research(self.product, event_id)
        ScriptedAdapters.failures["extract_preference"] = RuntimeError("controlled extraction failure")
        output = self.pipeline.follow_up_research(
            self.product, event_id, request_id="request-123", question="我长期关注企业权限。"
        )
        self.assertEqual(output["turn"]["status"], "completed")
        self.assertEqual(output["turn"]["candidate_count"], 0)

    async def test_running_research_is_idempotent(self):
        _, event_id = await self.recommendation()
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        namespace = "research_status:" + workspace
        state = self.pipeline.store.get(namespace, event_id)
        with patch("pipeline.orchestrator._PROCESS_TOKEN", "active-process"):
            self.pipeline.store.put(namespace, event_id,
                                    {**state, "status": "running", "process_token": "active-process"})
            result = self.pipeline.start_research(self.product, event_id)
        self.assertEqual(result["product"]["research"]["status"], "running")
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 0)

    async def test_interrupted_running_becomes_failed_then_requires_retry(self):
        _, event_id = await self.recommendation()
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        namespace = "research_status:" + workspace
        state = self.pipeline.store.get(namespace, event_id)
        self.pipeline.store.put(namespace, event_id, {**state, "status": "running", "process_token": "old-process"})
        interrupted = self.pipeline.start_research(self.product, event_id)
        self.assertEqual(interrupted["product"]["research"]["status"], "failed")
        self.assertEqual(interrupted["product"]["research"]["error"]["type"], "research_interrupted")
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 0)
        completed = self.pipeline.start_research(self.product, event_id, retry=True)
        self.assertEqual(completed["product"]["research"]["status"], "completed")
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 1)

    async def test_same_workspace_research_states_are_event_scoped_and_persisted(self):
        """同一 workspace 的两个 Event 各自保存状态和结果，重开 Store 后仍不互相覆盖。"""
        event_a, event_b = fixture_event(), second_fixture_event()
        output = await self.pipeline.run_events(
            self.product, [event_a, event_b], provenance="two-event-identity-test"
        )
        ids = {item["event_subject"]: item["event_id"] for item in output["product"]["detected_events"]}
        workspace = identity([self.product.workspace_id, self.product.competitor_name])

        completed_a = self.pipeline.start_research(self.product, ids["Export"])
        self.assertEqual(completed_a["product"]["research"]["status"], "completed")
        states = self.pipeline.store.items("research_status:" + workspace)
        self.assertEqual(states[ids["Export"]]["status"], "completed")
        self.assertEqual(states[ids["API"]]["status"], "not_started")
        saved_a = states[ids["Export"]].copy()

        completed_b = self.pipeline.start_research(self.product, ids["API"])
        self.assertEqual(completed_b["product"]["research"]["status"], "completed")
        states = self.pipeline.store.items("research_status:" + workspace)
        self.assertEqual(states[ids["Export"]], saved_a)
        self.assertEqual(states[ids["API"]]["status"], "completed")
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 2)

        reopened = ProductPipeline(self.root, adapter_type=ScriptedAdapters)
        reopened_states = reopened.store.items("research_status:" + workspace)
        self.assertEqual(reopened_states, states)
        self.assertEqual(reopened.start_research(self.product, ids["Export"])["product"]["research"]["status"],
                         "completed")
        self.assertEqual(reopened.start_research(self.product, ids["API"])["product"]["research"]["status"],
                         "completed")
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 2)

    async def test_product_event_id_is_stable_across_reopen_and_new_run(self):
        """产品 Event ID 由业务事实确定，重开 Pipeline 和新 run 都返回同一标识。"""
        event = fixture_event()
        first = await self.pipeline.run_events(self.product, [event], provenance="stable-id-first")
        first_id = first["product"]["detected_events"][0]["event_id"]
        reopened = ProductPipeline(self.root, adapter_type=ScriptedAdapters)
        second = await reopened.run_events(self.product, [event], provenance="stable-id-second")
        second_id = second["product"]["detected_events"][0]["event_id"]

        self.assertEqual(first_id, product_event_id(event))
        self.assertEqual(first_id, second_id)
        self.assertTrue(first_id.startswith("evt_"))
        self.assertNotEqual(first_id, event.candidate_id)
        self.assertNotIn(event.candidate_id, json.dumps(second["product"]))
        self.assertEqual(reopened.start_research(self.product, first_id)["product"]["research"]["event_id"], first_id)

    async def test_concurrent_not_started_claim_calls_research_once(self):
        """两个同时启动请求只能有一个占用 not_started，第二个不会增加 Agent 调用。"""
        _, event_id = await self.recommendation()
        entered, release = threading.Event(), threading.Event()
        original = ScriptedAdapters.research

        def blocked(adapter, event, mode, triage_result, official_hosts):
            if mode == "deep_research":
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("并发测试未释放 Research Agent。")
            return original(adapter, event, mode, triage_result, official_hosts)

        with patch.object(ScriptedAdapters, "research", new=blocked), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.pipeline.start_research, self.product, event_id)
            self.assertTrue(entered.wait(5))
            second = ProductPipeline(self.root, adapter_type=ScriptedAdapters).start_research(self.product, event_id)
            self.assertEqual(second["product"]["research"]["status"], "running")
            self.assertEqual(ScriptedAdapters.calls["deep_research"], 0)
            release.set()
            self.assertEqual(first.result(timeout=5)["product"]["research"]["status"], "completed")
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 1)

    async def test_concurrent_failed_retry_claim_calls_research_once(self):
        """failed 的两个同时 retry 请求也只能产生一次新的 Research Agent 调用。"""
        _, event_id = await self.recommendation()
        ScriptedAdapters.failures["deep_research"] = RuntimeError("controlled failure")
        self.assertEqual(self.pipeline.start_research(self.product, event_id)["product"]["research"]["status"],
                         "failed")
        del ScriptedAdapters.failures["deep_research"]
        entered, release = threading.Event(), threading.Event()
        original = ScriptedAdapters.research

        def blocked(adapter, event, mode, triage_result, official_hosts):
            if mode == "deep_research":
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("并发 retry 测试未释放 Research Agent。")
            return original(adapter, event, mode, triage_result, official_hosts)

        with patch.object(ScriptedAdapters, "research", new=blocked), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.pipeline.start_research, self.product, event_id, retry=True)
            self.assertTrue(entered.wait(5))
            second = ProductPipeline(self.root, adapter_type=ScriptedAdapters).start_research(
                self.product, event_id, retry=True
            )
            self.assertEqual(second["product"]["research"]["status"], "running")
            self.assertEqual(ScriptedAdapters.calls["deep_research"], 1)
            release.set()
            self.assertEqual(first.result(timeout=5)["product"]["research"]["status"], "completed")
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 2)

    async def test_different_events_can_start_while_one_is_running(self):
        """占用粒度是 workspace + event；Event A running 不会锁住 Event B。"""
        event_a, event_b = fixture_event(), second_fixture_event()
        output = await self.pipeline.run_events(
            self.product, [event_a, event_b], provenance="independent-event-claim-test"
        )
        ids = {item["event_subject"]: item["event_id"] for item in output["product"]["detected_events"]}
        entered, release = threading.Event(), threading.Event()
        original = ScriptedAdapters.research

        def block_only_a(adapter, event, mode, triage_result, official_hosts):
            if mode == "deep_research" and product_event_id(event) == ids["Export"]:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("不同 Event 测试未释放 Research Agent。")
            return original(adapter, event, mode, triage_result, official_hosts)

        with patch.object(ScriptedAdapters, "research", new=block_only_a), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.pipeline.start_research, self.product, ids["Export"])
            self.assertTrue(entered.wait(5))
            second = self.pipeline.start_research(self.product, ids["API"])
            self.assertEqual(second["product"]["research"]["status"], "completed")
            release.set()
            self.assertEqual(first.result(timeout=5)["product"]["research"]["status"], "completed")
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 2)

    async def test_knowledge_and_memory_have_distinct_stores(self):
        await self.replay()
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        refs = self.pipeline.store.get("workspaces", workspace)["stores"]
        self.assertNotEqual(refs["memory"], refs["own_product_knowledge"])
        self.assertNotEqual(refs["memory"], str(self.pipeline.store.path))

    async def test_focus_does_not_implicitly_write_memory(self):
        self.product = self.product.model_copy(update={"current_focus": "长期关注导出能力"})
        await self.replay()
        self.assertEqual(ScriptedAdapters.calls["write_memory"], 0)
        self.pipeline.remember_explicit(self.product, "长期关注导出能力")
        self.assertEqual(ScriptedAdapters.calls["write_memory"], 1)

    def test_module_adapter_passes_each_current_focus_to_triage(self):
        """配置更新后新建的真实适配器必须把当次 Focus 原样交给 TriageWorkflow。"""
        result = TriageResult(
            event_id=fixture_event().candidate_id, decision="monitor", reason="继续观察。",
            focus_relevance="相关。",
            own_product_relevance={"status": "insufficient_own_product_context", "claims": []},
            evidence_refs=["event:evidence_001"], uncertainty=[], missing_context=[],
        )
        received = []

        with patch("pipeline.adapters.TriageWorkflow") as workflow:
            workflow.return_value.run.side_effect = lambda _event, **kwargs: (
                received.append(kwargs["current_focus"]) or {"result": result}
            )
            for focus in ("Focus A", "Focus B"):
                adapter = ModuleAdapters(
                    self.root, self.product.model_copy(update={"current_focus": focus})
                )
                adapter.readers = lambda: (object(), object())
                adapter.triage(fixture_event())

        self.assertEqual(received, ["Focus A", "Focus B"])
    async def test_identity_ambiguity_kept_separate_from_uncertain_facts(self):
        ScriptedAdapters.dedup_ambiguous = True
        result = await self.replay()
        self.assertEqual(result["product"]["status"], "pending")
        self.assertEqual(ScriptedAdapters.calls["verification"], 0)

    async def test_new_workspace_does_not_reuse_other_baseline(self):
        await self.pipeline.run(self.product)
        other = self.product.model_copy(update={"workspace_id": "other"})
        result = await self.pipeline.run(other)
        self.assertTrue(result["product"]["baseline_created"])

    async def test_scan_recovery_preserves_original_history_pair(self):
        current = snapshot("First")
        store = self.pipeline.store
        first = store.record_scan("w", "r", current["url"], current)
        second = store.record_scan("w", "r", current["url"], snapshot("Changed"))
        self.assertEqual(first, second)
        self.assertTrue(second["is_first_scan"])

    async def test_legacy_failed_detection_recovers_previous_processed_baseline(self):
        store = self.pipeline.store
        url = self.source_url
        previous = {"is_first_scan": True, "current_snapshot": snapshot("A"),
                    "previous_snapshot": None, "snapshot_id": "legacy-a", "previous_snapshot_id": None}
        failed = {"is_first_scan": False, "current_snapshot": snapshot("B", "2026-09-03T10:00:00+08:00"),
                  "previous_snapshot": previous["current_snapshot"], "snapshot_id": "legacy-b",
                  "previous_snapshot_id": "legacy-a"}
        store.put("snapshots", "legacy-a", previous)
        store.put("snapshots", "legacy-b", failed)
        store.put("latest:legacy-workspace", url, "legacy-b")
        store.put("steps:legacy-run", "scan:legacy-b", {"status": "complete", "data": failed})
        store.put("steps:legacy-run", "comparison:legacy-b", {"status": "complete", "data": [{}]})
        store.put("steps:legacy-run", "detection:legacy-b:0", {"status": "failed"})
        recovered = store.record_scan("legacy-workspace", "new-run", url,
                                      snapshot("B", "2026-09-04T10:00:00+08:00"))
        self.assertEqual(recovered["previous_snapshot_id"], "legacy-a")
        self.assertEqual(store.get("processed_latest:legacy-workspace", url), "legacy-a")

    async def test_product_input_has_no_internal_id_fields(self):
        self.assertFalse({"candidate_id", "event_id", "memory_id", "chunk_id"} & set(ProductInput.model_fields))

    async def test_citation_ids_are_scoped_per_artifact(self):
        artifact = {"result": {"evidence_refs": ["knowledge_001"]},
                    "citations": [{"result_id": "knowledge_001", "content": "original"}]}
        left, right = present_artifact(artifact, "left"), present_artifact(artifact, "right")
        self.assertNotEqual(left["citations"][0]["result_id"], right["citations"][0]["result_id"])
        self.assertEqual(left["result"]["evidence_refs"], [left["citations"][0]["result_id"]])

    async def test_product_citations_keep_references_without_internal_artifact_key(self):
        output, event_id = await self.recommendation()
        product = output["product"]
        internal_keys = self.pipeline.store.items("triage:" + identity(
            [self.product.workspace_id, self.product.competitor_name]
        )).keys()
        encoded = json.dumps(product)
        self.assertTrue(product["citations"])
        self.assertTrue(all(ref in {item["result_id"] for item in product["citations"]}
                            for ref in product["triage_results"][0]["result"]["evidence_refs"]))
        self.assertIn(event_id, product["triage_results"][0]["result"]["event_id"])
        self.assertTrue(all(key not in encoded for key in internal_keys))

    async def test_public_urls_survive_local_path_filter(self):
        for url in ("https://example.com/changelog", "http://127.0.0.1:1234/product"):
            self.assertEqual(public_text(url), url)
        for path in ("E:\\internal\\store.sqlite3", "内部文件 E:/private/data.json", "file:///var/data"):
            self.assertEqual(public_text(path), "[内部路径已隐藏]")

    async def test_replay_preserves_official_event_host(self):
        workspace, _ = self.pipeline.configure_competitor(self.product)
        self.assertEqual(self.pipeline._official_hosts(workspace, fixture_event()), ["example.com"])

    async def test_redirect_cannot_inherit_official_identity(self):
        adapter = ModuleAdapters(self.root, self.product)
        current = snapshot("Export supports CSV and PDF.")
        current["final_url"] = "https://other.example/product"
        history = {"is_first_scan": False, "current_snapshot": current,
                   "previous_snapshot": snapshot("Export supports CSV only.", "2026-09-01T10:00:00+08:00")}
        inputs = adapter.detection_inputs(history, {"url": "https://example.com/product", "source_type": "product_page",
                                                   "credibility": "official", "official_domains": ["example.com"]})
        self.assertTrue(all(i.source_credibility == "unverified" for i in inputs))

    async def test_monitoring_does_not_read_legacy_own_product_files(self):
        product = self.product.model_copy(update={"own_product_documents": [str(self.root / "missing.md")]})
        result = await self.pipeline.run(product)
        self.assertEqual(result["product"]["status"], "complete")
        repeated = await self.pipeline.run(product)
        self.assertEqual(repeated["product"]["status"], "complete")
        self.assertEqual(ScriptedAdapters.calls["own"], 0)
        self.assertEqual(result["audit"]["steps"]["configuration"]["status"], "complete")

    def test_competitor_test_reset_is_scoped_and_preserves_configuration(self):
        product_a = ProductInput(
            workspace_id="w", own_product_id="main", competitor_name="Example",
            official_url="https://example.com",
        )
        product_b = ProductInput(
            workspace_id="w", own_product_id="main", competitor_name="Other",
            official_url="https://other.example",
        )
        workspace_a, _ = self.pipeline.configure_competitor(product_a)
        workspace_b, _ = self.pipeline.configure_competitor(product_b)
        self.pipeline.store.put("sources", workspace_a, {"sources": [{"url": "https://example.com"}]})
        self.pipeline.store.put("sources", workspace_b, {"sources": [{"url": "https://other.example"}]})
        ProductConfigurationService(self.pipeline.store).update(
            "w", "main", {"current_focus": "保留当前研究重点"}
        )
        knowledge_path = workspace_knowledge_path(self.root, "w")
        knowledge_path.write_bytes(b"own-product-knowledge-sentinel")

        snapshot_a = identity("snapshot-a")
        run_a = "run-a"
        self.pipeline.store.put("events:" + workspace_a, "event-a", {"marker": "a"})
        self.pipeline.store.put("triage:" + workspace_a, "triage-a", {"marker": "a"})
        self.pipeline.store.put("pending:" + workspace_a, "pending-a", {"status": "pending"})
        self.pipeline.store.put("research_status:" + workspace_a, "evt_same", {"status": "completed"})
        self.pipeline.store.put("research:" + workspace_a, "research-a", {"marker": "a"})
        self.pipeline.store.put("statement_observations:" + workspace_a, "statement-a", {
            "statement_id": "statement-a", "first_seen_snapshot_id": snapshot_a,
            "last_seen_snapshot_id": snapshot_a,
        })
        self.pipeline.store.put("statement_processing:" + workspace_a, "statement-a", {
            "statement_id": "statement-a", "snapshot_id": snapshot_a, "status": "processed",
        })
        self.pipeline.store.put("processed_latest:" + workspace_a, "https://example.com", snapshot_a)
        self.pipeline.store.put("observed_latest:" + workspace_a, "https://example.com", snapshot_a)
        self.pipeline.store.put("comparisons:" + workspace_a, "comparison-a", {
            "current_snapshot_id": snapshot_a, "previous_snapshot_id": None,
        })
        self.pipeline.store.put("snapshots", snapshot_a, {"snapshot_id": snapshot_a})
        self.pipeline.store.put("runs", run_a, {"product": product_a.model_dump(), "mode": "monitor"})
        self.pipeline.store.put("results", run_a, {"product": {"status": "complete"}})
        self.pipeline.store.put("steps:" + run_a, "scan", {"data": {"snapshot_id": snapshot_a}})
        self.pipeline.store.put("ui_competitor_scans:w", "Example", "2026-09-23T00:00:00Z")

        self.pipeline.store.put("events:" + workspace_b, "event-b", {"marker": "b"})
        self.pipeline.store.put("processed_latest:" + workspace_b, "https://other.example", "snapshot-b")

        cleared = self.pipeline.store.reset_competitor_test_data(
            workspace_id="w", product_id="main", competitor_name="Example"
        )

        self.assertEqual(cleared["events_cleared"], 1)
        self.assertEqual(cleared["pending_candidates_cleared"], 1)
        self.assertEqual(cleared["research_runs_cleared"], 1)
        self.assertEqual(cleared["snapshots_cleared"], 1)
        for namespace in (
            "events:", "triage:", "pending:", "research_status:", "research:",
            "statement_observations:", "statement_processing:", "processed_latest:",
            "observed_latest:", "comparisons:",
        ):
            self.assertEqual(self.pipeline.store.items(namespace + workspace_a), {})
        self.assertIsNone(self.pipeline.store.get("runs", run_a))
        self.assertIsNone(self.pipeline.store.get("snapshots", snapshot_a))
        self.assertIsNone(self.pipeline.store.get("ui_competitor_scans:w", "Example"))
        self.assertEqual(self.pipeline.store.items("events:" + workspace_b)["event-b"]["marker"], "b")
        self.assertEqual(
            self.pipeline.store.get("processed_latest:" + workspace_b, "https://other.example"),
            "snapshot-b",
        )
        self.assertIsNotNone(self.pipeline.store.get("workspaces", workspace_a))
        self.assertEqual(self.pipeline.store.get("sources", workspace_a)["sources"][0]["url"],
                         "https://example.com")
        self.assertEqual(
            ProductConfigurationService(self.pipeline.store).get("w", "main").current_focus,
            "保留当前研究重点",
        )
        self.assertEqual(knowledge_path.read_bytes(), b"own-product-knowledge-sentinel")

    async def test_research_lifecycle_can_restart_for_same_event_after_test_reset(self):
        product = self.product.model_copy(update={"own_product_id": "main"})
        first = await self.pipeline.run_events(product, [fixture_event()], provenance="first")
        event_id = first["product"]["detected_events"][0]["event_id"]
        completed = self.pipeline.start_research(product, event_id)["product"]["research"]
        self.assertEqual(completed["status"], "completed")

        cleared = self.pipeline.store.reset_competitor_test_data(
            workspace_id="w", product_id="main", competitor_name="Example"
        )
        second = await self.pipeline.run_events(product, [fixture_event()], provenance="second")
        repeated_event_id = second["product"]["detected_events"][0]["event_id"]
        workspace = identity(["w", "Example"])
        self.assertEqual(repeated_event_id, event_id)
        self.assertEqual(cleared["research_runs_cleared"], 1)
        self.assertEqual(self.pipeline.store.get("research_status:" + workspace, event_id)["status"],
                         "not_started")
        replayed = self.pipeline.start_research(product, event_id)["product"]["research"]
        self.assertEqual(replayed["status"], "completed")
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 2)

    async def test_historical_backfill_replays_after_reset_without_path_a_false_event(self):
        product = self.product.model_copy(update={"own_product_id": "main"})
        statement = statement_candidate(
            "Official launch statement.", initial=True, statement_date="2026-09-20"
        )
        ScriptedAdapters.versions = ["Stable current page.", "Stable current page."]
        ScriptedAdapters.statement_batches = [[statement], [statement]]

        first = await self.pipeline.run(product)
        first_event_id = first["product"]["historical_events"][0]["event_id"]
        self.pipeline.store.reset_competitor_test_data(
            workspace_id="w", product_id="main", competitor_name="Example"
        )
        second = await self.pipeline.run(product)

        self.assertTrue(second["product"]["baseline_created"])
        self.assertEqual(second["product"]["detected_events"], [])
        self.assertEqual(second["product"]["historical_events"][0]["event_id"], first_event_id)
        self.assertEqual(ScriptedAdapters.calls["path_a_detect"], 0)

    async def test_verification_no_event_with_formal_uncertain_stays_pending(self):
        ScriptedAdapters.verification = "no_event"
        ScriptedAdapters.detection_decision = "uncertain"
        output = await self.replay(True)
        self.assertEqual(output["product"]["status"], "pending")
        self.assertEqual(output["product"]["detected_events"], [])
        self.assertEqual(ScriptedAdapters.calls["triage"], 0)

    async def test_verification_without_formal_input_stays_pending(self):
        ScriptedAdapters.verification = "no_event"
        with patch.object(ScriptedAdapters, "verification_inputs", return_value=[]):
            output = await self.replay(True)
        self.assertEqual(output["product"]["status"], "pending")
        self.assertEqual(ScriptedAdapters.calls["detect"], 0)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        self.assertIsNone(self.pipeline.store.get("resolved:" + workspace, fixture_event(True).candidate_id))

    async def test_verification_decision_cannot_override_event_core(self):
        ScriptedAdapters.verification = "no_event"
        ScriptedAdapters.detection_decision = "event"
        output = await self.replay(True)
        self.assertEqual(len(output["product"]["detected_events"]), 1)
        self.assertEqual(ScriptedAdapters.calls["detect"], 1)
        self.assertEqual(ScriptedAdapters.calls["triage"], 1)

    async def test_partial_scan_retains_completed_source_baseline(self):
        value = discovery().model_dump()
        value["sources"].append({**value["sources"][0], "url": "https://example.com/other"})
        self.pipeline.confirm_monitoring_sources(self.product, SourceDiscoveryResult.model_validate(value))
        with patch.object(ScriptedAdapters, "scan", side_effect=[snapshot("Page one"), RuntimeError("failure")]):
            output = await self.pipeline.run(self.product)
        self.assertEqual(output["product"]["scan_status"], "partial")
        self.assertTrue(output["product"]["baseline_created"])
        self.assertEqual(len(self.pipeline.store.items("snapshots")), 1)


class RuntimeBoundarySyncTests(unittest.TestCase):
    """验证 CLI 式同步调用不会改变 Pipeline 的扫描和基线语义。"""

    def test_asyncio_run_pipeline_keeps_sync_scan_behavior(self):
        class SyncCallAdapter(ScriptedAdapters):
            def scan(self, url):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    return snapshot("Stable page.")
                raise RuntimeError("同步扫描错误地运行在 asyncio Event Loop 中。")

            def explicit_statement_candidates(self, _history, _source):
                return []

        with tempfile.TemporaryDirectory() as directory:
            ScriptedAdapters.calls = Counter()
            ScriptedAdapters.failures = {}
            product = ProductInput(
                workspace_id="sync", competitor_name="Example",
                official_url="https://example.com",
            )
            pipeline = ProductPipeline(directory, adapter_type=SyncCallAdapter)
            pipeline.confirm_monitoring_sources(product, discovery())
            result = asyncio.run(pipeline.run(product))

        self.assertEqual(result["product"]["status"], "complete")
        self.assertTrue(result["product"]["baseline_created"])


if __name__ == "__main__":
    unittest.main()
