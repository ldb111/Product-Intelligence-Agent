"""Source Discovery 工具对外使用的最小结构化数据。"""

from __future__ import annotations

from typing import Literal, TypedDict


class SearchResult(TypedDict):
    """一条与具体搜索供应商无关的网页搜索结果。"""

    title: str
    url: str
    snippet: str


class SearchResponse(TypedDict):
    """一次网页搜索的查询词和结果集合。"""

    query: str
    results: list[SearchResult]


class ReadPageResult(TypedDict):
    """Source Discovery 从 Stage 1 网页采集结果中真正需要的字段。"""

    requested_url: str
    final_url: str
    title: str
    main_content: str
    headings: list[str]
    acquisition_method: Literal["static", "browser"]
    quality_status: Literal["PASS"]
