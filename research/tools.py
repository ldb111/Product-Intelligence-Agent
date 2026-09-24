"""五个只读工具的薄包装；来源身份、检索范围和错误信息均由程序控制。"""

from dataclasses import asdict
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Callable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from memory.policy import contains_secret
from source_discovery.stage1_adapter import read_page_for_discovery

from .evidence import EvidenceError, EvidenceRegistry


class QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=1500, description="当前需要查明的问题")


class PageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_id: str = Field(description="输入或搜索结果中已登记的页面线索 ID，不能填写地址")


DESCRIPTIONS = {
    "web_search": "需要补充公开来源时搜索问题。输入 query，返回带 candidate_id 的标题和摘要；摘要不是证据，须 read_page 后才能引用。",
    "read_page": "读取已登记的页面线索。输入 candidate_id，返回正文证据及稳定 result_id；失败或截断部分不能作为不存在的证明。",
    "retrieve_competitor_knowledge": "查找当前竞品的已有事实和历史。输入 query，返回竞品知识原文和 result_id；不得将未覆盖范围视为能力不存在。",
    "retrieve_own_product_knowledge": "需要自身能力比较时检索当前产品资料。输入 query，返回自身知识原文和 result_id；无结果表示资料不足。",
    "retrieve_memory": "需要长期关注上下文时召回当前范围内已确认记忆。输入 query，返回 memory 类型和 result_id；仅影响关注优先级，不能证明产品事实。",
}
TOOL_DEFINITIONS = [
    {"type": "function", "function": {"name": name, "description": description,
     "parameters": (PageInput if name == "read_page" else QueryInput).model_json_schema()}}
    for name, description in DESCRIPTIONS.items()
]


def public_url(url: str) -> bool:
    """限制候选为普通公开网页地址，排除认证信息、私网字面地址和明显凭据。

    这是输入边界而非网络隔离：冻结采集器的 DNS、跳转和浏览器子资源行为沿用原实现。
    部署到可访问内网的环境前仍需出口隔离；本 MVP 只用于可信调用方的公开研究。
    """
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443} or contains_secret(url)):
            return False
        try:
            return ip_address(host).is_global
        except ValueError:
            return "." in host and not host.endswith((".localhost", ".local", ".internal"))
    except ValueError:
        return False


