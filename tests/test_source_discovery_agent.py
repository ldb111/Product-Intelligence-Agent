"""Source Discovery Agent V0.1 的配置、限制和证据校验测试。"""

from __future__ import annotations

import inspect
import os
import unittest
from contextlib import contextmanager
from typing import Any
from unittest.mock import Mock, patch

from langchain.mcp import MCPAdapter
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, ValidationError

from source_discovery.agent import (
    MAX_CANDIDATE_URLS,
    AgentConfigurationError,
    DiscoveryProgramState,
    EvidenceValidationError,
    SelectedMonitoringSource,
    SourceDiscoveryDecision,
    StructuredOutputRepairLimitError,
    _StructuredOutputRepairLimiter,
    _build_finalization_evidence_summary,
    _build_user_message,
    _tool_error_for_model,
    build_model_visible_tools,
    build_chat_model,
    load_model_config,
    load_source_discovery_tools,
    materialize_source_discovery_result,
    run_source_discovery,
    validate_result_evidence,
)
from source_discovery.mcp_server import mcp
from source_discovery.prompts import SOURCE_DISCOVERY_SYSTEM_PROMPT
from source_discovery.schemas import (
    MonitoringSource,
    SourceDiscoveryResult,
    SourceType,
)
from source_discovery.stage1_adapter import Stage1AdapterError


