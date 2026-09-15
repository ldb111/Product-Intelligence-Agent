"""腾讯云 Web Search API（WSA）的供应商适配实现。"""

from __future__ import annotations

import json
import os
from typing import Any

import requests

from source_discovery.schemas import SearchResponse, SearchResult
from source_discovery.search.base import SearchProviderError


WSA_ENDPOINT = "https://api.wsa.cloud.tencent.com/SearchPro"
WSA_API_KEY_ENV_NAME = "WSA_API_KEY"
DEFAULT_SEARCH_TIMEOUT_SECONDS = 15.0
MAX_SEARCH_RESULTS = 50
WSA_RESULT_COUNT_OPTIONS = (10, 20, 30, 40, 50)

WSA_ERROR_TYPES = {
    "UnauthorizedOperation": "authentication_failed",
    "RequestLimitExceeded": "quota_exceeded",
    "ResourceUnavailable": "quota_unavailable",
    "ResourceNotFound": "service_not_enabled",
    "InvalidParameter": "invalid_request",
    "InternalError": "provider_error",
}


def _request_count_for_limit(limit: int) -> int:
    """把通用 limit 映射到 WSA 支持的 10/20/30/40/50 档位。"""
    return next(count for count in WSA_RESULT_COUNT_OPTIONS if count >= limit)


class TencentWsaSearchProvider:
    """通过腾讯云服务 API KEY 调用 WSA，并转换为通用搜索结果。

    构造时可以注入 API KEY 和超时，便于测试；正式运行默认从 ``WSA_API_KEY`` 环境
    变量读取密钥。腾讯云专有的请求、响应和错误码都停留在本类内部。
    """

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout

    def search(self, query: str, limit: int) -> SearchResponse:
        """搜索公开网页，并返回供应商无关的标题、URL 和摘要。

        输入：非空查询词，以及 1～50 的最大结果数。
        处理：读取 API KEY、调用 WSA、识别供应商错误并解析 Pages 中的 JSON 字符串。
        输出：SearchResponse；可预期失败统一抛出 SearchProviderError。
        """
        cleaned_query = query.strip() if isinstance(query, str) else ""
        if not cleaned_query:
            raise SearchProviderError("invalid_query", "Search query cannot be empty.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise SearchProviderError(
                "invalid_limit", f"Search limit must be between 1 and {MAX_SEARCH_RESULTS}."
            )
        if self._timeout <= 0:
            raise SearchProviderError(
                "invalid_timeout", "Search timeout must be greater than zero."
            )

        api_key = self._api_key or os.getenv(WSA_API_KEY_ENV_NAME)
        if not api_key:
            raise SearchProviderError(
                "missing_api_key",
                f"Environment variable {WSA_API_KEY_ENV_NAME} is not configured.",
            )

        try:
            response = requests.post(
                WSA_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={
                    "Query": cleaned_query,
                    "Cnt": _request_count_for_limit(limit),
                },
                timeout=self._timeout,
            )
        except requests.Timeout as exc:
            raise SearchProviderError(
                "timeout", f"Web search timed out after {self._timeout:g} seconds."
            ) from exc
        except requests.RequestException as exc:
            raise SearchProviderError(
                "request_failed", f"Web search request failed: {exc}"
            ) from exc

        if response.status_code in {401, 403}:
            raise SearchProviderError(
                "authentication_failed", "Web search authentication failed."
            )
        if response.status_code == 429:
            raise SearchProviderError(
                "quota_exceeded", "Web search request limit or quota was exceeded."
            )
        if not 200 <= response.status_code < 300:
            raise SearchProviderError(
                "provider_http_error",
                f"Web search returned unexpected HTTP status {response.status_code}.",
            )

        try:
            payload = response.json()
        except requests.JSONDecodeError as exc:
            raise SearchProviderError(
                "invalid_response", "Web search returned invalid JSON."
            ) from exc

        return self._convert_response(cleaned_query, limit, payload)

    @staticmethod
    def _convert_response(query: str, limit: int, payload: Any) -> SearchResponse:
        """把 WSA 的嵌套响应转换成稳定的 SearchResponse。"""
        if not isinstance(payload, dict) or not isinstance(payload.get("Response"), dict):
            raise SearchProviderError(
                "invalid_response", "Web search response does not contain Response."
            )

        provider_response = payload["Response"]
        provider_error = provider_response.get("Error")
        if isinstance(provider_error, dict):
            code = str(provider_error.get("Code", "provider_error"))
            message = str(provider_error.get("Message", "Web search provider error."))
            raise SearchProviderError(WSA_ERROR_TYPES.get(code, "provider_error"), message)

        pages = provider_response.get("Pages")
        if not isinstance(pages, list):
            raise SearchProviderError(
                "invalid_response", "Web search response does not contain a Pages list."
            )

        results: list[SearchResult] = []
        for page in pages[:limit]:
            try:
                item = json.loads(page) if isinstance(page, str) else page
            except json.JSONDecodeError as exc:
                raise SearchProviderError(
                    "invalid_response", "Web search contains an invalid page result."
                ) from exc
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                raise SearchProviderError(
                    "invalid_response", "Web search page result has no valid URL."
                )
            results.append(
                {
                    "title": item.get("title") if isinstance(item.get("title"), str) else "",
                    "url": item["url"],
                    "snippet": item.get("passage") if isinstance(item.get("passage"), str) else "",
                }
            )

        return {"query": query, "results": results}
