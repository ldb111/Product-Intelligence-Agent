"""显式产品 Workflow：接线、状态保存和路由，不承担新的 AI 判断。"""

import asyncio

from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4

from event_core.schemas import DetectionInput, EventCandidate
from knowledge.schemas import KnowledgeError, StoreReadError, StoreWriteError
from llm.config import LLMConfigurationError
from llm.retrieval_models import EmbeddingAPIError, RerankAPIError
from memory.schemas import MemoryError, MemoryStoreReadError, MemoryStoreWriteError
from source_discovery.schemas import SourceDiscoveryResult
from triage.schemas import TriageResult

from .adapters import ModuleAdapters
from .documents import OwnProductDocumentService
from .output import build_product_result, present_research_action
from .schemas import PipelineFailure, ProductInput
from .statements import INITIAL_BACKFILL_MAX_EVENTS, select_initial_backfill
from .store import PipelineStore, identity, product_event_id


_PROCESS_TOKEN = str(uuid4())


class ProductPipeline:
    def __init__(self, directory: str | Path, *, adapter_type=ModuleAdapters):
        self.store = PipelineStore(directory)
        self.adapter_type = adapter_type

    def configure_competitor(self, product: ProductInput):
        """确定工作区/竞品内部键并保存配置；Monitoring 不读取或解析自身文件。"""
        product = ProductInput.model_validate(product.model_dump())
        workspace = identity([product.workspace_id, product.competitor_name])
        adapter = self.adapter_type(self.store.directory, product)
        self.store.put("workspaces", workspace, {"config": product.model_dump(), "stores": adapter.store_references()})
        return workspace, adapter

    def confirm_monitoring_sources(self, product: ProductInput, result: SourceDiscoveryResult) -> None:
        """保存正式发现结果或调用方明确确认的来源；单纯输入 URL 不会自动获得此资格。

        这是本地可信调用方接口，也用于受控演示页面的显式确认，不伪造模型发现轨迹。
        官方身份保留结果中的域声明；未声明的域不能被集成层自动升级为官方。
        """
        workspace, _ = self.configure_competitor(product)
        result = SourceDiscoveryResult.model_validate(result.model_dump())
        if result.competitor_name != product.competitor_name:
            raise PipelineFailure("configuration_error")
        sources = []
        for s in result.sources:
            ProductInput(workspace_id=product.workspace_id, competitor_name=product.competitor_name, official_url=s.url)
            data = s.model_dump(mode="json")
            data["credibility"] = "official" if urlsplit(s.url).hostname in result.official_domains else "unverified"
            data["official_domains"] = result.official_domains
            sources.append(data)
        self.store.put("sources", workspace, {**result.model_dump(mode="json"), "sources": sources})

    def remember_explicit(self, product: ProductInput, text: str) -> dict:
        """显式用户 Memory 操作独立于普通主流程；保存底层返回的确认状态与运行审计。"""
        workspace, adapter = self.configure_competitor(product)
        output = adapter.remember_explicit(text)
        self.store.put("memory_writes:" + workspace, identity(text), output)
        return output

    def _step(self, run_id, key, label, category, action):
        """执行一步并持久化成功/失败检查点；重试只跳过成功步骤，异常正文不落产品结果。"""
        namespace = "steps:" + run_id
        old = self.store.get(namespace, key)
        if old and old["status"] == "complete":
            return old["data"]
        stage = {"label": label, "status": "running", "attempts": (old or {}).get("attempts", 0) + 1,
                 "failures": (old or {}).get("failures", [])}
        self.store.put(namespace, key, stage)
        started = perf_counter()
        try:
            data = action()
        except Exception as exc:
            if isinstance(exc, PipelineFailure):
                category = exc.category
            elif isinstance(exc, LLMConfigurationError) or type(exc).__name__ in {"AgentConfigurationError", "AgentToolConfigurationError"}:
                category = "configuration_error"
            elif isinstance(exc, (EmbeddingAPIError, RerankAPIError, KnowledgeError, StoreReadError, StoreWriteError,
                                  MemoryError, MemoryStoreReadError, MemoryStoreWriteError)):
                category = "retrieval_error"
            stage.update(status="failed", error_type=category, exception_type=type(exc).__name__)
            stage["failures"].append({"type": category, "exception_type": type(exc).__name__})
            self.store.put(namespace, key, stage)
            raise PipelineFailure(category) from None
        stage.update(status="complete", data=data, latency_seconds=round(perf_counter() - started, 3))
        self.store.put(namespace, key, stage)
        return data

    def _artifact(self, run_id, workspace, kind, key, category, action):
        """跨运行复用同一上下文下的产物；返回引用而不是复制第二份研究结果。"""
        namespace = kind + ":" + workspace

        def create():
            found = self.store.get(namespace, key)
            if found is None:
                self.store.put(namespace, key, action())
            return {"namespace": namespace, "key": key, "reused": found is not None}

        reference = self._step(run_id, kind + ":" + key, kind, category, create)
        self.store.put("run_artifacts:" + run_id, kind + ":" + key, {**reference, "kind": kind})
        return self.store.get(namespace, key)

    def run_monitoring_scan(self, run_id, workspace, adapter, source):
        """保存观察并完成一次状态比较；只有 Event Core 正式终态才推进处理水位。"""
        key = identity([run_id, source["url"]])

        def scan():
            cached = self.store.get("snapshots", key)
            return cached or self.store.record_scan(workspace, run_id, source["url"], adapter.scan(source["url"]))

        history = self._step(run_id, "scan:" + key, "Stage 1 扫描与快照", "scan_error", scan)
        if history["is_first_scan"]:
            self._step(
                run_id, "baseline:" + key, "首次扫描建立基线", "scan_error",
                lambda: {"event_detection_skipped": True, **self.store.mark_comparison_processed(
                    workspace, source["url"], history, ["baseline"]
                )},
            )
            return history, []
        inputs = self._step(run_id, "comparison:" + key, "状态比较与 Contextual Diff", "event_detection_error",
                            lambda: [i.model_dump(mode="json") for i in adapter.detection_inputs(history, source)])
        candidates = []
        for index, data in enumerate(inputs):
            result = self._step(run_id, f"detection:{key}:{index}", "Event Detection", "event_detection_error",
                                lambda data=data: adapter.detect(DetectionInput.model_validate(data)).model_dump(mode="json"))
            candidates.extend(EventCandidate.model_validate(c) for c in result["candidates"])
        outcomes = [candidate.decision for candidate in candidates]
        for candidate in candidates:
            if candidate.decision == "uncertain":
                self._step(
                    run_id, "pending:" + candidate.candidate_id, "保存待核验候选", "event_detection_error",
                    lambda candidate=candidate: self._save_pending_detection(workspace, candidate),
                )
            elif candidate.decision == "event":
                # confirmed candidate 必须先完成 Dedup 并持久化 Event（或 Dedup Pending），
                # 才能推进 comparison watermark。Triage 等下游动作不属于这个安全边界。
                self._step(
                    run_id, "event_outcome:" + candidate.candidate_id, "持久化正式事件与去重结果",
                    "event_detection_error",
                    lambda candidate=candidate: self._persist_event_outcome(run_id, workspace, adapter, candidate),
                )
        self._step(
            run_id, "processed:" + key, "完成事件判断并推进比较水位", "event_detection_error",
            lambda: self.store.mark_comparison_processed(workspace, source["url"], history, outcomes),
        )
        return history, candidates

    def _save_pending_detection(
        self, workspace, candidate, discovery_origin="normal_monitoring"
    ):
        """在推进比较水位前持久化 uncertain，避免崩溃后既无变化也无 Pending。"""
        self._pending(workspace, candidate, candidate.uncertainty_reason, discovery_origin)
        return {"candidate_id": candidate.candidate_id, "status": "pending"}

    def _persist_event_outcome(
        self, run_id, workspace, adapter, candidate, discovery_origin="normal_monitoring"
    ):
        """完成 Event/Dedup 的可恢复持久化，但不执行 Knowledge、Triage 或 Research。

        输入是 Event Core 已确认的 candidate。方法先复用或执行正式 Dedup；模糊结果写入
        Pending，明确结果写入 workspace Event、当前 run Event 和 downstream action。
        这些记录成功后 comparison 才有资格推进。输出是可供后续动作恢复的持久化结果。
        """
        existing = self.store.items("events:" + workspace)
        if candidate.candidate_id in existing and candidate.model_dump(mode="json") == existing[candidate.candidate_id]:
            event = candidate
        else:
            verdict = self._step(
                run_id, "dedup:" + candidate.candidate_id, "事件去重", "event_detection_error",
                lambda: adapter.deduplicate(
                    candidate, [EventCandidate.model_validate(c) for c in existing.values()]
                ).model_dump(mode="json"),
            )
            if verdict["decision"] == "UNCERTAIN":
                self._pending(workspace, candidate, "事件事实已确认，但与历史事件的同一性仍待核验。")
                return {"status": "pending_dedup", "candidate_id": candidate.candidate_id}
            event = EventCandidate.model_validate(verdict["event"])

        event_data = event.model_dump(mode="json")
        self.store.put("events:" + workspace, event.candidate_id, event_data)
        self.store.put("run_events:" + run_id, event.candidate_id, event_data)
        self.store.put("run_event_origins:" + run_id, event.candidate_id,
                       {"discovery_origin": discovery_origin})
        action_namespace = "event_actions:" + workspace
        action = self.store.get(action_namespace, event.candidate_id)
        if action is None or action.get("event") != event_data:
            self.store.put(action_namespace, event.candidate_id, {
                "status": "pending", "event": event_data, "originating_run_id": run_id,
                "discovery_origin": discovery_origin,
            })
        return {"status": "event_persisted", "event": event_data}

    def _confirmed_event(
        self, run_id, workspace, product, adapter, candidate, discovery_origin="normal_monitoring"
    ):
        """复用已持久化的正式 Event，再执行可独立恢复的下游动作。"""
        outcome = self._step(
            run_id, "event_outcome:" + candidate.candidate_id, "持久化正式事件与去重结果",
            "event_detection_error",
            lambda: self._persist_event_outcome(
                run_id, workspace, adapter, candidate, discovery_origin
            ),
        )
        if outcome["status"] != "event_persisted":
            return
        event = EventCandidate.model_validate(outcome["event"])
        self._step(run_id, "admit:" + identity(event.model_dump(mode="json")), "确认原始证据进入竞品知识", "retrieval_error",
                    lambda: adapter.ingest_event(event))
        context = {"event": event.model_dump(mode="json"), "product": product.model_dump()}
        # 自身知识属于独立配置生命周期；只读取持久化 Registry 身份，不触碰上传文件。
        context["documents"] = adapter.own_product_context_identity()
        key = identity(context)
        triage = self._artifact(run_id, workspace, "triage", key, "triage_error", lambda: adapter.triage(event))
        decision = TriageResult.model_validate(triage["result"])
        self._ensure_research_state(workspace, event, key, decision)
        action = self.store.get("event_actions:" + workspace, event.candidate_id, {"event": outcome["event"]})
        self.store.put("event_actions:" + workspace, event.candidate_id,
                       {**action, "status": "complete", "completed_run_id": run_id})

    def _resume_event_actions(self, run_id, workspace, product, adapter):
        """新 run 恢复已经持久化、但 Knowledge/Triage/Research 尚未完成的 Event 动作。"""
        for action in self.store.items("event_actions:" + workspace).values():
            if action["status"] != "complete":
                self._confirmed_event(run_id, workspace, product, adapter,
                                      EventCandidate.model_validate(action["event"]),
                                      action.get("discovery_origin", "normal_monitoring"))

    def _ensure_research_state(self, workspace, event, triage_key, decision):
        """为 research 推荐建立独立 not_started 状态；monitor/no_action 不创建任务。"""
        if decision.decision != "research":
            return None
        event_data = event.model_dump(mode="json")
        event_id = product_event_id(event_data)
        namespace = "research_status:" + workspace
        state = self.store.get(namespace, event_id)
        if state is not None:
            return state
        artifact = self.store.get("research:" + workspace, triage_key)
        state = {
            "event_id": event_id, "event_candidate_id": event.candidate_id,
            "triage": {"namespace": "triage:" + workspace, "key": triage_key},
            "status": "completed" if artifact is not None else "not_started", "attempts": 0,
        }
        if artifact is not None:
            state["artifact"] = {"namespace": "research:" + workspace, "key": triage_key}
        self.store.put(namespace, event_id, state)
        return state

    def _research_context(self, workspace, event):
        """读取或从既有 Triage artifact 恢复 Event 对应的 research 推荐状态。"""
        event_id = product_event_id(event)
        state = self.store.get("research_status:" + workspace, event_id)
        if state is not None:
            reference = state["triage"]
            artifact = self.store.get(reference["namespace"], reference["key"])
            return state, TriageResult.model_validate(artifact["result"]) if artifact else None
        for triage_key, artifact in reversed(list(self.store.items("triage:" + workspace).items())):
            result = TriageResult.model_validate(artifact["result"])
            if result.event_id == event.candidate_id and result.decision == "research":
                return self._ensure_research_state(workspace, event, triage_key, result), result
        return None, None

    def start_research(self, product: ProductInput, event_id: str, *, retry: bool = False) -> dict:
        """用户显式启动单个 confirmed Event 的 Deep Research。

        输入只有产品配置、产品级 event_id 和明确 retry 标志；程序从持久化 Event 与
        Triage 恢复真实内部对象。not_started/failed-retry 才会调用现有 Research Agent，
        running/completed 均幂等返回。Research 失败只更新独立 action，不修改主 Pipeline。
        """
        workspace, adapter = self.configure_competitor(product)
        event = next((EventCandidate.model_validate(value)
                      for value in self.store.items("events:" + workspace).values()
                      if product_event_id(value) == event_id), None)
        if event is None:
            return {"product": {"accepted": False, "reason": "confirmed_event_not_found"}, "audit": {}}
        state, triage = self._research_context(workspace, event)
        if state is None or triage is None or triage.decision != "research":
            return {"product": {"accepted": False, "event_id": event_id,
                                "reason": "research_not_recommended"}, "audit": {}}

        namespace = "research_status:" + workspace
        # 状态判断与 running 写入必须在同一个 SQLite 事务中完成；这里不能复用上方
        # 为恢复 Triage 而读取的 state，否则两个请求可能同时依据 not_started 启动 Agent。
        claim = self.store.claim_research(workspace, event_id, _PROCESS_TOKEN, retry=retry)
        state = claim["state"]
        if state is None:
            return {"product": {"accepted": False, "reason": "research_state_not_found"}, "audit": {}}
        if not claim["claimed"] and state["status"] == "completed":
            return {"product": {"accepted": True, "research": present_research_action(self.store, workspace, state)},
                    "audit": {"status": "completed", "reused": True}}
        if not claim["claimed"] and state["status"] == "running":
            return {"product": {"accepted": True, "research": present_research_action(self.store, workspace, state)},
                    "audit": {"status": "running", "reused": True}}
        if not claim["claimed"] and state["status"] == "failed":
            return {"product": {"accepted": True, "research": present_research_action(self.store, workspace, state)},
                    "audit": {"status": "failed", "retry_required": True}}
        running = state
        try:
            artifact = adapter.research(event, "deep_research", triage, self._official_hosts(workspace, event))
        except Exception as exc:
            failed = {**running, "status": "failed", "error_type": "research_error",
                      "exception_type": type(exc).__name__}
            failed.pop("process_token", None)
            self.store.put(namespace, event_id, failed)
            return {"product": {"accepted": True, "research": present_research_action(self.store, workspace, failed)},
                    "audit": {"status": "failed", "exception_type": type(exc).__name__}}

        artifact_key = running["triage"]["key"]
        artifact_reference = {"namespace": "research:" + workspace, "key": artifact_key}
        self.store.put(artifact_reference["namespace"], artifact_key, artifact)
        completed = {**running, "status": "completed", "artifact": artifact_reference}
        completed.pop("process_token", None)
        self.store.put(namespace, event_id, completed)
        return {"product": {"accepted": True, "research": present_research_action(self.store, workspace, completed)},
                "audit": {"status": "completed", "artifact": artifact}}

    def _official_hosts(self, workspace, candidate):
        """沿用正式发现或上游 Evidence 已确认的来源身份；回放事件也保留同一信任边界。"""
        sources = self.store.get("sources", workspace, {})
        return sorted(set(sources.get("official_domains", [])) | {
            urlsplit(e.source_url).hostname for e in candidate.evidence if e.source_credibility == "official"
        } - {None})

    def _pending(self, workspace, candidate, reason, discovery_origin="normal_monitoring"):
        self.store.put("pending:" + workspace, candidate.candidate_id,
                       {"status": "pending", "reason": reason, "candidate": candidate.model_dump(mode="json"),
                        "discovery_origin": discovery_origin})

    def _verify(
        self, run_id, workspace, product, adapter, candidate,
        discovery_origin="normal_monitoring",
    ):
        """核验最多一轮；event/no_event 都必须携正式新证据重新进入 Event Core。"""
        resolved = self.store.get("resolved:" + workspace, candidate.candidate_id)
        if resolved:
            for data in resolved.get("events", []):
                self._confirmed_event(
                    run_id, workspace, product, adapter, EventCandidate.model_validate(data),
                    resolved.get("discovery_origin", discovery_origin),
                )
            return
        self._pending(workspace, candidate, candidate.uncertainty_reason, discovery_origin)
        key = identity([run_id, candidate.candidate_id])
        verification = self._artifact(run_id, workspace, "verification", key, "research_error",
            lambda: adapter.research(candidate, "verification", None, self._official_hosts(workspace, candidate)))
        decision = verification["result"]["decision"]
        if decision == "still_pending":
            self._pending(workspace, candidate, verification["result"]["reason"], discovery_origin)
            return
        inputs = self._step(
            run_id, "verification_inputs:" + key, "核验原文适配", "event_detection_error",
            lambda: [i.model_dump(mode="json") for i in adapter.verification_inputs(candidate, verification)],
        )
        if not inputs:
            self._pending(
                workspace, candidate, "核验没有形成可交给 Event Core 的充分官方新证据，保留待处理。",
                discovery_origin,
            )
            return
        formal = []
        for index, data in enumerate(inputs):
            output = self._step(run_id, f"redetect:{key}:{index}", "核验后正式事件检测", "event_detection_error",
                lambda data=data: adapter.detect(DetectionInput.model_validate(data)).model_dump(mode="json"))
            formal.extend(output["candidates"])
        confirmed = [item for item in formal if item["decision"] == "event"]
        uncertain = [item for item in formal if item["decision"] == "uncertain"]
        if not confirmed and uncertain:
            self._pending(workspace, candidate, "Event Core 仍为 uncertain，保留待处理。", discovery_origin)
            return
        if not confirmed and (not formal or any(item["decision"] != "no_event" for item in formal)):
            self._pending(
                workspace, candidate, "Event Core 未形成可解决候选的正式结论，保留待处理。",
                discovery_origin,
            )
            return
        for data in confirmed:
            self._confirmed_event(
                run_id, workspace, product, adapter, EventCandidate.model_validate(data), discovery_origin
            )
        formal_decision = "event" if confirmed else "no_event"
        resolved_status = "resolved_event" if confirmed else "resolved_no_event"
        self.store.put("resolved:" + workspace, candidate.candidate_id,
                       {"decision": formal_decision, "status": resolved_status, "events": confirmed,
                        "discovery_origin": discovery_origin})
        self.store.put("pending:" + workspace, candidate.candidate_id,
                       {"status": resolved_status, "candidate": candidate.model_dump(mode="json"),
                        "reason": verification["result"]["reason"],
                        "discovery_origin": discovery_origin})

    def _statement_plan(self, workspace, extracted: list[dict]) -> list[dict]:
        """合并失败重试、后续新声明和有限 Initial Backfill，生成本次 Path B 计划。

        已持久化 pending statement 优先重试，不消耗新的 Backfill 名额。首次候选先按可靠
        日期筛选最近 90 天、全竞品按时间倒序取剩余名额；后续只接受相对上一 Snapshot
        新增的结构单元。这里不读取或修改 Path A comparison watermark。
        """
        # Observation 必须先覆盖本次成功读取到的全部结构 statement，不能只记录最终入选
        # Backfill 的 20 条。processing state 仍只属于真正进入 Event Core 的声明。
        observations = self.store.observe_statements(workspace, extracted)
        states = self.store.items("statement_processing:" + workspace)
        planned = {
            key: value for key, value in states.items() if value.get("status") == "pending"
        }
        initial, normal = [], []
        for item in extracted:
            old = states.get(item["statement_id"])
            if old is not None:
                continue
            if item["initial_backfill"]:
                initial.append(item)
            elif not observations[item["statement_id"]]["was_seen"]:
                normal.append({**item, "discovery_origin": "normal_monitoring"})

        used_backfill = sum(
            value.get("discovery_origin") == "historical_backfill" for value in states.values()
        )
        available = max(0, INITIAL_BACKFILL_MAX_EVENTS - used_backfill)
        if initial and available:
            reference_at = max(item["input"]["current_observed_at"] for item in initial)
            for item in select_initial_backfill(initial, reference_at, available):
                planned[item["statement_id"]] = {**item, "discovery_origin": "historical_backfill"}
        for item in normal:
            planned[item["statement_id"]] = item
        return list(planned.values())

    def _process_explicit_statements(self, run_id, workspace, product, adapter, statements):
        """逐条执行 Path B，并让成功与失败 statement 拥有独立恢复边界。

        每条输入先注册为 pending。Event 必须先完成正式 Event/Dedup persistence，uncertain
        必须先保存 Pending Candidate，之后才能把 statement 标记 processed。单条失败会
        保留 pending 并继续处理其余声明；最后再把首个业务错误返回给本次 Pipeline。
        """
        failures: list[PipelineFailure] = []
        for statement in statements:
            state = self.store.register_statement(workspace, statement)
            if state["status"] == "processed":
                continue
            statement_id = state["statement_id"]
            try:
                result = self._step(
                    run_id, "statement_detection:" + statement_id,
                    "Explicit Statement Event Detection", "event_detection_error",
                    lambda state=state: adapter.detect(
                        DetectionInput.model_validate(state["input"])
                    ).model_dump(mode="json"),
                )
                candidates = [EventCandidate.model_validate(item) for item in result["candidates"]]
                outcomes = [candidate.decision for candidate in candidates]
                event_ids = []
                for candidate in candidates:
                    if candidate.decision == "uncertain":
                        self._step(
                            run_id, "statement_pending:" + candidate.candidate_id,
                            "保存 Path B 待核验候选", "event_detection_error",
                            lambda candidate=candidate, state=state: self._save_pending_detection(
                                workspace, candidate, state["discovery_origin"]
                            ),
                        )
                    elif candidate.decision == "event":
                        outcome = self._step(
                            run_id, "event_outcome:" + candidate.candidate_id,
                            "持久化 Path B 正式事件与去重结果", "event_detection_error",
                            lambda candidate=candidate, state=state: self._persist_event_outcome(
                                run_id, workspace, adapter, candidate, state["discovery_origin"]
                            ),
                        )
                        if outcome["status"] == "event_persisted":
                            event_ids.append(EventCandidate.model_validate(outcome["event"]).candidate_id)
                self.store.mark_statement_processed(workspace, statement_id, outcomes, event_ids)
                self.process_event_candidates(
                    run_id, workspace, product, adapter, candidates,
                    discovery_origin=state["discovery_origin"],
                )
            except PipelineFailure as exc:
                failures.append(exc)
        if failures:
            raise failures[0]

    def process_event_candidates(
        self, run_id, workspace, product, adapter, candidates,
        discovery_origin="normal_monitoring",
    ):
        """按正式候选状态分流；no_event 结束，uncertain 核验，event 进入确认链路。"""
        for candidate in candidates:
            candidate = EventCandidate.model_validate(candidate.model_dump())
            if candidate.competitor != product.competitor_name:
                raise PipelineFailure("configuration_error")
            if candidate.decision == "event":
                self._confirmed_event(
                    run_id, workspace, product, adapter, candidate, discovery_origin
                )
            elif candidate.decision == "uncertain":
                self._verify(
                    run_id, workspace, product, adapter, candidate, discovery_origin
                )

    async def run(self, product: ProductInput) -> dict:
        """用户主入口：配置→来源发现/已确认来源→扫描→候选后续路由。"""
        return await self._start(product, "monitor", {})

    def add_own_product_document(self, *, workspace_id: str, product_id: str,
                                 file: str | Path, display_name: str | None = None):
        """独立 Workspace Configuration 入口；不会启动 Monitoring 或 Intelligence。"""
        return OwnProductDocumentService(self.store.directory).add_own_product_document(
            workspace_id=workspace_id, product_id=product_id, file=file,
            display_name=display_name,
        )

    def replace_own_product_document(self, *, workspace_id: str, product_id: str,
                                     document_id: str, new_file: str | Path):
        """显式替换一个逻辑产品文档；document_id 在更新前后保持不变。"""
        return OwnProductDocumentService(self.store.directory).replace_own_product_document(
            workspace_id=workspace_id, product_id=product_id,
            document_id=document_id, new_file=new_file,
        )

    async def run_events(self, product: ProductInput, candidates: list[EventCandidate], *, provenance: str) -> dict:
        """从已有正式事件回放后半链路，供可信调用方接入或低成本演示；明确记录未重跑采集。"""
        if not provenance.strip():
            raise PipelineFailure("configuration_error")
        return await self._start(product, "event_replay", {"candidates": [c.model_dump(mode="json") for c in candidates],
                                                           "provenance": provenance})

    async def _start(self, product, mode, payload):
        run_id = str(uuid4())
        self.store.put("runs", run_id, {"product": product.model_dump(), "mode": mode, "payload": payload})
        return await self.resume(run_id)

    async def resume(self, run_id: str) -> dict:
        """读取持久化运行并继续未完成步骤；已完成运行直接返回，不再次调用模型或扫描。"""
        saved = self.store.get("results", run_id)
        if saved and saved["product"]["status"] != "failed":
            return saved
        run = self.store.get("runs", run_id)
        if run is None:
            raise PipelineFailure("configuration_error")
        product = ProductInput.model_validate(run["product"])
        workspace = identity([product.workspace_id, product.competitor_name])
        error = None
        try:
            self._step(run_id, "configuration", "工作区与竞品配置", "configuration_error",
                       lambda: {"workspace": self.configure_competitor(product)[0]})
            adapter = self.adapter_type(self.store.directory, product)
            if run["mode"] == "monitor":
                if self.store.get("sources", workspace) is None:
                    # Discovery 是已有异步入口；完成结果先保存，再执行后续同步步骤。
                    try:
                        discovered = await adapter.discover()
                        self.confirm_monitoring_sources(product, discovered)
                        self._step(run_id, "discovery", "Source Discovery", "source_discovery_error",
                                   lambda: discovered.model_dump(mode="json"))
                    except Exception as exc:
                        def failed(exc=exc):
                            raise exc
                        self._step(run_id, "discovery", "Source Discovery", "source_discovery_error", failed)
                sources = self.store.get("sources", workspace)
                if not sources["sources"]:
                    def no_sources():
                        raise PipelineFailure("source_discovery_error")
                    self._step(run_id, "source_availability", "检查可监控来源", "source_discovery_error", no_sources)
                # Event 已持久化后，Triage 等失败由独立 action 恢复；不要求再次制造 A → B。
                self._resume_event_actions(run_id, workspace, product, adapter)
                extracted_statements = []
                for source in sources["sources"]:
                    # FastAPI 等异步入口已经运行 Event Loop，而 Stage 1 浏览器采集使用
                    # Playwright 同步 API。把完整的单来源扫描步骤放到工作线程，既避免
                    # 同步 Playwright 与 Event Loop 冲突，也保留原有检查点和失败语义。
                    history, candidates = await asyncio.to_thread(
                        self.run_monitoring_scan, run_id, workspace, adapter, source
                    )
                    self.process_event_candidates(run_id, workspace, product, adapter, candidates)
                    statement_key = "statement_extraction:" + history["snapshot_id"]
                    statements = self._step(
                        run_id, statement_key, "提取 Explicit Statement Candidates",
                        "event_detection_error",
                        lambda history=history, source=source: adapter.explicit_statement_candidates(
                            history, source
                        ),
                    )
                    extracted_statements.extend(statements)
                self._process_explicit_statements(
                    run_id, workspace, product, adapter,
                    self._statement_plan(workspace, extracted_statements),
                )
            else:
                payload = self._step(run_id, "replay", "复用已确认 Event/Evidence（未重跑发现或采集）", "configuration_error",
                                     lambda: run["payload"])
                self.process_event_candidates(
                    run_id, workspace, product, adapter,
                    [EventCandidate.model_validate(c) for c in payload["candidates"]],
                    discovery_origin="event_replay",
                )
        except PipelineFailure as exc:
            error = exc.category
        except Exception as exc:
            error = "configuration_error"
            self.store.put("steps:" + run_id, "configuration", {"label": "配置或持久化", "status": "failed",
                "error_type": error, "exception_type": type(exc).__name__})
        result = build_product_result(self.store, run_id, product, workspace, error)
        output = {"run_id": run_id, "product": result.model_dump(), "audit": {"steps": self.store.items("steps:" + run_id),
                  "artifacts": {k: self.store.get(v["namespace"], v["key"]) for k, v in self.store.items("run_artifacts:" + run_id).items()}}}
        self.store.put("results", run_id, output)
        return output
