"""为 V0.3-4A 已确认 modified 的 Interactive State 生成结构化差异。

本模块只负责 Stage 1 V0.3-4B。它不重新匹配状态、不判断 changed，也不改变页面级
Contextual Diff。基础 Paragraph、Table、List 与 heading_path 继续复用现有引擎；本层
只补充 State 身份以及 Group/Card、card title、key_values、text_marks 的结构归属。
"""

from __future__ import annotations

import difflib
import json
from copy import deepcopy
from typing import Any

if __package__:
    from .contextual_diff import build_contextual_diff
else:
    from contextual_diff import build_contextual_diff


class ContextualStateDiffError(Exception):
    """表示 modified 状态缺少生成可信结构差异所需的数据。"""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def _comparable_states_by_key(
    snapshot: Any, snapshot_name: str
) -> dict[str, dict[str, Any]]:
    """从 Snapshot 取得可比较状态，并按精确 state_key 建立索引。

    comparison_eligible 缺省按 true，显式 false 的导航节点完全排除。4B 不做模糊匹配，
    也不会把重复 key 静默覆盖，因为那会让详细 Diff 无法确定对应哪份 Blocks。
    """
    if not isinstance(snapshot, dict):
        raise ContextualStateDiffError(
            "invalid_contextual_state_diff_input",
            f"{snapshot_name} must be a snapshot object.",
        )
    states = snapshot.get("interactive_states")
    if not isinstance(states, list):
        raise ContextualStateDiffError(
            "invalid_contextual_state_diff_input",
            f"interactive_states in {snapshot_name} must be a list.",
        )

    indexed: dict[str, dict[str, Any]] = {}
    for index, state in enumerate(states):
        if not isinstance(state, dict):
            raise ContextualStateDiffError(
                "invalid_contextual_state_diff_input",
                f"Interactive state {index} in {snapshot_name} must be an object.",
            )
        if state.get("comparison_eligible", True) is False:
            continue
        state_key = state.get("state_key")
        if not isinstance(state_key, str) or not state_key:
            raise ContextualStateDiffError(
                "invalid_contextual_state_diff_input",
                f"Comparable state {index} in {snapshot_name} has no valid state_key.",
            )
        if state_key in indexed:
            raise ContextualStateDiffError(
                "duplicate_state_key",
                f"Duplicate comparable state_key in {snapshot_name}: {state_key}.",
            )
        indexed[state_key] = state
    return indexed


def _canonical_value(value: Any) -> str:
    """把结构数据转换成确定性 JSON，仅用于判断两份结构是否完全相同。"""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ContextualStateDiffError(
            "invalid_contextual_state_diff_input",
            f"Structured Blocks contain a value that cannot be serialized: {exc}",
        ) from exc


