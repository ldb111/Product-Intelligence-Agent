"""只读适配 Stage 1 快照与局部差异，不改变冻结采集或比较行为。"""

from __future__ import annotations

import json
from typing import Any

from backend.change_detection import detect_change
from backend.content_diff import build_diff_result
from backend.contextual_state_diff import build_contextual_state_diff
from backend.state_matching import match_interactive_states

from .schemas import DetectionInput, SourceCredibility, StateContext


def _local_side(changes: list[dict], side: str) -> str | None:
    """提取差异已有的某一侧值；缺失保持 None，绝不改写为不存在。"""
    values: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if side in value and value[side] is not None:
                item = value[side]
                values.append(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
            else:
                for key in ("changed_rows", "item_changes", "block_changes"):
                    visit(value.get(key))
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(changes)
    return "\n".join(dict.fromkeys(values)) or None


def _local_context(changes: list[dict]) -> str | None:
    """保留局部标题、表头和邻近日期，避免把整页内容作为默认模型输入。"""
    context = [
        {key: change[key] for key in ("heading_path", "headers", "context_blocks", "card_title") if key in change}
        for change in changes
    ]
    return json.dumps(context, ensure_ascii=False) if context else None


def build_state_comparison_input(
    history: dict[str, Any], *, competitor: str, source_type: str,
    source_credibility: SourceCredibility = "unverified", state_key: str | None = None,
    diff_indexes: list[int] | None = None,
) -> DetectionInput:
    """从正式 Snapshot History 构造 Path A 局部输入。

    输入是 Stage 1 前后快照，以及调用方选择的状态和差异下标。程序复用冻结的比较与
    Diff 引擎，只传差异两侧与相邻上下文。默认保留同一状态的整组差异，让模型按业务
    动作拆分；超出输入上限时明确失败，由调用方选择更小的业务范围，不静默截断变化。
    """
    current = history["current_snapshot"]
    previous = history["previous_snapshot"]
    if previous is not None and previous["url"] != current["url"]:
        raise ValueError("不能把不同来源的 Snapshot 当成同一状态的历史。")
    scope: dict[str, Any] = {}
    previous_at = previous["captured_at"] if previous else None
    current_at = current["captured_at"]
    if state_key is None:
        result = build_diff_result(detect_change(history))
        status = (
            "baseline" if result["is_first_scan"] else
            "not_comparable" if result["changed"] is None else
            "comparable" if result["changed"] else "unchanged"
        )
        changes = result["contextual_diff"] or []
    else:
        comparison = match_interactive_states(history)
        change = next((c for c in comparison["state_changes"] if c["state_key"] == state_key), None)
        status = comparison["comparison_status"]
        if change is None and status not in {"baseline", "not_comparable"}:
            raise ValueError("请求的状态不存在或没有可比较资格。")
        if change is not None:
            status = "comparable" if change["change_type"] == "modified" else change["change_type"]
            scope = {key: change.get(key) or [] for key in ("scope_path", "state_path")}
        state_diffs = build_contextual_state_diff(history, comparison)
        selected = next((d for d in state_diffs if d["state_key"] == state_key), None)
        changes = selected["contextual_diff"] if selected else []
        for snapshot, side in ((previous, "previous"), (current, "current")):
            state = next((s for s in (snapshot or {}).get("interactive_states", []) if s["state_key"] == state_key), None)
            if state is not None:
                if side == "previous":
                    previous_at = state["captured_at"]
                else:
                    current_at = state["captured_at"]
    if diff_indexes is not None:
        if not diff_indexes or len(set(diff_indexes)) != len(diff_indexes) or any(i < 0 or i >= len(changes) for i in diff_indexes):
            raise ValueError("差异下标必须唯一且属于当前差异集合。")
        changes = [changes[i] for i in diff_indexes]
    old_text, new_text = _local_side(changes, "previous"), _local_side(changes, "current")
    # 基线和不可比较项只保留短审计片段；这些分支由程序直接判定，不发送给模型。
    if not changes:
        new_text = current.get("content", "")[:2000] or None
    return DetectionInput(
        competitor=competitor, source_url=current["final_url"], source_title=current["title"],
        source_type=source_type, source_credibility=source_credibility, detection_path="state_comparison",
        state_context=StateContext(comparison_status=status, state_key=state_key, **scope),
        previous_state=old_text, current_state=new_text, contextual_diff=changes,
        previous_observed_at=previous_at, current_observed_at=current_at,
        relevant_source_context=_local_context(changes),
    )


def build_explicit_statement_input(
    snapshot: dict[str, Any], excerpt: str, *, competitor: str, source_type: str,
    source_credibility: SourceCredibility = "unverified", relevant_source_context: str | None = None,
) -> DetectionInput:
    """从已采集页面选择 Path B 原文，返回不含模型创造证据的正式输入。

    excerpt 与可选上下文必须逐字存在于 Snapshot.content；来源、标题、观察时间复制
    快照。调用方负责官方身份的确认，本层不从域名猜测可信度，也不重新读取网页。
    """
    content = snapshot["content"]
    if not excerpt.strip() or excerpt not in content:
        raise ValueError("明确变化声明必须是快照中的非空原文片段。")
    if relevant_source_context and relevant_source_context not in content:
        raise ValueError("补充上下文不属于同一快照。")
    return DetectionInput(
        competitor=competitor, source_url=snapshot["final_url"], source_title=snapshot["title"],
        source_type=source_type, source_credibility=source_credibility,
        detection_path="explicit_statement", current_state=excerpt,
        current_observed_at=snapshot["captured_at"], relevant_source_context=relevant_source_context,
    )
