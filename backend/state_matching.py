"""比较前后 Snapshot 中可比较的 Interactive State。

本模块属于 Stage 1 V0.3-4A。它消费 Task 5 已经组成的 Snapshot History，按完全相同
的 state_key 匹配前后状态，并只根据状态已经保存的 content_hash 分类。它不会重新
遍历网页、重新计算 Hash、生成详细 Diff，也不会改变现有页面级 Change Detection。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

if __package__:
    from .change_detection import EXTRACTION_VERSION_CHANGED_REASON
else:
    # 保持项目现有 ``python backend/page_reader.py`` 运行方式可用。
    from change_detection import EXTRACTION_VERSION_CHANGED_REASON


INTERACTIVE_STATE_SCHEMA_UNAVAILABLE_REASON = (
    "interactive_state_schema_unavailable"
)
INTERACTIVE_STATE_SCHEMA_VERSION_CHANGED_REASON = (
    "interactive_state_schema_version_changed"
)
TRAVERSAL_INCOMPLETE_REASON = "traversal_incomplete"


class StateMatchingError(Exception):
    """表示状态匹配输入损坏，无法生成可信比较结论。

    输入：稳定错误类型和面向调用方的说明。
    处理：保留说明，并将错误类型单独存放，方便命令行或 API 转为结构化错误。
    输出：由匹配函数抛出；不会用缺失字段静默制造 unchanged 或 modified。
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class _StateCollection:
    """保存已排除 navigation-only 状态后的有序状态集合。"""

    by_key: dict[str, dict[str, Any]]
    ordered_keys: list[str]
    excluded_count: int


def _require_snapshot(snapshot: Any, name: str) -> dict[str, Any]:
    """确认输入确实是 Snapshot 对象，避免后续错误被误报为状态变化。"""
    if not isinstance(snapshot, dict):
        raise StateMatchingError(
            "invalid_state_matching_input", f"{name} must be a snapshot object."
        )
    return snapshot


def _optional_version(
    snapshot: dict[str, Any], field: str, snapshot_name: str
) -> str | None:
    """读取旧快照可能缺少的版本字段；字段存在但类型错误时明确失败。"""
    value = snapshot.get(field)
    if value is not None and not isinstance(value, str):
        raise StateMatchingError(
            "invalid_state_matching_input",
            f"{field} in {snapshot_name} must be a string.",
        )
    return value


def _traversal_status(snapshot: dict[str, Any], snapshot_name: str) -> tuple[str, bool]:
    """读取遍历状态，并判断它是否足以支持 current-only/previous-only 结论。

    ``complete`` 只说明编排过程没有记录错误；页面还必须成功恢复默认状态，才算一次
    完整可审计的覆盖。旧数据缺少遍历元数据时返回 unknown/incomplete，而不是猜测完整。
    """
    traversal = snapshot.get("interactive_state_traversal")
    if traversal is None:
        return "unknown", False
    if not isinstance(traversal, dict):
        raise StateMatchingError(
            "invalid_state_matching_input",
            f"interactive_state_traversal in {snapshot_name} must be an object.",
        )

    status = traversal.get("status")
    if status not in {"complete", "partial", "aborted"}:
        raise StateMatchingError(
            "invalid_state_matching_input",
            f"Traversal status in {snapshot_name} must be complete, partial, or aborted.",
        )
    page_restored = traversal.get("page_restored")
    if not isinstance(page_restored, bool):
        raise StateMatchingError(
            "invalid_state_matching_input",
            f"page_restored in {snapshot_name} must be a boolean.",
        )
    return status, status == "complete" and page_restored