def _split_state_blocks(
    blocks: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把基础 Blocks 与 Group/Card 分开，避免同一 Card 变化被重复输出。

    页面级 Contextual Diff 会透明展开 group。状态 Diff 需要保留 card title 和 marks，
    因此基础引擎只接收非 group Blocks；group 中的 cards 由本模块逐卡调用同一个基础
    引擎比较其 card.blocks。
    """
    ordinary_blocks: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "group":
            ordinary_blocks.append(block)
            continue
        raw_cards = block.get("cards")
        if isinstance(raw_cards, list):
            cards.extend(card for card in raw_cards if isinstance(card, dict))
    return ordinary_blocks, cards


def _card_title(card: dict[str, Any]) -> str:
    """读取 Card 的显示标题；缺失时返回空串并保持位置级差异。"""
    title = card.get("title")
    return title if isinstance(title, str) else ""


def _card_change(
    previous_card: dict[str, Any] | None,
    current_card: dict[str, Any] | None,
) -> dict[str, Any]:
    """生成一张 Card 的结构化 previous/current 和内部基础 Block 差异。"""
    change_type = (
        "added"
        if previous_card is None
        else "deleted"
        if current_card is None
        else "modified"
    )
    previous_blocks = previous_card.get("blocks") if previous_card else None
    current_blocks = current_card.get("blocks") if current_card else None
    block_changes = None
    if isinstance(previous_blocks, list) and isinstance(current_blocks, list):
        # 复用页面级结构引擎，保留 paragraph/table/list 和 heading_path 语义。
        block_changes = build_contextual_diff(
            {"blocks": previous_blocks}, {"blocks": current_blocks}
        )

    return {
        "block_type": "card",
        "change_type": change_type,
        "card_title": _card_title(current_card or previous_card or {}),
        "previous_card_title": (
            _card_title(previous_card) if previous_card is not None else None
        ),
        "current_card_title": (
            _card_title(current_card) if current_card is not None else None
        ),
        # previous/current 保留原始结构，而不是把卡片压成一段文本。key_values、links 和
        # text_marks 因此都能被下游准确审计，且不会在采集层解释业务含义。
        "previous": deepcopy(previous_card),
        "current": deepcopy(current_card),
        "block_changes": block_changes,
    }


def _build_card_changes(
    previous_cards: list[dict[str, Any]],
    current_cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 Card title 和 DOM 顺序对齐卡片，输出新增、删除或修改。

    title 只用于同一状态内部的结构对齐，不参与 state_key 身份。标题不同的替换保守地
    表示为删除与新增；同标题但内容不同才称为 Card modified。
    """
    matcher = difflib.SequenceMatcher(
        None,
        [_card_title(card) for card in previous_cards],
        [_card_title(card) for card in current_cards],
        autojunk=False,
    )
    changes: list[dict[str, Any]] = []
    for operation, previous_start, previous_end, current_start, current_end in (
        matcher.get_opcodes()
    ):
        previous_slice = previous_cards[previous_start:previous_end]
        current_slice = current_cards[current_start:current_end]
        if operation == "equal":
            for previous_card, current_card in zip(previous_slice, current_slice):
                if _canonical_value(previous_card) != _canonical_value(current_card):
                    changes.append(_card_change(previous_card, current_card))
            continue
        if operation == "replace":
            # 标题身份已经不同，不能把两张业务卡强行解释为同一 Card 的字段修改。
            changes.extend(_card_change(card, None) for card in previous_slice)
            changes.extend(_card_change(None, card) for card in current_slice)
        elif operation == "delete":
            changes.extend(_card_change(card, None) for card in previous_slice)
        elif operation == "insert":
            changes.extend(_card_change(None, card) for card in current_slice)
    return changes


def _build_state_block_diff(
    previous_blocks: list[dict[str, Any]],
    current_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """复用基础 Contextual Diff，并补充保留 Card 归属的结构差异。"""
    previous_ordinary, previous_cards = _split_state_blocks(previous_blocks)
    current_ordinary, current_cards = _split_state_blocks(current_blocks)
    ordinary_changes = build_contextual_diff(
        {"blocks": previous_ordinary}, {"blocks": current_ordinary}
    )
    return [
        *(ordinary_changes or []),
        *_build_card_changes(previous_cards, current_cards),
    ]


def build_contextual_state_diff(
    snapshot_history: dict[str, Any],
    state_comparison: dict[str, Any],
) -> list[dict[str, Any]]:
    """只为 4A 分类为 modified 的同一 state_key 生成 Contextual State Diff。

    输入：包含前后 Snapshot 的正式历史，以及未被改写的 4A State Matching 结果。
    处理：忽略 baseline、unchanged、current/previous-only、不确定项和排除节点；对每个
    modified key 从前后 Snapshot 精确取回 Structured Blocks，复用基础 Contextual Diff，
    再补充 Card 结构差异。
    输出：每个 modified 状态一条记录，包含语义身份、前后 Hash 和 contextual_diff。
    """
    if not isinstance(snapshot_history, dict) or not isinstance(
        state_comparison, dict
    ):
        raise ContextualStateDiffError(
            "invalid_contextual_state_diff_input",
            "Snapshot history and State Matching result must be objects.",
        )
    changes = state_comparison.get("state_changes")
    if not isinstance(changes, list):
        raise ContextualStateDiffError(
            "invalid_contextual_state_diff_input",
            "State Matching result must contain a state_changes list.",
        )
    modified_changes = [
        change
        for change in changes
        if isinstance(change, dict) and change.get("change_type") == "modified"
    ]
    if not modified_changes:
        return []

    previous_states = _comparable_states_by_key(
        snapshot_history.get("previous_snapshot"), "previous_snapshot"
    )
    current_states = _comparable_states_by_key(
        snapshot_history.get("current_snapshot"), "current_snapshot"
    )
    results: list[dict[str, Any]] = []
    for change in modified_changes:
        state_key = change.get("state_key")
        if not isinstance(state_key, str) or state_key not in previous_states or state_key not in current_states:
            raise ContextualStateDiffError(
                "modified_state_not_found",
                "A modified state_key is missing from the previous or current Snapshot.",
            )
        previous_state = previous_states[state_key]
        current_state = current_states[state_key]
        previous_blocks = previous_state.get("blocks")
        current_blocks = current_state.get("blocks")
        if not isinstance(previous_blocks, list) or not isinstance(current_blocks, list):
            raise ContextualStateDiffError(
                "modified_state_blocks_unavailable",
                f"Modified state {state_key} must contain Structured Blocks in both Snapshots.",
            )

        previous_hash = previous_state.get("content_hash")
        current_hash = current_state.get("content_hash")
        if (
            not isinstance(previous_hash, str)
            or not isinstance(current_hash, str)
            or previous_hash != change.get("previous_content_hash")
            or current_hash != change.get("current_content_hash")
        ):
            raise ContextualStateDiffError(
                "modified_state_hash_mismatch",
                f"Modified state {state_key} does not match the 4A hash evidence.",
            )

        results.append(
            {
                "state_key": state_key,
                "scope_path": deepcopy(current_state.get("scope_path")),
                "state_path": deepcopy(current_state.get("state_path")),
                "previous_hash": previous_hash,
                "current_hash": current_hash,
                "contextual_diff": _build_state_block_diff(
                    previous_blocks, current_blocks
                ),
            }
        )
    return results
