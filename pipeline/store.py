"""小型 SQLite 状态仓库：保存产品状态、检查点和原始快照，不实现业务判断。"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def identity(value) -> str:
    """从明确的业务输入生成稳定内部键；键不要求产品使用者填写。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class PipelineStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "pipeline.sqlite3"
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (namespace TEXT, key TEXT, body TEXT NOT NULL, PRIMARY KEY(namespace,key))")

    @contextmanager
    def connection(self):
        """每次操作独立提交并关闭连接；SQLite 异常向上传播，不能假装已经保存。"""
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, namespace: str, key: str, default=None):
        with self.connection() as db:
            row = db.execute("SELECT body FROM records WHERE namespace=? AND key=?", (namespace, key)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, namespace: str, key: str, body) -> None:
        """按任务/对象键幂等覆盖序列化记录；不覆盖其他工作区或其他对象。"""
        with self.connection() as db:
            self._put(db, namespace, key, body)

    @staticmethod
    def _put(db, namespace, key, body):
        db.execute("INSERT INTO records VALUES(?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET body=excluded.body",
                   (namespace, key, json.dumps(body, ensure_ascii=False, allow_nan=False)))

    def items(self, namespace: str) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT key,body FROM records WHERE namespace=? ORDER BY rowid", (namespace,)).fetchall()
        return {key: json.loads(body) for key, body in rows}

    def record_scan(self, workspace: str, run_id: str, url: str, snapshot: dict) -> dict:
        """原子保存当前快照及上一份引用，同一次扫描重试始终返回原来的历史对。

        快照由冻结 Stage 1 创建；这里仅建立关联，不重新实现 Hash/Diff/状态匹配。
        latest 指针与快照同事务提交，避免崩溃恢复时把当前快照误当成自己的历史。
        """
        scan_id = identity([run_id, url])
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT body FROM records WHERE namespace='snapshots' AND key=?", (scan_id,)).fetchone()
            if old:
                return json.loads(old[0])
            latest = db.execute("SELECT body FROM records WHERE namespace=? AND key=?", ("latest:" + workspace, url)).fetchone()
            previous_id = json.loads(latest[0]) if latest else None
            previous = None
            if previous_id:
                row = db.execute("SELECT body FROM records WHERE namespace='snapshots' AND key=?", (previous_id,)).fetchone()
                previous = json.loads(row[0])["current_snapshot"]
            history = {"is_first_scan": previous is None, "current_snapshot": snapshot,
                       "previous_snapshot": previous, "snapshot_id": scan_id, "previous_snapshot_id": previous_id}
            self._put(db, "snapshots", scan_id, history)
            self._put(db, "latest:" + workspace, url, scan_id)
        return history
