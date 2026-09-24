"""把既有 Long-term Memory 规则投影为“关注与偏好”产品操作。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from llm.retrieval_models import get_embedding

from .extraction import extract_candidates
from .schemas import MemoryRecord, MemoryReferences
from .store import MemoryStore
from .writing import MemoryWriter


class PreferenceNotFoundError(ValueError):
    """请求项不存在、已失效或超出当前工作区时使用的稳定业务错误。"""


class PreferenceService:
    """复用 Memory Store/Writer/Policy，提供不暴露内部字段的产品服务。

    输入是当前 workspace 和用户选择的类型/内容；显式新增、编辑与确认均通过
    ``MemoryWriter.remember_explicit``，系统识别只通过 ``remember_extracted`` 写入待确认。
    输出仅包含页面需要的 item_id、业务类型、内容、来源说明和更新时间。
    """

    def __init__(self, store: MemoryStore, *, embedding_factory: Callable = get_embedding,
                 extractor: Callable = extract_candidates):
        self.store = store
        self.embedding_factory = embedding_factory
        self.extractor = extractor

    @staticmethod
    def _source_label(record: MemoryRecord) -> str:
        if record.source_kind == "research_context":
            return "从研究交流中识别" if record.status == "pending" else "从研究交流中确认"
        return "你主动添加"

    @classmethod
    def _view(cls, record: MemoryRecord) -> dict:
        return {
            "item_id": record.memory_id,
            "type": record.memory_type,
            "content": record.content,
            "source": cls._source_label(record),
            "updated_at": record.updated_at.isoformat(),
        }

    def list(self, workspace_id: str) -> dict:
        records = self.store.active_records(scope="workspace", scope_id=workspace_id)
        return {
            "confirmed_items": [self._view(item) for item in records if item.status == "confirmed"],
            "awaiting_confirmation_items": [self._view(item) for item in records if item.status == "pending"],
        }

    def _record(self, workspace_id: str, item_id: str, status: str) -> MemoryRecord:
        record = self.store.get(item_id)
        if (record is None or record.scope != "workspace" or record.scope_id != workspace_id
                or record.status != status or not record.is_active):
            raise PreferenceNotFoundError("未找到该条关注信息。")
        return record

    def add(self, workspace_id: str, memory_type: str, content: str) -> dict:
        writer = MemoryWriter(self.store, self.embedding_factory())
        result = writer.remember_explicit(
            scope="workspace", scope_id=workspace_id, memory_type=memory_type,
            memory_key="preference:" + uuid4().hex, content=content,
            source_text=content, source_reference="user_preference", source_kind="direct_user",
            metadata={"product_surface": "preferences"},
        )
        return self._view(result.memory)

    def edit(self, workspace_id: str, item_id: str, memory_type: str, content: str) -> dict:
        current = self._record(workspace_id, item_id, "confirmed")
        writer = MemoryWriter(self.store, self.embedding_factory())
        result = writer.remember_explicit(
            scope="workspace", scope_id=workspace_id, memory_type=memory_type,
            memory_key=current.memory_key or "preference:" + current.memory_id.replace("-", ""),
            content=content, source_text=content, source_reference="user_preference_edit",
            source_kind="direct_user", references=current.references,
            metadata={**current.metadata, "product_surface": "preferences"},
        )
        return self._view(result.memory)

    def remove(self, workspace_id: str, item_id: str) -> None:
        self._record(workspace_id, item_id, "confirmed")
        if not self.store.deactivate(item_id, datetime.now(timezone.utc)):
            raise PreferenceNotFoundError("未找到该条关注信息。")

    def confirm(self, workspace_id: str, item_id: str) -> dict:
        pending = self._record(workspace_id, item_id, "pending")
        writer = MemoryWriter(self.store, self.embedding_factory())
        result = writer.remember_explicit(
            scope="workspace", scope_id=workspace_id, memory_type=pending.memory_type,
            # Pending 与 confirmed 使用不同稳定键；否则 Writer 会把相同内容判为
            # unchanged 并返回仍为 pending 的旧记录。只有 confirmed 写入成功后才停用候选。
            memory_key="preference_confirmed:" + pending.memory_id.replace("-", ""),
            content=pending.content, source_text=pending.source_text,
            source_reference=pending.source_reference, source_kind="research_context",
            references=pending.references,
            metadata={**pending.metadata, "confirmed_from_suggestion": True},
        )
        self.store.deactivate(pending.memory_id, datetime.now(timezone.utc))
        return self._view(result.memory)

    def ignore(self, workspace_id: str, item_id: str) -> None:
        self._record(workspace_id, item_id, "pending")
        if not self.store.deactivate(item_id, datetime.now(timezone.utc)):
            raise PreferenceNotFoundError("未找到该条待确认信息。")

    def extract_from_user_message(self, workspace_id: str, text: str, *, event_id: str,
                                  turn_id: str) -> list[dict]:
        """只分析用户原文；候选经过现有策略后只能保存为待确认。

        Assistant、Research Report、Event、Evidence 与网页正文不会传入本方法。稳定 key
        由工作区、事件、Turn 和候选原文生成，同一消息重放不会制造重复候选。
        """
        outcome = self.extractor(text, source_kind="research_context")
        writer = MemoryWriter(self.store, self.embedding_factory())
        saved = []
        for candidate in outcome.candidates:
            raw = f"{workspace_id}\n{event_id}\n{turn_id}\n{candidate.memory_type}\n{candidate.content}"
            key = "preference_candidate:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
            result = writer.remember_extracted(
                candidate, scope="workspace", scope_id=workspace_id,
                source_reference="research_follow_up:" + turn_id, source_text=text,
                source_kind="research_context", memory_key=key,
                references=MemoryReferences(event_id=event_id, research_id=turn_id),
                metadata={"product_surface": "preferences"},
            )
            if result.memory.status == "pending":
                saved.append(self._view(result.memory))
        return saved
