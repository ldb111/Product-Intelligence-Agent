"""创建并持久化 Product Intelligence Agent 的 JSON 页面快照。

本模块负责 Task 3 和 Task 4：接收已经由 page_reader 完成读取和内容标准化的页面数据，
增加带时区的采集时间和 Content Hash（内容哈希），并保存为 UTF-8 JSON 文件。
它不会重新请求网页，也不负责上一份快照查询、哈希比较或变化检测。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


# 使用模块文件定位项目根目录，而不是依赖用户从哪个工作目录执行命令。这样正式快照
# 始终进入项目的 data/snapshots，不会意外保存到当前终端所在的其他目录。
DEFAULT_SNAPSHOT_DIRECTORY = Path(__file__).resolve().parent.parent / "data" / "snapshots"
PAGE_DATA_FIELDS = ("url", "status_code", "title", "content")

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


def create_snapshot(page_data: dict[str, Any]) -> dict[str, Any]:
    """为页面数据增加采集时间和内容哈希，组成完整 Snapshot（页面快照）。

    在业务链路中的职责：连接 Task 2 页面结果和 Task 3 文件保存，但不会修改传入的
    page_data，也不会重新请求 URL。

    输入：包含 url、status_code、title、content 的页面结果字典。
    处理：确认四个必需字段存在，生成带时区的 captured_at，并且只根据 content 计算
    SHA-256 content_hash。
    输出：包含原四个字段、captured_at 和 content_hash 的新快照字典。
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

    return {
        "url": page_data["url"],
        "status_code": page_data["status_code"],
        "title": page_data["title"],
        "content": page_data["content"],
        "captured_at": captured_at,
        "content_hash": content_hash,
    }


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
