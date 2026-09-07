"""创建并持久化 Product Intelligence Agent 的 JSON 页面快照。

本模块负责创建并保存包含 Structured Blocks、抽取版本、采集时间和 Content Hash 的
页面快照，以及从同一 URL 的历史记录中找到当前快照之前时间最近的一份快照。它不会
重新请求网页，也不比较内容哈希或判断页面是否变化。
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any


# 使用模块文件定位项目根目录，而不是依赖用户从哪个工作目录执行命令。这样正式快照
# 始终进入项目的 data/snapshots，不会意外保存到当前终端所在的其他目录。
DEFAULT_SNAPSHOT_DIRECTORY = Path(__file__).resolve().parent.parent / "data" / "snapshots"
PAGE_DATA_FIELDS = (
    "url",
    "requested_url",
    "final_url",
    "status_code",
    "title",
    "content",
    "blocks",
    "acquisition_method",
)
# 抽取算法升级可能在网页未变化时改变 content 格式。把稳定版本写进每份新快照，能够
# 让 Change Detection 区分“网页变化”和“采集技术升级”，避免制造假 Diff。
EXTRACTION_VERSION = "structured_blocks_v8_computed_strikethrough"
# Interactive State 的结构和页面抽取版本是两条独立演进轴。只有真正附带 Traversal
# Result 的新快照才写入该版本；旧快照或普通页面采集不会被伪装成“完整但零状态”。
INTERACTIVE_STATE_SCHEMA_VERSION = "interactive_states_v1"
TRAVERSAL_AUDIT_FIELDS = (
    "status",
    "groups_discovered",
    "groups_completed",
    "non_default_states_captured",
    "page_restored",
    "bounds",
    "truncations",
    "skipped",
    "errors",
)

# Windows 文件名不能包含这些特殊字符和 ASCII 控制字符。URL 中常见的冒号、斜杠、
# 问号正好属于该范围，所以生成文件名时必须替换。
WINDOWS_INVALID_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class SnapshotError(Exception):
    """表示可预期、可以转换成明确错误 JSON 的快照处理失败。

    在业务链路中的职责：把缺少页面字段、JSON 转换失败或文件写入失败统一为稳定的
    错误类型，避免命令行直接暴露零散的 KeyError、TypeError 或 OSError。

    输入：稳定的错误类型标识和面向用户的错误说明。
    处理：保留异常消息，并额外记录错误类型。
    输出：由调用方抛出，最终在命令行入口转换为错误 JSON。
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def compute_content_hash(content: str) -> str:
    """只根据标准化后的页面正文计算稳定的 SHA-256 内容哈希。

    在业务链路中的职责：把较长的 content 转换为固定长度摘要，供后续任务快速判断
    两份 Snapshot（页面快照）的标准化正文是否完全一致。本函数只计算摘要，不比较
    两个摘要，也不会根据结果决定是否保存快照。

    输入：Task 2 已经完成 Content Normalization（内容标准化）的 content 字符串。
    处理：先用 UTF-8 把字符串编码为 bytes（字节），再用 Python 标准库 hashlib 的
    SHA-256 算法计算摘要，最后转换为 64 位小写十六进制字符串。
    输出：只由 content 决定的 SHA-256 十六进制 content_hash。

    为什么只计算 content：url、status_code、title 和 captured_at 描述的是来源、响应或
    采集上下文，它们变化时不一定代表正文变化。尤其 captured_at 每次采集都会不同，
    如果参与计算，同一页面连续抓取也会产生不同哈希，失去快速比较正文的意义。
    """
    content_bytes = content.encode("utf-8")
    return hashlib.sha256(content_bytes).hexdigest()


