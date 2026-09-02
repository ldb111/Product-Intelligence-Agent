"""根据 Change Detection 结果生成标准化页面正文的行级文本差异。

本模块只负责 Task 7：消费 Task 6 已经给出的 changed 结论，并在确实发生变化时比较
前后快照中已经保存的 content。它不会请求网页、查询历史、计算 Hash、重新判断是否
变化，也不会解释文本变化是否具有产品或竞争意义。
"""

from __future__ import annotations

import difflib
from typing import Any


class ContentDiffError(Exception):
    """表示生成 Diff 所需的 Change Detection 输入不完整或不合法。

    在业务链路中的职责：当 changed=true 但缺少前后快照或 content 时，阻止系统生成
    不完整、容易误导的差异结果，并让命令行返回结构稳定的错误 JSON。

    输入：稳定的错误类型标识和面向用户的错误说明。
    处理：保留异常消息，并额外记录错误类型。
    输出：由 build_diff_result 抛出，在命令行入口统一处理。
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def _get_content(snapshot: Any, snapshot_name: str) -> str:
    """读取生成 Diff 必需的 content，缺失或类型错误时给出稳定错误。"""
    if not isinstance(snapshot, dict):
        raise ContentDiffError(
            "invalid_diff_input", f"{snapshot_name} must be a snapshot object."
        )

    if "content" not in snapshot:
        raise ContentDiffError(
            "invalid_diff_input",
            f"{snapshot_name} is missing required field: content.",
        )

    content = snapshot["content"]
    if not isinstance(content, str):
        raise ContentDiffError(
            "invalid_diff_input", f"content in {snapshot_name} must be a string."
        )

    return content


def build_content_diff(previous_content: str, current_content: str) -> str:
    """生成上一版与当前版标准化正文的 Unified Diff（统一差异文本）。

    在业务链路中的职责：只回答“具体哪些文本行被删除或新增”，把 Task 2 已标准化的
    两份 content 转换为后续 AI Event Detection 可读取的基础差异证据。

    输入：上一份快照和当前快照中保存的 content 字符串。
    处理：按行拆分两个字符串，使用标准库 difflib.unified_diff 生成包含上下文的行级差异。
    输出：统一差异格式字符串；``-`` 开头表示旧版本删除行，``+`` 开头表示当前新增行。

    本函数不会修改输入。Python 字符串本身不可变，splitlines 会创建新的行列表，
    difflib 只读取这些列表并生成新的差异文本。
    """
    previous_lines = previous_content.splitlines()
    current_lines = current_content.splitlines()

    diff_lines = difflib.unified_diff(
        previous_lines,
        current_lines,
        fromfile="previous_snapshot",
        tofile="current_snapshot",
        lineterm="",
    )
    return "\n".join(diff_lines)


def build_diff_result(change_result: dict[str, Any]) -> dict[str, Any]:
    """根据 Task 6 结论决定是否生成 Diff，并保留原有链路数据。

    在业务链路中的职责：处理 First Scan、无变化、发生变化三个分支。只有 Task 6 已经
    明确给出 changed=true 时，才读取前后 content 并调用 build_content_diff。

    输入：包含 is_first_scan、changed、current_snapshot、previous_snapshot 的 Task 6 结果。
    处理：首次采集或无变化直接设置 diff=None；有变化时生成行级 Unified Diff。
    输出：保留 Task 6 所有字段并新增 diff 的字典；None 序列化为 JSON 后对应 null。

    业务规则：本函数信任 Task 6 的 changed 结论，不通过 content 或 Hash 再判断一次。
    这样可以保持职责边界，避免两个模块产生互相矛盾的变化结论。
    """
    if not isinstance(change_result, dict):
        raise ContentDiffError(
            "invalid_diff_input", "Change Detection result must be an object."
        )

    try:
        is_first_scan = change_result["is_first_scan"]
        changed = change_result["changed"]
    except KeyError as exc:
        raise ContentDiffError(
            "invalid_diff_input",
            f"Change Detection result is missing required field: {exc.args[0]}.",
        ) from exc

    if not isinstance(is_first_scan, bool):
        raise ContentDiffError(
            "invalid_diff_input", "is_first_scan must be a boolean."
        )

    result = dict(change_result)

    if is_first_scan:
        if changed is not None:
            raise ContentDiffError(
                "invalid_diff_input", "changed must be null for a first scan."
            )
        # 首次采集没有 Previous Snapshot，无法形成前后两份文本，因此不调用 Diff 算法。
        result["diff"] = None
        return result

    if not isinstance(changed, bool):
        raise ContentDiffError(
            "invalid_diff_input", "changed must be a boolean for a non-first scan."
        )

    if not changed:
        # Task 6 已经通过 Hash 确认正文相同，再运行 Diff 既没有信息增量，也会混淆职责。
        result["diff"] = None
        return result

    try:
        previous_snapshot = change_result["previous_snapshot"]
        current_snapshot = change_result["current_snapshot"]
    except KeyError as exc:
        raise ContentDiffError(
            "invalid_diff_input",
            f"Change Detection result is missing required field: {exc.args[0]}.",
        ) from exc

    previous_content = _get_content(previous_snapshot, "previous_snapshot")
    current_content = _get_content(current_snapshot, "current_snapshot")
    result["diff"] = build_content_diff(previous_content, current_content)
    return result
