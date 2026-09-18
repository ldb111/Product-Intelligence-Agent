"""Intelligence 的边界控制测试；假模型用于验证控制流，不冒充语义准确率评测。"""

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from event_core.schemas import EventCandidate, Evidence
from llm.client import RuntimeRecord
from llm.config import LLMConfigurationError
from llm.research import ResearchClient, load_research_config
from research.agent import ResearchAgent, ResearchLimits
from research.evidence import EvidenceError, EvidenceRegistry
from research.schemas import Claim, OwnComparison, ResearchReport, Support, VerificationResult
from research.tools import ResearchTools, TOOL_DEFINITIONS, public_url
from triage.schemas import TriageDecision, TriageResult
from triage.workflow import TriageWorkflow


def event(uncertain=False):
    evidence = Evidence(evidence_id="ev1", source_url="https://example.com/news", source_title="官方变化",
                        source_type="changelog", source_credibility="official",
                        evidence_status="pending" if uncertain else "confirmed",
                        evidence_capability=["explicit_statement"], observed_at=datetime.now(timezone.utc),
                        relevant_excerpt="免费档改为每月 2000 积分。", evidence_context={})
    common = dict(candidate_id="candidate1", competitor="测试竞品", detection_path="explicit_statement", evidence=[evidence])
    if uncertain:
        return EventCandidate(**common, decision="uncertain", decision_reason="需要核实", uncertainty_reason="免费积分是否改变？")
    return EventCandidate(**common, decision="event", decision_reason="明确变化", event_type="plan_packaging",
                          event_subject="免费档", change_action="调整积分", change_items=[{"item": "积分", "current_state": "每月 2000"}])


def record(role="reasoning"):
    return RuntimeRecord(role=role, provider="test", model="fixture", response_model="fixture", latency_seconds=0, token_usage=None)


def knowledge_item(domain="own_product"):
    return SimpleNamespace(knowledge_domain=domain, product_id="p", competitor_id="c", chunk_id=domain,
                           source_url=None, source_reference="fixture://document", title="测试文档",
                           content="自身免费档每月 500 积分。" if domain == "own_product" else "竞品免费档每月 2000 积分。",
                           source_credibility="user_provided" if domain == "own_product" else "official",
                           evidence_status="confirmed", observed_at=None)


def dependencies(own=True):
    knowledge = Mock(last_runtime=[])
    knowledge.retrieve.side_effect = lambda query, **kw: SimpleNamespace(results=(
        [knowledge_item(kw["knowledge_domain"])] if own or kw["knowledge_domain"] == "competitor" else []))
    memory = Mock(last_runtime=[])
    memory.retrieve_memory.return_value = SimpleNamespace(results=[SimpleNamespace(
        scope="workspace", scope_id="w", status="confirmed", is_active=True, memory_id="m1",
        memory_type="focus", content="长期关注免费计划。", source_reference="fixture://message")])
    search = Mock()
    search.search.return_value = {"results": [{"url": "https://example.com/news", "title": "官方更新", "snippet": "积分变化"}]}
    page = Mock(return_value={"final_url": "https://example.com/news", "title": "官方更新", "quality_status": "PASS",
                              "main_content": "官方确认免费档改为每月 2000 积分。"})
    return knowledge, memory, search, page


def tools(own=True):
    k, m, s, p = dependencies(own)
    return ResearchTools(knowledge=k, memory=m, search=s, product_id="p", competitor_id="c", memory_scope="workspace",
                         memory_scope_id="w", official_hosts=frozenset({"example.com"}), page_reader=p)


def triage_decision(decision="research"):
    return TriageDecision(decision=decision, reason="与关注有关", focus_relevance="长期关注计划变化",
                          own_product_relevance=OwnComparison(status="insufficient_own_product_context"),
                          evidence_refs=["event_evidence_001"], memory_context_refs=["memory_001"],
                          uncertainty=[], missing_context=["insufficient_own_product_context"])


def route():
    return TriageResult(**triage_decision().model_dump(), event_id="candidate1")


