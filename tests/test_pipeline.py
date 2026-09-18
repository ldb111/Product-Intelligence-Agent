"""集成控制测试：复用正式差异和事件构造器，模型回答用明确的脚本替身。"""

import json
import asyncio
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

from event_core.schemas import DedupResult, EventCandidate
from llm.config import LLMConfigurationError
from llm.retrieval_models import EmbeddingAPIError
from pipeline.adapters import ModuleAdapters
from pipeline.orchestrator import ProductPipeline
from pipeline.output import present_artifact, public_text
from pipeline.schemas import ProductInput
from pipeline.store import PipelineStore, identity
from source_discovery.schemas import SourceDiscoveryResult
from tests.test_event_core import data, detect_with_mock, snapshot
from triage.schemas import TriageResult


def fixture_event(uncertain=False):
    return detect_with_mock(data(old=None) if uncertain else data())[0].candidates[0]


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
        return snapshot(self.versions[index], f"2026-09-{index + 1:02d}T10:00:00+08:00")

    def detect(self, value):
        self.hit("detect")
        return detect_with_mock(value)[0]

    def deduplicate(self, candidate, existing):
        self.hit("dedup")
        if self.dedup_ambiguous:
            return DedupResult(decision="UNCERTAIN", reason="同一性待判断", pending_candidate=candidate)
        return DedupResult(decision="NEW_EVENT", reason="测试脚本的新事件", event=candidate)

    def ingest_own(self):
        self.hit("own")
        return {"results": [], "runtime": []}

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
            content = "We released PDF export in public beta."
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
        ScriptedAdapters.calls = Counter()
        ScriptedAdapters.failures = {}
        ScriptedAdapters.route = "research"
        ScriptedAdapters.verification = "still_pending"
        ScriptedAdapters.versions = ["Export supports CSV only.", "Export supports CSV and PDF."]
        ScriptedAdapters.dedup_ambiguous = False
        self.pipeline = ProductPipeline(self.root, adapter_type=ScriptedAdapters)

    async def replay(self, uncertain=False):
        return await self.pipeline.run_events(self.product, [fixture_event(uncertain)], provenance="controlled-test")

    async def test_first_scan_only_creates_baseline(self):
        result = await self.pipeline.run(self.product)
        self.assertTrue(result["product"]["baseline_created"])
        self.assertEqual(result["product"]["detected_events"], [])
        self.assertEqual(ScriptedAdapters.calls["detect"], 0)
        self.assertEqual(ScriptedAdapters.calls["triage"], 0)

    async def test_second_scan_enters_event_detection(self):
        await self.pipeline.run(self.product)
        result = await self.pipeline.run(self.product)
        self.assertEqual(result["product"]["status"], "complete")
        self.assertFalse(result["product"]["baseline_created"])
        self.assertEqual(len(result["product"]["detected_events"]), 1)
        self.assertEqual(ScriptedAdapters.calls["detect"], 1)

    async def test_unchanged_scan_does_not_call_event_model(self):
        ScriptedAdapters.versions = ["Export supports CSV only."] * 2
        await self.pipeline.run(self.product)
        result = await self.pipeline.run(self.product)
        self.assertFalse(result["product"]["baseline_created"])
        self.assertEqual(ScriptedAdapters.calls["detect"], 0)

    async def test_confirmed_event_routes_through_triage(self):
        output = await self.replay()
        self.assertEqual(len(output["product"]["triage_results"]), 1)
        self.assertEqual(ScriptedAdapters.calls["admit"], 1)

    async def test_research_route_calls_deep_research(self):
        await self.replay()
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 1)
        self.assertEqual(ScriptedAdapters.calls["verification"], 0)

    async def test_monitor_does_not_research(self):
        ScriptedAdapters.route = "monitor"
        result = await self.replay()
        self.assertEqual(result["product"]["research_results"], [])
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 0)

    async def test_no_action_does_not_research(self):
        ScriptedAdapters.route = "no_action"
        await self.replay()
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 0)

    async def test_uncertain_routes_to_verification_and_remains_pending(self):
        result = await self.replay(True)
        self.assertEqual(result["product"]["status"], "pending")
        self.assertEqual(len(result["product"]["pending_candidates"]), 1)
        self.assertEqual(ScriptedAdapters.calls["verification"], 1)
        self.assertEqual(ScriptedAdapters.calls["triage"], 0)
        self.assertEqual(ScriptedAdapters.calls["admit"], 0)

    async def test_verified_event_reenters_formal_detection_and_triage(self):
        ScriptedAdapters.verification = "event"
        result = await self.replay(True)
        self.assertEqual(result["product"]["status"], "complete")
        self.assertEqual(ScriptedAdapters.calls["detect"], 1)
        self.assertEqual(ScriptedAdapters.calls["triage"], 1)
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 1)
        self.assertEqual(result["product"]["pending_candidates"], [])

    async def test_verification_no_event_ends(self):
        ScriptedAdapters.verification = "no_event"
        result = await self.replay(True)
        self.assertEqual(result["product"]["pending_candidates"], [])
        self.assertEqual(result["product"]["detected_events"], [])
        self.assertEqual(ScriptedAdapters.calls["triage"], 0)

    async def test_research_outputs_not_admitted_or_remembered(self):
        await self.replay()
        self.assertEqual(ScriptedAdapters.calls["admit"], 1)
        self.assertEqual(ScriptedAdapters.calls["write_memory"], 0)

    async def test_missing_own_does_not_invent_capability(self):
        result = await self.replay()
        own = result["product"]["triage_results"][0]["result"]["own_product_relevance"]
        self.assertEqual(own, {"status": "insufficient_own_product_context", "claims": []})

    async def test_product_and_audit_separated(self):
        result = await self.replay()
        public = json.dumps(result["product"])
        for text in ("provider", "private-provider", "private-trace", "sqlite", "source_reference", "raw_tool_trace", "E:\\"):
            self.assertNotIn(text, public)
        self.assertIn("private-provider", json.dumps(result["audit"]))

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
        ScriptedAdapters.failures["detect"] = RuntimeError("private failure")
        result = await self.pipeline.run(self.product)
        self.assertEqual(result["product"]["error"]["type"], "event_detection_error")
        self.assertEqual(len(self.pipeline.store.items("snapshots")), 2)
        self.assertEqual(ScriptedAdapters.calls["triage"], 0)

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

    async def test_research_failure_preserves_triage(self):
        ScriptedAdapters.failures["deep_research"] = RuntimeError("private failure")
        result = await self.replay()
        self.assertEqual(result["product"]["error"]["type"], "research_error")
        self.assertEqual(len(result["product"]["triage_results"]), 1)
        self.assertNotIn("private failure", json.dumps(result["product"]))

    async def test_missing_model_config_is_configuration_error(self):
        ScriptedAdapters.failures["triage"] = LLMConfigurationError("missing configuration")
        self.assertEqual((await self.replay())["product"]["error"]["type"], "configuration_error")

    async def test_finished_run_resume_has_no_side_effect(self):
        first = await self.pipeline.run(self.product)
        counts = ScriptedAdapters.calls.copy()
        reopened = ProductPipeline(self.root, adapter_type=ScriptedAdapters)
        self.assertEqual(await reopened.resume(first["run_id"]), first)
        self.assertEqual(counts, ScriptedAdapters.calls)

    async def test_failed_research_resume_does_not_repeat_triage(self):
        ScriptedAdapters.failures["deep_research"] = RuntimeError("fail once")
        first = await self.replay()
        del ScriptedAdapters.failures["deep_research"]
        result = await ProductPipeline(self.root, adapter_type=ScriptedAdapters).resume(first["run_id"])
        self.assertEqual(result["product"]["status"], "complete")
        self.assertEqual(ScriptedAdapters.calls["triage"], 1)
        self.assertEqual(ScriptedAdapters.calls["admit"], 1)

    async def test_repeated_event_reuses_research_artifact(self):
        event = fixture_event()
        for _ in range(2):
            await self.pipeline.run_events(self.product, [event], provenance="same-replay")
        self.assertEqual(ScriptedAdapters.calls["triage"], 1)
        self.assertEqual(ScriptedAdapters.calls["deep_research"], 1)
        workspace = identity([self.product.workspace_id, self.product.competitor_name])
        self.assertEqual(len(self.pipeline.store.items("events:" + workspace)), 1)
        self.assertEqual(len(self.pipeline.store.items("research:" + workspace)), 1)

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

    async def test_product_input_has_no_internal_id_fields(self):
        self.assertFalse({"candidate_id", "event_id", "memory_id", "chunk_id"} & set(ProductInput.model_fields))

    async def test_citation_ids_are_scoped_per_artifact(self):
        artifact = {"result": {"evidence_refs": ["knowledge_001"]},
                    "citations": [{"result_id": "knowledge_001", "content": "original"}]}
        left, right = present_artifact(artifact, "left"), present_artifact(artifact, "right")
        self.assertNotEqual(left["citations"][0]["result_id"], right["citations"][0]["result_id"])
        self.assertEqual(left["result"]["evidence_refs"], [left["citations"][0]["result_id"]])

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

    async def test_configuration_failure_has_persisted_stage(self):
        product = self.product.model_copy(update={"own_product_documents": [str(self.root / "missing.md")]})
        result = await self.pipeline.run(product)
        self.assertEqual(result["product"]["error"]["type"], "configuration_error")
        self.assertEqual(result["product"]["pipeline_stages"][0]["status"], "failed")

    async def test_verification_claim_without_formal_event_stays_pending(self):
        ScriptedAdapters.verification = "event"
        no_event = fixture_event(True).model_dump(mode="json")
        no_event.update(decision="no_event", uncertainty_reason=None)
        result = Mock(model_dump=Mock(return_value={"candidates": [no_event], "runtime": None}))
        with patch.object(ScriptedAdapters, "detect", return_value=result):
            output = await self.replay(True)
        self.assertEqual(output["product"]["status"], "pending")
        self.assertEqual(output["product"]["detected_events"], [])
        self.assertEqual(ScriptedAdapters.calls["triage"], 0)

    async def test_partial_scan_retains_completed_source_baseline(self):
        value = discovery().model_dump()
        value["sources"].append({**value["sources"][0], "url": "https://example.com/other"})
        self.pipeline.confirm_monitoring_sources(self.product, SourceDiscoveryResult.model_validate(value))
        with patch.object(ScriptedAdapters, "scan", side_effect=[snapshot("Page one"), RuntimeError("failure")]):
            output = await self.pipeline.run(self.product)
        self.assertEqual(output["product"]["scan_status"], "partial")
        self.assertTrue(output["product"]["baseline_created"])
        self.assertEqual(len(self.pipeline.store.items("snapshots")), 1)


if __name__ == "__main__":
    unittest.main()
