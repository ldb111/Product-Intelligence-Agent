"""把已冻结的 Stage 1 网页采集能力适配为 Source Discovery 工具结果。"""

from __future__ import annotations

from typing import Any

from backend.browser_reader import BrowserReadError
from backend.page_reader import PageReadError, acquire_page_with_browser_fallback
from source_discovery.schemas import ReadPageResult


class Stage1AdapterError(Exception):
    """表示 Stage 1 采集被适配层转换后的稳定工具错误。"""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def _collect_headings(blocks: list[dict[str, Any]]) -> list[str]:
    """按原有 Block 顺序收集页面和 Card 内的标题文字。"""
    headings: list[str] = []
    for block in blocks:
        if block.get("type") == "heading" and isinstance(block.get("text"), str):
            headings.append(block["text"])
        if block.get("type") == "group":
            for card in block.get("cards", []):
                if isinstance(card, dict) and isinstance(card.get("blocks"), list):
                    headings.extend(_collect_headings(card["blocks"]))
    return headings


def read_page_for_discovery(url: str) -> ReadPageResult:
    """复用 Stage 1 完整采集链路，并返回 Source Discovery 所需的精简结果。

    输入：需要读取的 HTTP/HTTPS URL。
    处理：调用静态优先、浏览器兜底的现有采集入口，检查 Quality Gate，再把 Stage 1
    内部对象映射为稳定的工具 Schema。本函数不调用 CLI，不保存 Snapshot，也不比较历史。
    输出：可信页面的 ReadPageResult；采集失败或质量不可信时抛出 Stage1AdapterError。
    """
    try:
        acquisition_result = acquire_page_with_browser_fallback(url)
    except (PageReadError, BrowserReadError) as exc:
        raise Stage1AdapterError(exc.error_type, str(exc)) from exc

    quality = acquisition_result["quality_gate"]
    if not quality["downstream_allowed"]:
        reason_codes = [
            reason.get("code", "unknown")
            for reason in quality.get("reasons", [])
            if isinstance(reason, dict)
        ]
        reason_text = ", ".join(reason_codes) or "unknown"
        raise Stage1AdapterError(
            "quality_rejected",
            f"Stage 1 rejected the page with quality status {quality['status']}: {reason_text}.",
        )

    page_data = acquisition_result["page_data"]
    blocks = page_data["blocks"]
    return {
        "requested_url": page_data["requested_url"],
        "final_url": page_data["final_url"],
        "title": page_data["title"],
        "main_content": page_data["content"],
        "headings": _collect_headings(blocks),
        "acquisition_method": page_data["acquisition_method"],
        "quality_status": quality["status"],
    }