class ResearchTools:
    """每次任务新建实例；仅持有读取接口，范围标识由可信调用方固定。"""

    def __init__(self, *, knowledge, memory, search, product_id: str, competitor_id: str,
                 memory_scope: str, memory_scope_id: str, official_hosts: frozenset[str] = frozenset(),
                 page_reader: Callable = read_page_for_discovery):
        if not all((product_id.strip(), competitor_id.strip(), memory_scope_id.strip())):
            raise ValueError("必须明确指定产品、竞品和记忆范围。")
        self.knowledge, self.memory, self.search = knowledge, memory, search
        self.product_id, self.competitor_id = product_id, competitor_id
        self.memory_scope, self.memory_scope_id = memory_scope, memory_scope_id
        self.official_hosts, self.page_reader = official_hosts, page_reader
        self.registry = EvidenceRegistry()
        self.runtime: list[dict] = []

    def seed_event(self, event) -> dict:
        """从上游事件登记原证据和可读页面，返回不含原始地址/内部字段的任务输入。

        不将 observed_at 变成事件发生时间；uncertain 的原证据仍然保持 pending/conflict。
        官方身份沿用可信调用方认定，搜索结果不能自行声明官方身份。
        """
        event = type(event).model_validate(event.model_dump())
        for evidence in event.evidence:
            self.registry.add(source="event", domain="competitor", source_id=evidence.evidence_id,
                              url=evidence.source_url, title=evidence.source_title,
                              content=evidence.relevant_excerpt, credibility=evidence.source_credibility,
                              status=evidence.evidence_status, observed_at=evidence.observed_at.isoformat())
            self._candidate(evidence.source_url, evidence.source_title, "上游证据来源，可读取补充正文。")
        return {"candidate_id": event.candidate_id, "competitor": event.competitor,
                "decision": event.decision, "event_type": event.event_type,
                "event_subject": event.event_subject, "change_action": event.change_action,
                "change_items": [c.model_dump() for c in event.change_items],
                "event_time": event.event_time, "effective_time": event.effective_time,
                "uncertainty_reason": event.uncertainty_reason,
                "evidence": self.registry.model_items()}

    def seed_citations(self, citations: list[dict]) -> list[dict]:
        """把已完成 Initial Research 的真实登记来源带入追问任务。

        输入来自持久化 Artifact，而不是浏览器或模型。Registry 会按原始来源重新生成
        本轮稳定 ID 与 source_group；传入的旧内部 ID 不被信任，也不会作为新来源。
        """
        ids = []
        for item in citations:
            data = {key: item.get(key) for key in (
                "source", "domain", "source_id", "url", "title", "content",
                "source_reference", "credibility", "status", "observed_at",
            )}
            if (data["source"] not in {"event", "page", "knowledge", "memory", "search"}
                    or data["domain"] not in {"competitor", "own_product", "memory"}
                    or not data["source_id"] or not data["title"] or not data["content"]):
                continue
            citation = self.registry.add(**data)
            ids.append(citation.result_id)
        return self.registry.model_items(ids)

    def _candidate(self, url: str, title: str, snippet: str):
        """仅把真实工具或输入中的有效 URL 登记为线索，输出稳定候选 ID。"""
        if not public_url(url):
            return None
        return self.registry.add(source="search", domain="competitor", source_id=url, url=url,
                                 title=title, content=snippet[:2000], credibility="unverified", status="unread")

    def retrieve_knowledge(self, query: str, domain: str) -> dict:
        """使用冻结 RAG，固定域与产品过滤器，登记返回的原文和来源；不复制检索算法。"""
        filters = {"product_id": self.product_id} if domain == "own_product" else {"competitor_id": self.competitor_id}
        response = self.knowledge.retrieve(query, knowledge_domain=domain, top_n=5, top_k=3, filters=filters)
        self.runtime.extend(asdict(r) for r in self.knowledge.last_runtime)
        ids = []
        for item in response.results:
            if item.knowledge_domain != domain or any(getattr(item, k) != v for k, v in filters.items()):
                raise EvidenceError("检索结果超出指定产品范围。")
            evidence = self.registry.add(
                source="knowledge", domain=domain, source_id=item.chunk_id, url=item.source_url,
                source_reference=item.source_reference, title=item.title, content=item.content,
                credibility=item.source_credibility, status=item.evidence_status or "confirmed",
                observed_at=item.observed_at.isoformat() if item.observed_at else None,
            )
            ids.append(evidence.result_id)
        return {"context_type": "knowledge", "domain": domain, "results": self.registry.model_items(ids),
                "missing_context": "insufficient_own_product_context" if domain == "own_product" and not ids else None}

    def retrieve_memory(self, query: str) -> dict:
        """只召回固定范围的 active confirmed Memory，输出关注上下文；无写入入口。"""
        response = self.memory.retrieve_memory(query, scope=self.memory_scope, scope_id=self.memory_scope_id, top_k=3)
        self.runtime.extend(asdict(r) for r in self.memory.last_runtime)
        ids = []
        for item in response.results:
            if (item.scope != self.memory_scope or item.scope_id != self.memory_scope_id
                    or item.status != "confirmed" or not item.is_active):
                raise EvidenceError("Memory 结果超出已确认范围。")
            evidence = self.registry.add(source="memory", domain="memory", source_id=item.memory_id,
                                         title=item.memory_type, content=item.content,
                                         source_reference=item.source_reference,
                                         credibility="user_context", status="confirmed")
            ids.append(evidence.result_id)
        return {"context_type": "memory", "purpose": "priority_context_only", "results": self.registry.model_items(ids)}

    def execute(self, name: str, arguments: dict) -> dict:
        """校验模型参数后分派五个白名单工具，返回业务结果；错误不包含技术细节。

        read_page 只接受已登记 ID；成功采集后才产生可引用证据。返回正文限制长度，
        保留截断标记且只允许引用已提供的部分。失败由上层关闭搜索，不循环重试。
        """
        if name not in DESCRIPTIONS:
            raise EvidenceError("未知研究工具。")
        parsed = (PageInput if name == "read_page" else QueryInput).model_validate(arguments)
        if name == "read_page":
            candidate = self.registry.get(parsed.candidate_id)
            if candidate.source != "search" or not candidate.url or not public_url(candidate.url):
                raise EvidenceError("页面线索不可读取。")
            page = self.page_reader(candidate.url)
            if page["quality_status"] != "PASS" or not public_url(page["final_url"]) or not page["main_content"].strip():
                raise EvidenceError("页面未通过质量边界。")
            result = self.registry.add(
                source="page", domain="competitor", source_id=page["final_url"], url=page["final_url"],
                title=page["title"], content=page["main_content"][:16000], status="read",
                credibility="official" if urlsplit(page["final_url"]).hostname in self.official_hosts else "unverified",
                observed_at=datetime.now(timezone.utc).isoformat(),
            )
            return {"results": self.registry.model_items([result.result_id]), "truncated": len(page["main_content"]) > 16000}
        query = parsed.query.strip()
        if not query or contains_secret(query):
            raise EvidenceError("检索问题无效或包含敏感内容。")
        if name == "web_search":
            response = self.search.search(query, limit=5)
            ids = []
            for item in response["results"][:5]:
                candidate = self._candidate(item["url"], item["title"], item["snippet"])
                if candidate:
                    ids.append(candidate.result_id)
            return {"candidates": [{"candidate_id": item["result_id"], "title": item["title"],
                                     "snippet": item["content"]} for item in self.registry.model_items(ids)]}
        if name == "retrieve_memory":
            return self.retrieve_memory(query)
        domain = "own_product" if name == "retrieve_own_product_knowledge" else "competitor"
        return self.retrieve_knowledge(query, domain)