def _collect_comparable_states(
    snapshot: dict[str, Any], snapshot_name: str
) -> _StateCollection:
    """读取并校验可比较状态，完全排除 comparison_eligible=false 的节点。

    字段缺省按 true 处理，以兼容 D-1 和早期 D-2 生成的正常状态。被排除的导航父节点
    不要求 content_hash，也不会出现在匹配结果中。其余状态必须具有字符串 key/hash；
    同一快照内重复 key 会让一对一匹配含义不明确，因此明确报错而不是覆盖。
    """
    raw_states = snapshot.get("interactive_states")
    if not isinstance(raw_states, list):
        raise StateMatchingError(
            "invalid_state_matching_input",
            f"interactive_states in {snapshot_name} must be a list.",
        )

    by_key: dict[str, dict[str, Any]] = {}
    ordered_keys: list[str] = []
    excluded_count = 0
    for index, state in enumerate(raw_states):
        if not isinstance(state, dict):
            raise StateMatchingError(
                "invalid_state_matching_input",
                f"Interactive state {index} in {snapshot_name} must be an object.",
            )
        comparison_eligible = state.get("comparison_eligible", True)
        if not isinstance(comparison_eligible, bool):
            raise StateMatchingError(
                "invalid_state_matching_input",
                f"comparison_eligible for state {index} in {snapshot_name} must be a boolean.",
            )
        if not comparison_eligible:
            excluded_count += 1
            continue

        state_key = state.get("state_key")
        content_hash = state.get("content_hash")
        if not isinstance(state_key, str) or not state_key:
            raise StateMatchingError(
                "invalid_state_matching_input",
                f"Comparable state {index} in {snapshot_name} has no valid state_key.",
            )
        if not isinstance(content_hash, str) or not content_hash:
            raise StateMatchingError(
                "invalid_state_matching_input",
                f"Comparable state {state_key} in {snapshot_name} has no valid content_hash.",
            )
        if state_key in by_key:
            raise StateMatchingError(
                "duplicate_state_key",
                f"Duplicate comparable state_key in {snapshot_name}: {state_key}.",
            )
        by_key[state_key] = state
        ordered_keys.append(state_key)

    return _StateCollection(by_key, ordered_keys, excluded_count)


def _state_identity_fields(state: dict[str, Any]) -> dict[str, Any]:
    """保留解释匹配结果所需的语义路径，不复制完整 Blocks。"""
    return {
        "scope_path": state.get("scope_path"),
        "state_path": state.get("state_path"),
    }


def _base_result(
    *,
    is_first_scan: bool,
    changed: bool | None,
    comparison_status: str,
    comparison_skipped_reason: str | None,
    current_schema_version: str | None,
) -> dict[str, Any]:
    """建立字段稳定的结果骨架，供基线、跳过和正常比较共同使用。"""
    return {
        "is_first_scan": is_first_scan,
        "changed": changed,
        "comparison_status": comparison_status,
        "comparison_skipped_reason": comparison_skipped_reason,
        "interactive_state_schema_version": current_schema_version,
        "previous_traversal_status": None,
        "current_traversal_status": None,
        "previous_eligible_state_count": 0,
        "current_eligible_state_count": 0,
        "excluded_state_count": 0,
        "state_changes": [],
    }


