"""把已冻结的 Stage 1 网页采集能力适配为 Source Discovery 工具结果。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from backend.browser_reader import BrowserReadError
from backend.page_reader import PageReadError, acquire_page_with_browser_fallback
from source_discovery.schemas import PageLink, ReadPageResult


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


def _collect_links(blocks: list[dict[str, Any]]) -> list[PageLink]:
    """从 Stage 1 Structured Blocks 收集真实 href，不在这里猜测或改写 URL。

    输入：Stage 1 已经完成噪声删除和可见性处理的结构化内容块。
    处理：递归读取标题、段落、列表和卡片中已有的 ``links`` 字段；这里只保留锚文本
    与原始 href，绝对地址解析、协议过滤和去重由 Agent 的 Candidate Registry 统一负责。
    输出：按页面结构顺序排列的精简链接列表。
    """
    links: list[PageLink] = []

    def append_links(raw_links: Any) -> None:
        if not isinstance(raw_links, list):
            return
        for link in raw_links:
            if not isinstance(link, dict):
                continue
            href = link.get("href")
            if not isinstance(href, str):
                continue
            text = link.get("text")
            links.append(
                {
                    "anchor_text": text if isinstance(text, str) else "",
                    "href": href,
                    "page_region": "other",
                }
            )

    for block in blocks:
        append_links(block.get("links"))
        if block.get("type") == "group":
            for card in block.get("cards", []):
                if not isinstance(card, dict):
                    continue
                # card.links 已覆盖卡片内部链接；不再递归 card.blocks，避免重复收集。
                append_links(card.get("links"))
        elif block.get("type") == "list":
            for item in block.get("items", []):
                if not isinstance(item, dict):
                    continue
                append_links(item.get("links"))
                children = item.get("children")
                if isinstance(children, list):
                    links.extend(_collect_links(children))
    return links


def _link_page_region(anchor: Tag) -> str:
    """根据通用语义容器标记链接区域，不向 Agent 暴露 DOM 路径或 CSS。"""
    if anchor.find_parent("footer") is not None:
        return "footer"
    if anchor.find_parent("nav") is not None or anchor.find_parent(
        attrs={"role": "navigation"}
    ) is not None:
        return "nav"
    if anchor.find_parent("main") is not None:
        return "main"
    return "other"


def extract_source_discovery_links(html: bytes, base_url: str) -> list[PageLink]:
    """从同一次采集的真实 HTML/可见 DOM 提取 Source Discovery 原始链接。

    输入：Stage 1 最终选中的 HTML bytes 与真实 final_url。
    处理：读取所有 anchor，解析相对地址、删除 fragment、保留 query，并标记有限页面区域；
    这里只做协议和重复 href 清理，操作型/法律型噪声由 Candidate Admission 统一判断。
    输出：按 DOM 顺序排列、URL 已规范为 HTTP/HTTPS 绝对地址的 PageLink。
    """
    soup = BeautifulSoup(html, "html.parser")
    links: list[PageLink] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        absolute = urljoin(base_url, href.strip())
        parsed = urlsplit(absolute)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            continue
        normalized = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
        )
        key = urlunsplit(
            (
                parsed.scheme.casefold(),
                parsed.netloc.casefold(),
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
        if key in seen:
            continue
        seen.add(key)
        aria_label = anchor.get("aria-label")
        link_title = anchor.get("title")
        link: PageLink = {
            "anchor_text": anchor.get_text(" ", strip=True),
            "href": normalized,
            "page_region": _link_page_region(anchor),
        }
        if isinstance(aria_label, str) and aria_label.strip():
            link["aria_label"] = aria_label.strip()
        if isinstance(link_title, str) and link_title.strip():
            link["title"] = link_title.strip()
        if anchor.has_attr("download"):
            link["is_download"] = True
        links.append(link)
    return links


def resolve_collected_page_title(page_title: Any, html: bytes | None) -> str:
    """从同一次成功采集的结果中确定页面展示标题。

    输入：Stage 1 已提取的真实页面标题，以及可选的同次采集 HTML。
    处理：先采用 Stage 1 的页面标题；若为空，再严格按 HTML ``title``、``og:title``、
    ``twitter:title`` 顺序取值。这里只读取已有 DOM，不重新访问页面，也不让模型补写标题。
    输出：规范空白后的确定性标题；本层没有可用标题时返回空字符串，交给 Materialize
    继续使用 Candidate Registry 和 final_url 兜底。
    """

    def normalize(value: Any) -> str:
        return " ".join(value.split()) if isinstance(value, str) else ""

    resolved = normalize(page_title)
    if resolved or not isinstance(html, bytes):
        return resolved

    soup = BeautifulSoup(html, "html.parser")
    html_title = normalize(soup.title.get_text(" ", strip=True) if soup.title else None)
    if html_title:
        return html_title

    for attribute, marker in (("property", "og:title"), ("name", "twitter:title")):
        meta = soup.find(
            "meta",
            attrs={attribute: lambda value: isinstance(value, str) and value.casefold() == marker},
        )
        meta_title = normalize(meta.get("content") if isinstance(meta, Tag) else None)
        if meta_title:
            return meta_title
    return ""


def read_page_for_discovery(url: str) -> ReadPageResult:
    """复用 Stage 1 完整采集链路，并返回 Source Discovery 所需的精简结果。

    输入：需要读取的 HTTP/HTTPS URL。
    处理：调用静态优先、浏览器兜底的现有采集入口，检查 Quality Gate，再把 Stage 1
    内部对象映射为稳定的工具 Schema。本函数不调用 CLI，不保存 Snapshot，也不比较历史。
    输出：可信页面的 ReadPageResult；采集失败或质量不可信时抛出 Stage1AdapterError。
    """
    try:
        acquisition_result = acquire_page_with_browser_fallback(
            url,
            include_source_html=True,
        )
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
    source_html = acquisition_result.get("source_html")
    links = (
        extract_source_discovery_links(source_html, page_data["final_url"])
        if isinstance(source_html, bytes)
        else _collect_links(blocks)
    )
    return {
        "requested_url": page_data["requested_url"],
        "final_url": page_data["final_url"],
        "title": resolve_collected_page_title(page_data.get("title"), source_html),
        "main_content": page_data["content"],
        "headings": _collect_headings(blocks),
        "links": links,
        "acquisition_method": page_data["acquisition_method"],
        "quality_status": quality["status"],
    }
