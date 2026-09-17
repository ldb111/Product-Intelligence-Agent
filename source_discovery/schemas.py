"""Source Discovery 工具对外使用的最小结构化数据。"""

from __future__ import annotations

from enum import Enum
from typing import Literal, NotRequired, TypedDict

from pydantic import BaseModel, Field, model_validator


class SearchResult(TypedDict):
    """一条与具体搜索供应商无关的网页搜索结果。"""

    title: str
    url: str
    snippet: str


class SearchResponse(TypedDict):
    """一次网页搜索的查询词和结果集合。"""

    query: str
    results: list[SearchResult]


class PageLink(TypedDict):
    """网页结构中真实存在、可供 Agent 继续发现候选来源的链接。"""

    anchor_text: str
    href: str
    aria_label: NotRequired[str]
    title: NotRequired[str]
    page_region: NotRequired[Literal["nav", "main", "footer", "other"]]
    is_download: NotRequired[bool]


class ReadPageResult(TypedDict):
    """Source Discovery 从 Stage 1 网页采集结果中真正需要的字段。"""

    requested_url: str
    final_url: str
    title: str
    main_content: str
    headings: list[str]
    links: list[PageLink]
    acquisition_method: Literal["static", "browser"]
    quality_status: Literal["PASS"]


class SourceType(str, Enum):
    """建议监控来源的业务类型。"""

    PRODUCT_PAGE = "product_page"
    PRICING = "pricing"
    CHANGELOG = "changelog"
    RELEASE_NOTES = "release_notes"
    ANNOUNCEMENT = "announcement"
    DOCS = "docs"
    BLOG = "blog"
    MARKETPLACE = "marketplace"
    MODEL_CATALOG = "model_catalog"
    STATUS_PAGE = "status_page"
    OTHER = "other"


class MonitoringSource(BaseModel):
    """一个经过真实页面读取确认、建议长期监控的官方来源。"""

    url: str = Field(min_length=1, description="真实读取并确认后的页面网址")
    title: str = Field(min_length=1, description="真实页面标题")
    source_type: SourceType = Field(description="该页面对应的监控来源类型")
    monitoring_reason: str = Field(
        min_length=1,
        description="该页面值得长期监控以及可能发现何种产品变化的简短理由",
    )


class SourceDiscoveryResult(BaseModel):
    """单个竞品一次来源发现任务的最终结构化结果。"""

    competitor_name: str = Field(min_length=1, description="本次研究的竞品名称")
    official_domains: list[str] = Field(description="已由真实证据确认的官方域名")
    sources: list[MonitoringSource] = Field(description="建议长期监控的来源集合")
    discovery_complete: bool = Field(description="主要监控来源覆盖是否已经足够")
    remaining_gaps: list[str] = Field(description="仍未确认的具体重要来源或信息范围")

    @model_validator(mode="after")
    def validate_completion_and_gaps(self) -> "SourceDiscoveryResult":
        """保证完成状态与剩余缺口表达一致，避免模型输出自相矛盾。"""
        if self.discovery_complete and self.remaining_gaps:
            raise ValueError("remaining_gaps must be empty when discovery_complete is true")
        if not self.discovery_complete and not self.remaining_gaps:
            raise ValueError(
                "remaining_gaps must contain a concrete gap when discovery_complete is false"
            )
        return self
