"""Long-term Memory 的独立 SQLite 持久化与事务性历史保留。"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from llm.retrieval_models import unit_vector

from .schemas import (
    MemoryRecord, MemoryReferences, MemoryStoreReadError, MemoryStoreWriteError,
    MemoryVectorSpaceError,
)

logger = logging.getLogger(__name__)

_COLUMNS = """memory_id, scope, scope_id, memory_type, memory_key, content, status,
source_kind, source_reference, source_text, created_at, updated_at, is_active,
supersedes_memory_id, references_json, metadata_json, vector_json"""


class MemoryStore:
    def __init__(self, path: str | Path):
        """创建独立 Memory 数据库，输入是持久化路径，输出是可复用 Store。

        数据库不与 Knowledge 表混用；一个 Memory 库固定一个 Embedding 向量空间。
        每次操作使用短连接，保证 Windows 下进程重启后可以重新打开同一文件。
        """
        self.path = Path(path)
        if str(path) == ":memory:":
            raise ValueError("长期记忆必须使用持久化文件路径。")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection, connection:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS vector_space (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        space_id TEXT NOT NULL, dimension INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS memories (
                        memory_id TEXT PRIMARY KEY,
                        scope TEXT NOT NULL CHECK(scope IN ('user','workspace','research')),
                        scope_id TEXT NOT NULL,
                        memory_type TEXT NOT NULL CHECK(memory_type IN (
                            'focus','preference','constraint','goal','decision','hypothesis','open_question'
                        )),
                        memory_key TEXT,
                        content TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('pending','confirmed','superseded')),
                        source_kind TEXT NOT NULL,
                        source_reference TEXT NOT NULL,
                        source_text TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        is_active INTEGER NOT NULL CHECK(is_active IN (0,1)),
                        supersedes_memory_id TEXT,
                        references_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        vector_json TEXT,
                        FOREIGN KEY(supersedes_memory_id) REFERENCES memories(memory_id) ON DELETE SET NULL
                    );
                    CREATE INDEX IF NOT EXISTS memories_scope_status
                        ON memories(scope, scope_id, status, is_active);
                    CREATE UNIQUE INDEX IF NOT EXISTS one_active_confirmed_memory_key
                        ON memories(scope, scope_id, memory_key)
                        WHERE memory_key IS NOT NULL AND status='confirmed' AND is_active=1;
                """)
        except (sqlite3.Error, OSError):
            logger.warning("memory_store_initialization_failed")
            raise MemoryStoreWriteError("Memory 存储初始化失败。") from None

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _check_space(connection, space_id: str, dimension: int | None = None) -> None:
        row = connection.execute("SELECT space_id, dimension FROM vector_space WHERE singleton=1").fetchone()
        if row and (row[0] != space_id or dimension is not None and row[1] != dimension):
            raise MemoryVectorSpaceError("Embedding 模型或维度与 Memory 库不一致；请使用独立库重新写入。")

    @staticmethod
    def _record(row) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row[0], scope=row[1], scope_id=row[2], memory_type=row[3], memory_key=row[4],
            content=row[5], status=row[6], source_kind=row[7], source_reference=row[8], source_text=row[9],
            created_at=row[10], updated_at=row[11], is_active=bool(row[12]), supersedes_memory_id=row[13],
            references=MemoryReferences.model_validate(json.loads(row[14])), metadata=json.loads(row[15]),
        )

    @staticmethod
    def _insert(connection, record: MemoryRecord, vector: list[float] | None) -> None:
        connection.execute(
            f"INSERT INTO memories ({_COLUMNS}) VALUES ({','.join('?' for _ in range(17))})",
            (
                record.memory_id, record.scope, record.scope_id, record.memory_type, record.memory_key,
                record.content, record.status, record.source_kind, record.source_reference, record.source_text,
                record.created_at.isoformat(), record.updated_at.isoformat(), int(record.is_active),
                record.supersedes_memory_id, record.references.model_dump_json(),
                json.dumps(record.metadata, ensure_ascii=False, sort_keys=True, allow_nan=False),
                json.dumps(vector, allow_nan=False) if vector is not None else None,
            ),
        )

    def assert_space(self, space_id: str) -> None:
        """在外部 Embedding 调用前核对空间身份；空库允许首次写入。"""
        try:
            with closing(self._connect()) as connection:
                self._check_space(connection, space_id)
        except sqlite3.Error:
            raise MemoryStoreReadError("Memory 向量空间读取失败。") from None

    def get(self, memory_id: str) -> MemoryRecord | None:
        """按稳定 ID 读取单条历史记录；不存在返回 None。"""
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(f"SELECT {_COLUMNS} FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            return self._record(row) if row else None
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            raise MemoryStoreReadError("Memory 记录读取失败。") from None

    def current_by_key(self, scope: str, scope_id: str, memory_key: str) -> MemoryRecord | None:
        """查找同 scope/key 的 active confirmed 记录，供显式 supersession 使用。"""
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM memories WHERE scope=? AND scope_id=? AND memory_key=? "
                    "AND status='confirmed' AND is_active=1", (scope, scope_id, memory_key),
                ).fetchone()
            return self._record(row) if row else None
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            raise MemoryStoreReadError("当前 Memory 读取失败。") from None

    def exact_active(self, record: MemoryRecord) -> MemoryRecord | None:
        """查找完全相同的 active 记忆，避免无 key 内容无限重复写入。"""
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM memories WHERE scope=? AND scope_id=? AND memory_type=? "
                    "AND content=? AND status=? AND is_active=1 ORDER BY created_at LIMIT 1",
                    (record.scope, record.scope_id, record.memory_type, record.content, record.status),
                ).fetchone()
            return self._record(row) if row else None
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            raise MemoryStoreReadError("Memory 去重读取失败。") from None

    def insert_confirmed(self, record: MemoryRecord, vector: list[float], space_id: str) -> tuple[MemoryRecord, str]:
        """原子插入 confirmed Memory，并在稳定 key 明确更新时保留 superseded 历史。

        输入是程序构造的 record、真实向量和空间身份。事务内再次检查并发状态；新旧
        内容相同则返回 unchanged。任何写入错误都会回滚旧 Memory 的 active 状态。
        """
        if record.status != "confirmed" or not record.is_active:
            raise MemoryStoreWriteError("confirmed 写入需要 active confirmed 记录。")
        try:
            normalized = unit_vector(vector)
            dimension = len(normalized)
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self._check_space(connection, space_id, dimension)
                old_row = None
                if record.memory_key:
                    old_row = connection.execute(
                        f"SELECT {_COLUMNS} FROM memories WHERE scope=? AND scope_id=? AND memory_key=? "
                        "AND status='confirmed' AND is_active=1",
                        (record.scope, record.scope_id, record.memory_key),
                    ).fetchone()
                else:
                    old_row = connection.execute(
                        f"SELECT {_COLUMNS} FROM memories WHERE scope=? AND scope_id=? AND memory_type=? "
                        "AND content=? AND status='confirmed' AND is_active=1 LIMIT 1",
                        (record.scope, record.scope_id, record.memory_type, record.content),
                    ).fetchone()
                if old_row:
                    old = self._record(old_row)
                    if old.content == record.content and old.memory_type == record.memory_type:
                        return old, "unchanged"
                    # 只有调用方提供相同稳定 key 的显式 confirmed 写入才进入本路径。
                    connection.execute(
                        "UPDATE memories SET status='superseded', is_active=0, updated_at=? WHERE memory_id=?",
                        (record.updated_at.isoformat(), old.memory_id),
                    )
                    record = record.model_copy(update={"supersedes_memory_id": old.memory_id})
                    action = "superseded"
                else:
                    action = "inserted"
                connection.execute("INSERT OR IGNORE INTO vector_space VALUES (1, ?, ?)", (space_id, dimension))
                self._insert(connection, record, normalized)
            return record, action
        except MemoryVectorSpaceError:
            raise
        except (sqlite3.Error, ValueError, TypeError, OSError):
            logger.warning("memory_store_confirmed_write_failed")
            raise MemoryStoreWriteError("Memory 更新失败，旧记录保持不变。") from None

    def insert_pending(self, record: MemoryRecord) -> tuple[MemoryRecord, str]:
        """保存未经确认的 Candidate；不生成向量，也不覆盖同 key 的 confirmed 记录。"""
        if record.status != "pending":
            raise MemoryStoreWriteError("Candidate 只能以 pending 状态写入。")
        old = self.exact_active(record)
        if old:
            return old, "unchanged"
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self._insert(connection, record, None)
            return record, "pending"
        except (sqlite3.Error, ValueError, TypeError, OSError):
            logger.warning("memory_store_pending_write_failed")
            raise MemoryStoreWriteError("Pending Memory 写入失败。") from None

    def eligible(self, *, scope: str, scope_id: str, memory_types: tuple[str, ...] | None = None):
        """先按 scope 和 active confirmed 强过滤，返回记录及内部向量供语义排名。"""
        values: list[object] = [scope, scope_id]
        clause = ""
        if memory_types:
            clause = " AND memory_type IN (" + ",".join("?" for _ in memory_types) + ")"
            values.extend(memory_types)
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    f"SELECT {_COLUMNS} FROM memories WHERE scope=? AND scope_id=? "
                    "AND status='confirmed' AND is_active=1" + clause, values,
                ).fetchall()
            result = []
            for row in rows:
                if row[16] is None:
                    raise ValueError("missing_vector")
                result.append((self._record(row), unit_vector(json.loads(row[16]))))
            return result
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            raise MemoryStoreReadError("可检索 Memory 读取失败。") from None

    def active_records(self, *, scope: str, scope_id: str,
                       statuses: tuple[str, ...] = ("confirmed", "pending")) -> list[MemoryRecord]:
        """读取一个明确业务范围内仍有效的长期信息，供产品服务生成安全列表。

        输入是现有 Memory scope、scope_id 和允许状态；SQL 在返回前完成范围与 active
        过滤，避免产品层先读取全库再自行筛选。输出不含向量，历史 superseded 记录默认
        不进入页面，但仍保留在数据库中供审计。
        """
        allowed = {"pending", "confirmed", "superseded"}
        if not statuses or len(set(statuses)) != len(statuses) or not set(statuses) <= allowed:
            raise ValueError("Memory 状态过滤无效。")
        placeholders = ",".join("?" for _ in statuses)
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    f"SELECT {_COLUMNS} FROM memories WHERE scope=? AND scope_id=? "
                    f"AND is_active=1 AND status IN ({placeholders}) "
                    "ORDER BY updated_at DESC, memory_id DESC",
                    [scope, scope_id, *statuses],
                ).fetchall()
            return [self._record(row) for row in rows]
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            raise MemoryStoreReadError("长期信息列表读取失败。") from None

    def check_query_space(self, space_id: str, dimension: int) -> None:
        """查询向量生成后核对模型和维度，防止静默截断余弦计算。"""
        try:
            with closing(self._connect()) as connection:
                self._check_space(connection, space_id, dimension)
        except sqlite3.Error:
            raise MemoryStoreReadError("Memory 向量空间读取失败。") from None

    def deactivate(self, memory_id: str, updated_at: datetime) -> bool:
        """将 Memory 软失效且保留来源历史；用于用户撤销或保留期处理。"""
        try:
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    "UPDATE memories SET is_active=0, updated_at=? WHERE memory_id=? AND is_active=1",
                    (updated_at.isoformat(), memory_id),
                )
            return cursor.rowcount == 1
        except sqlite3.Error:
            raise MemoryStoreWriteError("Memory 失效操作失败。") from None

    def delete(self, memory_id: str) -> bool:
        """物理删除指定 Memory，供明确的用户删除请求使用；不接受模糊范围。"""
        try:
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute("DELETE FROM memories WHERE memory_id=?", (memory_id,))
            return cursor.rowcount == 1
        except sqlite3.Error:
            raise MemoryStoreWriteError("Memory 删除失败。") from None

    def counts(self) -> dict[str, int]:
        """返回总数及各状态数量，供持久化和 supersession 验收。"""
        try:
            with closing(self._connect()) as connection:
                result = {"total": connection.execute("SELECT count(*) FROM memories").fetchone()[0]}
                result.update({status: connection.execute(
                    "SELECT count(*) FROM memories WHERE status=?", (status,)
                ).fetchone()[0] for status in ("pending", "confirmed", "superseded")})
            return result
        except sqlite3.Error:
            raise MemoryStoreReadError("Memory 计数读取失败。") from None
