"""根据前后 Structured Blocks 生成带结构上下文的差异。

本模块只消费 Snapshot 中已经保存的 blocks，不请求网页、不重新判断 changed，也不解释
变化是否具有竞争意义。它利用 heading level、Block 顺序、表格行列和列表父子关系，为
Stage 2 保留“变化发生在哪里”的确定性上下文。
"""

from __future__ import annotations

import difflib
import json
from typing import Any


SUPPORTED_CONTEXTUAL_BLOCK_TYPES = {"paragraph", "table", "list"}
MAX_PRECEDING_CONTEXT_CHARS = 160
# 只保留变化 Block 前最近的少量结构，既能覆盖“日期 paragraph → 主题 heading → list”
# 这类组合，又不会把整个页面正文复制进每条 contextual_diff。
MAX_CONTEXT_BLOCKS = 3


def _short_text(value: Any) -> str:
    """把可选文本压缩为适合 preceding_context 的短单行内容。"""
    text = " ".join(str(value or "").split())
    if len(text) <= MAX_PRECEDING_CONTEXT_CHARS:
        return text
    return f"{text[:MAX_PRECEDING_CONTEXT_CHARS].rstrip()}…"


def _summarize_block(block: dict[str, Any]) -> str:
    """提取一个 Block 的简短文字，供下一个 Block 作为前置上下文。"""
    block_type = block.get("type")
    if block_type in {"heading", "paragraph", "code"}:
        return _short_text(block.get("text"))
    if block_type == "list":
        items = block.get("items", [])
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return _short_text(items[0].get("text"))
    if block_type == "table":
        headers = block.get("headers", [])
        if isinstance(headers, list):
            return _short_text(" | ".join(str(cell) for cell in headers))
    return ""


def _build_context_block(block: dict[str, Any]) -> dict[str, Any] | None:
    """把一个相邻 Block 转换成体积有限、仍保留类型信息的上下文摘要。

    输入：当前按 DOM 顺序遇到的 Structured Block。
    处理：heading 保留 level，其他 Block 只保留最长 160 字符的简短摘要。
    输出：可放入 context_blocks 的小字典；没有可用文字时返回 None。
    """
    text = _summarize_block(block)
    if not text:
        return None
    context_block = {"type": block.get("type"), "text": text}
    if block.get("type") == "heading" and isinstance(block.get("level"), int):
        context_block["level"] = block["level"]
    return context_block


def _iter_compatible_blocks(blocks: list[Any]):
    """把 group/card 透明展开为原有基础 Blocks，保持既有 Contextual Diff 行为。

    group 是结构保留层，card.blocks 仍按原 DOM 顺序保存 heading、paragraph、list、table、
    code。比较前只做只读展开，旧快照的扁平 blocks 与新结构可以走同一套上下文算法。
    """
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "group":
            yield block
            continue
        cards = block.get("cards", [])
        if not isinstance(cards, list):
            continue
        for card in cards:
            if not isinstance(card, dict):
                continue
            card_blocks = card.get("blocks", [])
            if isinstance(card_blocks, list):
                yield from _iter_compatible_blocks(card_blocks)


def _annotate_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    """为可比较 Block 补充当时有效的标题路径和最近前置文本。

    heading_path 根据 h1～h6 的 level 维护：遇到同级或更高层标题时，旧的下级路径会
    退出。这样算法只依赖页面结构，不需要知道“Kimi Work”或“Release Notes”的语义。
    """
    heading_stack: list[dict[str, Any]] = []
    preceding_context: str | None = None
    context_history: list[dict[str, Any]] = []
    annotated: list[dict[str, Any]] = []

    for block in _iter_compatible_blocks(blocks):
        block_type = block.get("type")
        if block_type == "heading":
            level = block.get("level")
            text = _short_text(block.get("text"))
            if isinstance(level, int) and 1 <= level <= 6 and text:
                heading_stack = [
                    heading for heading in heading_stack if heading["level"] < level
                ]
                heading_stack.append({"level": level, "text": text})
                preceding_context = text
                context_history.append(
                    {"type": "heading", "level": level, "text": text}
                )
                context_history = context_history[-MAX_CONTEXT_BLOCKS:]
            continue

        if block_type in SUPPORTED_CONTEXTUAL_BLOCK_TYPES:
            annotated.append(
                {
                    "block": block,
                    "block_type": block_type,
                    "heading_path": [dict(heading) for heading in heading_stack],
                    "preceding_context": preceding_context,
                    "context_blocks": [
                        dict(context_block) for context_block in context_history
                    ],
                }
            )

        summary = _summarize_block(block)
        if summary:
            preceding_context = summary
        context_block = _build_context_block(block)
        if context_block is not None:
            context_history.append(context_block)
            context_history = context_history[-MAX_CONTEXT_BLOCKS:]

    return annotated


