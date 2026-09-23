"""Frontend Integration HTTP/Application 契约测试，不调用真实模型或网络。"""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from api.application import ProductApplicationService
from api.main import create_app
from event_core.schemas import ChangeItem, EventCandidate, Evidence
from pipeline.orchestrator import ProductPipeline
from pipeline.schemas import ProductInput
from pipeline.store import product_event_id
from source_discovery.schemas import MonitoringSource, SourceDiscoveryResult, SourceType
from triage.schemas import TriageResult


class StubHttpService:
    """记录 Route 传入的产品参数，验证 HTTP 层没有另做业务判断。"""

    def __init__(self):
        self.calls = []
        self.upload_path_exists = False

    def overview(self, workspace_id):
        self.calls.append(("overview", workspace_id))
        return {"competitor_count": 0, "pending_count": 0, "last_scan": None, "events": []}

    async def scan(self, workspace_id):
        self.calls.append(("scan", workspace_id))
        return {"status": "complete", "completed_at": "2026-09-21T00:00:00+00:00",
                "new_event_count": 0, "competitors": []}

    def list_competitors(self, workspace_id):
        self.calls.append(("competitors", workspace_id))
        return []

    async def add_competitor(self, workspace_id, body):
        self.calls.append(("add_competitor", workspace_id, body))
        return {"name": body["name"], "official_url": body["official_url"], "sources": []}

    async def update_competitor(self, workspace_id, name, body):
        self.calls.append(("update_competitor", workspace_id, name, body))
        return {"name": name, "official_url": body["official_url"], "sources": []}

    def reset_competitor_test_data(self, workspace_id, name):
        self.calls.append(("reset_competitor_test_data", workspace_id, name))
        return {"success": True, "cleared": {
            "events_cleared": 1, "triage_results_cleared": 1,
            "pending_candidates_cleared": 0, "research_runs_cleared": 1,
            "research_artifacts_cleared": 1, "statement_states_cleared": 3,
            "monitoring_states_cleared": 4, "snapshots_cleared": 1,
            "run_records_cleared": 1,
        }}

    def research(self, workspace_id, event_id):
        self.calls.append(("research", workspace_id, event_id))
        return {"accepted": True, "research": {"event_id": event_id, "recommended": True,
                                                   "status": "not_started"}}

    def start_research(self, workspace_id, event_id, *, retry):
        self.calls.append(("start_research", workspace_id, event_id, retry))
        return {"accepted": True, "research": {"event_id": event_id, "recommended": True,
                                                   "status": "running"}}

    def own_product(self, workspace_id, product_id):
        self.calls.append(("own_product", workspace_id, product_id))
        return {"product_id": product_id, "product_name": None, "current_focus": None,
                "editable": True, "documents": []}

    def update_own_product(self, workspace_id, product_id, body):
        self.calls.append(("update_own_product", workspace_id, product_id, body))
        return {"product_id": product_id, "product_name": body.get("product_name"),
                "current_focus": body.get("current_focus"), "editable": True,
                "documents": []}

    def add_document(self, workspace_id, product_id, file, display_name):
        self.upload_path_exists = Path(file).is_file()
        self.calls.append(("add_document", workspace_id, product_id, display_name, Path(file)))
        return {"document_id": "doc_1", "display_name": display_name, "file_format": "txt",
                "parsing_status": "parsed", "ingestion_status": "completed",
                "content_status": "current", "knowledge_status": "available", "unit_count": 1}

    def replace_document(self, workspace_id, product_id, document_id, file):
        self.calls.append(("replace_document", workspace_id, product_id, document_id))
        return {"document_id": document_id, "display_name": "产品.txt", "file_format": "txt",
                "parsing_status": "parsed", "ingestion_status": "completed",
                "content_status": "current", "knowledge_status": "available", "unit_count": 1}


