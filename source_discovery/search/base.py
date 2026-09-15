"""定义 Source Discovery 使用的供应商无关搜索边界。"""

from __future__ import annotations

from typing import Protocol

from source_discovery.schemas import SearchResponse


class SearchProviderError(Exception):
    """表示搜索供应商调用中可预期、可稳定报告的失败。"""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class SearchProvider(Protocol):
    """不同网页搜索供应商共同遵守的最小接口。"""

    def search(self, query: str, limit: int) -> SearchResponse:
        """根据查询词返回不超过 limit 条的通用搜索结果。"""

