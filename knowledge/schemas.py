"""双知识域的数据契约：来源由程序保存，模型只计算相关性。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

KnowledgeDomain = Literal["own_product", "competitor"]
DomainSelection = Literal["own_product", "competitor", "both"]


class KnowledgeError(ValueError):
    """知识层输入或边界错误。"""


class EmptyDocumentError(KnowledgeError): pass
class EmptyChunkError(KnowledgeError): pass
class InvalidDomainError(KnowledgeError): pass
class KnowledgeAdmissionError(KnowledgeError): pass
class DocumentSetConflictError(KnowledgeError):
    """文件级文档集合与当前 Registry 状态冲突。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class StoreWriteError(RuntimeError): pass
class StoreReadError(RuntimeError): pass
class VectorSpaceError(KnowledgeError): pass


def domains_for(selection: str) -> tuple[str, ...]:
    """把显式域选择转换为存储过滤条件；无默认 both 或未知域回退。"""
    if selection == "both":
        return ("own_product", "competitor")
    if selection in {"own_product", "competitor"}:
        return (selection,)
    raise InvalidDomainError("知识域必须为 own_product、competitor 或显式 both。")


def content_hash(content: str) -> str:
    """为实际文本计算稳定 SHA-256；只做精确去重，不判断语义相同。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceMetadata(Contract):
    knowledge_domain: KnowledgeDomain
    document_id: str = Field(min_length=1)
    product_id: str | None = None
    competitor_id: str | None = None
    source_type: str = Field(min_length=1)
    source_url: str | None = None
    source_reference: str | None = None
    title: str = Field(min_length=1, max_length=300)
    observed_at: datetime | None = None
    document_time: str | None = None
    evidence_status: Literal["confirmed", "pending", "conflict"] | None = None
    source_credibility: Literal["user_provided", "official", "unverified"] = "unverified"
    verification_reference: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("knowledge_domain", mode="before")
    @classmethod
    def valid_domain(cls, value):
        if value not in {"own_product", "competitor"}:
            raise InvalidDomainError("入库文档必须属于一个合法知识域。")
        return value

    @field_validator("document_id", "source_type", "title", "source_reference", "verification_reference")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("身份和来源字段不得只含空白。")
        return value

    @field_validator("source_url")
    @classmethod
    def valid_source_url(cls, value):
        """只校验来源地址格式，不猜测官方身份，也不改写调用方的原 URL。"""
        if value is None:
            return value
        try:
            url = urlsplit(value)
            valid = (url.scheme in {"http", "https"} and url.hostname
                     and not url.username and not url.password)
            url.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("来源 URL 必须是无认证信息的 HTTP/HTTPS 地址。")
        return value

    @field_validator("observed_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("观察时间必须包含时区。")
        return value

    @model_validator(mode="after")
    def traceable(self):
        if not self.source_url and not self.source_reference:
            raise ValueError("每份知识必须有原始 URL 或来源引用。")
        return self


class KnowledgeDocument(SourceMetadata):
    """调用方明确提供的纯文本或 Markdown；空时间保持 null，不推算事实。"""

    content: str

    @field_validator("content")
    @classmethod
    def not_empty(cls, value):
        if not value.strip():
            raise EmptyDocumentError("文档正文为空。")
        return value


class KnowledgeChunk(SourceMetadata):
    chunk_id: str
    content: str
    content_hash: str
    document_content_hash: str
    chunk_index: int
    heading_path: list[str]
    table_context: str | None = None
    start_offset: int
    end_offset: int

    def model_text(self) -> str:
        """组合标题、章节和正文作为语义输入；来源/供应商元数据不发送给模型。

        content 仍保留规范化原文的精确子串，上下文单独存储，便于后续引用核对。
        """
        context = "\n".join([self.title, *self.heading_path, self.table_context or ""]).strip()
        return context + "\n\n" + self.content


class RetrievalResult(KnowledgeChunk):
    retrieval_score: float
    retrieval_rank: int
    rerank_score: float | None = None


class RetrievalResponse(Contract):
    """业务结果和候选排名支持后续评测；不混入模型供应商或用量。"""

    query: str
    knowledge_domain: DomainSelection
    candidates: list[RetrievalResult]
    results: list[RetrievalResult]
    status: Literal["ok", "no_results"]


class IngestionResult(Contract):
    document_id: str
    knowledge_domain: KnowledgeDomain
    content_hash: str
    chunk_ids: list[str]
    status: Literal["inserted", "updated", "unchanged"]


class DocumentSetIngestionResult(Contract):
    """文件级原子替换结果；unit IDs 只供可信 Integration Registry 使用。"""

    document_id: str
    knowledge_domain: KnowledgeDomain
    unit_document_ids: list[str]
    status: Literal["inserted", "updated"]
