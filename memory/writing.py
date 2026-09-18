"""Memory 写入入口：显式输入可确认，抽取候选只能 pending。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from llm.retrieval_models import EmbeddingRole, ModelCallRecord

from .policy import validate_candidate, validate_source_before_model
from .schemas import (
    MemoryCandidate, MemoryRecord, MemoryReferences, MemorySourceKind,
    MemoryWriteResult,
)
from .store import MemoryStore


class MemoryWriter:
    def __init__(self, store: MemoryStore, embedding: EmbeddingRole,
                 clock: Callable[[], datetime] | None = None):
        self.store = store
        self.embedding = embedding
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_runtime: list[ModelCallRecord] = []

    def _record(self, candidate: MemoryCandidate, *, scope: str, scope_id: str,
                source_kind: MemorySourceKind, source_reference: str, source_text: str,
                status: str, memory_key: str | None, references: MemoryReferences | None,
                metadata: dict | None) -> MemoryRecord:
        now = self.clock()
        return MemoryRecord(
            memory_id=str(uuid4()), scope=scope, scope_id=scope_id, memory_type=candidate.memory_type,
            memory_key=memory_key, content=candidate.content, status=status,
            source_kind=source_kind, source_reference=source_reference, source_text=source_text,
            created_at=now, updated_at=now, is_active=True,
            references=references or MemoryReferences(), metadata=metadata or {},
        )

    def remember_explicit(self, *, scope: str, scope_id: str, memory_type: str,
                          content: str, source_reference: str, source_text: str,
                          source_kind: MemorySourceKind = "direct_user",
                          memory_key: str | None = None,
                          references: MemoryReferences | None = None,
                          metadata: dict | None = None) -> MemoryWriteResult:
        """写入调用方明确要求记住的长期上下文，成功返回 confirmed 记录。

        程序先验证来源、逐字支持、类型和边界，再检查完全重复项。只有同 scope、
        scope_id、稳定 key 的显式新值才 supersede 旧值；向量化失败不会改变旧记录。
        """
        self.last_runtime = []
        validate_source_before_model(source_text, source_reference)
        candidate = MemoryCandidate(memory_type=memory_type, content=content)
        validate_candidate(candidate, source_text=source_text, source_kind=source_kind)
        record = self._record(
            candidate, scope=scope, scope_id=scope_id, source_kind=source_kind,
            source_reference=source_reference, source_text=source_text, status="confirmed",
            memory_key=memory_key, references=references, metadata=metadata,
        )
        old = self.store.current_by_key(record.scope, record.scope_id, memory_key) if memory_key else self.store.exact_active(record)
        if old and old.content == record.content and old.memory_type == record.memory_type:
            return MemoryWriteResult(action="unchanged", memory=old)
        self.store.assert_space(self.embedding.space_id)
        output = self.embedding.embed([record.content])
        self.last_runtime = output.runtime
        saved, action = self.store.insert_confirmed(record, output.vectors[0], self.embedding.space_id)
        return MemoryWriteResult(
            action=action, memory=saved,
            superseded_memory_id=saved.supersedes_memory_id if action == "superseded" else None,
        )

    def remember_extracted(self, candidate: MemoryCandidate, *, scope: str, scope_id: str,
                           source_reference: str, source_text: str,
                           source_kind: MemorySourceKind = "research_context",
                           memory_key: str | None = None,
                           references: MemoryReferences | None = None,
                           metadata: dict | None = None) -> MemoryWriteResult:
        """把通过策略的 LLM Candidate 保存为 pending，绝不自动覆盖 active Memory。

        Pending 不向量化，因其未经确认且默认不可检索。调用方如果明确确认，应使用
        remember_explicit 重新提交直接来源，而不是让模型自行晋升状态。
        """
        self.last_runtime = []
        validate_source_before_model(source_text, source_reference)
        validate_candidate(candidate, source_text=source_text, source_kind=source_kind)
        record = self._record(
            candidate, scope=scope, scope_id=scope_id, source_kind=source_kind,
            source_reference=source_reference, source_text=source_text, status="pending",
            memory_key=memory_key, references=references, metadata=metadata,
        )
        saved, action = self.store.insert_pending(record)
        return MemoryWriteResult(action=action, memory=saved)
