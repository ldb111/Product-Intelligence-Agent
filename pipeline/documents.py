"""Workspace Configuration 的自身产品资料服务；生命周期与 Monitoring 完全分离。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from file_parsing import parse_and_prepare
from knowledge import KnowledgeIngestor, KnowledgeStore
from knowledge.schemas import DocumentSetConflictError
from llm.retrieval_models import EmbeddingRole, get_embedding
from memory.policy import contains_secret

from .schemas import OwnProductDocumentResult
from .store import identity


def product_scope_id(workspace_id: str, product_id: str | None = None) -> str:
    """生成 Knowledge 使用的产品作用域；旧调用未提供 product_id 时保持原有身份。"""
    return identity([workspace_id, product_id]) if product_id is not None else identity(workspace_id)


def workspace_knowledge_path(directory: str | Path, workspace_id: str) -> Path:
    """每个 workspace 使用独立 Knowledge 数据库，产品再由 product_id 字段隔离。"""
    return Path(directory) / (identity(workspace_id) + ".knowledge.sqlite3")


def _file_sha256(path: Path) -> str:
    """在解析前流式计算完整源文件身份，用于跨路径精确重复判断。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class OwnProductDocumentService:
    """提供显式 Add/Replace，供未来 UI 或配置 API 调用。

    输入是 workspace、产品身份和一个本地文件。服务固定 own_product domain，复用
    parse_and_prepare、KnowledgeIngestor 与 KnowledgeStore。输出是产品级状态，不包含
    本机绝对路径、Knowledge unit IDs、chunks、向量或底层异常。
    """

    def __init__(self, directory: str | Path,
                 *, embedding_factory: Callable[[], EmbeddingRole] = get_embedding):
        self.directory = Path(directory)
        self.embedding_factory = embedding_factory

    def _store(self, workspace_id: str) -> KnowledgeStore:
        return KnowledgeStore(workspace_knowledge_path(self.directory, workspace_id))

    @staticmethod
    def _scope(workspace_id: str, product_id: str) -> str:
        if not workspace_id.strip() or not product_id.strip():
            raise ValueError("workspace_id 和 product_id 不能为空。")
        return product_scope_id(workspace_id, product_id)

    @staticmethod
    def _public(record: dict, *, content_status: str = "current",
                parsing_status: str | None = None, ingestion_status: str | None = None,
                failure_reason: str | None = None) -> OwnProductDocumentResult:
        """把内部 Registry 投影为用户结果，只保留可理解的当前状态。"""
        return OwnProductDocumentResult(
            document_id=record["document_id"], display_name=record["display_name"],
            file_format=record["file_format"],
            parsing_status=parsing_status or record["parsing_status"],
            ingestion_status=ingestion_status or record["ingestion_status"],
            content_status=content_status, knowledge_status="available",
            failure_reason=failure_reason, created_at=record["created_at"],
            updated_at=record["updated_at"], unit_count=len(record["unit_document_ids"]),
        )

    @staticmethod
    def _failure(path: Path, *, parsing_status: str, ingestion_status: str,
                 reason: str, document_id: str | None = None, display_name: str | None = None,
                 current: dict | None = None) -> OwnProductDocumentResult:
        """生成稳定失败结果；Replace 失败时明确表示旧 Knowledge 仍是当前版本。"""
        if current:
            return OwnProductDocumentService._public(
                current, content_status="current_preserved", parsing_status=parsing_status,
                ingestion_status=ingestion_status, failure_reason=reason,
            )
        return OwnProductDocumentResult(
            document_id=document_id, display_name=display_name or path.name,
            file_format=path.suffix.lower().lstrip("."), parsing_status=parsing_status,
            ingestion_status=ingestion_status, content_status="unavailable",
            knowledge_status="unavailable", failure_reason=reason,
        )

    @staticmethod
    def _parse_failure_reason(status: str, warnings: list[str]) -> str:
        if status == "scanned_or_no_text":
            return "未提取到足够的文本内容，可能是扫描型 PDF，当前版本暂不支持 OCR。"
        if status == "unsupported":
            return "当前文件格式不受支持。"
        if "file_not_found" in warnings:
            return "文件不存在或已不可用。"
        return "文件解析失败。"

    @staticmethod
    def _display_name_conflict(path: Path, name: str,
                               existing: dict) -> OwnProductDocumentResult:
        """返回同名不同内容的受控冲突，并指向已经持久化的逻辑文档。"""
        return OwnProductDocumentResult(
            document_id=existing["document_id"], display_name=name,
            file_format=path.suffix.lower().lstrip("."), parsing_status="pending",
            ingestion_status="not_started", content_status="conflict",
            knowledge_status="available",
            failure_reason="存在同名资料但内容不同，请明确选择更新已有资料或作为新资料处理。",
            created_at=existing["created_at"], updated_at=existing["updated_at"],
            unit_count=len(existing["unit_document_ids"]),
        )

    @staticmethod
    def _knowledge_documents(prepared, file_hash: str):
        """把完整源文件哈希补入每个 unit 的 provenance，不改变 Chunk Schema。"""
        return [document.model_copy(update={
            "metadata": {**document.metadata, "source_file_sha256": file_hash}
        }) for document in prepared.ingestion_input]

    def get_own_product_document(self, *, workspace_id: str, product_id: str,
                                 document_id: str) -> OwnProductDocumentResult | None:
        """按当前 workspace/product 查询一个稳定产品文档 ID；其他作用域返回不存在。"""
        scope = self._scope(workspace_id, product_id)
        record = self._store(workspace_id).document_set_record("own_product", scope, document_id)
        return self._public(record) if record else None

    def add_own_product_document(self, *, workspace_id: str, product_id: str,
                                 file: str | Path, display_name: str | None = None
                                 ) -> OwnProductDocumentResult:
        """新增逻辑产品文档；完整文件哈希重复时直接复用已有文档。

        文件名只用于展示和冲突提醒，不参与版本推断。通过重复与同名检查后才解析、
        Embedding，并以文件级事务写入完整 unit set 和当前 Registry。
        """
        path = Path(file)
        name = (display_name or path.name).strip()
        if not name:
            raise ValueError("display_name 不能为空。")
        scope = self._scope(workspace_id, product_id)
        store = self._store(workspace_id)
        if not path.is_file():
            prepared = parse_and_prepare(path, document_id="unpersisted", product_id=scope)
            return self._failure(
                path, parsing_status=prepared.status, ingestion_status="not_started",
                reason=self._parse_failure_reason(prepared.status, prepared.warnings), display_name=name,
            )
        file_hash = _file_sha256(path)
        duplicate = store.document_set_by_content_hash("own_product", scope, file_hash)
        if duplicate:
            return self._public(duplicate, content_status="existing")
        name_key = name.casefold()
        same_name = store.document_set_by_display_name("own_product", scope, name_key)
        if same_name:
            return self._display_name_conflict(path, name, same_name)
        document_id = "doc_" + uuid4().hex
        prepared = parse_and_prepare(path, document_id=document_id, product_id=scope)
        if prepared.status != "parsed":
            return self._failure(
                path, parsing_status=prepared.status, ingestion_status="not_started",
                reason=self._parse_failure_reason(prepared.status, prepared.warnings),
                document_id=document_id, display_name=name,
            )
        if not prepared.ingestion_input:
            return self._failure(
                path, parsing_status="parsed", ingestion_status="failed",
                reason="文件没有可入库的文本内容。", document_id=document_id, display_name=name,
            )
        documents = self._knowledge_documents(prepared, file_hash)
        if any(contains_secret(document.content) for document in documents):
            return self._failure(
                path, parsing_status="parsed", ingestion_status="failed",
                reason="文件包含认证信息，不能进入知识库。", document_id=document_id,
                display_name=name,
            )
        now = datetime.now(timezone.utc).isoformat()
        registry = {
            "document_id": document_id, "workspace_id": workspace_id,
            "product_id": scope, "display_name": name, "display_name_key": name_key,
            "file_format": path.suffix.lower().lstrip("."),
            "current_content_hash": file_hash, "parsing_status": "parsed",
            "ingestion_status": "completed",
            "unit_document_ids": [item.document_id for item in documents],
            "created_at": now, "updated_at": now, "knowledge_domain": "own_product",
        }
        try:
            KnowledgeIngestor(store, self.embedding_factory()).replace_document_set(
                logical_document_id=document_id, documents=documents,
                registry=registry, create_only=True,
            )
        except DocumentSetConflictError as exc:
            if exc.code == "duplicate_content":
                duplicate = store.document_set_by_content_hash("own_product", scope, file_hash)
                if duplicate:
                    return self._public(duplicate, content_status="existing")
            if exc.code == "display_name_conflict":
                same_name = store.document_set_by_display_name("own_product", scope, name_key)
                if same_name:
                    return self._display_name_conflict(path, name, same_name)
            return self._failure(
                path, parsing_status="parsed", ingestion_status="failed",
                reason="文档状态已被其他操作更新，请重新读取后再试。",
                document_id=document_id, display_name=name,
            )
        except Exception:
            return self._failure(
                path, parsing_status="parsed", ingestion_status="failed",
                reason="知识入库失败，未创建可检索文档。",
                document_id=document_id, display_name=name,
            )
        return self._public(registry)

    def replace_own_product_document(self, *, workspace_id: str, product_id: str,
                                     document_id: str, new_file: str | Path
                                     ) -> OwnProductDocumentResult:
        """以稳定 document_id 原子替换整个当前文件，失败时完整保留旧版本。

        文件哈希相同时在解析前返回 unchanged。新文件完成解析、Chunking 和全部 Embedding
        后，KnowledgeStore 才在一个事务中删除旧 units、写入新 units 并更新 Registry。
        """
        path = Path(new_file)
        scope = self._scope(workspace_id, product_id)
        store = self._store(workspace_id)
        current = store.document_set_record("own_product", scope, document_id)
        if current is None:
            return self._failure(
                path, parsing_status="pending", ingestion_status="not_started",
                reason="当前 workspace/product 中不存在该产品文档。", document_id=document_id,
            )
        if not path.is_file():
            prepared = parse_and_prepare(path, document_id=document_id, product_id=scope)
            return self._failure(
                path, parsing_status=prepared.status, ingestion_status="not_started",
                reason=self._parse_failure_reason(prepared.status, prepared.warnings), current=current,
            )
        file_hash = _file_sha256(path)
        if file_hash == current["current_content_hash"]:
            return self._public(current, content_status="unchanged")
        prepared = parse_and_prepare(path, document_id=document_id, product_id=scope)
        if prepared.status != "parsed":
            return self._failure(
                path, parsing_status=prepared.status, ingestion_status="not_started",
                reason=self._parse_failure_reason(prepared.status, prepared.warnings), current=current,
            )
        if not prepared.ingestion_input:
            return self._failure(
                path, parsing_status="parsed", ingestion_status="failed",
                reason="文件没有可入库的文本内容。", current=current,
            )
        documents = self._knowledge_documents(prepared, file_hash)
        if any(contains_secret(document.content) for document in documents):
            return self._failure(
                path, parsing_status="parsed", ingestion_status="failed",
                reason="文件包含认证信息，旧版本保持不变。", current=current,
            )
        registry = {
            **current, "file_format": path.suffix.lower().lstrip("."),
            "current_content_hash": file_hash,
            "unit_document_ids": [item.document_id for item in documents],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "parsing_status": "parsed", "ingestion_status": "completed",
        }
        try:
            KnowledgeIngestor(store, self.embedding_factory()).replace_document_set(
                logical_document_id=document_id, documents=documents,
                registry=registry, expected_content_hash=current["current_content_hash"],
            )
        except DocumentSetConflictError as exc:
            # Parsing/Embedding 在事务外执行。冲突后必须重新读取 Registry，不能把请求开始时
            # 的旧快照当成当前状态返回；同目标更新则按最终内容判定为幂等成功。
            latest = store.document_set_record("own_product", scope, document_id)
            if (exc.code == "document_changed" and latest
                    and latest["current_content_hash"] == file_hash):
                return self._public(latest, content_status="unchanged")
            if latest:
                return self._public(
                    latest, content_status="conflict", parsing_status="parsed",
                    ingestion_status="failed",
                    failure_reason="文档已被其他操作更新，请重新读取当前版本后再试。",
                )
            return self._failure(
                path, parsing_status="parsed", ingestion_status="failed",
                reason="文档状态已被其他操作更新，请重新读取后再试。",
                document_id=document_id,
            )
        except Exception:
            return self._failure(
                path, parsing_status="parsed", ingestion_status="failed",
                reason="知识替换失败，旧版本保持不变。", current=current,
            )
        return self._public(registry)
