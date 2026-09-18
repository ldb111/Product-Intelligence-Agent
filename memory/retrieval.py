"""Scope → active confirmed → Embedding 语义排名，不与 Knowledge 召回混合。"""

from __future__ import annotations

from llm.retrieval_models import EmbeddingRole, ModelCallRecord, unit_vector

from .policy import contains_secret
from .schemas import MemoryRetrievalResponse, MemoryRetrievalResult
from .store import MemoryStore


class MemoryRetriever:
    def __init__(self, store: MemoryStore, embedding: EmbeddingRole):
        self.store = store
        self.embedding = embedding
        self.last_runtime: list[ModelCallRecord] = []

    def retrieve_memory(self, query: str, *, scope: str, scope_id: str,
                        top_k: int = 5, memory_types: tuple[str, ...] | None = None) -> MemoryRetrievalResponse:
        """检索单一 scope/scope_id 的长期上下文，返回明确标记为 memory 的结果。

        先在数据库过滤 active + confirmed，再生成查询向量并精确计算余弦。Pending、
        superseded 和其他 scope 从不进入模型输入。Memory 数量小，本版不调用 Rerank。
        """
        self.last_runtime = []
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Memory 检索查询不能为空。")
        if contains_secret(query):
            raise ValueError("检索查询包含认证或秘密信息，禁止发送给 Embedding。")
        if type(top_k) is not int or not 1 <= top_k <= 50:
            raise ValueError("Memory Top-K 必须在 1 到 50 之间。")
        if memory_types is not None:
            allowed = {"focus", "preference", "constraint", "goal", "decision", "hypothesis", "open_question"}
            if not memory_types or len(set(memory_types)) != len(memory_types) or not set(memory_types) <= allowed:
                raise ValueError("Memory 类型过滤无效。")
        eligible = self.store.eligible(scope=scope, scope_id=scope_id, memory_types=memory_types)
        if not eligible:
            return MemoryRetrievalResponse(
                query=query, scope=scope, scope_id=scope_id, results=[], status="no_results",
            )
        self.store.assert_space(self.embedding.space_id)
        output = self.embedding.embed([query])
        self.last_runtime = output.runtime
        query_vector = unit_vector(output.vectors[0])
        self.store.check_query_space(self.embedding.space_id, len(query_vector))
        scored = []
        for record, vector in eligible:
            if len(vector) != len(query_vector):
                raise ValueError("Memory 向量维度不一致。")
            score = max(-1.0, min(1.0, sum(a * b for a, b in zip(query_vector, vector))))
            scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], item[1].memory_id))
        results = [MemoryRetrievalResult(
            **record.model_dump(), retrieval_score=score, retrieval_rank=index + 1,
        ) for index, (score, record) in enumerate(scored[:top_k])]
        return MemoryRetrievalResponse(
            query=query, scope=scope, scope_id=scope_id, results=results,
            status="ok" if results else "no_results",
        )
