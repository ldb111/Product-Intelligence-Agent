"""面向网页的 Application Service：复用冻结业务入口并生成安全查询视图。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_core.schemas import EventCandidate
from memory import MemoryStore, PreferenceNotFoundError, PreferenceService
from pipeline.configuration import ProductConfigurationService
from pipeline.documents import OwnProductDocumentService
from pipeline.orchestrator import ProductPipeline
from pipeline.output import present_artifact, present_event, present_research_action
from pipeline.schemas import ProductInput
from pipeline.store import identity, product_event_id
from triage.schemas import TriageResult


class ProductApiError(RuntimeError):
    """将底层失败收敛为稳定的产品错误，不把异常正文送给浏览器。"""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


class ProductApplicationService:
    """连接 HTTP 与现有 ProductPipeline 的唯一入口。

    输入来自路由层的 workspace、竞品、文档或事件 ID；写操作调用正式 Pipeline/
    Document Service，读操作只从同一持久化仓库生成产品视图。输出不会包含内部候选 ID、
    SQLite 命名空间、文件路径、模型信息或工具轨迹。
    """

    def __init__(self, directory: str | Path, *, pipeline: ProductPipeline | None = None):
        self.pipeline = pipeline or ProductPipeline(directory)
        self.documents = OwnProductDocumentService(self.pipeline.store.directory)
        self.configuration = ProductConfigurationService(self.pipeline.store)

    def _preferences(self, workspace_id: str) -> PreferenceService:
        """复用 Pipeline 已使用的 workspace Memory 文件，不建立第二套偏好存储。"""
        path = self.pipeline.store.directory / (identity(workspace_id) + ".memory.sqlite3")
        return PreferenceService(MemoryStore(path))

    def _configured_products(self, workspace_id: str) -> list[ProductInput]:
        products: list[ProductInput] = []
        for saved in self.pipeline.store.items("workspaces").values():
            config = saved.get("config")
            if not isinstance(config, dict):
                continue
            product = ProductInput.model_validate(config)
            if product.workspace_id == workspace_id:
                products.append(self.configuration.apply(product))
        return sorted(products, key=lambda item: item.competitor_name.casefold())

    def _configured_product(self, workspace_id: str, competitor_name: str) -> ProductInput:
        for product in self._configured_products(workspace_id):
            if product.competitor_name == competitor_name:
                return product
        raise ProductApiError("competitor_not_found", "未找到该竞品。", 404)

    @staticmethod
    def _workspace(product: ProductInput) -> str:
        return identity([product.workspace_id, product.competitor_name])

    def _source_view(self, source: dict[str, Any]) -> dict[str, Any]:
        return {
            "url": source.get("url"),
            "title": source.get("title"),
            "source_type": source.get("source_type"),
            "monitoring_reason": source.get("monitoring_reason"),
            "credibility": source.get("credibility", "unverified"),
        }

    def list_competitors(self, workspace_id: str) -> list[dict[str, Any]]:
        """读取已保存配置与真实 Source Discovery 结果，生成竞品卡片。"""
        scan_times = self.pipeline.store.items("ui_competitor_scans:" + workspace_id)
        rows = []
        for product in self._configured_products(workspace_id):
            workspace = self._workspace(product)
            discovery = self.pipeline.store.get("sources", workspace, {})
            rows.append({
                "name": product.competitor_name,
                "official_url": product.official_url,
                "additional_urls": product.additional_urls,
                "own_product_id": product.own_product_id,
                "product_profile": product.product_profile,
                "current_focus": product.current_focus,
                "sources": [self._source_view(item) for item in discovery.get("sources", [])],
                "discovery_complete": discovery.get("discovery_complete"),
                "remaining_gaps": discovery.get("remaining_gaps", []),
                "last_scan_at": scan_times.get(product.competitor_name),
            })
        return rows

    async def add_competitor(self, workspace_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """执行真实来源发现，成功后再正式保存竞品与来源。

        先发现后确认可以避免来源识别失败时留下看似已经配置完成的空竞品。正式保存仍由
        ProductPipeline.confirm_monitoring_sources 完成，可信度也继续使用冻结实现的规则。
        """
        if any(item.competitor_name == data["name"] for item in self._configured_products(workspace_id)):
            raise ProductApiError("competitor_exists", "该竞品已经存在。", 409)
        product = ProductInput(
            workspace_id=workspace_id,
            own_product_id=data.get("own_product_id"),
            competitor_name=data["name"],
            official_url=data["official_url"],
            additional_urls=data.get("additional_urls", []),
            product_profile=data.get("product_profile"),
            current_focus=data.get("current_focus", ""),
        )
        try:
            adapter = self.pipeline.adapter_type(self.pipeline.store.directory, product)
            discovered = await adapter.discover()
            self.pipeline.confirm_monitoring_sources(product, discovered)
        except ProductApiError:
            raise
        except Exception:
            raise ProductApiError("source_discovery_failed", "来源识别失败，请重试。", 502) from None
        return self._configured_product_view(product)

    async def update_competitor(
        self, workspace_id: str, competitor_name: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """通过正式配置入口更新竞品；来源 URL 变化时重新执行真实来源发现。"""
        current = self._configured_product(workspace_id, competitor_name)
        values = current.model_dump()
        values.update({key: value for key, value in changes.items() if value is not None})
        updated = ProductInput.model_validate(values)
        source_changed = (
            updated.official_url != current.official_url
            or updated.additional_urls != current.additional_urls
        )
        try:
            if source_changed:
                adapter = self.pipeline.adapter_type(self.pipeline.store.directory, updated)
                discovered = await adapter.discover()
                self.pipeline.confirm_monitoring_sources(updated, discovered)
            else:
                self.pipeline.configure_competitor(updated)
        except Exception:
            raise ProductApiError("competitor_update_failed", "竞品更新失败，请重试。", 502) from None
        return self._configured_product_view(updated)

    def reset_competitor_test_data(
        self, workspace_id: str, competitor_name: str
    ) -> dict[str, Any]:
        """清理单个竞品的测试运行历史，保留竞品、来源和自身产品配置。

        路由传入产品层 workspace 与竞品名称；这里先解析正式配置中的 own product scope，
        再交给 PipelineStore 做单事务清理。该入口不调用 Event、Triage 或 Research，因而
        不改变正式业务判断，只让下次扫描重新走正常首次扫描与 Historical Backfill。
        """
        product = self._configured_product(workspace_id, competitor_name)
        try:
            cleared = self.pipeline.store.reset_competitor_test_data(
                workspace_id=workspace_id,
                product_id=product.own_product_id,
                competitor_name=competitor_name,
            )
        except ValueError:
            raise ProductApiError(
                "test_reset_failed", "重置失败，未能完整清除测试数据。", 409
            ) from None
        return {"success": True, "cleared": cleared}

    def _configured_product_view(self, product: ProductInput) -> dict[str, Any]:
        return next(
            row for row in self.list_competitors(product.workspace_id)
            if row["name"] == product.competitor_name
        )

    async def scan(self, workspace_id: str) -> dict[str, Any]:
        """运行已配置竞品，并把底层失败映射成可行动、无 traceback 的产品错误。"""
        products = self._configured_products(workspace_id)
        if not products:
            raise ProductApiError("configuration_error", "请先配置竞品。", 409)
        completed_at = datetime.now(timezone.utc).isoformat()
        results = []
        new_event_count = 0
        historical_backfill_count = 0
        for product in products:
            try:
                result = await self.pipeline.run(product)
                public = result["product"]
                item = {"competitor": product.competitor_name, "status": public["status"]}
                if public["status"] == "failed":
                    item["error"] = self._scan_error(public.get("error", {}).get("type"))
                new_event_count += len(public.get("detected_events", []))
                historical_backfill_count += len(public.get("historical_events", []))
            except Exception:
                # 未被 Pipeline 收敛的运行异常仍记录为失败，但绝不把异常正文送到浏览器。
                item = {
                    "competitor": product.competitor_name,
                    "status": "failed",
                    "error": self._scan_error("runtime_error"),
                }
            results.append(item)
            self.pipeline.store.put(
                "ui_competitor_scans:" + workspace_id,
                product.competitor_name,
                completed_at,
            )
        failed = [item for item in results if item["status"] == "failed"]
        status = "failed" if len(failed) == len(results) else "partial" if failed else "complete"
        scan = {
            "status": status,
            "completed_at": completed_at,
            "new_event_count": new_event_count,
            "historical_backfill_count": historical_backfill_count,
            "competitors": results,
        }
        if failed:
            scan["error"] = failed[0]["error"]
        self.pipeline.store.put("ui_scans:" + workspace_id, "latest", scan)
        return scan

    @staticmethod
    def _scan_error(error_type: str | None) -> dict[str, str]:
        """把 Pipeline 类别收敛为四种产品错误，避免泄漏异常正文。"""
        if error_type == "configuration_error":
            return {"type": "configuration_error", "message": "扫描配置不完整，请检查竞品设置。"}
        if error_type in {"source_discovery_error", "scan_error"}:
            return {"type": "source_error", "message": "部分来源无法访问，请稍后重试。"}
        if error_type == "runtime_error":
            return {"type": "runtime_error", "message": "扫描服务暂时不可用，请重试。"}
        return {"type": "pipeline_error", "message": "扫描流程未完成，请重试。"}

    def _triage_for_event(self, workspace: str, internal_event_id: str) -> dict[str, Any] | None:
        for key, artifact in reversed(list(self.pipeline.store.items("triage:" + workspace).items())):
            result = artifact.get("result", {})
            if result.get("event_id") != internal_event_id:
                continue
            view = present_artifact(artifact, "triage:" + key)
            view["result"].pop("event_id", None)
            return view["result"]
        return None

    def _research_view(self, workspace: str, state: dict[str, Any]) -> dict[str, Any]:
        """生成网页所需的 Research 视图，并补回可核验的来源元数据。

        Research 产物内部还包含 source_group、工具来源类别等开发字段。网页不需要这些
        实现细节，因此这里只保留 Claim 关联所需的公开引用 ID、正文、链接、可信度与
        观察日期；结论本身及其 Evidence 关系保持原样。
        """
        action = present_research_action(self.pipeline.store, workspace, state)
        if action.get("status") != "completed" or "citations" not in action:
            return action
        reference = state.get("artifact", {})
        artifact = self.pipeline.store.get(reference.get("namespace", ""), reference.get("key", ""))
        raw_citations = artifact.get("citations", []) if isinstance(artifact, dict) else []
        raw_by_id = {item.get("result_id"): item for item in raw_citations}
        prefix = "research:" + state["event_id"] + ":"
        safe_citations = []
        for citation in action["citations"]:
            raw_id = citation.get("result_id", "")
            raw = raw_by_id.get(raw_id.removeprefix(prefix), {})
            safe = {key: citation[key] for key in ("result_id", "url", "title", "content") if key in citation}
            for key in ("credibility", "status", "observed_at"):
                if raw.get(key) is not None:
                    safe[key] = raw[key]
            safe_citations.append(safe)
        action["citations"] = safe_citations
        return action

    def _event_view(
        self, product: ProductInput, internal_event_id: str, event_data: dict[str, Any]
    ) -> dict[str, Any]:
        workspace = self._workspace(product)
        action = self.pipeline.store.get("event_actions:" + workspace, internal_event_id, {})
        origin = action.get("discovery_origin", "normal_monitoring")
        facts, _ = present_event(event_data, discovery_origin=origin)
        evidence = event_data.get("evidence", [])
        observed = [item.get("observed_at") for item in evidence if item.get("observed_at")]
        title_parts = [facts.get("event_subject"), facts.get("change_action")]
        triage = self._triage_for_event(workspace, internal_event_id)
        event_id = facts["event_id"]
        research_state = self.pipeline.store.get("research_status:" + workspace, event_id)
        research = (
            self._research_view(workspace, research_state)
            if research_state is not None else None
        )
        return {
            "event_id": event_id,
            "competitor": facts.get("competitor"),
            "event_type": facts.get("event_type"),
            "title": " · ".join(str(item) for item in title_parts if item),
            "summary": facts.get("decision_reason"),
            "event_time": facts.get("event_time"),
            "observation_time": max(observed) if observed else None,
            "effective_time": facts.get("effective_time"),
            "affected_scope": facts.get("affected_scope"),
            "discovery_origin": facts.get("discovery_origin"),
            "change_items": facts.get("change_items", []),
            "evidence": [{
                "result_id": "event:" + item["evidence_id"],
                "url": item.get("source_url"),
                "title": item.get("source_title"),
                "source_type": item.get("source_type"),
                "credibility": item.get("source_credibility"),
                "status": item.get("evidence_status"),
                "observed_at": item.get("observed_at"),
                "content": item.get("relevant_excerpt"),
            } for item in evidence],
            "triage": triage,
            "research": research,
        }

    def overview(self, workspace_id: str) -> dict[str, Any]:
        """聚合全部 confirmed Event；Pending 只返回数量，不混入事件列表。"""
        events = []
        pending_count = 0
        for product in self._configured_products(workspace_id):
            workspace = self._workspace(product)
            for internal_id, event in self.pipeline.store.items("events:" + workspace).items():
                events.append(self._event_view(product, internal_id, event))
            pending_count += sum(
                1 for item in self.pipeline.store.items("pending:" + workspace).values()
                if item.get("status") == "pending"
            )
        return {
            "competitor_count": len(self._configured_products(workspace_id)),
            "last_scan": self.pipeline.store.get("ui_scans:" + workspace_id, "latest"),
            "pending_count": pending_count,
            "events": events,
        }

    def _find_event(self, workspace_id: str, event_id: str) -> tuple[ProductInput, str, dict[str, Any]]:
        for product in self._configured_products(workspace_id):
            workspace = self._workspace(product)
            for internal_id, event in self.pipeline.store.items("events:" + workspace).items():
                if product_event_id(event) == event_id:
                    return product, internal_id, event
        raise ProductApiError("event_not_found", "未找到该竞争事件。", 404)

    def research(self, workspace_id: str, event_id: str) -> dict[str, Any]:
        product, internal_id, event = self._find_event(workspace_id, event_id)
        workspace = self._workspace(product)
        state = self.pipeline.store.get("research_status:" + workspace, event_id)
        if state is not None:
            return {"accepted": True, "research": self._research_view(workspace, state)}
        triage = self._triage_for_event(workspace, internal_id)
        if triage is None or triage.get("decision") != "research":
            return {"accepted": False, "event_id": event_id, "reason": "research_not_recommended"}
        return {"accepted": True, "research": {"event_id": event_id, "recommended": True,
                                                   "status": "not_started"}}

    def start_research(self, workspace_id: str, event_id: str, *, retry: bool) -> dict[str, Any]:
        product, _, _ = self._find_event(workspace_id, event_id)
        result = self.pipeline.start_research(product, event_id, retry=retry)["product"]
        workspace = self._workspace(product)
        state = self.pipeline.store.get("research_status:" + workspace, event_id)
        if result.get("accepted") and state is not None:
            result["research"] = self._research_view(workspace, state)
        return result

    def _follow_up_view(
        self, product: ProductInput, event_id: str, turn: dict[str, Any]
    ) -> dict[str, Any]:
        """将内部 Turn 与 Artifact 投影为网页对话，隐藏执行权、命名空间和审计轨迹。"""
        view = {
            key: turn[key] for key in (
                "turn_id", "user_message", "status", "candidate_count", "created_at", "updated_at"
            ) if key in turn
        }
        if turn.get("status") == "completed":
            reference = turn.get("artifact", {})
            artifact = self.pipeline.store.get(reference.get("namespace", ""), reference.get("key", ""))
            if isinstance(artifact, dict):
                public = present_artifact(artifact, "follow_up:" + turn["turn_id"])
                raw_by_id = {item.get("result_id"): item for item in artifact.get("citations", [])}
                prefix = "follow_up:" + turn["turn_id"] + ":"
                citations = []
                for citation in public["citations"]:
                    raw = raw_by_id.get(citation.get("result_id", "").removeprefix(prefix), {})
                    safe = {key: citation[key] for key in ("result_id", "url", "title", "content") if key in citation}
                    for key in ("credibility", "status", "observed_at"):
                        if raw.get(key) is not None:
                            safe[key] = raw[key]
                    citations.append(safe)
                view.update(result=public["result"], citations=citations)
        elif turn.get("status") == "failed":
            view["error"] = {"message": "本次继续研究未完成，可以重试。"}
        return view

    def follow_up_conversation(self, workspace_id: str, event_id: str) -> dict[str, Any]:
        product, _, _ = self._find_event(workspace_id, event_id)
        workspace = self._workspace(product)
        initial = self.pipeline.store.get("research_status:" + workspace, event_id)
        available = bool(initial and initial.get("status") == "completed")
        product_id = product.own_product_id or "default"
        turns = self.pipeline.store.follow_up_turns(
            workspace_id=workspace_id, product_id=product_id, event_id=event_id,
        ) if available else []
        return {"available": available, "turns": [
            self._follow_up_view(product, event_id, turn) for turn in turns
        ]}

    def send_follow_up(
        self, workspace_id: str, event_id: str, *, request_id: str, question: str
    ) -> dict[str, Any]:
        product, _, _ = self._find_event(workspace_id, event_id)
        outcome = self.pipeline.follow_up_research(
            product, event_id, request_id=request_id, question=question,
        )
        if not outcome.get("accepted"):
            raise ProductApiError("follow_up_unavailable", "初始研究完成后才能继续追问。", 409)
        return self._follow_up_view(product, event_id, outcome["turn"])

    def retry_follow_up(self, workspace_id: str, event_id: str, turn_id: str) -> dict[str, Any]:
        product, _, _ = self._find_event(workspace_id, event_id)
        outcome = self.pipeline.follow_up_research(product, event_id, retry_turn_id=turn_id)
        if not outcome.get("accepted"):
            raise ProductApiError("follow_up_not_found", "未找到可重试的追问。", 404)
        return self._follow_up_view(product, event_id, outcome["turn"])

    def preferences(self, workspace_id: str, product_id: str) -> dict[str, Any]:
        return self._preferences(workspace_id).list(workspace_id)

    def add_preference(self, workspace_id: str, product_id: str, data: dict[str, str]) -> dict[str, Any]:
        return self._preferences(workspace_id).add(workspace_id, data["type"], data["content"])

    def edit_preference(
        self, workspace_id: str, product_id: str, item_id: str, data: dict[str, str]
    ) -> dict[str, Any]:
        try:
            return self._preferences(workspace_id).edit(
                workspace_id, item_id, data["type"], data["content"]
            )
        except PreferenceNotFoundError:
            raise ProductApiError("preference_not_found", "未找到该条关注信息。", 404) from None

    def remove_preference(self, workspace_id: str, product_id: str, item_id: str) -> dict[str, bool]:
        try:
            self._preferences(workspace_id).remove(workspace_id, item_id)
        except PreferenceNotFoundError:
            raise ProductApiError("preference_not_found", "未找到该条关注信息。", 404) from None
        return {"removed": True}

    def confirm_preference(self, workspace_id: str, product_id: str, item_id: str) -> dict[str, Any]:
        try:
            return self._preferences(workspace_id).confirm(workspace_id, item_id)
        except PreferenceNotFoundError:
            raise ProductApiError("preference_not_found", "未找到该条待确认信息。", 404) from None

    def ignore_preference(self, workspace_id: str, product_id: str, item_id: str) -> dict[str, bool]:
        try:
            self._preferences(workspace_id).ignore(workspace_id, item_id)
        except PreferenceNotFoundError:
            raise ProductApiError("preference_not_found", "未找到该条待确认信息。", 404) from None
        return {"ignored": True}

    def own_product(self, workspace_id: str, product_id: str) -> dict[str, Any]:
        configuration = self.configuration.get(workspace_id, product_id)
        return {
            "product_id": product_id,
            "product_name": configuration.product_name or None,
            "current_focus": configuration.current_focus or None,
            "editable": True,
            "documents": [item.model_dump() for item in self.documents.list_own_product_documents(
                workspace_id=workspace_id, product_id=product_id
            )],
        }

    def update_own_product(
        self, workspace_id: str, product_id: str, changes: dict[str, str]
    ) -> dict[str, Any]:
        """保存自身产品配置，再返回同一产品页视图；不会写入 Memory。"""
        try:
            self.configuration.update(workspace_id, product_id, changes)
        except ValueError:
            raise ProductApiError("configuration_error", "产品配置无法保存，请检查输入。", 422) from None
        return self.own_product(workspace_id, product_id)

    def add_document(
        self, workspace_id: str, product_id: str, file: str | Path, display_name: str
    ) -> dict[str, Any]:
        return self.pipeline.add_own_product_document(
            workspace_id=workspace_id, product_id=product_id, file=file,
            display_name=display_name,
        ).model_dump()

    def replace_document(
        self, workspace_id: str, product_id: str, document_id: str, file: str | Path
    ) -> dict[str, Any]:
        return self.pipeline.replace_own_product_document(
            workspace_id=workspace_id, product_id=product_id,
            document_id=document_id, new_file=file,
        ).model_dump()