def match_interactive_states(snapshot_history: dict[str, Any]) -> dict[str, Any]:
    """按稳定 state_key 比较前后 Snapshot 的 Interactive State。

    输入：Task 5 输出的 is_first_scan、current_snapshot、previous_snapshot。
    处理：首次扫描只建基线；非首次先核对页面抽取版本和独立 State schema，再排除
    comparison_eligible=false 的状态，按完全相同 key 匹配并比较已有 content_hash。
    输出：状态级分类和三态 changed。V1 不做模糊匹配，也不生成详细状态 Diff。

    完整性规则：modified 因为前后都有明确观察而属于确定变化；只有一侧出现的状态，
    只有在前后遍历都 complete 且页面已恢复时才分类为 current_only/previous_only。
    任一侧 partial/aborted/unknown 时分别使用 unresolved 或 missing_unconfirmed。若没有
    确定变化且覆盖不完整，changed=None，不能把“没采到”误写成“没有变化”。
    """
    if not isinstance(snapshot_history, dict):
        raise StateMatchingError(
            "invalid_state_matching_input", "Snapshot history must be an object."
        )
    try:
        is_first_scan = snapshot_history["is_first_scan"]
        current_raw = snapshot_history["current_snapshot"]
        previous_raw = snapshot_history["previous_snapshot"]
    except KeyError as exc:
        raise StateMatchingError(
            "invalid_state_matching_input",
            f"Snapshot history is missing required field: {exc.args[0]}.",
        ) from exc
    if not isinstance(is_first_scan, bool):
        raise StateMatchingError(
            "invalid_state_matching_input", "is_first_scan must be a boolean."
        )

    current_snapshot = _require_snapshot(current_raw, "current_snapshot")
    current_schema = _optional_version(
        current_snapshot,
        "interactive_state_schema_version",
        "current_snapshot",
    )

    # First Scan 只有一个观察点，任何 current-only 状态都只是基线，不是“最近新增”。
    if is_first_scan:
        return _base_result(
            is_first_scan=True,
            changed=None,
            comparison_status="baseline",
            comparison_skipped_reason=None,
            current_schema_version=current_schema,
        )

    previous_snapshot = _require_snapshot(previous_raw, "previous_snapshot")
    previous_extraction = _optional_version(
        previous_snapshot, "extraction_version", "previous_snapshot"
    )
    current_extraction = _optional_version(
        current_snapshot, "extraction_version", "current_snapshot"
    )
    if previous_extraction != current_extraction:
        return _base_result(
            is_first_scan=False,
            changed=None,
            comparison_status="not_comparable",
            comparison_skipped_reason=EXTRACTION_VERSION_CHANGED_REASON,
            current_schema_version=current_schema,
        )

    previous_schema = _optional_version(
        previous_snapshot,
        "interactive_state_schema_version",
        "previous_snapshot",
    )
    if previous_schema is None or current_schema is None:
        # 缺少 schema 的历史文件不能被当成“已完整遍历但零状态”，否则所有当前状态都会
        # 被误报为 current_only。旧文件保持可读，但本次状态比较明确不可用。
        return _base_result(
            is_first_scan=False,
            changed=None,
            comparison_status="not_comparable",
            comparison_skipped_reason=INTERACTIVE_STATE_SCHEMA_UNAVAILABLE_REASON,
            current_schema_version=current_schema,
        )
    if previous_schema != current_schema:
        return _base_result(
            is_first_scan=False,
            changed=None,
            comparison_status="not_comparable",
            comparison_skipped_reason=INTERACTIVE_STATE_SCHEMA_VERSION_CHANGED_REASON,
            current_schema_version=current_schema,
        )

    previous_status, previous_complete = _traversal_status(
        previous_snapshot, "previous_snapshot"
    )
    current_status, current_complete = _traversal_status(
        current_snapshot, "current_snapshot"
    )
    previous_states = _collect_comparable_states(
        previous_snapshot, "previous_snapshot"
    )
    current_states = _collect_comparable_states(current_snapshot, "current_snapshot")
    complete_comparison = previous_complete and current_complete
    state_changes: list[dict[str, Any]] = []

    # 先按 current 的原始顺序输出匹配项和 current-only，便于结果与本轮遍历审计对应。
    for state_key in current_states.ordered_keys:
        current_state = current_states.by_key[state_key]
        previous_state = previous_states.by_key.get(state_key)
        if previous_state is None:
            change_type = "current_only" if complete_comparison else "unresolved"
            change = {
                "state_key": state_key,
                **_state_identity_fields(current_state),
                "change_type": change_type,
                "observation": "current_only",
                "previous_content_hash": None,
                "current_content_hash": current_state["content_hash"],
            }
            if not complete_comparison:
                change["reason"] = TRAVERSAL_INCOMPLETE_REASON
            state_changes.append(change)
            continue

        unchanged = previous_state["content_hash"] == current_state["content_hash"]
        state_changes.append(
            {
                "state_key": state_key,
                **_state_identity_fields(current_state),
                "change_type": "unchanged" if unchanged else "modified",
                "previous_content_hash": previous_state["content_hash"],
                "current_content_hash": current_state["content_hash"],
            }
        )

    # previous-only 在 current 顺序中没有位置，因此按上一轮原始顺序追加。
    for state_key in previous_states.ordered_keys:
        if state_key in current_states.by_key:
            continue
        previous_state = previous_states.by_key[state_key]
        change_type = "previous_only" if complete_comparison else "missing_unconfirmed"
        change = {
            "state_key": state_key,
            **_state_identity_fields(previous_state),
            "change_type": change_type,
            "observation": "previous_only",
            "previous_content_hash": previous_state["content_hash"],
            "current_content_hash": None,
        }
        if not complete_comparison:
            change["reason"] = TRAVERSAL_INCOMPLETE_REASON
        state_changes.append(change)

    definite_change_types = {"modified", "current_only", "previous_only"}
    has_definite_change = any(
        change["change_type"] in definite_change_types for change in state_changes
    )
    changed: bool | None
    if has_definite_change:
        changed = True
    elif complete_comparison:
        changed = False
    else:
        changed = None

    result = _base_result(
        is_first_scan=False,
        changed=changed,
        comparison_status="complete" if complete_comparison else "incomplete",
        comparison_skipped_reason=None,
        current_schema_version=current_schema,
    )
    result.update(
        {
            "previous_traversal_status": previous_status,
            "current_traversal_status": current_status,
            "previous_eligible_state_count": len(previous_states.by_key),
            "current_eligible_state_count": len(current_states.by_key),
            "excluded_state_count": (
                previous_states.excluded_count + current_states.excluded_count
            ),
            "state_changes": state_changes,
        }
    )
    return result
