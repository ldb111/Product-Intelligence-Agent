"""根据前后页面快照已有的 content_hash 判断标准化正文是否变化。

本模块只负责 Task 6：消费 Task 5 已经找到的 current_snapshot 和
previous_snapshot，不重新读取历史文件，也不重新计算 Hash。它只回答正文是否变化，
不生成 Diff（差异）、竞争事件或任何变化原因解释。
"""

from __future__ import annotations

from typing import Any


EXTRACTION_VERSION_CHANGED_REASON = "extraction_version_changed"


class ChangeDetectionError(Exception):
    """表示 Change Detection 输入结构不完整或不合法。

    在业务链路中的职责：当非首次采集缺少当前或上一份 content_hash 时，阻止系统
    静默生成一个不可信的 changed 结果，并让命令行能够输出稳定、明确的错误 JSON。

    输入：稳定的错误类型标识和面向用户的错误说明。
    处理：保留异常消息，并额外记录错误类型。
    输出：由 detect_change 抛出，在命令行入口统一转换为错误 JSON。
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def _get_content_hash(snapshot: Any, snapshot_name: str) -> str:
    """从指定快照读取字符串类型的 content_hash，缺失时给出稳定错误。"""
    if not isinstance(snapshot, dict):
        raise ChangeDetectionError(
            "invalid_change_detection_input",
            f"{snapshot_name} must be a snapshot object.",
        )

    if "content_hash" not in snapshot:
        raise ChangeDetectionError(
            "invalid_change_detection_input",
            f"{snapshot_name} is missing required field: content_hash.",
        )

    content_hash = snapshot["content_hash"]
    if not isinstance(content_hash, str):
        raise ChangeDetectionError(
            "invalid_change_detection_input",
            f"content_hash in {snapshot_name} must be a string.",
        )

    return content_hash


def _get_extraction_version(snapshot: Any, snapshot_name: str) -> str | None:
    """读取可选 extraction_version；旧快照缺少该字段时返回 None。

    None 在这里代表 legacy（旧版未记录抽取版本），不是读取错误。若字段明确存在但不是
    字符串，则说明快照结构损坏，不能把它静默当作某个正常版本参与比较。
    """
    if not isinstance(snapshot, dict):
        raise ChangeDetectionError(
            "invalid_change_detection_input",
            f"{snapshot_name} must be a snapshot object.",
        )

    extraction_version = snapshot.get("extraction_version")
    if extraction_version is not None and not isinstance(extraction_version, str):
        raise ChangeDetectionError(
            "invalid_change_detection_input",
            f"extraction_version in {snapshot_name} must be a string.",
        )
    return extraction_version


def detect_change(snapshot_history: dict[str, Any]) -> dict[str, Any]:
    """根据 Task 5 历史结果中的 content_hash 生成 Change Detection 结果。

    在业务链路中的职责：位于 Previous Snapshot（上一份快照）查询之后，只判断经过
    Content Normalization（内容标准化）的页面正文有没有变化，为后续 Diff 准备结论。

    输入：包含 is_first_scan、current_snapshot、previous_snapshot 的 Task 5 结果。
    处理：首次采集不进行比较；非首次采集先核对 extraction_version，相同才比较 Hash。
    输出：is_first_scan、changed、跳过原因、前后 Hash，并保留两份快照供后续使用。

    业务规则：First Scan 只有一个观察点，没有前后两个版本，因此 changed 必须是 None
    （JSON 中为 null），表示“未知、不可判断”。false 则表示确实比较过两个版本并确认
    内容相同，两者不能混为一谈。抽取版本不同也返回 changed=None，但 is_first_scan 仍为
    False，并通过 comparison_skipped_reason 明确这是采集技术基线切换。captured_at、
    title、status_code 等字段不参与判断。
    """
    if not isinstance(snapshot_history, dict):
        raise ChangeDetectionError(
            "invalid_change_detection_input",
            "Snapshot history must be an object.",
        )

    try:
        is_first_scan = snapshot_history["is_first_scan"]
        current_snapshot = snapshot_history["current_snapshot"]
        previous_snapshot = snapshot_history["previous_snapshot"]
    except KeyError as exc:
        raise ChangeDetectionError(
            "invalid_change_detection_input",
            f"Snapshot history is missing required field: {exc.args[0]}.",
        ) from exc

    if not isinstance(is_first_scan, bool):
        raise ChangeDetectionError(
            "invalid_change_detection_input", "is_first_scan must be a boolean."
        )

    current_content_hash = _get_content_hash(
        current_snapshot, "current_snapshot"
    )

    if is_first_scan:
        # 首次采集不能读取或比较上一份 Hash，因为 Previous Snapshot 本来就不存在。
        # changed=None 保留了“尚无比较依据”的真实业务语义，不能用 false 代替。
        return {
            "is_first_scan": True,
            "changed": None,
            "comparison_skipped_reason": None,
            "previous_content_hash": None,
            "current_content_hash": current_content_hash,
            "current_snapshot": current_snapshot,
            "previous_snapshot": previous_snapshot,
        }

    previous_content_hash = _get_content_hash(
        previous_snapshot, "previous_snapshot"
    )
    current_extraction_version = _get_extraction_version(
        current_snapshot, "current_snapshot"
    )
    previous_extraction_version = _get_extraction_version(
        previous_snapshot, "previous_snapshot"
    )

    if previous_extraction_version != current_extraction_version:
        # 版本变化时，Hash 差异可能只来自序列化算法，而不是网页内容。这里保留“并非
        # First Scan”的事实，但把本次新版本快照作为技术基线，不生成变化结论或 Diff。
        return {
            "is_first_scan": False,
            "changed": None,
            "comparison_skipped_reason": EXTRACTION_VERSION_CHANGED_REASON,
            "previous_content_hash": previous_content_hash,
            "current_content_hash": current_content_hash,
            "current_snapshot": current_snapshot,
            "previous_snapshot": previous_snapshot,
        }

    # 这里只比较 Task 4 已经计算并保存在快照中的 Hash，不读取 content 重新计算。
    # 相同表示标准化正文完全一致；不同只表示正文发生变化，不解释具体变化或竞争意义。
    changed = previous_content_hash != current_content_hash

    return {
        "is_first_scan": False,
        "changed": changed,
        "comparison_skipped_reason": None,
        "previous_content_hash": previous_content_hash,
        "current_content_hash": current_content_hash,
        "current_snapshot": current_snapshot,
        "previous_snapshot": previous_snapshot,
    }