def _build_interactive_state_snapshot_data(
    traversal_result: dict[str, Any],
) -> dict[str, Any]:
    """把 Traversal Result 转成 Snapshot 中稳定、可审计的状态数据。

    输入：正式 Traversal Orchestrator 返回的结果。
    处理：验证状态列表和最关键的完整性字段；保留状态原始字典，并复制有限的遍历审计
    信息。运行时 DOM 定位不由本函数补造，也不会参与页面 content_hash。
    输出：可合并进 Snapshot 的 schema version、interactive_states 和 traversal 元数据。
    """
    if not isinstance(traversal_result, dict):
        raise SnapshotError(
            "invalid_traversal_result", "Traversal result must be an object."
        )
    interactive_states = traversal_result.get("interactive_states")
    traversal_status = traversal_result.get("status")
    page_restored = traversal_result.get("page_restored")
    if not isinstance(interactive_states, list):
        raise SnapshotError(
            "invalid_traversal_result",
            "Traversal result must contain an interactive_states list.",
        )
    if traversal_status not in {"complete", "partial", "aborted"}:
        raise SnapshotError(
            "invalid_traversal_result",
            "Traversal status must be complete, partial, or aborted.",
        )
    if not isinstance(page_restored, bool):
        raise SnapshotError(
            "invalid_traversal_result",
            "Traversal result must contain a boolean page_restored field.",
        )
    if not all(isinstance(state, dict) for state in interactive_states):
        raise SnapshotError(
            "invalid_traversal_result",
            "Every interactive state must be an object.",
        )

    traversal_audit = {
        field: deepcopy(traversal_result[field])
        for field in TRAVERSAL_AUDIT_FIELDS
        if field in traversal_result
    }
    return {
        "interactive_state_schema_version": INTERACTIVE_STATE_SCHEMA_VERSION,
        "interactive_states": deepcopy(interactive_states),
        "interactive_state_traversal": traversal_audit,
    }


