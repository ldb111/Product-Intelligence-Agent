"""小型 SQLite 状态仓库：保存产品状态、检查点和原始快照，不实现业务判断。"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def identity(value) -> str:
    """从明确的业务输入生成稳定内部键；键不要求产品使用者填写。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def product_event_id(event) -> str:
    """从 confirmed Event 的业务事实生成产品级稳定标识，不暴露内部 candidate_id。"""
    data = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
    facts = {key: data.get(key) for key in (
        "competitor", "event_type", "event_subject", "change_action", "change_items",
        "event_time", "effective_time", "affected_scope",
    )}
    return "evt_" + identity(facts)[:24]


def product_pending_id(event) -> str:
    """为产品层 Pending 展示生成独立标识；它不能被当成 confirmed Event ID。"""
    data = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
    value = {
        "competitor": data.get("competitor"), "decision_reason": data.get("decision_reason"),
        "uncertainty_reason": data.get("uncertainty_reason"),
        "evidence": [{key: item.get(key) for key in ("source_url", "observed_at", "relevant_excerpt")}
                     for item in data.get("evidence", [])],
    }
    return "pending_" + identity(value)[:24]


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

    def reset_competitor_test_data(
        self, *, workspace_id: str, product_id: str | None, competitor_name: str
    ) -> dict[str, int]:
        """原子清除一个已配置竞品的测试运行历史，同时保留正式配置和知识数据。

        输入使用产品层 workspace、own product 和 competitor 三元组。方法先核对已保存
        的竞品配置，再在一个 ``BEGIN IMMEDIATE`` 事务中删除该竞品独有 namespace、
        对应 run 检查点及其 Snapshot。``workspaces``、``sources``、产品配置和独立
        Knowledge Store 均不在删除范围内。输出只包含各类清理数量，不暴露内部键。
        """
        runtime_workspace = identity([workspace_id, competitor_name])
        scoped_namespaces = {
            "events": "events:" + runtime_workspace,
            "event_actions": "event_actions:" + runtime_workspace,
            "pending": "pending:" + runtime_workspace,
            "resolved": "resolved:" + runtime_workspace,
            "research_states": "research_status:" + runtime_workspace,
            "triage": "triage:" + runtime_workspace,
            "research_artifacts": "research:" + runtime_workspace,
            "verification_artifacts": "verification:" + runtime_workspace,
            "statement_observations": "statement_observations:" + runtime_workspace,
            "statement_processing": "statement_processing:" + runtime_workspace,
            "comparisons": "comparisons:" + runtime_workspace,
            "observed_latest": "observed_latest:" + runtime_workspace,
            "processed_latest": "processed_latest:" + runtime_workspace,
            "legacy_latest": "latest:" + runtime_workspace,
        }

        def load_rows(db, namespace: str) -> list[tuple[str, object]]:
            rows = db.execute(
                "SELECT key,body FROM records WHERE namespace=?", (namespace,)
            ).fetchall()
            return [(key, json.loads(body)) for key, body in rows]

        def collect_snapshot_ids(value, found: set[str]) -> None:
            """只收集明确命名的 Snapshot 引用，避免把任意哈希误当成快照删除。"""
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {
                        "snapshot_id", "previous_snapshot_id", "current_snapshot_id",
                        "first_seen_snapshot_id", "last_seen_snapshot_id",
                    } and isinstance(item, str):
                        found.add(item)
                    else:
                        collect_snapshot_ids(item, found)
            elif isinstance(value, list):
                for item in value:
                    collect_snapshot_ids(item, found)

        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            configured = db.execute(
                "SELECT body FROM records WHERE namespace='workspaces' AND key=?",
                (runtime_workspace,),
            ).fetchone()
            if configured is None:
                raise ValueError("待重置的竞品配置不存在。")
            saved_config = json.loads(configured[0]).get("config", {})
            if (
                saved_config.get("workspace_id") != workspace_id
                or saved_config.get("competitor_name") != competitor_name
                or saved_config.get("own_product_id") != product_id
            ):
                raise ValueError("测试重置范围与已保存竞品配置不一致。")

            run_ids: list[str] = []
            for run_id, run in load_rows(db, "runs"):
                config = run.get("product", {}) if isinstance(run, dict) else {}
                if (
                    config.get("workspace_id") == workspace_id
                    and config.get("competitor_name") == competitor_name
                    and config.get("own_product_id") == product_id
                ):
                    run_ids.append(run_id)

            snapshot_ids: set[str] = set()
            for name, namespace in scoped_namespaces.items():
                for _, value in load_rows(db, namespace):
                    if name in {"observed_latest", "processed_latest", "legacy_latest"}:
                        if isinstance(value, str):
                            snapshot_ids.add(value)
                    else:
                        collect_snapshot_ids(value, snapshot_ids)
            for run_id in run_ids:
                for _, value in load_rows(db, "steps:" + run_id):
                    collect_snapshot_ids(value, snapshot_ids)

            counts = {
                "events_cleared": len(load_rows(db, scoped_namespaces["events"])),
                "triage_results_cleared": len(load_rows(db, scoped_namespaces["triage"])),
                "pending_candidates_cleared": len(load_rows(db, scoped_namespaces["pending"])),
                "research_runs_cleared": len(load_rows(db, scoped_namespaces["research_states"])),
                "research_artifacts_cleared": len(load_rows(db, scoped_namespaces["research_artifacts"])),
                "statement_states_cleared": (
                    len(load_rows(db, scoped_namespaces["statement_observations"]))
                    + len(load_rows(db, scoped_namespaces["statement_processing"]))
                ),
                "monitoring_states_cleared": sum(
                    len(load_rows(db, scoped_namespaces[name]))
                    for name in (
                        "comparisons", "observed_latest", "processed_latest", "legacy_latest",
                        "event_actions", "resolved",
                    )
                ),
                "snapshots_cleared": len(snapshot_ids),
                "run_records_cleared": len(run_ids),
            }

            for namespace in scoped_namespaces.values():
                db.execute("DELETE FROM records WHERE namespace=?", (namespace,))
            for run_id in run_ids:
                for namespace in (
                    "steps:" + run_id,
                    "run_artifacts:" + run_id,
                    "run_events:" + run_id,
                    "run_event_origins:" + run_id,
                ):
                    db.execute("DELETE FROM records WHERE namespace=?", (namespace,))
                db.execute(
                    "DELETE FROM records WHERE namespace IN ('runs','results') AND key=?",
                    (run_id,),
                )
            for snapshot_id in snapshot_ids:
                db.execute(
                    "DELETE FROM records WHERE namespace='snapshots' AND key=?", (snapshot_id,)
                )
            db.execute(
                "DELETE FROM records WHERE namespace=? AND key=?",
                ("ui_competitor_scans:" + workspace_id, competitor_name),
            )
        return counts

    def claim_research(self, workspace: str, event_id: str, process_token: str, *, retry: bool = False) -> dict:
        """原子占用单个 Event 的 Research 执行权。

        输入由 workspace、产品级 confirmed Event ID、当前进程标识和显式 retry 组成。
        方法在一个 SQLite ``BEGIN IMMEDIATE`` 事务内重新读取最新状态，只允许
        ``not_started``，或 retry=True 时的 ``failed``，转换为 ``running``。这样两个
        几乎同时到达的请求不会都根据旧读取结果调用 Research Agent。旧进程留下的
        running 会先在同一事务内转为 interrupted；输出同时包含是否成功占用和最新状态。
        """
        namespace = "research_status:" + workspace
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT body FROM records WHERE namespace=? AND key=?", (namespace, event_id)
            ).fetchone()
            if row is None:
                return {"claimed": False, "state": None}
            state = json.loads(row[0])
            status = state.get("status")

            if status == "running":
                if state.get("process_token") == process_token:
                    return {"claimed": False, "state": state}
                state = {**state, "status": "failed", "error_type": "research_interrupted"}
                state.pop("process_token", None)
                self._put(db, namespace, event_id, state)
                status = "failed"

            if status == "completed" or (status == "failed" and not retry):
                return {"claimed": False, "state": state}
            if status not in {"not_started", "failed"}:
                raise ValueError("Research 状态无法进入 running。")

            running = {
                **state,
                "status": "running",
                "attempts": state.get("attempts", 0) + 1,
                "process_token": process_token,
            }
            running.pop("error_type", None)
            self._put(db, namespace, event_id, running)
            return {"claimed": True, "state": running}

    @staticmethod
    def follow_up_namespace(workspace_id: str, product_id: str, event_id: str) -> str:
        """把追问对话严格限定在 workspace、product、event 三元组内。"""
        return "research_follow_up:" + identity([workspace_id, product_id, event_id])

    def claim_follow_up(
        self, *, workspace_id: str, product_id: str, event_id: str,
        request_id: str, question: str, process_token: str,
    ) -> dict:
        """先原子保存用户消息并占用执行权，重复请求不会再次调用研究智能体。

        输入中的 request_id 只用于生成稳定 Turn 标识；原始值不进入记录。事务提交后，
        用户消息已可恢复，即使后续模型或 Memory 识别失败也不会丢失。
        """
        namespace = self.follow_up_namespace(workspace_id, product_id, event_id)
        turn_id = "turn_" + identity([workspace_id, product_id, event_id, request_id])[:24]
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT body FROM records WHERE namespace=? AND key=?", (namespace, turn_id)
            ).fetchone()
            if row is not None:
                return {"claimed": False, "turn": json.loads(row[0])}
            turn = {
                "turn_id": turn_id,
                "user_message": question,
                "status": "running",
                "attempts": 1,
                "process_token": process_token,
                "candidate_count": 0,
                "created_at": now,
                "updated_at": now,
            }
            self._put(db, namespace, turn_id, turn)
            return {"claimed": True, "turn": turn}

    def retry_follow_up(
        self, *, workspace_id: str, product_id: str, event_id: str,
        turn_id: str, process_token: str,
    ) -> dict:
        """只允许显式重试失败回合；成功或运行中的回合保持幂等。"""
        namespace = self.follow_up_namespace(workspace_id, product_id, event_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT body FROM records WHERE namespace=? AND key=?", (namespace, turn_id)
            ).fetchone()
            if row is None:
                return {"claimed": False, "turn": None}
            turn = json.loads(row[0])
            if turn.get("status") != "failed":
                return {"claimed": False, "turn": turn}
            turn.update(
                status="running", attempts=turn.get("attempts", 0) + 1,
                process_token=process_token,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            turn.pop("error_type", None)
            self._put(db, namespace, turn_id, turn)
            return {"claimed": True, "turn": turn}

    def finish_follow_up(
        self, *, workspace_id: str, product_id: str, event_id: str,
        turn_id: str, process_token: str, artifact: dict, candidate_count: int,
    ) -> dict:
        """在同一事务内保存回答 Artifact，再把回合标记为完成。"""
        namespace = self.follow_up_namespace(workspace_id, product_id, event_id)
        artifact_namespace = namespace + ":artifacts"
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT body FROM records WHERE namespace=? AND key=?", (namespace, turn_id)
            ).fetchone()
            if row is None:
                raise ValueError("待完成的追问回合不存在。")
            turn = json.loads(row[0])
            if turn.get("status") != "running" or turn.get("process_token") != process_token:
                raise ValueError("追问回合执行权已变化。")
            self._put(db, artifact_namespace, turn_id, artifact)
            turn.update(
                status="completed", candidate_count=max(0, candidate_count),
                artifact={"namespace": artifact_namespace, "key": turn_id},
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            turn.pop("process_token", None)
            self._put(db, namespace, turn_id, turn)
            return turn

    def fail_follow_up(
        self, *, workspace_id: str, product_id: str, event_id: str,
        turn_id: str, process_token: str,
    ) -> dict:
        """保留已经写入的用户消息，只记录可重试失败状态，不保存异常正文。"""
        namespace = self.follow_up_namespace(workspace_id, product_id, event_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT body FROM records WHERE namespace=? AND key=?", (namespace, turn_id)
            ).fetchone()
            if row is None:
                raise ValueError("待失败的追问回合不存在。")
            turn = json.loads(row[0])
            if turn.get("status") != "running" or turn.get("process_token") != process_token:
                return turn
            turn.update(
                status="failed", error_type="follow_up_error",
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            turn.pop("process_token", None)
            self._put(db, namespace, turn_id, turn)
            return turn

    def follow_up_turns(self, *, workspace_id: str, product_id: str, event_id: str) -> list[dict]:
        """按创建顺序返回一个事件的持久化对话，不跨产品或工作区读取。"""
        namespace = self.follow_up_namespace(workspace_id, product_id, event_id)
        return sorted(
            self.items(namespace).values(),
            key=lambda item: (item.get("created_at", ""), item.get("turn_id", "")),
        )

    def observe_statements(self, workspace: str, statements: list[dict]) -> dict[str, dict]:
        """原子记录本次成功扫描实际看见的全部 statement identity。

        输入包含结构提取所得的 statement_id、来源、Snapshot 和是否 Initial Scan。无论声明
        是否满足 90 天、是否进入 20 条 Backfill、是否带日期，都写入独立 observation 状态。
        输出为每个 identity 标明写入前是否已经 seen，供上层只把真正首次出现的后续声明
        认作 normal_monitoring。这里不创建 processing 状态，也不表示 Event Core 已处理。
        """
        namespace = "statement_observations:" + workspace
        observed: dict[str, dict] = {}
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for statement in statements:
                key = statement["statement_id"]
                if key in observed:
                    continue
                row = db.execute(
                    "SELECT body FROM records WHERE namespace=? AND key=?", (namespace, key)
                ).fetchone()
                if row is None:
                    state = {
                        "statement_id": key,
                        "source_url": statement["source_url"],
                        "first_seen_snapshot_id": statement["snapshot_id"],
                        "last_seen_snapshot_id": statement["snapshot_id"],
                        "seen_at_baseline": bool(statement["initial_backfill"]),
                    }
                    was_seen = False
                else:
                    state = json.loads(row[0])
                    state["last_seen_snapshot_id"] = statement["snapshot_id"]
                    was_seen = True
                self._put(db, namespace, key, state)
                observed[key] = {"was_seen": was_seen, "observation": state}
        return observed

    def register_statement(self, workspace: str, statement: dict) -> dict:
        """持久化一个准备进入 Path B 的 statement，不覆盖既有处理终态。

        输入包含稳定 statement_id、正式 DetectionInput、来源和 discovery_origin。首次写入
        状态为 pending；同一声明在 same-run/new-run 再次出现时返回旧状态，使失败项可重试、
        已成功项可跳过。Statement 状态独立于 Path A comparison watermark 和 Event Dedup。
        """
        namespace = "statement_processing:" + workspace
        key = statement["statement_id"]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT body FROM records WHERE namespace=? AND key=?", (namespace, key)
            ).fetchone()
            if row is not None:
                return json.loads(row[0])
            state = {**statement, "status": "pending", "attempts": 0}
            self._put(db, namespace, key, state)
            return state

    def mark_statement_processed(
        self, workspace: str, statement_id: str, outcomes: list[str], event_ids: list[str]
    ) -> dict:
        """在 Path B 正式结果安全持久化后，原子记录 statement 已完成。

        event 的 Event/Dedup outcome、uncertain 的 Pending Candidate 会由调用方先持久化；
        no_event 则已有成功 Detection checkpoint。这里只更新独立 statement 状态，因此
        Path B 的成功或失败不会推进、回退或伪造 Path A processed watermark。
        """
        allowed = {"event", "no_event", "uncertain"}
        normalized = list(dict.fromkeys(outcomes))
        if not normalized or not set(normalized) <= allowed:
            raise ValueError("Statement 只有在 Event Core 正式终态后才能完成。")
        namespace = "statement_processing:" + workspace
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT body FROM records WHERE namespace=? AND key=?", (namespace, statement_id)
            ).fetchone()
            if row is None:
                raise ValueError("待完成的 Statement 不存在。")
            state = json.loads(row[0])
            state.update(
                status="processed", event_core_outcomes=normalized,
                event_candidate_ids=list(dict.fromkeys(event_ids)),
                attempts=state.get("attempts", 0) + 1,
            )
            self._put(db, namespace, statement_id, state)
            return state

    @staticmethod
    def _put(db, namespace, key, body):
        db.execute("INSERT INTO records VALUES(?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET body=excluded.body",
                   (namespace, key, json.dumps(body, ensure_ascii=False, allow_nan=False)))

    @staticmethod
    def _legacy_processed_snapshot(db, snapshot_id: str) -> str | None:
        """从旧检查点判断 legacy latest 是否已经完成 Event Detection。

        旧版本没有独立 processed 指针。若能找到该 Snapshot 对应的旧 comparison，只有
        全部 detection 检查点完成才把它作为已处理基线；失败或尚未执行时退回它记录的
        previous_snapshot_id。找不到运行检查点时沿用旧 latest，避免破坏更早的正常状态。
        """
        snapshot_row = db.execute(
            "SELECT body FROM records WHERE namespace='snapshots' AND key=?", (snapshot_id,)
        ).fetchone()
        if snapshot_row is None:
            raise ValueError("旧 latest 指针指向不存在的 Snapshot。")
        history = json.loads(snapshot_row[0])
        if history.get("is_first_scan"):
            return snapshot_id
        scan_rows = db.execute(
            "SELECT namespace,body FROM records WHERE namespace LIKE 'steps:%' AND key=?",
            ("scan:" + snapshot_id,),
        ).fetchall()
        if not scan_rows:
            return snapshot_id
        namespace = scan_rows[-1][0]
        comparison_row = db.execute(
            "SELECT body FROM records WHERE namespace=? AND key=?",
            (namespace, "comparison:" + snapshot_id),
        ).fetchone()
        if comparison_row is None:
            return history.get("previous_snapshot_id")
        comparison = json.loads(comparison_row[0])
        if comparison.get("status") != "complete":
            return history.get("previous_snapshot_id")
        inputs = comparison.get("data", [])
        if not inputs:
            return snapshot_id
        detection_rows = db.execute(
            "SELECT body FROM records WHERE namespace=? AND key LIKE ?",
            (namespace, "detection:" + snapshot_id + ":%"),
        ).fetchall()
        detections = [json.loads(row[0]) for row in detection_rows]
        if len(detections) < len(inputs) or any(item.get("status") != "complete" for item in detections):
            return history.get("previous_snapshot_id")
        return snapshot_id

    def items(self, namespace: str) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT key,body FROM records WHERE namespace=? ORDER BY rowid", (namespace,)).fetchall()
        return {key: json.loads(body) for key, body in rows}

    def record_scan(self, workspace: str, run_id: str, url: str, snapshot: dict) -> dict:
        """保存真实观察，并以“已处理水位”建立待判断的比较。

        输入是 Stage 1 已创建的 Snapshot。当前观察会立刻写入历史和 observed 指针，
        但 processed 指针不会在这里推进。这样 Event Detection 失败时，后续新 run
        仍会使用上一个已处理 Snapshot，而不会把尚未判断的变化误当成新基线。
        输出包含固定的 previous/current 对；同一 run 恢复时直接复用，不重复扫描。
        """
        scan_id = identity([run_id, url])
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT body FROM records WHERE namespace='snapshots' AND key=?", (scan_id,)).fetchone()
            if old:
                return json.loads(old[0])
            processed = db.execute("SELECT body FROM records WHERE namespace=? AND key=?",
                                   ("processed_latest:" + workspace, url)).fetchone()
            if processed:
                previous_id = json.loads(processed[0])
            else:
                # 旧版本只有 latest 指针。首次升级读取时把它视为既有已处理基线，
                # 不改表、不重写历史记录，保证本地 SQLite 可以原位继续使用。
                legacy = db.execute("SELECT body FROM records WHERE namespace=? AND key=?",
                                    ("latest:" + workspace, url)).fetchone()
                legacy_id = json.loads(legacy[0]) if legacy else None
                previous_id = self._legacy_processed_snapshot(db, legacy_id) if legacy_id else None
                if previous_id:
                    self._put(db, "processed_latest:" + workspace, url, previous_id)
            previous = None
            if previous_id:
                row = db.execute("SELECT body FROM records WHERE namespace='snapshots' AND key=?", (previous_id,)).fetchone()
                if row is None:
                    raise ValueError("已处理 Snapshot 水位指向不存在的历史记录。")
                previous = json.loads(row[0])["current_snapshot"]
            comparison_id = identity([workspace, url, previous_id, scan_id])
            history = {"is_first_scan": previous is None, "current_snapshot": snapshot,
                       "previous_snapshot": previous, "snapshot_id": scan_id, "previous_snapshot_id": previous_id,
                       "comparison_id": comparison_id, "processing_status": "pending"}
            self._put(db, "snapshots", scan_id, history)
            self._put(db, "observed_latest:" + workspace, url, scan_id)
            self._put(db, "comparisons:" + workspace, comparison_id, {
                "source_url": url, "previous_snapshot_id": previous_id, "current_snapshot_id": scan_id,
                "status": "pending", "event_core_outcomes": [], "run_id": run_id,
            })
        return history

    def mark_comparison_processed(self, workspace: str, url: str, history: dict, outcomes: list[str]) -> dict:
        """在 Event Core 形成正式终态后推进 processed comparison watermark。

        输入是 record_scan 返回的固定比较和 Event Core 的正式结果；首次 Baseline 使用
        ``baseline``。方法在一个事务中完成 comparison 状态、Snapshot 状态和 processed
        指针更新。若更晚的 run 已经推进水位，旧 run 恢复只能补记完成状态，不能把水位
        回退。输出记录水位是否由本次比较推进，供检查点与审计复核。
        """
        allowed = {"baseline", "event", "no_event", "uncertain"}
        normalized = list(dict.fromkeys(outcomes))
        if not normalized or not set(normalized) <= allowed:
            raise ValueError("比较只有在 Baseline 或 Event Core 正式终态后才能完成。")
        scan_id = history["snapshot_id"]
        previous_id = history.get("previous_snapshot_id")
        comparison_id = history.get("comparison_id") or identity([workspace, url, previous_id, scan_id])
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot_row = db.execute(
                "SELECT body FROM records WHERE namespace='snapshots' AND key=?", (scan_id,)
            ).fetchone()
            comparison_row = db.execute(
                "SELECT body FROM records WHERE namespace=? AND key=?",
                ("comparisons:" + workspace, comparison_id),
            ).fetchone()
            if snapshot_row is None:
                raise ValueError("待完成的 Snapshot 不存在。")
            if comparison_row is None:
                # 同一个旧 run 在升级后恢复时，scan 步骤会直接返回旧格式 history。
                # 在这里补齐轻量 comparison 记录，不修改 Stage 1 Snapshot 内容。
                comparison = {"source_url": url, "previous_snapshot_id": previous_id,
                              "current_snapshot_id": scan_id, "status": "pending",
                              "event_core_outcomes": [], "run_id": None}
            else:
                comparison = json.loads(comparison_row[0])

            processed = db.execute(
                "SELECT body FROM records WHERE namespace=? AND key=?",
                ("processed_latest:" + workspace, url),
            ).fetchone()
            if processed:
                current_processed = json.loads(processed[0])
            else:
                legacy = db.execute("SELECT body FROM records WHERE namespace=? AND key=?",
                                    ("latest:" + workspace, url)).fetchone()
                legacy_id = json.loads(legacy[0]) if legacy else None
                current_processed = self._legacy_processed_snapshot(db, legacy_id) if legacy_id else None
            can_advance = current_processed in {None, previous_id, scan_id}
            # comparison outcome、Snapshot 处理状态和 watermark 在同一事务提交。
            # 先写 outcome 再写指针，使代码顺序也明确表达 no_event 的安全边界；
            # 任一步异常都会回滚整个事务，不会留下只有水位没有结论的状态。
            comparison.update(status="processed", event_core_outcomes=normalized,
                              watermark_advanced=can_advance)
            self._put(db, "comparisons:" + workspace, comparison_id, comparison)
            saved_history = json.loads(snapshot_row[0])
            saved_history.update(comparison_id=comparison_id, processing_status="processed", event_core_outcomes=normalized,
                                 watermark_advanced=can_advance)
            self._put(db, "snapshots", scan_id, saved_history)
            if can_advance:
                self._put(db, "processed_latest:" + workspace, url, scan_id)
            self._put(db, "observed_latest:" + workspace, url, scan_id)
        return {"snapshot_id": scan_id, "comparison_id": comparison_id,
                "event_core_outcomes": normalized, "watermark_advanced": can_advance}