class HttpApiContractTests(unittest.TestCase):
    def setUp(self):
        self.service = StubHttpService()
        self.client = TestClient(create_app(self.service))

    def test_overview_and_scan_delegate_to_application_service(self):
        overview = self.client.get("/api/workspaces/team/overview")
        scan = self.client.post("/api/workspaces/team/scan")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(scan.json()["data"]["status"], "complete")
        self.assertIn(("overview", "team"), self.service.calls)
        self.assertIn(("scan", "team"), self.service.calls)

    def test_competitor_discovery_input_is_forwarded(self):
        response = self.client.post("/api/workspaces/team/competitors", json={
            "name": "OpenAI", "official_url": "https://openai.com",
            "own_product_id": "main",
        })
        self.assertEqual(response.status_code, 200)
        call = next(item for item in self.service.calls if item[0] == "add_competitor")
        self.assertEqual(call[2]["official_url"], "https://openai.com")
        self.assertEqual(call[2]["own_product_id"], "main")

    def test_test_reset_route_requires_guard_and_forwards_exact_competitor(self):
        disabled = self.client.post(
            "/api/workspaces/team/competitors/OpenAI/reset-test-data"
        )
        self.assertEqual(disabled.status_code, 404)

        enabled = TestClient(create_app(self.service, test_reset_enabled=True))
        response = enabled.post(
            "/api/workspaces/team/competitors/OpenAI/reset-test-data"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["success"])
        self.assertEqual(response.json()["data"]["cleared"]["events_cleared"], 1)
        self.assertIn(("reset_competitor_test_data", "team", "OpenAI"), self.service.calls)

    def test_research_retry_uses_explicit_retry_contract(self):
        response = self.client.post(
            "/api/workspaces/team/events/evt_1/research", json={"retry": True}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(("start_research", "team", "evt_1", True), self.service.calls)

    def test_product_configuration_update_uses_product_scope_without_storage_key(self):
        response = self.client.put(
            "/api/workspaces/team/own-product?product_id=main",
            json={"product_name": "Acme", "current_focus": "Agent 治理"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            ("update_own_product", "team", "main", {
                "product_name": "Acme", "current_focus": "Agent 治理"
            }),
            self.service.calls,
        )
        self.assertNotIn("storage", response.text.lower())

    def test_upload_uses_temporary_file_and_removes_it_after_service_returns(self):
        response = self.client.post(
            "/api/workspaces/team/own-product/documents",
            data={"product_id": "main", "display_name": "产品资料.txt"},
            files={"file": ("产品资料.txt", "真实产品资料", "text/plain")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.service.upload_path_exists)
        uploaded_path = next(item[4] for item in self.service.calls if item[0] == "add_document")
        self.assertFalse(uploaded_path.exists())

    def test_upload_rejects_unsupported_file_before_product_service(self):
        response = self.client.post(
            "/api/workspaces/team/own-product/documents",
            data={"product_id": "main"}, files={"file": ("danger.exe", b"MZ")},
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["code"], "unsupported_file")


class FakeDiscoveryAdapter:
    """仅为 Application Service 验证正式 Discovery→confirm 调用顺序。"""

    def __init__(self, _directory, product):
        self.product = product

    def store_references(self):
        return {"pipeline": "local"}

    async def discover(self):
        return SourceDiscoveryResult(
            competitor_name=self.product.competitor_name,
            official_domains=["openai.com"],
            sources=[MonitoringSource(
                url="https://openai.com/changelog", title="OpenAI Changelog",
                source_type=SourceType.CHANGELOG, monitoring_reason="跟踪产品更新",
            )],
            discovery_complete=True, remaining_gaps=[],
        )


class EventLoopSensitiveScanAdapter(FakeDiscoveryAdapter):
    """模拟 Playwright Sync API：若扫描仍位于 asyncio Event Loop 就立即失败。"""

    failure: Exception | None = None

    def scan(self, url):
        if self.failure is not None:
            raise self.failure
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return {
                "url": url,
                "final_url": url,
                "captured_at": "2026-09-22T00:00:00+00:00",
                "content": "Stable controlled page.",
            }
        raise RuntimeError("同步 Playwright 仍在 asyncio Event Loop 内运行。")

    def explicit_statement_candidates(self, _history, _source):
        return []


class ApplicationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pipeline = ProductPipeline(self.temp.name, adapter_type=FakeDiscoveryAdapter)
        self.service = ProductApplicationService(self.temp.name, pipeline=self.pipeline)

    def tearDown(self):
        self.temp.cleanup()

    async def test_add_competitor_calls_existing_discovery_and_pipeline_confirmation(self):
        result = await self.service.add_competitor("team", {
            "name": "OpenAI", "official_url": "https://openai.com",
            "own_product_id": "main", "additional_urls": [], "current_focus": "企业能力",
        })
        self.assertEqual(result["sources"][0]["credibility"], "official")
        self.assertEqual(result["sources"][0]["source_type"], "changelog")
        self.assertEqual(len(self.pipeline.store.items("workspaces")), 1)

    async def test_scan_calls_existing_product_pipeline_run(self):
        product = ProductInput(workspace_id="team", own_product_id="main",
                               competitor_name="OpenAI", official_url="https://openai.com")
        self.pipeline.configure_competitor(product)
        self.pipeline.run = AsyncMock(return_value={"product": {
            "status": "complete", "detected_events": [{"event_id": "evt_1"}],
            "historical_events": [{"event_id": "evt_historical"}],
        }})
        result = await self.service.scan("team")
        self.pipeline.run.assert_awaited_once_with(product)
        self.assertEqual(result["new_event_count"], 1)
        self.assertEqual(result["historical_backfill_count"], 1)

    def test_product_configuration_defaults_empty_and_survives_store_reopen(self):
        empty = self.service.own_product("team", "main")
        self.assertIsNone(empty["product_name"])
        self.assertIsNone(empty["current_focus"])

        self.pipeline.remember_explicit = Mock()
        saved = self.service.update_own_product("team", "main", {
            "product_name": "Acme Agent Platform",
            "current_focus": "企业级 Agent 管理与治理",
        })
        self.assertEqual(saved["product_name"], "Acme Agent Platform")
        reopened = ProductApplicationService(self.temp.name)
        loaded = reopened.own_product("team", "main")
        self.assertEqual(loaded["product_name"], "Acme Agent Platform")
        self.assertEqual(loaded["current_focus"], "企业级 Agent 管理与治理")
        self.pipeline.remember_explicit.assert_not_called()

    def test_research_view_keeps_verification_metadata_and_removes_developer_fields(self):
        workspace = "workspace-key"
        event_id = "evt_public"
        artifact_key = "research-artifact"
        self.pipeline.store.put("research:" + workspace, artifact_key, {
            "result": {"event_summary": [{"text": "正式上线", "kind": "fact", "support": [{
                "result_id": "page_001", "excerpt": "正式上线。",
                "evidence_summary": "官方更新日志确认该能力已经正式上线。",
            }]}], "evidence_refs": ["page_001"]},
            "citations": [{
                "result_id": "page_001", "url": "https://example.com/changelog",
                "title": "官方更新日志", "content": "正式上线。", "source": "page",
                "domain": "competitor", "source_group": "developer-only",
                "credibility": "official", "status": "confirmed",
                "observed_at": "2026-09-20T00:00:00Z",
            }],
        })
        state = {"event_id": event_id, "status": "completed", "artifact": {
            "namespace": "research:" + workspace, "key": artifact_key,
        }}

        view = self.service._research_view(workspace, state)
        citation = view["citations"][0]

        self.assertEqual(view["result"]["event_summary"][0]["support"][0]["evidence_summary"],
                         "官方更新日志确认该能力已经正式上线。")
        self.assertEqual(citation["credibility"], "official")
        self.assertEqual(citation["observed_at"], "2026-09-20T00:00:00Z")
        self.assertNotIn("source_group", citation)
        self.assertNotIn("source", citation)
        self.assertNotIn("domain", citation)

    async def test_each_scan_receives_latest_saved_focus_for_triage_context(self):
        product = ProductInput(workspace_id="team", own_product_id="main",
                               competitor_name="OpenAI", official_url="https://openai.com")
        self.pipeline.configure_competitor(product)
        self.pipeline.run = AsyncMock(return_value={"product": {
            "status": "complete", "detected_events": []
        }})

        self.service.update_own_product("team", "main", {"current_focus": "Focus A"})
        await self.service.scan("team")
        first = self.pipeline.run.await_args.args[0]
        self.assertEqual(first.current_focus, "Focus A")

        self.pipeline.run.reset_mock()
        self.service.update_own_product("team", "main", {"current_focus": "Focus B"})
        await self.service.scan("team")
        second = self.pipeline.run.await_args.args[0]
        self.assertEqual(second.current_focus, "Focus B")

    async def test_scan_failure_returns_product_error_without_exception_text(self):
        product = ProductInput(workspace_id="team", own_product_id="main",
                               competitor_name="OpenAI", official_url="https://openai.com")
        self.pipeline.configure_competitor(product)
        self.pipeline.run = AsyncMock(return_value={"product": {
            "status": "failed", "detected_events": [],
            "error": {"type": "scan_error", "message": "private traceback text"},
        }})
        result = await self.service.scan("team")
        self.assertEqual(result["error"]["type"], "source_error")
        self.assertEqual(result["error"]["message"], "部分来源无法访问，请稍后重试。")
        self.assertNotIn("private", str(result))

    def test_overview_serializes_real_event_without_internal_candidate_id(self):
        product = ProductInput(workspace_id="team", own_product_id="main",
                               competitor_name="OpenAI", official_url="https://openai.com")
        workspace, _ = self.pipeline.configure_competitor(product)
        evidence = Evidence(
            evidence_id="evidence_internal", source_url="https://openai.com/changelog",
            source_title="OpenAI Changelog", source_type="changelog",
            source_credibility="official", evidence_status="confirmed",
            evidence_capability=["event"], observed_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
            relevant_excerpt="正式发布企业能力", evidence_context={},
        )
        candidate = EventCandidate(
            decision="event", event_type="capability", event_subject="企业能力",
            change_action="正式发布", change_items=[ChangeItem(item="企业能力", current_state="已发布")],
            event_time="2026-09-21", decision_reason="官方更新日志确认发布。",
            candidate_id="candidate_internal", competitor="OpenAI",
            detection_path="explicit_statement", evidence=[evidence],
        )
        self.pipeline.store.put("events:" + workspace, candidate.candidate_id,
                                candidate.model_dump(mode="json"))
        self.pipeline.store.put("event_actions:" + workspace, candidate.candidate_id, {
            "status": "complete", "event": candidate.model_dump(mode="json"),
            "discovery_origin": "historical_backfill",
        })
        triage = TriageResult(
            event_id=candidate.candidate_id, decision="monitor", reason="继续观察。",
            focus_relevance="相关性一般。",
            own_product_relevance={"status": "insufficient_own_product_context", "claims": []},
            evidence_refs=["event:evidence_internal"], uncertainty=[], missing_context=[],
        )
        self.pipeline.store.put("triage:" + workspace, "triage_key", {
            "result": triage.model_dump(mode="json"), "citations": [],
        })

        result = self.service.overview("team")
        event = result["events"][0]
        self.assertEqual(event["event_id"], product_event_id(candidate))
        self.assertEqual(event["discovery_origin"], "historical_backfill")
        self.assertEqual(event["triage"]["decision"], "monitor")
        self.assertNotIn("candidate_id", str(event))
        self.assertNotIn("candidate_internal", str(event))

    def test_document_actions_call_existing_product_pipeline_services(self):
        self.pipeline.add_own_product_document = Mock(return_value=Mock(
            model_dump=Mock(return_value={"document_id": "doc_1"})
        ))
        self.pipeline.replace_own_product_document = Mock(return_value=Mock(
            model_dump=Mock(return_value={"document_id": "doc_1", "content_status": "current"})
        ))
        added = self.service.add_document("team", "main", "product.txt", "产品.txt")
        replaced = self.service.replace_document("team", "main", "doc_1", "product-v2.txt")
        self.assertEqual(added["document_id"], "doc_1")
        self.assertEqual(replaced["content_status"], "current")
        self.pipeline.add_own_product_document.assert_called_once()
        self.pipeline.replace_own_product_document.assert_called_once()


class RuntimeBoundaryApiTests(unittest.TestCase):
    """从真实 FastAPI async route 验证同步扫描的运行线程和错误契约。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        EventLoopSensitiveScanAdapter.failure = None
        self.product = ProductInput(
            workspace_id="team", own_product_id="main",
            competitor_name="Controlled", official_url="https://example.com",
        )
        self.pipeline = ProductPipeline(
            self.temp.name, adapter_type=EventLoopSensitiveScanAdapter
        )
        self.pipeline.confirm_monitoring_sources(self.product, SourceDiscoveryResult(
            competitor_name="Controlled", official_domains=["example.com"],
            sources=[MonitoringSource(
                url="https://example.com", title="Controlled",
                source_type=SourceType.CHANGELOG, monitoring_reason="运行边界测试",
            )],
            discovery_complete=True, remaining_gaps=[],
        ))
        self.client = TestClient(create_app(ProductApplicationService(
            self.temp.name, pipeline=self.pipeline
        )))

    def test_async_scan_route_keeps_sync_browser_outside_event_loop(self):
        response = self.client.post("/api/workspaces/team/scan")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "complete")

    def test_scan_exception_maps_to_source_error_without_traceback(self):
        EventLoopSensitiveScanAdapter.failure = RuntimeError("private traceback detail")
        response = self.client.post("/api/workspaces/team/scan")
        body = response.json()["data"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"]["type"], "source_error")
        self.assertNotIn("private traceback", response.text)


if __name__ == "__main__":
    unittest.main()