class _ScriptedChatModel(BaseChatModel):
    """按测试脚本依次返回 Tool Call，不会产生任何真实模型费用。"""

    responses: list[AIMessage]
    response_index: int = 0
    bound_tool_names: list[str] = Field(default_factory=list)
    bound_tool_name_history: list[list[str]] = Field(default_factory=list)
    message_history: list[list[Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "source-discovery-scripted-test-model"

    def bind_tools(self, tools, **kwargs):
        self.bound_tool_names = [getattr(item, "name", str(item)) for item in tools]
        self.bound_tool_name_history.append(self.bound_tool_names)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.message_history.append(list(messages))
        message = self.responses[self.response_index]
        self.response_index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


def _tool_call(name: str, call_id: str, **arguments: Any) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": arguments, "id": call_id}],
    )


def _final_response(
    *,
    sources: list[dict[str, Any]] | None = None,
    complete: bool = True,
) -> AIMessage:
    return _tool_call(
        "SourceDiscoveryDecision",
        "final-result",
        competitor_name="Example",
        official_domains=["example.com"] if sources else [],
        selected_sources=sources or [],
        discovery_complete=complete,
        remaining_gaps=[] if complete else ["尚未确认产品更新入口"],
    )


def _page_result(requested_url: str, final_url: str | None = None) -> dict[str, Any]:
    return {
        "requested_url": requested_url,
        "final_url": final_url or requested_url,
        "title": "Example Product",
        "main_content": "Complete product information for monitoring.",
        "headings": ["Product"],
        "links": [],
        "acquisition_method": "static",
        "quality_status": "PASS",
    }


TEST_MODEL_ENV = {
    "LLM_PROVIDER": "aliyun_bailian",
    "LLM_API_KEY": "test-key",
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "test-model-from-environment",
}


@contextmanager
def _use_test_model(model: BaseChatModel):
    """让正式入口仍走环境配置，同时用本地脚本模型阻止真实模型请求。"""
    with (
        patch.dict(os.environ, TEST_MODEL_ENV, clear=True),
        patch("source_discovery.agent.load_dotenv"),
        patch("source_discovery.agent.build_chat_model", return_value=model),
    ):
        yield


class SourceDiscoverySchemaTests(unittest.TestCase):
    def test_source_discovery_result_schema_accepts_expected_fields(self) -> None:
        result = SourceDiscoveryResult(
            competitor_name="Example",
            official_domains=["example.com"],
            sources=[
                MonitoringSource(
                    url="https://example.com/product",
                    title="Example Product",
                    source_type=SourceType.PRODUCT_PAGE,
                    monitoring_reason="用于观察产品能力与定位变化。",
                )
            ],
            discovery_complete=True,
            remaining_gaps=[],
        )

        self.assertEqual(result.sources[0].source_type, SourceType.PRODUCT_PAGE)
        self.assertEqual(
            set(SourceDiscoveryResult.model_fields),
            {
                "competitor_name",
                "official_domains",
                "sources",
                "discovery_complete",
                "remaining_gaps",
            },
        )
        self.assertEqual(
            set(MonitoringSource.model_fields),
            {"url", "title", "source_type", "monitoring_reason"},
        )

    def test_internal_decision_selects_candidate_without_url_or_title(self) -> None:
        decision = SourceDiscoveryDecision(
            competitor_name="Example",
            official_domains=["example.com"],
            selected_sources=[
                SelectedMonitoringSource(
                    candidate_id="candidate_001",
                    source_type=SourceType.PRODUCT_PAGE,
                    monitoring_reason="用于观察产品能力变化。",
                )
            ],
            discovery_complete=True,
            remaining_gaps=[],
        )

        source_fields = SelectedMonitoringSource.model_fields
        self.assertEqual(
            set(source_fields),
            {"candidate_id", "source_type", "monitoring_reason"},
        )
        self.assertNotIn("url", source_fields)
        self.assertNotIn("title", source_fields)
        self.assertEqual(decision.selected_sources[0].candidate_id, "candidate_001")

    def test_internal_decision_rejects_model_supplied_url_or_title(self) -> None:
        with self.assertRaises(ValidationError):
            SelectedMonitoringSource.model_validate(
                {
                    "candidate_id": "candidate_001",
                    "url": "https://model.example/rewritten",
                    "title": "Guessed Title",
                    "source_type": "product_page",
                    "monitoring_reason": "用于观察产品变化。",
                }
            )

    def test_completion_and_remaining_gaps_must_be_consistent(self) -> None:
        with self.assertRaises(ValidationError):
            SourceDiscoveryResult(
                competitor_name="Example",
                official_domains=[],
                sources=[],
                discovery_complete=True,
                remaining_gaps=["仍有缺口"],
            )
        with self.assertRaises(ValidationError):
            SourceDiscoveryResult(
                competitor_name="Example",
                official_domains=[],
                sources=[],
                discovery_complete=False,
                remaining_gaps=[],
            )


class SourceDiscoveryProgramStateTests(unittest.TestCase):
    def test_candidate_urls_stop_at_forty(self) -> None:
        state = DiscoveryProgramState()
        results = [
            {"title": str(index), "url": f"https://example.com/{index}", "snippet": "x"}
            for index in range(MAX_CANDIDATE_URLS + 5)
        ]

        accepted = state.add_search_results(results)

        self.assertEqual(len(accepted), MAX_CANDIDATE_URLS)
        self.assertEqual(len(state.candidate_urls), MAX_CANDIDATE_URLS)

    def test_duplicate_candidate_url_is_not_counted_twice(self) -> None:
        state = DiscoveryProgramState()
        repeated = {"title": "A", "url": "https://example.com/a", "snippet": "x"}

        first = state.add_search_results([repeated])
        second = state.add_search_results([repeated])

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(state.candidate_urls, ["https://example.com/a"])

    def test_user_and_search_candidates_receive_stable_ids(self) -> None:
        state = DiscoveryProgramState()

        official = state.register_user_url("https://example.com/")
        duplicate = state.register_user_url("https://example.com/#overview")
        search_results = state.add_search_results(
            [
                {
                    "title": "Pricing",
                    "url": "https://example.com/pricing",
                    "snippet": "Plans",
                }
            ]
        )

        self.assertEqual(official.candidate_id, "candidate_001")
        self.assertIs(official, duplicate)
        self.assertEqual(search_results[0]["candidate_id"], "candidate_002")
        self.assertEqual(search_results[0]["provenance"], "web_search")
        self.assertEqual(len(state.candidates), 2)

    def test_page_links_are_resolved_filtered_deduplicated_and_registered(self) -> None:
        state = DiscoveryProgramState()
        state.register_user_url("https://example.com/products/current")

        accepted = state.add_page_links(
            [
                {"anchor_text": "Pricing", "href": "/pricing#plans"},
                {"anchor_text": "Pricing duplicate", "href": "https://example.com/pricing"},
                {"anchor_text": "Self", "href": "#details"},
                {"anchor_text": "Email", "href": "mailto:team@example.com"},
                {"anchor_text": "Phone", "href": "tel:123"},
                {"anchor_text": "Script", "href": "javascript:void(0)"},
            ],
            final_url="https://example.com/products/current",
        )

        self.assertEqual(len(accepted), 1)
        pricing = state.candidate_by_id("candidate_002")
        self.assertEqual(pricing.url, "https://example.com/pricing")
        self.assertEqual(pricing.provenance, "page_link")
        self.assertEqual(pricing.anchor_text, "Pricing")
        self.assertEqual(
            pricing.discovered_from,
            "https://example.com/products/current",
        )

    def test_page_link_candidates_keep_global_limit_without_closing_search(self) -> None:
        state = DiscoveryProgramState()
        state.register_user_url("https://example.com/")
        links = [
            {"anchor_text": str(index), "href": f"/page-{index}"}
            for index in range(MAX_CANDIDATE_URLS + 5)
        ]

        state.add_page_links(links, final_url="https://example.com/")

        self.assertEqual(len(state.candidates), MAX_CANDIDATE_URLS)
        self.assertFalse(state.search_closed)
        self.assertFalse(state.finalization_mode)

    def test_search_replaces_low_priority_page_link_when_pool_is_full(self) -> None:
        state = DiscoveryProgramState()
        state.register_user_url("https://example.com/")
        state.add_page_links(
            [
                {"anchor_text": f"Page {index}", "href": f"/page-{index}"}
                for index in range(MAX_CANDIDATE_URLS)
            ],
            final_url="https://example.com/",
        )

        replaced = state.candidate_by_id("candidate_040")
        accepted = state.add_search_results(
            [
                {
                    "title": "Search result",
                    "url": "https://example.com/search-result",
                    "snippet": "Result",
                }
            ]
        )

        self.assertEqual(len(state.candidates), MAX_CANDIDATE_URLS)
        self.assertEqual(len(state.candidate_registry), MAX_CANDIDATE_URLS + 1)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["candidate_id"], "candidate_041")
        self.assertEqual(state.candidate_by_id("candidate_001").provenance, "user_input")
        self.assertIs(state.candidate_by_id("candidate_040"), replaced)
        self.assertFalse(replaced.active)
        self.assertNotIn(replaced, state.candidates)
        self.assertNotIn(replaced.url, state.candidate_urls)
        self.assertTrue(
            any(candidate.url.endswith("/search-result") for candidate in state.candidates)
        )

    def test_registry_keeps_inactive_ids_and_never_reuses_them(self) -> None:
        state = DiscoveryProgramState()
        state.register_user_url("https://example.com/")
        state.add_page_links(
            [
                {"anchor_text": f"Page {index}", "href": f"/page-{index}"}
                for index in range(MAX_CANDIDATE_URLS)
            ],
            final_url="https://example.com/",
        )

        replaced = state.candidate_by_id("candidate_040")
        first_search = state.add_search_results(
            [
                {
                    "title": "First search result",
                    "url": "https://search.example.net/first",
                    "snippet": "Result",
                }
            ]
        )
        second_search = state.add_search_results(
            [
                {
                    "title": "Second search result",
                    "url": "https://search.example.net/second",
                    "snippet": "Result",
                }
            ]
        )

        self.assertFalse(replaced.active)
        self.assertIs(state.candidate_by_id("candidate_040"), replaced)
        self.assertEqual(first_search[0]["candidate_id"], "candidate_041")
        self.assertEqual(second_search[0]["candidate_id"], "candidate_042")
        self.assertEqual(len(state.candidates), MAX_CANDIDATE_URLS)
        self.assertEqual(len(state.candidate_registry), MAX_CANDIDATE_URLS + 2)

    def test_search_replacement_never_removes_successfully_read_candidate(self) -> None:
        state = DiscoveryProgramState()
        state.register_user_url("https://example.com/")
        state.add_page_links(
            [
                {"anchor_text": f"Page {index}", "href": f"/page-{index}"}
                for index in range(MAX_CANDIDATE_URLS)
            ],
            final_url="https://example.com/",
        )
        protected = state.candidate_by_id("candidate_002")
        state.record_successful_page(_page_result(protected.url), protected)
        state.add_search_results(
            [
                {
                    "title": f"Search {index}",
                    "url": f"https://search.example.net/{index}",
                    "snippet": "Result",
                }
                for index in range(MAX_CANDIDATE_URLS)
            ]
        )

        self.assertEqual(state.candidate_by_id("candidate_001").provenance, "user_input")
        self.assertIs(state.candidate_by_id("candidate_002"), protected)
        self.assertTrue(protected.read_successful)
        self.assertEqual(len(state.candidates), MAX_CANDIDATE_URLS)

    def test_obvious_noise_is_filtered_but_docs_and_support_are_kept(self) -> None:
        state = DiscoveryProgramState()
        parent = state.register_user_url("https://example.com/")
        accepted = state.add_page_links(
            [
                {"anchor_text": "Login", "href": "/login"},
                {"anchor_text": "Continue", "href": "/sign-in"},
                {"anchor_text": "Create", "href": "/sign-up"},
                {"anchor_text": "Register", "href": "/signup"},
                {"anchor_text": "Privacy", "href": "/privacy"},
                {"anchor_text": "Terms", "href": "/terms"},
                {"anchor_text": "Contact us", "href": "/contact"},
                {"anchor_text": "Logo", "href": "/assets/logo.svg"},
                {"anchor_text": "Follow", "href": "https://x.com/example"},
                {"anchor_text": "Documentation", "href": "/docs"},
                {"anchor_text": "Support", "href": "/support"},
            ],
            final_url="https://example.com/",
            parent_candidate=parent,
        )

        self.assertEqual(
            {candidate["url"] for candidate in accepted},
            {"https://example.com/docs", "https://example.com/support"},
        )

    def test_official_parent_page_link_has_higher_priority_than_third_party(self) -> None:
        state = DiscoveryProgramState()
        official = state.register_user_url("https://example.com/")
        third_party = state.add_search_results(
            [
                {
                    "title": "Article",
                    "url": "https://news.example.net/article",
                    "snippet": "Third party",
                }
            ]
        )
        third_party_candidate = state.candidate_by_id(third_party[0]["candidate_id"])
        state.add_page_links(
            [{"anchor_text": "Official docs", "href": "/docs", "page_region": "nav"}],
            final_url=official.url,
            parent_candidate=official,
        )
        state.add_page_links(
            [{"anchor_text": "Related", "href": "/related", "page_region": "nav"}],
            final_url=third_party_candidate.url,
            parent_candidate=third_party_candidate,
        )

        official_link = next(item for item in state.candidates if item.url.endswith("/docs"))
        third_party_link = next(
            item for item in state.candidates if item.url.endswith("/related")
        )
        self.assertGreater(
            official_link.admission_priority,
            third_party_link.admission_priority,
        )
        self.assertEqual(official_link.model_view()["page_region"], "nav")