def create_snapshot(
    page_data: dict[str, Any],
    traversal_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """为页面数据增加采集时间和内容哈希，组成完整 Snapshot（页面快照）。

    在业务链路中的职责：连接 Task 2 页面结果和 Task 3 文件保存，但不会修改传入的
    page_data，也不会重新请求 URL。

    输入：包含 URL、状态码、标题、content、blocks 和 acquisition_method 的页面结果；
    可选传入正式 Traversal Result。
    处理：确认必需字段存在，原样保留 blocks，生成带时区的 captured_at，只根据
    content 计算 SHA-256 content_hash，并写入当前 extraction_version。
    输出：包含页面数据、captured_at、content_hash 和 extraction_version 的新快照字典；
    提供 Traversal 时额外保存独立 schema version、interactive_states 和完整性审计信息。
    """
    missing_fields = [field for field in PAGE_DATA_FIELDS if field not in page_data]
    if missing_fields:
        raise SnapshotError(
            "invalid_snapshot_data",
            f"Page data is missing required fields: {', '.join(missing_fields)}.",
        )

    # astimezone() 把当前时间变成带 UTC 偏移量的本地时间；isoformat() 生成类似
    # 2026-09-02T14:35:20.123456+08:00 的明确时间，避免日后无法判断快照所属时区。
    captured_at = datetime.now().astimezone().isoformat(timespec="microseconds")
    content_hash = compute_content_hash(page_data["content"])

    snapshot = {
        "url": page_data["url"],
        "requested_url": page_data["requested_url"],
        "final_url": page_data["final_url"],
        "status_code": page_data["status_code"],
        "title": page_data["title"],
        "content": page_data["content"],
        # blocks 保持 list/dict 结构直接进入 JSON，不转换成字符串，也不参与 content_hash。
        "blocks": page_data["blocks"],
        # acquisition_method 只用于说明可信正文来自 requests 还是 Chromium；它和 URL、
        # captured_at 一样不参与 content_hash，因此采集方式切换不会自行制造内容变化。
        "acquisition_method": page_data["acquisition_method"],
        "captured_at": captured_at,
        "content_hash": content_hash,
        "extraction_version": EXTRACTION_VERSION,
    }
    if traversal_result is not None:
        snapshot.update(_build_interactive_state_snapshot_data(traversal_result))
    return snapshot


def build_snapshot_filename(snapshot: dict[str, Any]) -> str:
    """根据 URL 和 captured_at 生成 Windows 文件系统安全的 JSON 文件名。

    在业务链路中的职责：让人能够从文件名大致识别来源和采集时间，同时避免 URL 中的
    ``:``, ``/``, ``?`` 等字符导致 Windows 无法创建文件。

    输入：已经包含 url 和 captured_at 的快照字典。
    处理：替换 Windows 非法字符和空白、合并重复下划线，并限制 URL 部分长度。
    输出：以 ``.json`` 结尾的安全文件名。最终防覆盖由 save_snapshot 负责。
    """
    try:
        url_part = str(snapshot["url"])
        time_part = str(snapshot["captured_at"])
    except KeyError as exc:
        raise SnapshotError(
            "invalid_snapshot_data",
            f"Snapshot is missing required field: {exc.args[0]}.",
        ) from exc

    safe_url_part = WINDOWS_INVALID_FILENAME_CHARACTERS.sub("_", url_part)
    safe_url_part = re.sub(r"\s+", "_", safe_url_part)
    safe_url_part = re.sub(r"_+", "_", safe_url_part).strip(" ._")
    # 限制 URL 部分长度，避免很长的查询参数让整个 Windows 路径超过常见长度限制。
    safe_url_part = safe_url_part[:120].rstrip(" .") or "page"

    safe_time_part = WINDOWS_INVALID_FILENAME_CHARACTERS.sub("-", time_part)
    safe_time_part = re.sub(r"\s+", "_", safe_time_part).strip(" ._") or "unknown-time"

    # 固定 snapshot_ 前缀还可避免 URL 恰好生成 CON、NUL 等 Windows 保留设备名。
    return f"snapshot_{safe_url_part}_{safe_time_part}.json"


def save_snapshot(
    snapshot: dict[str, Any], directory: str | Path | None = None
) -> Path:
    """将快照以 UTF-8 JSON 保存，并保证任何已有文件都不会被覆盖。

    在业务链路中的职责：完成 Snapshot 的持久化，让后续任务能够读取历史页面数据。

    输入：完整快照字典；可选保存目录，省略时使用项目的 data/snapshots。
    处理：创建目录、生成安全文件名、转换为中文可读 JSON，并使用排他创建模式写入。
    如果同名文件已存在，会依次尝试 ``_1``、``_2`` 等后缀，而不是覆盖旧文件。
    输出：本次实际创建的 Snapshot 文件 Path（路径）对象。
    """
    target_directory = Path(directory) if directory is not None else DEFAULT_SNAPSHOT_DIRECTORY

    try:
        # parents=True 会同时创建缺少的父目录；exist_ok=True 允许目录已经存在。
        target_directory.mkdir(parents=True, exist_ok=True)
        # ensure_ascii=False 防止中文被写成 \uXXXX；先完成序列化再创建文件，可避免
        # 数据无法转换为 JSON 时留下一个不完整的空文件。
        json_text = json.dumps(snapshot, ensure_ascii=False, indent=2)
    except (OSError, TypeError, ValueError) as exc:
        raise SnapshotError(
            "snapshot_save_failed", f"Failed to prepare snapshot file: {exc}"
        ) from exc

    filename = build_snapshot_filename(snapshot)
    filename_path = Path(filename)
    suffix_number = 0

    while True:
        if suffix_number == 0:
            candidate_path = target_directory / filename
        else:
            candidate_path = target_directory / (
                f"{filename_path.stem}_{suffix_number}{filename_path.suffix}"
            )

        try:
            # 模式 x 表示“仅新建”：如果文件已存在就抛出 FileExistsError。这个判断由
            # 操作系统原子完成，即使短时间或并发保存，也不会先检查后写入而意外覆盖。
            with candidate_path.open("x", encoding="utf-8", newline="\n") as snapshot_file:
                snapshot_file.write(f"{json_text}\n")
        except FileExistsError:
            suffix_number += 1
            continue
        except OSError as exc:
            raise SnapshotError(
                "snapshot_save_failed", f"Failed to save snapshot file: {exc}"
            ) from exc

        return candidate_path


def _parse_captured_at(value: Any, source: str) -> datetime:
    """把 Snapshot 中的 ISO 8601 字符串解析为可比较的带时区日期时间。

    在业务链路中的职责：确保当前快照和历史快照使用同一种可靠的时间比较方式，避免
    直接比较字符串或文件名而得到错误顺序。

    输入：captured_at 字段值，以及用于错误提示的快照来源说明。
    处理：使用 datetime.fromisoformat 解析，并确认结果包含 UTC 时区偏移量。
    输出：可按真实时间先后比较的 datetime；格式无效或不带时区时抛出 SnapshotError。
    """
    if not isinstance(value, str):
        raise SnapshotError(
            "invalid_snapshot_data", f"captured_at in {source} must be a string."
        )

    try:
        captured_at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SnapshotError(
            "invalid_snapshot_data",
            f"captured_at in {source} is not valid ISO 8601: {value}.",
        ) from exc

    # 不带时区的 datetime 无法明确代表全球时间线上的一个时刻。如果允许它参与比较，
    # 不同机器或时区可能得到不同结果，因此把它视为无效快照数据。
    if captured_at.utcoffset() is None:
        raise SnapshotError(
            "invalid_snapshot_data", f"captured_at in {source} must include a timezone."
        )

    return captured_at


def find_previous_snapshot(
    current_snapshot: dict[str, Any], directory: str | Path | None = None
) -> dict[str, Any] | None:
    """查找同一 URL 在当前快照之前时间最近的一份历史 Snapshot。

    在业务链路中的职责：只回答“当前快照的上一份历史快照是哪一份”，为下一阶段的
    Change Detection（变化检测）准备前后两个版本；本函数不比较 content_hash。

    输入：包含 url 和 captured_at 的当前快照；可选快照目录，默认 data/snapshots。
    处理：读取目录内 JSON，只保留 URL 完全相同且 captured_at 严格早于当前时间的记录，
    然后选择 captured_at 最大、也就是时间距离当前快照最近的一份。
    输出：找到时返回完整的上一份快照字典；目录不存在、为空或没有候选记录时返回 None。

    重要业务规则：时间依据只来自 JSON 内部 captured_at，不使用文件名；严格使用小于
    而不是小于等于，确保当前快照自己和相同采集时间的记录不会被错误选中。旧快照没有
    blocks 或 extraction_version 仍然是合法历史记录，本函数不会要求或补写这些字段。
    """
    try:
        current_url = current_snapshot["url"]
        current_captured_at_value = current_snapshot["captured_at"]
    except KeyError as exc:
        raise SnapshotError(
            "invalid_snapshot_data",
            f"Current snapshot is missing required field: {exc.args[0]}.",
        ) from exc

    current_captured_at = _parse_captured_at(
        current_captured_at_value, "current snapshot"
    )
    snapshot_directory = (
        Path(directory) if directory is not None else DEFAULT_SNAPSHOT_DIRECTORY
    )

    # 目录不存在或尚无快照表示该 URL 没有可用历史，这属于正常的 First Scan（首次采集），
    # 不应该被当成文件系统错误。
    if not snapshot_directory.exists():
        return None

    previous_snapshot: dict[str, Any] | None = None
    previous_captured_at: datetime | None = None

    # glob 返回顺序可能受文件系统影响，所以不能依赖遍历顺序。循环中始终保留当前已知
    # 时间最大的候选记录，最终结果只由 JSON 内容决定。
    for snapshot_path in snapshot_directory.glob("*.json"):
        try:
            with snapshot_path.open("r", encoding="utf-8") as snapshot_file:
                historical_snapshot = json.load(snapshot_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotError(
                "snapshot_read_failed",
                f"Failed to read snapshot file {snapshot_path}: {exc}",
            ) from exc

        if not isinstance(historical_snapshot, dict):
            raise SnapshotError(
                "invalid_snapshot_data",
                f"Snapshot file {snapshot_path} must contain a JSON object.",
            )

        # URL 使用完全相等判断，不做大小写转换、去除参数或其他“近似匹配”，防止把不同
        # 页面混进同一条历史链路。
        if historical_snapshot.get("url") != current_url:
            continue

        if "captured_at" not in historical_snapshot:
            raise SnapshotError(
                "invalid_snapshot_data",
                f"Snapshot file {snapshot_path} is missing captured_at.",
            )

        historical_captured_at = _parse_captured_at(
            historical_snapshot["captured_at"], str(snapshot_path)
        )

        # 严格排除当前快照自己、相同时间记录和未来记录。这里不读取或比较 content_hash。
        if historical_captured_at >= current_captured_at:
            continue

        if (
            previous_captured_at is None
            or historical_captured_at > previous_captured_at
        ):
            previous_snapshot = historical_snapshot
            previous_captured_at = historical_captured_at

    return previous_snapshot


def build_snapshot_history(
    current_snapshot: dict[str, Any], directory: str | Path | None = None
) -> dict[str, Any]:
    """组成包含 First Scan 状态、当前快照和上一份快照的 Task 5 输出。

    输入：当前 Snapshot，以及可选的历史快照目录。
    处理：调用 find_previous_snapshot 查找上一份记录，并根据是否找到设置 is_first_scan。
    输出：包含 is_first_scan、current_snapshot、previous_snapshot 的字典；首次采集时
    previous_snapshot 为 None，序列化成 JSON 后对应 null。
    """
    previous_snapshot = find_previous_snapshot(current_snapshot, directory)

    return {
        "is_first_scan": previous_snapshot is None,
        "current_snapshot": current_snapshot,
        "previous_snapshot": previous_snapshot,
    }
