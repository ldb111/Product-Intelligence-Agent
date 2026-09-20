"""从 Stage 1 Structured Content 形成有界的 Path B statement 候选。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from backend.structured_content import serialize_blocks
from event_core.inputs import build_explicit_statement_input

from .store import identity


INITIAL_BACKFILL_DAYS = 90
INITIAL_BACKFILL_MAX_EVENTS = 20
STATEMENT_EXTRACTION_MAX_PER_SOURCE = 60

_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(20\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])(?!\d)"),
    re.compile(r"(?<!\d)(20\d{2})年(0?[1-9]|1[0-2])月(0?[1-9]|[12]\d|3[01])日"),
)
_ENGLISH_DATE = re.compile(
    r"(?i)\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"([0-3]?\d)(?:st|nd|rd|th)?,?\s+(20\d{2})\b"
)
_ENGLISH_DATE_REVERSED = re.compile(
    r"(?i)\b([0-3]?\d)(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December),?\s+(20\d{2})\b"
)
_MONTHS = {
    name.lower(): index for index, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July", "August",
         "September", "October", "November", "December"), start=1
    )
}


def _date_from_text(text: str) -> date | None:
    """只接受正文中带年、月、日的完整日期，不用观察时间补造声明日期。"""
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return date(*(int(value) for value in match.groups()))
            except ValueError:
                continue
    match = _ENGLISH_DATE.search(text)
    if match:
        month, day, year = match.groups()
        try:
            return date(int(year), _MONTHS[month.lower()], int(day))
        except ValueError:
            return None
    match = _ENGLISH_DATE_REVERSED.search(text)
    if match:
        day, month, year = match.groups()
        try:
            return date(int(year), _MONTHS[month.lower()], int(day))
        except ValueError:
            return None
    return None


def _block_units(blocks: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    """把结构块转成有限 statement 单元，并保留最近标题作为日期/语义上下文。

    输入是 Snapshot 原样保存的 blocks。段落、列表项、表格行和 Card 分别形成局部单元；
    Heading 只作为相邻上下文，不单独当成变化声明。输出的 excerpt/context 都必须逐字
    存在于 Snapshot.content，之后仍由 Event Core 判断它是不是 Explicit Change Statement。
    """
    units: list[tuple[str, str | None]] = []
    heading: str | None = None
    for block in blocks:
        block_type = block.get("type")
        if block_type == "heading":
            heading = block.get("text") or None
        elif block_type == "paragraph" and block.get("text"):
            units.append((block["text"], heading))
        elif block_type == "list":
            stack = list(block.get("items", []))
            while stack:
                item = stack.pop(0)
                if item.get("text"):
                    units.append((item["text"], heading))
                for child in item.get("children", []):
                    stack.extend(child.get("items", []))
        elif block_type == "table":
            headers = " | ".join(block.get("headers", [])) or heading
            for row in block.get("rows", []):
                excerpt = " | ".join(row)
                if excerpt:
                    units.append((excerpt, headers))
        elif block_type == "group":
            for card in block.get("cards", []):
                excerpt = serialize_blocks(card.get("blocks", []))
                context = card.get("title") or heading
                if excerpt:
                    units.append((excerpt, context))
        if len(units) >= STATEMENT_EXTRACTION_MAX_PER_SOURCE:
            break
    return units[:STATEMENT_EXTRACTION_MAX_PER_SOURCE]


def extract_statement_candidates(history: dict, source: dict, *, competitor: str) -> list[dict]:
    """从一次成功扫描生成可恢复的 Path B 输入，不进行 Event 语义判断。

    只有正式来源记录为 official，且最终落地域名仍在确认域名中时才生成候选。首次扫描
    返回当前页面能识别出的全部有界局部结构单元，供上层记录 Seen Baseline，并统一执行
    90 天和全竞品 20 条限制。后续是否属于真正新增由 SQLite observation state 判断，不把
    “未 processing”或临时 Snapshot 差集当作新增。这里不依赖动作关键词，Current Fact 与
    Event 的最终区别仍由现有 Event Core Path B 判断。
    """
    from urllib.parse import urlsplit

    current = history["current_snapshot"]
    final_host = urlsplit(current["final_url"]).hostname
    official_hosts = source.get("official_domains", [urlsplit(source["url"]).hostname])
    if source.get("credibility") != "official" or final_host not in official_hosts:
        return []

    def records(snapshot: dict) -> list[dict]:
        result = []
        content = snapshot["content"]
        for excerpt, context in _block_units(snapshot.get("blocks", [])):
            if excerpt not in content:
                continue
            if context and context not in content:
                context = None
            statement_date = _date_from_text("\n".join(part for part in (context, excerpt) if part))
            statement_id = identity([
                snapshot["final_url"], " ".join(excerpt.split()), " ".join((context or "").split()),
            ])
            value = build_explicit_statement_input(
                snapshot, excerpt, competitor=competitor, source_type=source["source_type"],
                source_credibility="official", relevant_source_context=context,
            )
            result.append({
                "statement_id": statement_id,
                "source_url": snapshot["final_url"],
                "statement_date": statement_date.isoformat() if statement_date else None,
                "input": value.model_dump(mode="json"),
            })
        return result

    current_records = records(current)
    for item in current_records:
        item.update(
            initial_backfill=bool(history["is_first_scan"]),
            snapshot_id=history["snapshot_id"],
        )
    return current_records


def select_initial_backfill(candidates: list[dict], reference_at: str, limit: int) -> list[dict]:
    """先按可靠声明日期筛选最近 90 天，再按时间降序取剩余配额。"""
    reference_date = datetime.fromisoformat(reference_at).date()
    cutoff = reference_date - timedelta(days=INITIAL_BACKFILL_DAYS)
    eligible = [
        item for item in candidates
        if item.get("statement_date") and cutoff <= date.fromisoformat(item["statement_date"]) <= reference_date
    ]
    eligible.sort(key=lambda item: (item["statement_date"], item["statement_id"]), reverse=True)
    return eligible[:max(0, limit)]