class SourceDiscoveryEvidenceTests(unittest.TestCase):
    def test_materialization_uses_exact_final_url_query_and_real_title(self) -> None:
        state = DiscoveryProgramState()
        candidate = state.register_user_url("https://example.com/start")
        state.record_successful_page(
            {
                **_page_result(
                    "https://example.com/start",
                    "https://example.com/product?trk=123",
                ),
                "title": "Verified Product Title",
            },
            candidate,
        )
        decision = SourceDiscoveryDecision(
            competitor_name="Example",
            official_domains=["example.com"],
            selected_sources=[
                SelectedMonitoringSource(
                    candidate_id=candidate.candidate_id,
                    source_type=SourceType.PRODUCT_PAGE,
                    monitoring_reason="用于观察产品能力变化。",
                )
            ],
            discovery_complete=True,
            remaining_gaps=[],
        )

        result = materialize_source_discovery_result(decision, state)
        verified = validate_result_evidence(result, state)

        self.assertEqual(
            verified.sources[0].url,
            "https://example.com/product?trk=123",
        )
        self.assertEqual(verified.sources[0].title, "Verified Product Title")

    def test_unread_candidate_cannot_be_materialized(self) -> None:
        state = DiscoveryProgramState()
        candidate = state.register_user_url("https://example.com/unread")
        decision = SourceDiscoveryDecision(
            competitor_name="Example",
            official_domains=["example.com"],
            selected_sources=[
                SelectedMonitoringSource(
                    candidate_id=candidate.candidate_id,
                    source_type=SourceType.PRODUCT_PAGE,
                    monitoring_reason="用于观察产品能力变化。",
                )
            ],
            discovery_complete=True,
            remaining_gaps=[],
        )

        with self.assertRaises(EvidenceValidationError):
            materialize_source_discovery_result(decision, state)

    def test_materialization_uses_registry_title_when_page_title_is_empty(self) -> None:
        state = DiscoveryProgramState()
        candidate = state.register_candidate(
            "https://example.com/docs",
            "web_search",
            title="Registry Documentation",
        )
        state.record_successful_page(
            {**_page_result(candidate.url), "title": ""},
            candidate,
        )
        decision = SourceDiscoveryDecision(
            competitor_name="Example",
            official_domains=["example.com"],
            selected_sources=[
                SelectedMonitoringSource(
                    candidate_id=candidate.candidate_id,
                    source_type=SourceType.DOCS,
                    monitoring_reason="用于观察文档变化。",
                )
            ],
            discovery_complete=True,
            remaining_gaps=[],
        )

        result = materialize_source_discovery_result(decision, state)
        verified = validate_result_evidence(result, state)

        self.assertEqual(verified.sources[0].title, "Registry Documentation")

    def test_materialization_uses_final_url_when_all_titles_are_empty(self) -> None:
        state = DiscoveryProgramState()
        candidate = state.register_user_url("https://example.com/start")
        state.record_successful_page(
            {
                **_page_result(
                    candidate.url,
                    "https://example.com/document/verified",
                ),
                "title": "",
            },
            candidate,
        )
        decision = SourceDiscoveryDecision(
            competitor_name="Example",
            official_domains=["example.com"],
            selected_sources=[
                SelectedMonitoringSource(
                    candidate_id=candidate.candidate_id,
                    source_type=SourceType.DOCS,
                    monitoring_reason="用于观察文档变化。",
                )
            ],
            discovery_complete=True,
            remaining_gaps=[],
        )

        result = materialize_source_discovery_result(decision, state)
        verified = validate_result_evidence(result, state)

        self.assertEqual(
            verified.sources[0].title,
            "https://example.com/document/verified",
        )

    def test_materialization_still_rejects_unknown_candidate(self) -> None:
        decision = SourceDiscoveryDecision(
            competitor_name="Example",
            official_domains=["example.com"],
            selected_sources=[
                SelectedMonitoringSource(
                    candidate_id="candidate_999",
                    source_type=SourceType.DOCS,
                    monitoring_reason="用于观察文档变化。",
                )
            ],
            discovery_complete=True,
            remaining_gaps=[],
        )

        with self.assertRaises(EvidenceValidationError):
            materialize_source_discovery_result(decision, DiscoveryProgramState())

    def test_materialization_requires_real_final_url(self) -> None:
        state = DiscoveryProgramState()
        candidate = state.register_user_url("https://example.com/docs")
        state.record_successful_page(
            {**_page_result(candidate.url), "final_url": "", "title": ""},
            candidate,
        )
        decision = SourceDiscoveryDecision(
            competitor_name="Example",
            official_domains=["example.com"],
            selected_sources=[
                SelectedMonitoringSource(
                    candidate_id=candidate.candidate_id,
                    source_type=SourceType.DOCS,
                    monitoring_reason="用于观察文档变化。",
                )
            ],
            discovery_complete=True,
            remaining_gaps=[],
        )

        with self.assertRaises(EvidenceValidationError):
            materialize_source_discovery_result(decision, state)

    def test_unread_url_cannot_enter_final_sources(self) -> None:
        result = SourceDiscoveryResult(
            competitor_name="Example",
            official_domains=["example.com"],
            sources=[
                MonitoringSource(
                    url="https://example.com/product",
                    title="Example Product",
                    source_type=SourceType.PRODUCT_PAGE,
                    monitoring_reason="观察产品变化。",
                )
            ],
            discovery_complete=True,
            remaining_gaps=[],
        )

        with self.assertRaises(EvidenceValidationError):
            validate_result_evidence(result, DiscoveryProgramState())

    def test_redirect_final_url_and_real_title_are_required(self) -> None:
        state = DiscoveryProgramState(input_urls={"https://example.com/start"})
        state.record_successful_page(
            _page_result("https://example.com/start", "https://example.com/product")
        )
        valid = SourceDiscoveryResult(
            competitor_name="Example",
            official_domains=["example.com"],
            sources=[
                MonitoringSource(
                    url="https://example.com/product",
                    title="Example Product",
                    source_type=SourceType.PRODUCT_PAGE,
                    monitoring_reason="观察产品变化。",
                )
            ],
            discovery_complete=True,
            remaining_gaps=[],
        )

        verified = validate_result_evidence(valid, state)
        self.assertEqual(verified.sources[0].url, "https://example.com/product")

        wrong_title = valid.model_copy(deep=True)
        wrong_title.sources[0].title = "Guessed Title"
        with self.assertRaises(EvidenceValidationError):
            validate_result_evidence(wrong_title, state)

    def test_unread_page_link_candidate_cannot_enter_final_sources(self) -> None:
        state = DiscoveryProgramState()
        state.register_user_url("https://example.com/")
        state.add_page_links(
            [{"anchor_text": "Pricing", "href": "/pricing"}],
            final_url="https://example.com/",
        )
        result = SourceDiscoveryResult(
            competitor_name="Example",
            official_domains=["example.com"],
            sources=[
                MonitoringSource(
                    url="https://example.com/pricing",
                    title="Pricing",
                    source_type=SourceType.PRICING,
                    monitoring_reason="观察价格变化。",
                )
            ],
            discovery_complete=True,
            remaining_gaps=[],
        )

        with self.assertRaises(EvidenceValidationError):
            validate_result_evidence(result, state)


