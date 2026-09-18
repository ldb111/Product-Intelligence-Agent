"""Long-term Memory V0.1 的稳定数据契约与三层数据边界。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

MemoryScope = Literal["user", "workspace", "research"]
MemoryType = Literal["focus", "preference", "constraint", "goal", "decision", "hypothesis", "open_question"]
MemoryStatus = Literal["pending", "confirmed", "superseded"]
MemorySourceKind = Literal[
    "direct_user", "research_context", "event", "evidence", "snapshot",
    "knowledge", "pricing", "web_page",
]


class MemoryError(ValueError):
    """长期记忆输入、策略或边界错误。"""


class MemoryPolicyError(MemoryError): pass
class MemoryStoreWriteError(RuntimeError): pass
class MemoryStoreReadError(RuntimeError): pass
class MemoryVectorSpaceError(MemoryError): pass


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryReferences(Contract):
    """只保存对业务事实的标识引用，不复制 Event、Knowledge 或 Evidence 正文。"""

    event_id: str | None = None
    document_id: str | None = None
    competitor_id: str | None = None
    research_id: str | None = None

    @field_validator("event_id", "document_id", "competitor_id", "research_id")
    @classmethod
    def nonblank_reference(cls, value):
        if value is not None and not value.strip():
            raise ValueError("关联标识不得只包含空白。")
        return value


class MemoryCandidate(Contract):
    """LLM 只能提出类型和原文片段；没有 scope、状态、身份或写库权限。"""

    memory_type: MemoryType = Field(description="长期上下文类型")
    content: str = Field(
        min_length=1, max_length=1000,
        description="输入中直接表达的连续原文片段，不得改写、概括或推断",
    )

    @field_validator("content")
    @classmethod
    def trimmed(cls, value):
        if value != value.strip():
            raise ValueError("候选内容不得包含首尾空白。")
        return value


class MemoryCandidateBatch(Contract):
    candidates: list[MemoryCandidate] = Field(
        default_factory=list, max_length=10,
        description="没有明确、持续且值得跨任务保留的信息时返回空列表",
    )


class MemoryRecord(Contract):
    memory_id: str = Field(min_length=1)
    scope: MemoryScope
    scope_id: str = Field(min_length=1, max_length=200)
    memory_type: MemoryType
    memory_key: str | None = Field(default=None, max_length=120)
    content: str = Field(min_length=1, max_length=1000)
    status: MemoryStatus
    source_kind: MemorySourceKind
    source_reference: str = Field(min_length=1, max_length=500)
    source_text: str = Field(min_length=1, max_length=4000)
    created_at: datetime
    updated_at: datetime
    is_active: bool
    supersedes_memory_id: str | None = None
    references: MemoryReferences = Field(default_factory=MemoryReferences)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("scope_id", "content", "source_reference", "source_text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Memory 身份、内容和来源不得为空。")
        return value

    @field_validator("memory_key")
    @classmethod
    def stable_key(cls, value):
        if value is None:
            return value
        if not value or any(not (c.islower() or c.isdigit() or c in "_.:-") for c in value):
            raise ValueError("memory_key 只能使用小写字母、数字及 _ . : -。")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def timezone_required(cls, value):
        if value.utcoffset() is None:
            raise ValueError("Memory 时间必须包含时区。")
        return value

    @model_validator(mode="after")
    def consistent_state(self):
        if self.updated_at < self.created_at:
            raise ValueError("更新时间不能早于创建时间。")
        if self.status == "superseded" and self.is_active:
            raise ValueError("已被替代的 Memory 不能保持 active。")
        if self.supersedes_memory_id == self.memory_id:
            raise ValueError("Memory 不能替代自身。")
        return self


class MemoryRetrievalResult(MemoryRecord):
    context_type: Literal["memory"] = "memory"
    retrieval_score: float
    retrieval_rank: int


class MemoryRetrievalResponse(Contract):
    query: str
    scope: MemoryScope
    scope_id: str
    results: list[MemoryRetrievalResult]
    status: Literal["ok", "no_results"]


class MemoryWriteResult(Contract):
    action: Literal["inserted", "superseded", "pending", "unchanged"]
    memory: MemoryRecord
    superseded_memory_id: str | None = None