def _record_identity(record: dict[str, Any]) -> tuple[Any, ...]:
    """生成 Block 对齐使用的稳定标识，标题路径变化也会被视为位置变化。"""
    heading_identity = tuple(
        (heading["level"], heading["text"])
        for heading in record["heading_path"]
    )
    block_json = json.dumps(
        record["block"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return record["block_type"], heading_identity, block_json


def _context_fields(
    previous_record: dict[str, Any] | None,
    current_record: dict[str, Any] | None,
) -> dict[str, Any]:
    """选择当前版本优先的标题路径和前置上下文。"""
    source = current_record or previous_record
    assert source is not None
    fields = {
        "heading_path": source["heading_path"],
        "preceding_context": source["preceding_context"],
        "context_blocks": source["context_blocks"],
    }

    if (
        previous_record is not None
        and current_record is not None
        and previous_record["heading_path"] != current_record["heading_path"]
    ):
        fields["previous_heading_path"] = previous_record["heading_path"]
        fields["current_heading_path"] = current_record["heading_path"]
    if (
        previous_record is not None
        and current_record is not None
        and previous_record["context_blocks"] != current_record["context_blocks"]
    ):
        fields["previous_context_blocks"] = previous_record["context_blocks"]
        fields["current_context_blocks"] = current_record["context_blocks"]
    return fields


def _paragraph_change(
    previous_record: dict[str, Any] | None,
    current_record: dict[str, Any] | None,
) -> dict[str, Any]:
    """生成 paragraph 的 previous/current 文本变化。"""
    previous_block = previous_record["block"] if previous_record else None
    current_block = current_record["block"] if current_record else None
    change_type = (
        "added"
        if previous_block is None
        else "deleted"
        if current_block is None
        else "modified"
    )
    result = {
        "block_type": "paragraph",
        "change_type": change_type,
        **_context_fields(previous_record, current_record),
        "previous": previous_block.get("text") if previous_block else None,
        "current": current_block.get("text") if current_block else None,
    }

    previous_links = previous_block.get("links", []) if previous_block else []
    current_links = current_block.get("links", []) if current_block else []
    if previous_links != current_links:
        result["previous_links"] = previous_links
        result["current_links"] = current_links
    return result


def _safe_rows(block: dict[str, Any] | None) -> list[list[str]]:
    """读取格式正确的二维 rows；异常或缺失值按空表处理。"""
    if block is None or not isinstance(block.get("rows"), list):
        return []
    return [
        [str(cell) for cell in row]
        for row in block["rows"]
        if isinstance(row, list)
    ]


def _safe_headers(block: dict[str, Any] | None) -> list[str]:
    """读取表头并统一为字符串列表。"""
    if block is None or not isinstance(block.get("headers"), list):
        return []
    return [str(cell) for cell in block["headers"]]


def _changed_cells(
    previous_row: list[str],
    current_row: list[str],
    previous_headers: list[str],
    current_headers: list[str],
) -> list[dict[str, Any]]:
    """按列位置说明单元格变化，并尽量给出对应表头名称。"""
    changes: list[dict[str, Any]] = []
    column_count = max(len(previous_row), len(current_row))
    for column_index in range(column_count):
        previous_value = (
            previous_row[column_index] if column_index < len(previous_row) else None
        )
        current_value = (
            current_row[column_index] if column_index < len(current_row) else None
        )
        if previous_value == current_value:
            continue
        if column_index < len(current_headers):
            column_name = current_headers[column_index]
        elif column_index < len(previous_headers):
            column_name = previous_headers[column_index]
        else:
            column_name = f"column_{column_index + 1}"
        changes.append(
            {
                "column": column_name,
                "column_index": column_index,
                "previous": previous_value,
                "current": current_value,
            }
        )
    return changes


def _rows_by_unique_key(rows: list[list[str]]) -> dict[str, list[str]] | None:
    """首列非空且唯一时，将其作为通用 row key；否则返回 None 使用位置对齐。"""
    keys = [row[0] for row in rows if row]
    if len(keys) != len(rows) or any(not key for key in keys) or len(set(keys)) != len(keys):
        return None
    return {row[0]: row for row in rows}


def _table_row_changes(
    previous_rows: list[list[str]],
    current_rows: list[list[str]],
    previous_headers: list[str],
    current_headers: list[str],
) -> list[dict[str, Any]]:
    """优先用唯一首列定位业务行，无法确认时退回保守的位置对齐。"""
    changes: list[dict[str, Any]] = []
    previous_by_key = _rows_by_unique_key(previous_rows)
    current_by_key = _rows_by_unique_key(current_rows)

    if previous_by_key is not None and current_by_key is not None:
        ordered_keys = list(previous_by_key)
        ordered_keys.extend(key for key in current_by_key if key not in previous_by_key)
        for row_key in ordered_keys:
            previous_row = previous_by_key.get(row_key)
            current_row = current_by_key.get(row_key)
            if previous_row == current_row:
                continue
            change_type = (
                "added"
                if previous_row is None
                else "deleted"
                if current_row is None
                else "modified"
            )
            changes.append(
                {
                    "change_type": change_type,
                    "row_key": row_key,
                    "previous": previous_row,
                    "current": current_row,
                    "changed_cells": _changed_cells(
                        previous_row or [],
                        current_row or [],
                        previous_headers,
                        current_headers,
                    ),
                }
            )
        return changes

    matcher = difflib.SequenceMatcher(
        None,
        [tuple(row) for row in previous_rows],
        [tuple(row) for row in current_rows],
        autojunk=False,
    )
    for operation, previous_start, previous_end, current_start, current_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        previous_slice = previous_rows[previous_start:previous_end]
        current_slice = current_rows[current_start:current_end]
        paired_count = min(len(previous_slice), len(current_slice))
        for index in range(paired_count):
            previous_row = previous_slice[index]
            current_row = current_slice[index]
            changes.append(
                {
                    "change_type": "modified",
                    "row_key": current_row[0] if current_row else None,
                    "previous": previous_row,
                    "current": current_row,
                    "changed_cells": _changed_cells(
                        previous_row,
                        current_row,
                        previous_headers,
                        current_headers,
                    ),
                }
            )
        for previous_row in previous_slice[paired_count:]:
            changes.append(
                {
                    "change_type": "deleted",
                    "row_key": previous_row[0] if previous_row else None,
                    "previous": previous_row,
                    "current": None,
                    "changed_cells": [],
                }
            )
        for current_row in current_slice[paired_count:]:
            changes.append(
                {
                    "change_type": "added",
                    "row_key": current_row[0] if current_row else None,
                    "previous": None,
                    "current": current_row,
                    "changed_cells": [],
                }
            )
    return changes


def _table_change(
    previous_record: dict[str, Any] | None,
    current_record: dict[str, Any] | None,
) -> dict[str, Any]:
    """生成带表头、变化行和变化列的 table 差异。"""
    previous_block = previous_record["block"] if previous_record else None
    current_block = current_record["block"] if current_record else None
    previous_headers = _safe_headers(previous_block)
    current_headers = _safe_headers(current_block)
    previous_rows = _safe_rows(previous_block)
    current_rows = _safe_rows(current_block)
    change_type = (
        "added"
        if previous_block is None
        else "deleted"
        if current_block is None
        else "modified"
    )
    return {
        "block_type": "table",
        "change_type": change_type,
        **_context_fields(previous_record, current_record),
        "headers": current_headers or previous_headers,
        "previous_headers": previous_headers,
        "current_headers": current_headers,
        "changed_rows": _table_row_changes(
            previous_rows,
            current_rows,
            previous_headers,
            current_headers,
        ),
    }


def _flatten_list_items(
    block: dict[str, Any], parent_path: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    """按展示顺序展开列表，并为嵌套子项保留直接父项和完整父路径。"""
    flattened: list[dict[str, Any]] = []
    items = block.get("items", [])
    if not isinstance(items, list):
        return flattened

    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", ""))
        item_record = {
            "text": text,
            "links": item.get("links", []),
            "parent_item": parent_path[-1] if parent_path else None,
            "parent_path": list(parent_path),
        }
        flattened.append(item_record)
        children = item.get("children", [])
        if isinstance(children, list):
            for child_list in children:
                if isinstance(child_list, dict) and child_list.get("type") == "list":
                    flattened.extend(
                        _flatten_list_items(child_list, (*parent_path, text))
                    )
    return flattened


def _list_item_identity(item: dict[str, Any]) -> tuple[Any, ...]:
    """列表项对齐同时考虑文字、链接和父路径。"""
    links_json = json.dumps(
        item.get("links", []), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return tuple(item["parent_path"]), item["text"], links_json


def _list_item_changes(
    previous_items: list[dict[str, Any]], current_items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """用序列对齐生成 item 级新增、删除和修改，并保留 parent_item。"""
    changes: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(
        None,
        [_list_item_identity(item) for item in previous_items],
        [_list_item_identity(item) for item in current_items],
        autojunk=False,
    )

    for operation, previous_start, previous_end, current_start, current_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        previous_slice = previous_items[previous_start:previous_end]
        current_slice = current_items[current_start:current_end]
        paired_count = min(len(previous_slice), len(current_slice))

        for index in range(paired_count):
            previous_item = previous_slice[index]
            current_item = current_slice[index]
            # 只有父路径一致时才称为修改；移动到另一个父项下应明确表示删除和新增。
            if previous_item["parent_path"] == current_item["parent_path"]:
                changes.append(
                    {
                        "change_type": "modified",
                        "previous": previous_item["text"],
                        "current": current_item["text"],
                        "parent_item": current_item["parent_item"],
                        "parent_path": current_item["parent_path"],
                    }
                )
            else:
                changes.extend(
                    [
                        {
                            "change_type": "deleted",
                            "previous": previous_item["text"],
                            "current": None,
                            "parent_item": previous_item["parent_item"],
                            "parent_path": previous_item["parent_path"],
                        },
                        {
                            "change_type": "added",
                            "previous": None,
                            "current": current_item["text"],
                            "parent_item": current_item["parent_item"],
                            "parent_path": current_item["parent_path"],
                        },
                    ]
                )

        for previous_item in previous_slice[paired_count:]:
            changes.append(
                {
                    "change_type": "deleted",
                    "previous": previous_item["text"],
                    "current": None,
                    "parent_item": previous_item["parent_item"],
                    "parent_path": previous_item["parent_path"],
                }
            )
        for current_item in current_slice[paired_count:]:
            changes.append(
                {
                    "change_type": "added",
                    "previous": None,
                    "current": current_item["text"],
                    "parent_item": current_item["parent_item"],
                    "parent_path": current_item["parent_path"],
                }
            )
    return changes


def _list_change(
    previous_record: dict[str, Any] | None,
    current_record: dict[str, Any] | None,
) -> dict[str, Any]:
    """生成 list Block 的 item 级变化，并保留标题与前置上下文。"""
    previous_block = previous_record["block"] if previous_record else None
    current_block = current_record["block"] if current_record else None
    previous_items = _flatten_list_items(previous_block) if previous_block else []
    current_items = _flatten_list_items(current_block) if current_block else []
    change_type = (
        "added"
        if previous_block is None
        else "deleted"
        if current_block is None
        else "modified"
    )
    return {
        "block_type": "list",
        "change_type": change_type,
        **_context_fields(previous_record, current_record),
        "previous_ordered": previous_block.get("ordered") if previous_block else None,
        "current_ordered": current_block.get("ordered") if current_block else None,
        "item_changes": _list_item_changes(previous_items, current_items),
    }


def _build_block_change(
    previous_record: dict[str, Any] | None,
    current_record: dict[str, Any] | None,
) -> dict[str, Any]:
    """按 Block 类型调用对应的结构差异生成器。"""
    block_type = (
        current_record["block_type"] if current_record else previous_record["block_type"]
    )
    if block_type == "paragraph":
        return _paragraph_change(previous_record, current_record)
    if block_type == "table":
        return _table_change(previous_record, current_record)
    return _list_change(previous_record, current_record)


def build_contextual_diff(
    previous_snapshot: Any, current_snapshot: Any
) -> list[dict[str, Any]] | None:
    """从两个 Snapshot 的 Structured Blocks 生成 contextual_diff。

    输入：上一份和当前 Snapshot；两者需要包含 list 类型的 blocks。
    处理：根据 heading level 标注标题路径，并为每个 Block 保留最多三个相邻上下文摘要；
    使用 SequenceMatcher 对齐 Block，同类型替换继续下钻到段落、表格或列表项。
    输出：结构化变化列表。任一快照缺少 blocks 时返回 None，兼容 legacy Snapshot。

    业务边界：这里只基于相邻 Block 和确定性结构做尽力定位。它不是语义 Diff，不会把
    距离很远但含义相似的段落、表格行或列表项强行配对。
    """
    if not isinstance(previous_snapshot, dict) or not isinstance(current_snapshot, dict):
        return None
    previous_blocks = previous_snapshot.get("blocks")
    current_blocks = current_snapshot.get("blocks")
    if not isinstance(previous_blocks, list) or not isinstance(current_blocks, list):
        return None

    previous_records = _annotate_blocks(previous_blocks)
    current_records = _annotate_blocks(current_blocks)
    matcher = difflib.SequenceMatcher(
        None,
        [_record_identity(record) for record in previous_records],
        [_record_identity(record) for record in current_records],
        autojunk=False,
    )
    changes: list[dict[str, Any]] = []

    for operation, previous_start, previous_end, current_start, current_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        previous_slice = previous_records[previous_start:previous_end]
        current_slice = current_records[current_start:current_end]
        paired_count = min(len(previous_slice), len(current_slice))

        for index in range(paired_count):
            previous_record = previous_slice[index]
            current_record = current_slice[index]
            if previous_record["block_type"] == current_record["block_type"]:
                changes.append(_build_block_change(previous_record, current_record))
            else:
                changes.append(_build_block_change(previous_record, None))
                changes.append(_build_block_change(None, current_record))
        for previous_record in previous_slice[paired_count:]:
            changes.append(_build_block_change(previous_record, None))
        for current_record in current_slice[paired_count:]:
            changes.append(_build_block_change(None, current_record))

    return changes