def pending():
    return VerificationResult(decision="still_pending", reason="需要更多直接证据", new_evidence_refs=[],
                              support=[], unresolved_questions=["变化是否成立？"])


def report():
    return ResearchReport(event_summary=[Claim(text="积分变化", kind="fact", support=[Support(
        result_id="event_evidence_001", excerpt="每月 2000 积分")])], background=[], competitor_context=[],
        own_product_comparison=OwnComparison(status="insufficient_own_product_context"), implications=[], watch_next=[],
        uncertainties=["自身资料不足"], evidence_refs=["event_evidence_001"])


def call(name, **args):
    return {"id": "call1", "name": name, "arguments": json.dumps(args)}


class FakeClient:
    def __init__(self, batches, output):
        self.batches, self.output = iter(batches), output
        self.runtime = []
        self.turns = self.finalizations = 0

    def start(self, prompt, payload):
        self.payload = payload

    def choose_tools(self, definitions):
        self.turns += 1
        self.runtime.append(record("research"))
        return next(self.batches, [])

    def accept_result(self, call_id, result):
        pass

    def finalize(self, prompt, payload, schema):
        self.finalizations += 1
        self.final_payload = payload
        self.runtime.append(record("research"))
        return self.output


class TriageTests(unittest.TestCase):
    def run_route(self, decision="research", own=True):
        k, m, _, _ = dependencies(own)
        client = Mock()
        client.decide.return_value = (triage_decision(decision), record())
        workflow = TriageWorkflow(knowledge=k, memory=m, product_id="p", competitor_id="c",
                                  memory_scope="workspace", memory_scope_id="w", client=client)
        return workflow.run(event(), current_focus="免费计划"), client, k, m

    def test_high_relevance_routes_research(self):
        self.assertEqual(self.run_route()[0]["result"].decision, "research")

    def test_medium_relevance_routes_monitor(self):
        self.assertEqual(self.run_route("monitor")[0]["result"].decision, "monitor")

    def test_low_relevance_routes_no_action(self):
        self.assertEqual(self.run_route("no_action")[0]["result"].decision, "no_action")

    def test_missing_own_context_is_explicit(self):
        output, _, _, _ = self.run_route(own=False)
        self.assertEqual(output["result"].own_product_relevance.claims, [])
        self.assertIn("insufficient_own_product_context", output["result"].missing_context)

    def test_contexts_are_separate_and_scoped(self):
        output, _, k, m = self.run_route()
        self.assertEqual(output["context"]["memory_context"]["purpose"], "priority_context_only")
        self.assertEqual(k.retrieve.call_args_list[0].kwargs["filters"], {"product_id": "p"})
        self.assertEqual(k.retrieve.call_args_list[1].kwargs["filters"], {"competitor_id": "c"})
        self.assertEqual(m.retrieve_memory.call_args.kwargs["scope_id"], "w")
        self.assertTrue(all(i["source"] == "event" for i in output["context"]["event"]["evidence"]))

    def test_uses_reasoning_role(self):
        k, m, _, _ = dependencies()
        with patch("triage.workflow.get_llm") as factory:
            TriageWorkflow(knowledge=k, memory=m, product_id="p", competitor_id="c", memory_scope="workspace", memory_scope_id="w")
        factory.assert_called_once_with(role="reasoning")

    def test_triage_never_calls_web(self):
        with patch.object(ResearchTools, "execute", side_effect=AssertionError("no web")):
            output, client, _, _ = self.run_route()
        self.assertEqual(client.decide.call_count, 1)
        self.assertEqual(output["audit"]["web_tool_calls"], 0)

    def test_event_is_unchanged(self):
        e = event()
        before = e.model_dump()
        k, m, _, _ = dependencies()
        client = Mock(decide=Mock(return_value=(triage_decision(), record())))
        TriageWorkflow(knowledge=k, memory=m, product_id="p", competitor_id="c", memory_scope="workspace",
                       memory_scope_id="w", client=client).run(e)
        self.assertEqual(e.model_dump(), before)

    def test_uncertain_rejected_before_retrieval(self):
        k, m, _, _ = dependencies()
        with self.assertRaises(ValueError):
            TriageWorkflow(knowledge=k, memory=m, product_id="p", competitor_id="c", memory_scope="workspace",
                           memory_scope_id="w", client=Mock()).run(event(True))
        k.retrieve.assert_not_called()

    def test_memory_cannot_be_triage_fact(self):
        k, m, _, _ = dependencies()
        decision = triage_decision().model_copy(update={"evidence_refs": ["memory_001"]})
        with self.assertRaises(EvidenceError):
            TriageWorkflow(knowledge=k, memory=m, product_id="p", competitor_id="c", memory_scope="workspace",
                           memory_scope_id="w", client=Mock(decide=Mock(return_value=(decision, record())))).run(event())


