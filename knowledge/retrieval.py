"""显式域过滤 → 向量召回 Top-N → 专用重排序 → Top-K，不生成最终答案。"""

from llm.retrieval_models import EmbeddingRole, ModelCallRecord, RerankRole

from .schemas import RetrievalResponse, domains_for
from .store import KnowledgeStore


class KnowledgeRetriever:
    def __init__(self, store: KnowledgeStore, embedding: EmbeddingRole, rerank: RerankRole | None):
        self.store = store
        self.embedding = embedding
        self.rerank = rerank
        self.last_runtime: list[ModelCallRecord] = []

    def retrieve(self, query: str, *, knowledge_domain: str, top_n: int = 10, top_k: int = 5,
                 filters: dict | None = None) -> RetrievalResponse:
        """查询指定知识域，返回原始候选与最终结果；未命中时明确 no_results。

        调用方必须指定域；both 仅在本次 SQL 查询允许两个独立域参与，不预先混库。
        可显式传 rerank=None 禁用排序；排序失败时不会自动降级。模型仅返回索引与
        分数，所有正文和来源从本地候选复制，以供后续引用及 Recall/Precision 评测。
        """
        self.last_runtime = []
        domains_for(knowledge_domain)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("检索查询不能为空。")
        if type(top_n) is not int or type(top_k) is not int or not 1 <= top_k <= top_n <= 100:
            raise ValueError("需要 1 <= Top-K <= Top-N <= 100。")
        if filters and not set(filters) <= {"product_id", "competitor_id", "source_type", "evidence_status"}:
            raise ValueError("不支持的元数据过滤字段。")
        self.store.assert_space(self.embedding.space_id)
        embedded = self.embedding.embed([query])
        self.last_runtime.extend(embedded.runtime)
        candidates = self.store.search(embedded.vectors[0], knowledge_domain=knowledge_domain,
                                       space_id=self.embedding.space_id, top_n=top_n, filters=filters)
        results = candidates[:top_k]
        if candidates and self.rerank is not None:
            reranked = self.rerank.rerank(query, [c.model_text() for c in candidates], min(top_k, len(candidates)))
            self.last_runtime.extend(reranked.runtime)
            results = [candidates[item.index].model_copy(update={"rerank_score": item.score})
                       for item in reranked.items]
        return RetrievalResponse(query=query, knowledge_domain=knowledge_domain, candidates=candidates,
                                 results=results, status="ok" if results else "no_results")