class SourceDiscoveryAgentTests(unittest.IsolatedAsyncioTestCase):
    def test_missing_model_configuration_has_clear_error(self) -> None:
        with self.assertRaises(AgentConfigurationError) as context:
            load_model_config({})

        message = str(context.exception)
        self.assertIn("LLM_API_KEY", message)
        self.assertIn("LLM_BASE_URL", message)
        self.assertIn("LLM_MODEL", message)
        self.assertIn("LLM_PROVIDER", message)

    def test_environment_model_configuration_is_used_without_runtime_override(self) -> None:
        config = load_model_config(TEST_MODEL_ENV)
        model = build_chat_model(config)

        self.assertEqual(config.provider, "aliyun_bailian")
        self.assertEqual(model.model_name, "test-model-from-environment")
        self.assertNotIn("model", inspect.signature(run_source_discovery).parameters)

    async def test_first_party_mcp_loads_only_two_approved_tools(self) -> None:
        async with MCPAdapter(mcp) as adapter:
            tools = await load_source_discovery_tools(adapter)

        self.assertEqual({item.name for item in tools}, {"web_search", "read_page"})

        model_tools = build_model_visible_tools(tools, DiscoveryProgramState())
        tools_by_name = {item.name: item for item in model_tools}
        self.assertEqual(set(tools_by_name), {"web_search", "read_candidate"})
        self.assertEqual(
            set(tools_by_name["read_candidate"].args_schema.model_fields),
            {"candidate_id"},
        )
        self.assertNotIn("read_page", tools_by_name)
        self.assertNotIn("MCP", tools_by_name["read_candidate"].description)

    async def test_agent_runs_through_mcp_and_validates_real_tool_evidence(self) -> None:
        model = _ScriptedChatModel(
            responses=[
                _tool_call("web_search", "search-1", query="Example product", limit=5),
                _tool_call("read_candidate", "read-1", candidate_id="candidate_001"),
                _final_response(
                    sources=[
                        {
                            "candidate_id": "candidate_001",
                            "source_type": "product_page",
                            "monitoring_reason": "用于观察产品能力变化。",
                        }
                    ]
                ),
            ]
        )
        provider = Mock()
        provider.search.return_value = {
            "query": "Example product",
            "results": [
                {
                    "title": "Candidate",
                    "url": "https://example.com/start",
                    "snippet": "Official product page",
                }
            ],
        }
        state = DiscoveryProgramState()

        with (
            _use_test_model(model),
            patch("source_discovery.mcp_server._search_provider", provider),
            patch(
                "source_discovery.mcp_server.read_page_for_discovery",
                return_value=_page_result(
                    "https://example.com/start",
                    "https://example.com/product",
                ),
            ),
        ):
            result = await run_source_discovery("Example", program_state=state)

        self.assertEqual(result.sources[0].url, "https://example.com/product")
        self.assertEqual(result.sources[0].title, "Example Product")
        self.assertEqual(state.search_count, 1)
        self.assertEqual(state.read_count, 1)
        self.assertEqual(state.tool_call_count, 2)
        self.assertIn("web_search", model.bound_tool_names)
        self.assertIn("read_candidate", model.bound_tool_names)
        self.assertNotIn("read_page", model.bound_tool_names)

    async def test_user_urls_are_registered_and_visible_with_candidate_ids(self) -> None:
        model = _ScriptedChatModel(responses=[_final_response(complete=False)])
        state = DiscoveryProgramState()

        with _use_test_model(model):
            await run_source_discovery(
                "Example",
                official_url="https://example.com/",
                additional_urls=["https://example.com/docs"],
                program_state=state,
            )

        self.assertEqual(
            [candidate.candidate_id for candidate in state.candidates],
            ["candidate_001", "candidate_002"],
        )
        user_message = next(
            message
            for message in model.message_history[0]
            if isinstance(message, HumanMessage)
        )
        self.assertIn("candidate_001", str(user_message.content))
        self.assertIn("candidate_002", str(user_message.content))

    async def test_successful_page_links_become_readable_candidates(self) -> None:
        model = _ScriptedChatModel(
            responses=[
                _tool_call("web_search", "search-1", query="Example", limit=1),
                _tool_call("read_candidate", "read-home", candidate_id="candidate_001"),
                _tool_call("read_candidate", "read-pricing", candidate_id="candidate_002"),
                _final_response(
                    sources=[
                        {
                            "candidate_id": "candidate_002",
                            "source_type": "pricing",
                            "monitoring_reason": "用于观察套餐与价格变化。",
                        }
                    ]
                ),
            ]
        )
        provider = Mock()
        provider.search.return_value = {
            "query": "Example",
            "results": [
                {
                    "title": "Home",
                    "url": "https://example.com/",
                    "snippet": "Official site",
                }
            ],
        }

        def read_page(url: str) -> dict[str, Any]:
            if url == "https://example.com/":
                return {
                    **_page_result(url),
                    "links": [
                        {"anchor_text": "Pricing", "href": "/pricing#plans"},
                        {"anchor_text": "Email", "href": "mailto:team@example.com"},
                    ],
                }
            return {
                **_page_result(url, "https://example.com/plans"),
                "title": "Pricing",
            }

        reader = Mock(side_effect=read_page)
        state = DiscoveryProgramState()
        with (
            _use_test_model(model),
            patch("source_discovery.mcp_server._search_provider", provider),
            patch("source_discovery.mcp_server.read_page_for_discovery", reader),
        ):
            result = await run_source_discovery("Example", program_state=state)

        self.assertEqual(reader.call_args_list[1].args[0], "https://example.com/pricing")
        self.assertEqual(result.sources[0].url, "https://example.com/plans")
        pricing = state.candidate_by_id("candidate_002")
        self.assertEqual(pricing.provenance, "page_link")
        self.assertEqual(pricing.final_url, "https://example.com/plans")
        self.assertEqual(state.read_count, 2)

    async def test_inactive_candidate_id_remains_readable_and_materializable(self) -> None:
        state = DiscoveryProgramState()
        state.register_user_url("https://example.com/")
        state.add_page_links(
            [
                {"anchor_text": f"Page {index}", "href": f"/page-{index}"}
                for index in range(MAX_CANDIDATE_URLS)
            ],
            final_url="https://example.com/",
        )
        inactive = state.candidate_by_id("candidate_040")
        state.add_search_results(
            [
                {
                    "title": "Replacement",
                    "url": "https://search.example.net/replacement",
                    "snippet": "Result",
                }
            ]
        )
        self.assertFalse(inactive.active)
        self.assertNotIn(inactive, state.candidates)

        model = _ScriptedChatModel(
            responses=[
                _tool_call(
                    "read_candidate",
                    "read-inactive",
                    candidate_id=inactive.candidate_id,
                ),
                _final_response(
                    sources=[
                        {
                            "candidate_id": inactive.candidate_id,
                            "source_type": "product_page",
                            "monitoring_reason": "用于观察产品能力变化。",
                        }
                    ]
                ),
            ]
        )
        reader = Mock(side_effect=lambda url: _page_result(url))

        with _use_test_model(model), patch(
            "source_discovery.mcp_server.read_page_for_discovery", reader
        ):
            result = await run_source_discovery("Example", program_state=state)

        reader.assert_called_once_with(inactive.url)
        self.assertTrue(inactive.read_successful)
        self.assertIn(inactive.url, {page["requested_url"] for page in state.successful_pages})
        self.assertEqual(result.sources[0].url, inactive.url)
        self.assertEqual(state.read_count, 1)
        self.assertFalse(state.finalization_mode)

    async def test_unknown_candidate_id_does_not_execute_read_page(self) -> None:
        model = _ScriptedChatModel(
            responses=[
                _tool_call("read_candidate", "unknown", candidate_id="candidate_999"),
                _final_response(complete=False),
            ]
        )
        reader = Mock()
        state = DiscoveryProgramState()

        with _use_test_model(model), patch(
            "source_discovery.mcp_server.read_page_for_discovery", reader
        ):
            await run_source_discovery("Example", program_state=state)

        reader.assert_not_called()
        self.assertEqual(state.tool_call_count, 1)
        self.assertEqual(state.read_count, 0)

    async def test_read_candidate_page_failure_returns_safe_tool_error(self) -> None:
        model = _ScriptedChatModel(
            responses=[
                _tool_call("read_candidate", "read-1", candidate_id="candidate_001"),
                _final_response(complete=False),
            ]
        )
        state = DiscoveryProgramState()
        reader = Mock(side_effect=Stage1AdapterError("quality_rejected", "details"))

        with _use_test_model(model), patch(
            "source_discovery.mcp_server.read_page_for_discovery", reader
        ):
            result = await run_source_discovery(
                "Example",
                official_url="https://example.com/",
                program_state=state,
            )

        self.assertFalse(result.discovery_complete)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(state.read_count, 1)
        self.assertEqual(state.successful_pages, [])

    async def test_search_limit_closes_search_but_keeps_read_available(self) -> None:
        responses = [
            _tool_call("web_search", f"search-{index}", query=f"query {index}", limit=1)
            for index in range(6)
        ]
        responses.append(
            _tool_call(
                "read_candidate",
                "read-after-search-closed",
                candidate_id="candidate_001",
            )
        )
        responses.append(_final_response(complete=False))
        model = _ScriptedChatModel(responses=responses)
        provider = Mock()
        provider.search.side_effect = lambda query, limit: {
            "query": query,
            "results": [
                {
                    "title": query,
                    "url": f"https://example.com/{query.rsplit(' ', 1)[-1]}",
                    "snippet": "candidate",
                }
            ],
        }
        reader = Mock(side_effect=lambda url: _page_result(url))
        state = DiscoveryProgramState()

        with (
            _use_test_model(model),
            patch("source_discovery.mcp_server._search_provider", provider),
            patch("source_discovery.mcp_server.read_page_for_discovery", reader),
        ):
            result = await run_source_discovery("Example", program_state=state)

        self.assertEqual(provider.search.call_count, 6)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(state.search_count, 6)
        self.assertEqual(state.read_count, 1)
        self.assertEqual(state.tool_call_count, 7)
        self.assertTrue(state.search_closed)
        self.assertEqual(state.search_closed_reason, "search_limit")
        self.assertFalse(state.finalization_mode)
        self.assertFalse(result.discovery_complete)
        self.assertEqual(result.remaining_gaps, ["尚未确认产品更新入口"])
        self.assertNotIn("web_search", model.bound_tool_name_history[-1])
        self.assertIn("read_candidate", model.bound_tool_name_history[-1])

    async def test_read_limit_enters_finalization_and_stops_read_loop(self) -> None:
        urls = [f"https://example.com/{index}" for index in range(12)]
        responses = [
            _tool_call(
                "read_candidate",
                f"read-{index}",
                candidate_id=f"candidate_{index + 1:03d}",
            )
            for index, _url in enumerate(urls)
        ]
        responses.append(_final_response(complete=False))
        model = _ScriptedChatModel(responses=responses)
        reader = Mock(side_effect=lambda url: _page_result(url))
        state = DiscoveryProgramState()

        with _use_test_model(model), patch(
            "source_discovery.mcp_server.read_page_for_discovery", reader
        ):
            await run_source_discovery(
                "Example",
                additional_urls=urls,
                program_state=state,
            )

        self.assertEqual(reader.call_count, 12)
        self.assertEqual(state.read_count, 12)
        self.assertEqual(state.tool_call_count, 12)
        self.assertTrue(state.finalization_mode)
        self.assertEqual(state.finalization_reason, "read_limit")
        self.assertNotIn("web_search", model.bound_tool_name_history[-1])
        self.assertNotIn("read_candidate", model.bound_tool_name_history[-1])

    async def test_total_tool_count_includes_rejections_and_finalizes_at_twenty(self) -> None:
        valid_urls = [f"https://example.com/{index}" for index in range(11)]
        responses = [
            _tool_call("web_search", f"search-{index}", query=f"query {index}", limit=1)
            for index in range(5)
        ]
        responses.extend(
            _tool_call(
                "read_candidate",
                f"read-{index}",
                candidate_id=f"candidate_{index + 1:03d}",
            )
            for index, _url in enumerate(valid_urls)
        )
        responses.extend(
            _tool_call(
                "read_candidate",
                f"rejected-{index}",
                candidate_id=f"unknown_{index}",
            )
            for index in range(4)
        )
        responses.append(_final_response(complete=False))
        model = _ScriptedChatModel(responses=responses)
        provider = Mock()
        provider.search.side_effect = lambda query, limit: {"query": query, "results": []}
        reader = Mock(side_effect=lambda url: _page_result(url))
        state = DiscoveryProgramState()

        with (
            _use_test_model(model),
            patch("source_discovery.mcp_server._search_provider", provider),
            patch("source_discovery.mcp_server.read_page_for_discovery", reader),
        ):
            await run_source_discovery(
                "Example",
                additional_urls=valid_urls,
                program_state=state,
            )

        self.assertEqual(state.tool_call_count, 20)
        self.assertEqual(state.search_count, 5)
        self.assertEqual(state.read_count, 11)
        self.assertEqual(provider.search.call_count, 5)
        self.assertEqual(reader.call_count, 11)
        self.assertTrue(state.finalization_mode)
        self.assertEqual(state.finalization_reason, "total_tool_call_limit")

    async def test_repeated_rejected_call_is_not_executed_and_forces_finalization(self) -> None:
        model = _ScriptedChatModel(
            responses=[
                _tool_call("read_candidate", "rejected-1", candidate_id="candidate_999"),
                _tool_call("read_candidate", "rejected-2", candidate_id="candidate_999"),
                _final_response(complete=False),
            ]
        )
        reader = Mock()
        state = DiscoveryProgramState()

        with _use_test_model(model), patch(
            "source_discovery.mcp_server.read_page_for_discovery", reader
        ):
            await run_source_discovery("Example", program_state=state)

        reader.assert_not_called()
        self.assertEqual(state.tool_call_count, 2)
        self.assertEqual(state.read_count, 0)
        self.assertTrue(state.finalization_mode)
        self.assertEqual(state.finalization_reason, "repeated_rejected_tool_call")

    async def test_candidate_limit_does_not_close_search_and_allows_candidate_read(self) -> None:
        candidates = [
            {"title": str(index), "url": f"https://example.com/{index}", "snippet": "x"}
            for index in range(MAX_CANDIDATE_URLS)
        ]
        model = _ScriptedChatModel(
            responses=[
                _tool_call("web_search", "search-1", query="initial", limit=40),
                _tool_call("read_candidate", "read-1", candidate_id="candidate_001"),
                _final_response(complete=False),
            ]
        )
        provider = Mock()
        provider.search.return_value = {"query": "initial", "results": candidates}
        reader = Mock(side_effect=lambda url: _page_result(url))
        state = DiscoveryProgramState()

        with (
            _use_test_model(model),
            patch("source_discovery.mcp_server._search_provider", provider),
            patch("source_discovery.mcp_server.read_page_for_discovery", reader),
        ):
            await run_source_discovery("Example", program_state=state)

        self.assertEqual(provider.search.call_count, 1)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(state.search_count, 1)
        self.assertEqual(state.tool_call_count, 2)
        self.assertEqual(len(state.candidate_urls), MAX_CANDIDATE_URLS)
        self.assertFalse(state.search_closed)
        self.assertFalse(state.finalization_mode)
        self.assertIn("web_search", model.bound_tool_name_history[-1])
        self.assertIn("read_candidate", model.bound_tool_name_history[-1])

    async def test_page_links_at_capacity_do_not_block_later_search(self) -> None:
        model = _ScriptedChatModel(
            responses=[
                _tool_call("read_candidate", "read-home", candidate_id="candidate_001"),
                _tool_call("web_search", "search-1", query="product updates", limit=1),
                _final_response(complete=False),
            ]
        )
        provider = Mock()
        provider.search.return_value = {
            "query": "product updates",
            "results": [
                {
                    "title": "Official update",
                    "url": "https://example.com/update",
                    "snippet": "Update",
                }
            ],
        }
        reader = Mock(
            return_value={
                **_page_result("https://example.com/"),
                "links": [
                    {"anchor_text": f"Page {index}", "href": f"/page-{index}"}
                    for index in range(MAX_CANDIDATE_URLS)
                ],
            }
        )
        state = DiscoveryProgramState()

        with (
            _use_test_model(model),
            patch("source_discovery.mcp_server._search_provider", provider),
            patch("source_discovery.mcp_server.read_page_for_discovery", reader),
        ):
            await run_source_discovery(
                "Example",
                official_url="https://example.com/",
                program_state=state,
            )

        self.assertEqual(provider.search.call_count, 1)
        self.assertEqual(state.search_count, 1)
        self.assertFalse(state.search_closed)
        self.assertFalse(state.finalization_mode)
        self.assertEqual(len(state.candidates), MAX_CANDIDATE_URLS)
        self.assertTrue(
            any(candidate.url.endswith("/update") for candidate in state.candidates)
        )

    async def test_search_can_continue_before_search_and_candidate_limits(self) -> None:
        model = _ScriptedChatModel(
            responses=[
                _tool_call("web_search", "search-1", query="product", limit=1),
                _tool_call("web_search", "search-2", query="release notes", limit=1),
                _final_response(complete=False),
            ]
        )
        provider = Mock()
        provider.search.side_effect = lambda query, limit: {
            "query": query,
            "results": [
                {
                    "title": query,
                    "url": f"https://example.com/{query.replace(' ', '-')}",
                    "snippet": "candidate",
                }
            ],
        }
        state = DiscoveryProgramState()

        with _use_test_model(model), patch(
            "source_discovery.mcp_server._search_provider", provider
        ):
            await run_source_discovery("Example", program_state=state)

        self.assertEqual(provider.search.call_count, 2)
        self.assertFalse(state.search_closed)
        self.assertFalse(state.finalization_mode)

    def test_finalization_evidence_summary_only_lists_successful_reads(self) -> None:
        state = DiscoveryProgramState()
        state.add_search_results(
            [
                {
                    "title": "Unread Candidate",
                    "url": "https://example.com/unread",
                    "snippet": "candidate",
                }
            ]
        )
        verified_candidate = state.register_user_url("https://example.com/start")
        state.record_successful_page(
            {
                **_page_result(
                    "https://example.com/start",
                    "https://example.com/final",
                ),
                "title": "Verified Page",
            },
            verified_candidate,
        )

        summary = _build_finalization_evidence_summary(state)

        self.assertIn("https://example.com/final", summary)
        self.assertIn("Verified Page", summary)
        self.assertIn(verified_candidate.candidate_id, summary)
        self.assertNotIn("https://example.com/unread", summary)
        self.assertNotIn("Complete product information", summary)

    async def test_finalization_model_sees_verified_url_and_title_whitelist(self) -> None:
        urls = [f"https://example.com/{index}" for index in range(12)]
        model = _ScriptedChatModel(
            responses=[
                *[
                    _tool_call(
                        "read_candidate",
                        f"read-{index}",
                        candidate_id=f"candidate_{index + 1:03d}",
                    )
                    for index, _url in enumerate(urls)
                ],
                _final_response(complete=False),
            ]
        )
        reader = Mock(side_effect=lambda url: _page_result(url))
        state = DiscoveryProgramState()

        with _use_test_model(model), patch(
            "source_discovery.mcp_server.read_page_for_discovery", reader
        ):
            await run_source_discovery(
                "Example",
                additional_urls=urls,
                program_state=state,
            )

        final_messages = model.message_history[-1]
        evidence_message = next(
            message
            for message in reversed(final_messages)
            if isinstance(message, HumanMessage)
            and "最终收敛证据白名单" in str(message.content)
        )
        evidence_text = str(evidence_message.content)
        self.assertIn("candidate_001", evidence_text)
        self.assertIn("https://example.com/0", evidence_text)
        self.assertIn("Example Product", evidence_text)
        self.assertNotIn("Complete product information", evidence_text)

    def test_structured_output_allows_at_most_one_correction(self) -> None:
        limiter = _StructuredOutputRepairLimiter()

        self.assertIn("修正", limiter(ValueError("first")))
        with self.assertRaises(StructuredOutputRepairLimitError):
            limiter(ValueError("second"))

    def test_model_visible_surfaces_do_not_expose_implementation_configuration(self) -> None:
        user_message = _build_user_message(
            "Example",
            "https://example.com",
            ["https://example.com/docs"],
        )
        technical_error = ToolMessage(
            content="WSA_API_KEY Bearer TencentWsaSearchProvider backend.page_reader",
            tool_call_id="error-1",
            status="error",
        )
        safe_error = _tool_error_for_model(technical_error, "web_search")
        visible_text = "\n".join(
            [SOURCE_DISCOVERY_SYSTEM_PROMPT, user_message, str(safe_error.content)]
        ).casefold()

        for forbidden in (
            "wsa_api_key",
            "llm_api_key",
            "llm_base_url",
            "tencentwsasearchprovider",
            "backend.page_reader",
            "bearer",
        ):
            self.assertNotIn(forbidden, visible_text)
        self.assertIn("read_candidate", SOURCE_DISCOVERY_SYSTEM_PROMPT)
        self.assertNotIn("调用 read_page", SOURCE_DISCOVERY_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