class ToolTests(unittest.TestCase):
    def test_competitor_rag(self):
        t = tools()
        self.assertEqual(t.execute("retrieve_competitor_knowledge", {"query": "积分"})["results"][0]["domain"], "competitor")

    def test_own_rag(self):
        self.assertEqual(tools().execute("retrieve_own_product_knowledge", {"query": "积分"})["results"][0]["domain"], "own_product")

    def test_memory_is_read_only_context(self):
        t = tools()
        self.assertEqual(t.execute("retrieve_memory", {"query": "关注"})["context_type"], "memory")
        self.assertEqual([c[0] for c in t.memory.method_calls], ["retrieve_memory"])

    def test_search_is_not_evidence(self):
        t = tools()
        result = t.execute("web_search", {"query": "积分"})
        with self.assertRaises(EvidenceError):
            t.registry.get(result["candidates"][0]["candidate_id"], factual=True)

    def test_read_materializes_real_source(self):
        t = tools()
        t.seed_event(event())
        t.execute("read_page", {"candidate_id": "candidate_001"})
        self.assertEqual(t.registry.get("research_evidence_001").url, "https://example.com/news")
        t.page_reader.assert_called_once_with("https://example.com/news")

    def test_url_cannot_be_page_argument(self):
        t = tools()
        with self.assertRaises(ValueError):
            t.execute("read_page", {"url": "https://invented.example/new"})
        t.page_reader.assert_not_called()

    def test_unknown_candidate_cannot_be_read(self):
        t = tools()
        with self.assertRaises(EvidenceError):
            t.execute("read_page", {"candidate_id": "fake"})
        t.page_reader.assert_not_called()

    def test_memory_scope_leak_rejected(self):
        t = tools()
        t.memory.retrieve_memory.return_value.results[0].scope_id = "other"
        with self.assertRaises(EvidenceError):
            t.execute("retrieve_memory", {"query": "关注"})

    def test_unconfirmed_memory_rejected(self):
        t = tools()
        t.memory.retrieve_memory.return_value.results[0].status = "pending"
        with self.assertRaises(EvidenceError):
            t.execute("retrieve_memory", {"query": "关注"})

    def test_inactive_memory_rejected(self):
        t = tools()
        t.memory.retrieve_memory.return_value.results[0].is_active = False
        with self.assertRaises(EvidenceError):
            t.execute("retrieve_memory", {"query": "关注"})

    def test_model_cannot_override_product_scope(self):
        t = tools()
        with self.assertRaises(ValueError):
            t.execute("retrieve_own_product_knowledge", {"query": "积分", "product_id": "other"})
        t.knowledge.retrieve.assert_not_called()

    def test_redirect_does_not_inherit_official_identity(self):
        t = tools()
        t.seed_event(event())
        t.page_reader.return_value["final_url"] = "https://thirdparty.example/news"
        t.execute("read_page", {"candidate_id": "candidate_001"})
        self.assertEqual(t.registry.get("research_evidence_001").credibility, "unverified")

    def test_knowledge_has_no_write_calls(self):
        t = tools()
        t.execute("retrieve_own_product_knowledge", {"query": "积分"})
        self.assertEqual([c[0] for c in t.knowledge.method_calls], ["retrieve"])

    def test_cross_domain_knowledge_rejected(self):
        t = tools()
        t.knowledge.retrieve.side_effect = None
        t.knowledge.retrieve.return_value = SimpleNamespace(results=[knowledge_item("competitor")])
        with self.assertRaises(EvidenceError):
            t.execute("retrieve_own_product_knowledge", {"query": "积分"})

    def test_tool_metadata_has_no_implementation_details(self):
        text = json.dumps(TOOL_DEFINITIONS).lower()
        for forbidden in ("api_key", "provider", "sdk", "endpoint", "deepseek", "tencent", "base_url"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(len(TOOL_DEFINITIONS), 5)

    def test_private_or_credential_urls_rejected(self):
        for url in ("http://127.0.0.1/a", "http://localhost/a", "file:///tmp/x", "https://user:pass@example.com"):
            self.assertFalse(public_url(url))


class CitationTests(unittest.TestCase):
    def setUp(self):
        self.tools = tools()
        self.tools.seed_event(event())
        self.registry = self.tools.registry

    def test_real_id_materializes(self):
        self.assertEqual(self.registry.validate_report(report())[0].source_id, "ev1")

    def test_fabricated_id_fails(self):
        r = report().model_copy(update={"evidence_refs": ["fake"]})
        with self.assertRaises(EvidenceError):
            self.registry.validate_report(r)

    def test_generated_url_fails(self):
        r = report().model_copy(update={"uncertainties": ["https://fake.example/evidence"]})
        with self.assertRaises(EvidenceError):
            self.registry.validate_report(r)

    def test_memory_is_never_factual_support(self):
        self.tools.execute("retrieve_memory", {"query": "关注"})
        with self.assertRaises(EvidenceError):
            self.registry.check_support([Support(result_id="memory_001", excerpt="长期关注")])

    def test_fabricated_excerpt_fails(self):
        with self.assertRaises(EvidenceError):
            self.registry.check_support([Support(result_id="event_evidence_001", excerpt="永久免费无限使用")])

    def test_thirdparty_page_cannot_override_official_fact(self):
        self.tools.official_hosts = frozenset()
        self.tools.execute("read_page", {"candidate_id": "candidate_001"})
        r = report().model_copy(update={"event_summary": [Claim(text="引用第三方覆盖原始事实", kind="fact",
            support=[Support(result_id="research_evidence_001", excerpt="官方确认")])],
            "evidence_refs": ["research_evidence_001"]})
        with self.assertRaises(EvidenceError):
            self.registry.validate_report(r)

    def test_verification_cannot_relabel_old_evidence_as_new(self):
        r = VerificationResult(decision="event", reason="变化", new_evidence_refs=["event_evidence_001"],
                               support=[Support(result_id="event_evidence_001", excerpt="每月 2000 积分")],
                               unresolved_questions=[])
        with self.assertRaises(EvidenceError):
            self.registry.validate_verification(r, {"event_evidence_001"})

    def test_comparison_without_own_evidence_fails(self):
        r = report()
        r = r.model_copy(update={"own_product_comparison": OwnComparison(status="supported", claims=r.event_summary)})
        with self.assertRaises(EvidenceError):
            self.registry.validate_report(r)

    def test_comparison_with_both_domains_passes(self):
        self.tools.execute("retrieve_own_product_knowledge", {"query": "积分"})
        claim = Claim(text="测试自身积分较低", kind="fact", support=[
            Support(result_id="knowledge_001", excerpt="每月 500 积分"),
            Support(result_id="event_evidence_001", excerpt="每月 2000 积分")])
        r = report().model_copy(update={"own_product_comparison": OwnComparison(status="supported", claims=[claim]),
                                      "evidence_refs": ["event_evidence_001", "knowledge_001"]})
        self.assertEqual(len(self.registry.validate_report(r)), 2)

    def test_suggestion_cannot_be_fact(self):
        r = report().model_copy(update={"implications": report().event_summary})
        with self.assertRaises(EvidenceError):
            self.registry.validate_report(r)

    def test_source_ids_stable_on_repeat(self):
        first = self.tools.execute("retrieve_own_product_knowledge", {"query": "积分"})
        second = self.tools.execute("retrieve_own_product_knowledge", {"query": "计划"})
        self.assertEqual(first, second)

    def test_same_original_source_has_same_group(self):
        self.tools.execute("read_page", {"candidate_id": "candidate_001"})
        self.assertEqual(self.registry.get("event_evidence_001").source_group,
                         self.registry.get("research_evidence_001").source_group)

    def test_missing_own_cannot_contain_claim(self):
        with self.assertRaises(ValueError):
            OwnComparison(status="insufficient_own_product_context", claims=report().event_summary)


class AgentTests(unittest.TestCase):
    def verification(self, batches, output=None, limits=None, t=None):
        client = FakeClient(batches, output or pending())
        agent = ResearchAgent(t or tools(), client=client, limits=limits)
        return agent.run(event(True), mode="verification"), agent, client

    def confirmed_result(self, decision):
        return VerificationResult(decision=decision, reason="官方原文直接说明候选变化", new_evidence_refs=["research_evidence_001"],
                                  support=[Support(result_id="research_evidence_001", excerpt="官方确认")], unresolved_questions=[])

    def test_new_evidence_confirms_candidate(self):
        output, _, _ = self.verification([[call("read_page", candidate_id="candidate_001")]], self.confirmed_result("event"))
        self.assertEqual(output["result"]["decision"], "event")

    def test_direct_refutation_can_be_no_event(self):
        t = tools()
        t.page_reader.return_value["main_content"] = "官方确认该传闻不实，免费档积分没有变化。"
        output, _, _ = self.verification([[call("read_page", candidate_id="candidate_001")]], self.confirmed_result("no_event"), t=t)
        self.assertEqual(output["result"]["decision"], "no_event")

    def test_notfound_cannot_be_no_event(self):
        t = tools()
        t.search.search.return_value = {"results": []}
        with self.assertRaises(EvidenceError):
            self.verification([[call("web_search", query="变化")]], pending().model_copy(update={"decision": "no_event"}), t=t)

    def test_insufficient_evidence_pending(self):
        self.assertEqual(self.verification([])[0]["result"]["decision"], "still_pending")

    def test_third_party_cannot_confirm(self):
        t = tools()
        t.official_hosts = frozenset()
        with self.assertRaises(EvidenceError):
            self.verification([[call("read_page", candidate_id="candidate_001")]], self.confirmed_result("event"), t=t)

    def test_tool_limit(self):
        output, _, client = self.verification([[call("retrieve_memory", query="关注")]] * 10,
                                              limits=ResearchLimits(max_tool_calls=2))
        self.assertEqual(output["audit"]["tool_calls"]["tool"], 2)
        self.assertEqual(client.turns, 2)

    def test_batch_respects_hard_stop(self):
        output, _, client = self.verification([[call("retrieve_memory", query="关注")] * 8],
                                              limits=ResearchLimits(max_tool_calls=2))
        self.assertEqual(output["audit"]["tool_calls"]["tool"], 2)
        self.assertEqual(client.turns, 1)

    def test_invalid_arguments_close_search(self):
        malformed = {"id": "bad", "name": "web_search", "arguments": "{"}
        output, _, client = self.verification([[malformed]] * 5)
        self.assertEqual(output["audit"]["stop_reason"], "tool_error")
        self.assertEqual(client.turns, 1)

    def test_search_limit(self):
        output, _, client = self.verification([[call("web_search", query="变化")]] * 10,
                                              limits=ResearchLimits(max_search_calls=1))
        self.assertEqual(output["audit"]["stop_reason"], "search_limit")
        self.assertEqual(output["audit"]["tool_calls"]["search"], 1)
        self.assertEqual(client.finalizations, 1)

    def test_read_limit(self):
        output, _, _ = self.verification([[call("read_page", candidate_id="candidate_001")]] * 5,
                                         limits=ResearchLimits(max_page_reads=1))
        self.assertEqual(output["audit"]["tool_calls"]["read"], 1)
        self.assertEqual(output["audit"]["stop_reason"], "read_limit")

    def test_rejected_call_closes_search_without_retry(self):
        output, _, client = self.verification([[call("delete_memory", query="x")]] * 5)
        self.assertEqual(client.turns, 1)
        self.assertEqual(client.finalizations, 1)
        self.assertEqual(output["audit"]["stop_reason"], "tool_error")

    def test_max_turns_no_graph_recursion(self):
        output, _, client = self.verification([[call("retrieve_memory", query="关注")]] * 5,
                                              limits=ResearchLimits(max_model_turns=2))
        self.assertEqual(client.turns, 2)
        self.assertEqual(output["audit"]["stop_reason"], "model_turn_limit")

    def test_finalization_no_more_tools(self):
        output, _, client = self.verification([[], [call("web_search", query="should not run")]])
        self.assertEqual(client.turns, 1)
        self.assertEqual(output["audit"]["tool_calls"]["tool"], 0)
        self.assertEqual(output["audit"]["states"], ["SEARCH_OPEN", "SEARCH_CLOSED", "FINALIZATION", "COMPLETE"])
        self.assertTrue(all(i["source"] != "search" for i in client.final_payload["evidence"]))
        self.assertTrue(all(i["source"] != "search" for i in client.final_payload["event"]["evidence"]))

    def test_error_does_not_leak_secret(self):
        t = tools()
        t.search.search.side_effect = RuntimeError("private-credential-value")
        output, _, _ = self.verification([[call("web_search", query="x")]], t=t)
        self.assertNotIn("private-credential-value", json.dumps(output))

    def test_deep_research(self):
        agent = ResearchAgent(tools(), client=FakeClient([[call("retrieve_competitor_knowledge", query="积分")]], report()))
        self.assertEqual(agent.run(event(), mode="deep_research", triage_result=route())["mode"], "deep_research")

    def test_deep_requires_matching_research_triage(self):
        with self.assertRaises(ValueError):
            ResearchAgent(tools(), client=FakeClient([], report())).run(event(), mode="deep_research")

    def test_agent_cannot_reuse_previous_evidence(self):
        _, agent, _ = self.verification([])
        with self.assertRaises(ValueError):
            agent.run(event(True), mode="verification")


class ResearchRoleTests(unittest.TestCase):
    def config(self):
        return {"LLM_RESEARCH_PROVIDER": "test", "LLM_RESEARCH_MODEL": "research-model",
                "LLM_RESEARCH_BASE_URL": "https://example.com/v1", "TEST_API_KEY": "dummy-fixture"}

    def test_independent_role_configuration(self):
        c = load_research_config(self.config())
        self.assertEqual((c.role, c.model), ("research", "research-model"))
        self.assertNotIn("dummy-fixture", repr(c))

    def test_missing_configuration_fails(self):
        for key in self.config():
            config = self.config()
            del config[key]
            with self.assertRaises(LLMConfigurationError):
                load_research_config(config)

    def test_no_fallback_to_fast_or_reasoning(self):
        with self.assertRaises(LLMConfigurationError):
            load_research_config({"LLM_MODEL": "fast", "LLM_REASONING_MODEL": "reasoning", "LLM_API_KEY": "dummy"})

    def test_invalid_service_address_rejected(self):
        with self.assertRaises(LLMConfigurationError):
            load_research_config({**self.config(), "LLM_RESEARCH_BASE_URL": "https://name:pass@example.com"})

    def test_roundtrip_preserves_transport_only_field(self):
        with patch("llm.research.OpenAI"):
            client = ResearchClient(load_research_config(self.config()))
        message = Mock(tool_calls=[])
        message.model_dump.return_value = {"role": "assistant", "content": "", "reasoning_content": "opaque-transport"}
        client._request = Mock(return_value=message)
        client.start("rules", {})
        client.choose_tools(TOOL_DEFINITIONS)
        self.assertEqual(client._messages[-1]["reasoning_content"], "opaque-transport")
        self.assertEqual(client.runtime, [])

    def test_finalization_request_has_no_tools(self):
        with patch("llm.research.OpenAI"):
            client = ResearchClient(load_research_config(self.config()))
        client._request = Mock(return_value=SimpleNamespace(content=pending().model_dump_json(), tool_calls=[]))
        client.finalize("closed", {}, VerificationResult)
        self.assertNotIn("tools", client._request.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
