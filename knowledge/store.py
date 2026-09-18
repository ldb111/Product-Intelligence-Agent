"""SQLite 持久化 + 精确余弦检索，适用于本地小型知识库，无新增依赖。"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from llm.retrieval_models import unit_vector

from .schemas import (
    KnowledgeChunk, KnowledgeDocument, RetrievalResult, StoreReadError,
    StoreWriteError, VectorSpaceError, domains_for,
)

logger = logging.getLogger(__name__)


class KnowledgeStore:
    def __init__(self, path: str | Path):
        """创建或打开本地数据库；每次操作使用独立连接，返回后可跨进程重开。

        一个库固定一个向量空间，两个域通过复合主键和 SQL 过滤逻辑隔离。
        不支持 :memory:，因为本层承诺可持久化且短连接不会共享内存数据库。
        """
        self.path = Path(path)
        if str(path) == ":memory:":
            raise ValueError("知识库需要持久化文件路径。")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection, connection:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS vector_space (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        space_id TEXT NOT NULL, dimension INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS documents (
                        domain TEXT NOT NULL CHECK(domain IN ('own_product','competitor')),
                        document_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                        payload TEXT NOT NULL, PRIMARY KEY(domain, document_id)
                    );
                    CREATE TABLE IF NOT EXISTS chunks (
                        chunk_id TEXT PRIMARY KEY, domain TEXT NOT NULL,
                        document_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                        payload TEXT NOT NULL, vector TEXT NOT NULL,
                        FOREIGN KEY(domain, document_id)
                            REFERENCES documents(domain, document_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS chunks_domain_document
                        ON chunks(domain, document_id);
                """)
        except (sqlite3.Error, OSError):
            logger.warning("knowledge_store_initialization_failed")
            raise StoreWriteError("知识存储初始化失败。") from None

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _check_space(connection, space_id, dimension=None):
        existing = connection.execute("SELECT space_id, dimension FROM vector_space WHERE singleton=1").fetchone()
        if existing and (existing[0] != space_id or dimension is not None and existing[1] != dimension):
            raise VectorSpaceError("向量模型或维度与知识库不一致；请使用独立库重新入库。")

    def assert_space(self, space_id: str):
        """在调用收费服务前检查向量空间身份，空库允许首次建立身份。"""
        try:
            with closing(self._connect()) as connection:
                self._check_space(connection, space_id)
        except sqlite3.Error:
            raise StoreReadError("知识存储读取失败。") from None

    def document_record(self, domain: str, document_id: str):
        """按域和文档 ID 读取规范化原文及入库指纹；不存在返回 None。"""
        if domain == "both":
            raise ValueError("读取单份文档必须指定单域。")
        domains_for(domain)
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT fingerprint, payload FROM documents WHERE domain=? AND document_id=?",
                    (domain, document_id),
                ).fetchone()
            return (row[0], KnowledgeDocument.model_validate_json(row[1])) if row else None
        except (sqlite3.Error, ValueError):
            raise StoreReadError("知识文档读取失败。") from None

    def upsert(self, document: KnowledgeDocument, fingerprint: str, chunks: list[KnowledgeChunk],
               vectors: list[list[float]], space_id: str) -> str:
        """将一份文档和全部切片原子替换，返回 inserted / updated / unchanged。

        向量先在内存中完整校验，再开启事务。模型或写入失败不会先删除旧版本；同 ID
        更新会删除旧切片，防止陈旧事实被继续检索。指纹包含正文、来源和切片参数。
        """
        if not chunks or len(chunks) != len(vectors):
            raise StoreWriteError("切片与向量数量不一致，未写入。")
        try:
            normalized = [unit_vector(v) for v in vectors]
            dimension = len(normalized[0])
            if any(len(v) != dimension for v in normalized):
                raise ValueError("dimensions")
            if any(c.document_id != document.document_id or c.knowledge_domain != document.knowledge_domain
                   for c in chunks):
                raise ValueError("document_identity")
            rows = [(c.chunk_id, c.knowledge_domain, c.document_id, c.chunk_index,
                     c.model_dump_json(), json.dumps(v, allow_nan=False)) for c, v in zip(chunks, normalized)]
            with closing(self._connect()) as connection, connection:
                # BEGIN IMMEDIATE 使读指纹和替换属于同一事务，避免并发写入竞态。
                connection.execute("BEGIN IMMEDIATE")
                self._check_space(connection, space_id, dimension)
                old = connection.execute(
                    "SELECT fingerprint FROM documents WHERE domain=? AND document_id=?",
                    (document.knowledge_domain, document.document_id),
                ).fetchone()
                if old and old[0] == fingerprint:
                    return "unchanged"
                connection.execute("INSERT OR IGNORE INTO vector_space VALUES (1, ?, ?)", (space_id, dimension))
                connection.execute("DELETE FROM documents WHERE domain=? AND document_id=?",
                                   (document.knowledge_domain, document.document_id))
                connection.execute("INSERT INTO documents VALUES (?, ?, ?, ?)",
                                   (document.knowledge_domain, document.document_id, fingerprint, document.model_dump_json()))
                connection.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?)", rows)
            return "updated" if old else "inserted"
        except VectorSpaceError:
            raise
        except (sqlite3.Error, ValueError, TypeError, OSError):
            logger.warning("knowledge_store_write_failed")
            raise StoreWriteError("知识存储写入失败，旧版本未被替换。") from None

    def search(self, query_vector: list[float], *, knowledge_domain: str, space_id: str,
               top_n: int, filters: dict | None = None) -> list[RetrievalResult]:
        """先筛域和显式元数据，再计算精确余弦并截取 Top-N；空集合返回 []。

        输入是已向量化查询和程序给出的过滤条件；禁止把过滤交给模型。当前为线性
        扫描，适合 MVP 小语料；不是 ANN，也不将简单字段过滤宣称为安全权限系统。
        """
        domains = domains_for(knowledge_domain)
        if type(top_n) is not int or top_n < 1:
            raise ValueError("Top-N 必须为正整数。")
        filters = filters or {}
        allowed = {"product_id", "competitor_id", "source_type", "evidence_status"}
        if not set(filters) <= allowed:
            raise ValueError("不支持的元数据过滤字段。")
        vector = unit_vector(query_vector)
        try:
            with closing(self._connect()) as connection:
                self._check_space(connection, space_id, len(vector))
                placeholders = ",".join("?" for _ in domains)
                rows = connection.execute(
                    f"SELECT payload, vector FROM chunks WHERE domain IN ({placeholders})", domains,
                )
                scored = []
                for payload, raw_vector in rows:
                    chunk = KnowledgeChunk.model_validate_json(payload)
                    if any(getattr(chunk, key) != value for key, value in filters.items()):
                        continue
                    stored_vector = unit_vector(json.loads(raw_vector))
                    if len(stored_vector) != len(vector):
                        raise VectorSpaceError("存储向量维度不一致，不能继续检索。")
                    score = sum(a * b for a, b in zip(vector, stored_vector))
                    scored.append((max(-1.0, min(1.0, score)), chunk))
            scored.sort(key=lambda pair: (-pair[0], pair[1].chunk_id))
            return [RetrievalResult(**chunk.model_dump(), retrieval_score=score, retrieval_rank=i + 1)
                    for i, (score, chunk) in enumerate(scored[:top_n])]
        except VectorSpaceError:
            raise
        except (sqlite3.Error, ValueError, TypeError):
            raise StoreReadError("知识检索读取失败。") from None

    def counts(self) -> dict[str, int]:
        """返回文档/切片计数，供重复入库和持久化验收，不读取正文。"""
        try:
            with closing(self._connect()) as connection:
                return {name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                        for name in ("documents", "chunks")}
        except sqlite3.Error:
            raise StoreReadError("知识计数读取失败。") from None
