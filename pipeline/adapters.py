"""真实模块的薄适配：没有新模型、提示词、检索算法或事件判定规则。"""

from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from backend.page_reader import acquire_page_with_browser_fallback
from backend.snapshot import create_snapshot
from backend.state_matching import match_interactive_states
from event_core import deduplicate_event, detect_event
from event_core.inputs import build_explicit_statement_input, build_state_comparison_input
from knowledge import KnowledgeIngestor, KnowledgeRetriever, KnowledgeStore
from knowledge.inputs import event_documents, own_product_file
from llm.retrieval_models import get_embedding, get_rerank
from memory import MemoryRetriever, MemoryStore, MemoryWriter, PreferenceService
from memory.policy import contains_secret
from research.agent import ResearchAgent
from research.tools import ResearchTools
from source_discovery.agent import run_source_discovery
from source_discovery.search.tencent_wsa import TencentWsaSearchProvider
from triage.workflow import TriageWorkflow

from .documents import product_scope_id, workspace_knowledge_path
from .store import identity
from .statements import extract_statement_candidates


class ModuleAdapters:
    """只提供与真实公共函数一一对应的操作，测试可替换外部调用。"""

    def __init__(self, directory: str | Path, product):
        self.directory = Path(directory)
        self.product = product
        self.product_id = product_scope_id(product.workspace_id, product.own_product_id)
        self.competitor_id = identity([product.workspace_id, product.competitor_name])
        self.knowledge_path = workspace_knowledge_path(self.directory, product.workspace_id)
        self.memory_path = self.directory / (identity(product.workspace_id) + ".memory.sqlite3")

    @property
    def scope(self):
        return dict(product_id=self.product_id, competitor_id=self.competitor_id,
                    memory_scope="workspace", memory_scope_id=self.product.workspace_id)

    def store_references(self) -> dict:
        """两知识域共用冻结的分域知识库，Memory 独立；地址仅存内部状态。"""
        return {"own_product_knowledge": str(self.knowledge_path), "competitor_knowledge": str(self.knowledge_path),
                "memory": str(self.memory_path)}

    async def discover(self):
        return await run_source_discovery(self.product.competitor_name, self.product.official_url,
                                          self.product.additional_urls)

    def scan(self, url: str) -> dict:
        """真实采集并执行冻结 Quality Gate，通过后才调用正式快照构造器。"""
        result = acquire_page_with_browser_fallback(url)
        if not result["quality_gate"]["downstream_allowed"]:
            raise ValueError("页面质量不允许进入后续流程。")
        return create_snapshot(result["page_data"], traversal_result=result.get("traversal_result"))

    def detection_inputs(self, history: dict, source: dict) -> list:
        """复用页面及交互状态适配器；首次扫描由上层跳过，不让模型决定基线资格。"""
        final_host = urlsplit(history["current_snapshot"]["final_url"]).hostname
        official_hosts = source.get("official_domains", [urlsplit(source["url"]).hostname])
        credibility = "official" if source["credibility"] == "official" and final_host in official_hosts else "unverified"
        args = dict(competitor=self.product.competitor_name, source_type=source["source_type"],
                    source_credibility=credibility)
        # unchanged 也交给 Event Core 的确定性边界，得到正式 no_event 后才能推进
        # processed watermark；该分支不会调用 LLM。
        page = build_state_comparison_input(history, **args)
        inputs = [page]
        states = match_interactive_states(history)
        for change in states["state_changes"]:
            if change["change_type"] != "unchanged":
                inputs.append(build_state_comparison_input(history, state_key=change["state_key"], **args))
        return inputs

    def explicit_statement_candidates(self, history: dict, source: dict) -> list[dict]:
        """从当前 Structured Content 提取有界局部单元，交给正式 Path B 判断。

        输入是本次固定 Snapshot History 和已确认 Monitoring Source。这里仅负责来源资格、
        结构切分、可靠日期识别与稳定 statement identity；不按动作关键词认定 Event。
        输出最多 60 个局部候选，Initial Backfill 的 90 天与全竞品 20 条限制由上层统一执行。
        """
        return extract_statement_candidates(history, source, competitor=self.product.competitor_name)

    def detect(self, data):
        return detect_event(data)

    def deduplicate(self, candidate, existing):
        return deduplicate_event(candidate, existing)

    def ingest_own(self) -> dict:
        """旧 md/txt compatibility 入口；正式产品上传使用 OwnProductDocumentService。

        本方法保留给显式迁移脚本，不再由 Monitoring run 自动调用，也不扩展五格式。
        """
        results, runtime = [], []
        if not self.product.own_product_documents:
            return {"results": results, "runtime": runtime}
        ingestor = KnowledgeIngestor(KnowledgeStore(self.knowledge_path), get_embedding())
        for path in self.product.own_product_documents:
            document = own_product_file(path, document_id="own:" + identity([self.product_id, str(Path(path).resolve())]),
                                        product_id=self.product_id, title="自身产品资料：" + Path(path).name,
                                        source_type="product_document")
            if contains_secret(document.content):
                raise ValueError("产品资料含认证信息。")
            results.append(ingestor.ingest(document).model_dump(mode="json"))
            runtime.extend(asdict(r) for r in ingestor.last_runtime)
        return {"results": results, "runtime": runtime}

    def own_product_context_identity(self) -> list[dict]:
        """返回当前 Registry 的稳定内容身份，供 Triage artifact 判断知识是否已更新。"""
        records = KnowledgeStore(self.knowledge_path).document_sets("own_product", self.product_id)
        return [{"document_id": item["document_id"],
                 "current_content_hash": item["current_content_hash"]} for item in records]

    def ingest_event(self, event) -> dict:
        """仅将正式确认的原始 Event/Evidence 交给已有知识入口；研究结果不具备此入口。"""
        ingestor = KnowledgeIngestor(KnowledgeStore(self.knowledge_path), get_embedding())
        results, runtime = [], []
        for document in event_documents(event, competitor_id=self.competitor_id,
                                         source_reference="event:" + event.candidate_id):
            results.append(ingestor.ingest(document).model_dump(mode="json"))
            runtime.extend(asdict(r) for r in ingestor.last_runtime)
        return {"results": results, "runtime": runtime}

    def readers(self):
        """每个调用复用既有角色入口和正式检索器；没有第二套 RAG 或 Memory 算法。"""
        embedding = get_embedding()
        return (KnowledgeRetriever(KnowledgeStore(self.knowledge_path), embedding, get_rerank()),
                MemoryRetriever(MemoryStore(self.memory_path), embedding))

    def triage(self, event):
        knowledge, memory = self.readers()
        output = TriageWorkflow(knowledge=knowledge, memory=memory, **self.scope).run(
            event, product_profile=self.product.product_profile, current_focus=self.product.current_focus)
        return {**output, "result": output["result"].model_dump()}

    def research(self, event, mode, triage_result, official_hosts):
        knowledge, memory = self.readers()
        tools = ResearchTools(knowledge=knowledge, memory=memory, search=TencentWsaSearchProvider(),
                              official_hosts=frozenset(official_hosts), **self.scope)
        return ResearchAgent(tools).run(event, mode=mode, triage_result=triage_result,
                                        current_focus=self.product.current_focus)

    def follow_up(self, event, triage_result, official_hosts, *, initial_artifact: dict,
                  conversation_history: list[dict], question: str) -> dict:
        """复用同一 ResearchAgent、工具与引用登记表回答一个事件的后续问题。"""
        knowledge, memory = self.readers()
        tools = ResearchTools(
            knowledge=knowledge, memory=memory, search=TencentWsaSearchProvider(),
            official_hosts=frozenset(official_hosts), **self.scope,
        )
        return ResearchAgent(tools).run(
            event, mode="follow_up", triage_result=triage_result,
            current_focus=self.product.current_focus,
            initial_report=initial_artifact["result"],
            initial_citations=initial_artifact.get("evidence_registry", initial_artifact.get("citations", [])),
            conversation_history=conversation_history,
            question=question,
        )

    def extract_preference_candidates(self, text: str, *, event_id: str, turn_id: str) -> list[dict]:
        """只把用户追问原文交给既有 Memory Extraction，并且只产生待确认项。"""
        service = PreferenceService(MemoryStore(self.memory_path))
        return service.extract_from_user_message(
            self.product.workspace_id, text, event_id=event_id, turn_id=turn_id,
        )

    def verification_inputs(self, candidate, verification) -> list:
        """把核验的新官方原文送回 Event Core，绝不把 decision 字符串直接改成 confirmed Event。

        新证据必须有真实观察时间。每条引用仍属于一个来源；不拼接跨页正文，不复用
        Research 的 reason 作为产品事实。正式检测若未确认，上层保留原候选等待处理。
        """
        citations = {c["result_id"]: c for c in verification["citations"]}
        inputs = []
        for support in verification["result"]["support"]:
            item = citations.get(support["result_id"])
            if not item or item.get("credibility") != "official":
                continue
            if not item.get("url") or not item.get("observed_at"):
                continue
            snapshot = {"content": item["content"], "final_url": item["url"], "title": item["title"],
                        "captured_at": item["observed_at"]}
            try:
                inputs.append(build_explicit_statement_input(
                    snapshot, support["excerpt"], competitor=candidate.competitor,
                    source_type="verified_statement", source_credibility="official",
                ))
            except (KeyError, TypeError, ValueError):
                # Research 的结论不能弥补缺失或不一致的正式原文；无法构造输入时由
                # orchestrator 保持 Pending，而不是伪造 no_event 或让整个运行失败。
                continue
        return inputs

    def remember_explicit(self, text: str) -> dict:
        """只有调用方另行明确要求记住时使用；普通 current_focus 和 Research 输出不会调用。"""
        writer = MemoryWriter(MemoryStore(self.memory_path), get_embedding())
        result = writer.remember_explicit(scope="workspace", scope_id=self.product.workspace_id,
            memory_type="focus", memory_key="research_focus", content=text, source_text=text,
            source_reference="user:" + identity([self.product.workspace_id, text]))
        return {"result": result.model_dump(mode="json"), "runtime": [asdict(r) for r in writer.last_runtime]}
