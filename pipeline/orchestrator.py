"""显式产品 Workflow：接线、状态保存和路由，不承担新的 AI 判断。"""

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
from .output import build_product_result
from .schemas import PipelineFailure, ProductInput
from .store import PipelineStore, identity


class ProductPipeline:
    def __init__(self, directory: str | Path, *, adapter_type=ModuleAdapters):
        self.store = PipelineStore(directory)
        self.adapter_type = adapter_type

    def configure_competitor(self, product: ProductInput):
        """确定工作区/竞品内部键并保存配置；文件资格在产生模型请求前检查。"""
        product = ProductInput.model_validate(product.model_dump())
        for name in product.own_product_documents:
            path = Path(name)
            if not path.is_file() or path.suffix.lower() not in {".md", ".txt"}:
                raise PipelineFailure("configuration_error")
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
        """一个已确认来源一次扫描；先保存快照历史，再根据首次标记决定是否检测。"""
        key = identity([run_id, source["url"]])

        def scan():
            cached = self.store.get("snapshots", key)
            return cached or self.store.record_scan(workspace, run_id, source["url"], adapter.scan(source["url"]))

        history = self._step(run_id, "scan:" + key, "Stage 1 扫描与快照", "scan_error", scan)
        if history["is_first_scan"]:
            self._step(run_id, "baseline:" + key, "首次扫描建立基线", "scan_error", lambda: {"event_detection_skipped": True})
            return []
        inputs = self._step(run_id, "comparison:" + key, "状态比较与 Contextual Diff", "event_detection_error",
                            lambda: [i.model_dump(mode="json") for i in adapter.detection_inputs(history, source)])
        candidates = []
        for index, data in enumerate(inputs):
            result = self._step(run_id, f"detection:{key}:{index}", "Event Detection", "event_detection_error",
                                lambda data=data: adapter.detect(DetectionInput.model_validate(data)).model_dump(mode="json"))
            candidates.extend(EventCandidate.model_validate(c) for c in result["candidates"])
        return candidates

    def _confirmed_event(self, run_id, workspace, product, adapter, candidate):
        """正式事件先复用语义去重，再入库和分诊；只有 research 路由执行深度研究。"""
        existing = self.store.items("events:" + workspace)
        if candidate.candidate_id in existing and candidate.model_dump(mode="json") == existing[candidate.candidate_id]:
            event = candidate
        else:
            verdict = self._step(run_id, "dedup:" + candidate.candidate_id, "事件去重", "event_detection_error",
                lambda: adapter.deduplicate(candidate, [EventCandidate.model_validate(c) for c in existing.values()]).model_dump(mode="json"))
            if verdict["decision"] == "UNCERTAIN":
                self._pending(workspace, candidate, "事件事实已确认，但与历史事件的同一性仍待核验。")
                return
            event = EventCandidate.model_validate(verdict["event"])
        self.store.put("events:" + workspace, event.candidate_id, event.model_dump(mode="json"))
        self.store.put("run_events:" + run_id, event.candidate_id, event.model_dump(mode="json"))
        self._step(run_id, "admit:" + identity(event.model_dump(mode="json")), "确认原始证据进入竞品知识", "retrieval_error",
                    lambda: adapter.ingest_event(event))
        context = {"event": event.model_dump(mode="json"), "product": product.model_dump()}
        # 文档路径相同但正文更新时，重新分诊是新上下文，不是重复运行。
        context["documents"] = [identity(Path(p).read_text(encoding="utf-8-sig")) for p in product.own_product_documents]
        key = identity(context)
        triage = self._artifact(run_id, workspace, "triage", key, "triage_error", lambda: adapter.triage(event))
        decision = TriageResult.model_validate(triage["result"])
        if decision.decision == "research":
            self._artifact(run_id, workspace, "research", key, "research_error",
                lambda: adapter.research(event, "deep_research", decision, self._official_hosts(workspace, event)))

    def _official_hosts(self, workspace, candidate):
        """沿用正式发现或上游 Evidence 已确认的来源身份；回放事件也保留同一信任边界。"""
        sources = self.store.get("sources", workspace, {})
        return sorted(set(sources.get("official_domains", [])) | {
            urlsplit(e.source_url).hostname for e in candidate.evidence if e.source_credibility == "official"
        } - {None})

    def _pending(self, workspace, candidate, reason):
        self.store.put("pending:" + workspace, candidate.candidate_id,
                       {"status": "pending", "reason": reason, "candidate": candidate.model_dump(mode="json")})

    def _verify(self, run_id, workspace, product, adapter, candidate):
        """核验最多一轮；支持确认时经 Event Core 再构造正式事件，不能循环自我确认。"""
        resolved = self.store.get("resolved:" + workspace, candidate.candidate_id)
        if resolved:
            for data in resolved.get("events", []):
                self._confirmed_event(run_id, workspace, product, adapter, EventCandidate.model_validate(data))
            return
        self._pending(workspace, candidate, candidate.uncertainty_reason)
        key = identity([run_id, candidate.candidate_id])
        verification = self._artifact(run_id, workspace, "verification", key, "research_error",
            lambda: adapter.research(candidate, "verification", None, self._official_hosts(workspace, candidate)))
        decision = verification["result"]["decision"]
        if decision == "still_pending":
            self._pending(workspace, candidate, verification["result"]["reason"])
            return
        confirmed = []
        if decision == "event":
            inputs = self._step(run_id, "verification_inputs:" + key, "核验原文适配", "event_detection_error",
                lambda: [i.model_dump(mode="json") for i in adapter.verification_inputs(candidate, verification)])
            for index, data in enumerate(inputs):
                output = self._step(run_id, f"redetect:{key}:{index}", "核验后正式事件检测", "event_detection_error",
                    lambda data=data: adapter.detect(DetectionInput.model_validate(data)).model_dump(mode="json"))
                confirmed.extend(c for c in output["candidates"] if c["decision"] == "event")
            if not confirmed:
                self._pending(workspace, candidate, "核验倾向确认，但正式事件检测未生成确认事件，保留待处理。")
                return
        for data in confirmed:
            self._confirmed_event(run_id, workspace, product, adapter, EventCandidate.model_validate(data))
        self.store.put("resolved:" + workspace, candidate.candidate_id, {"decision": decision, "events": confirmed})
        self.store.put("pending:" + workspace, candidate.candidate_id,
                       {"status": "resolved", "candidate": candidate.model_dump(mode="json"), "reason": verification["result"]["reason"]})

    def process_event_candidates(self, run_id, workspace, product, adapter, candidates):
        """按正式候选状态分流；no_event 结束，uncertain 核验，event 进入确认链路。"""
        for candidate in candidates:
            candidate = EventCandidate.model_validate(candidate.model_dump())
            if candidate.competitor != product.competitor_name:
                raise PipelineFailure("configuration_error")
            if candidate.decision == "event":
                self._confirmed_event(run_id, workspace, product, adapter, candidate)
            elif candidate.decision == "uncertain":
                self._verify(run_id, workspace, product, adapter, candidate)

    async def run(self, product: ProductInput) -> dict:
        """用户主入口：配置→来源发现/已确认来源→扫描→候选后续路由。"""
        return await self._start(product, "monitor", {})

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
            self._step(run_id, "own_knowledge", "自身资料入库", "retrieval_error", adapter.ingest_own)
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
                for source in sources["sources"]:
                    candidates = self.run_monitoring_scan(run_id, workspace, adapter, source)
                    self.process_event_candidates(run_id, workspace, product, adapter, candidates)
            else:
                payload = self._step(run_id, "replay", "复用已确认 Event/Evidence（未重跑发现或采集）", "configuration_error",
                                     lambda: run["payload"])
                self.process_event_candidates(run_id, workspace, product, adapter,
                    [EventCandidate.model_validate(c) for c in payload["candidates"]])
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
