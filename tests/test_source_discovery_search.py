"""Source Discovery 搜索供应商边界和腾讯 WSA 转换测试。"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import Mock, patch

import requests

from source_discovery.schemas import SearchResponse
from source_discovery.search.base import SearchProvider, SearchProviderError
from source_discovery.search.tencent_wsa import TencentWsaSearchProvider, WSA_ENDPOINT


class _MockSearchProvider:
    """用于证明调用方只依赖通用 SearchProvider 接口。"""

    def search(self, query: str, limit: int) -> SearchResponse:
        return {
            "query": query,
            "results": [
                {"title": "Result", "url": "https://example.com", "snippet": "Summary"}
            ][:limit],
        }


def _search_with_provider(provider: SearchProvider) -> SearchResponse:
    return provider.search("product update", 1)


class SearchProviderTests(unittest.TestCase):
    def test_mock_provider_follows_supplier_independent_contract(self) -> None:
        response = _search_with_provider(_MockSearchProvider())

        self.assertEqual(response["query"], "product update")
        self.assertEqual(response["results"][0]["url"], "https://example.com")

    @patch("source_discovery.search.tencent_wsa.requests.post")
    def test_tencent_wsa_converts_provider_response(self, mock_post: Mock) -> None:
        provider_result = {
            "title": "Product release",
            "url": "https://example.com/release",
            "passage": "A new product capability was released.",
            "site": "Example",
        }
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "Response": {"Pages": [json.dumps(provider_result, ensure_ascii=False)]}
        }

        response = TencentWsaSearchProvider(api_key="test-key").search("release", 5)

        self.assertEqual(
            response,
            {
                "query": "release",
                "results": [
                    {
                        "title": "Product release",
                        "url": "https://example.com/release",
                        "snippet": "A new product capability was released.",
                    }
                ],
            },
        )
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"], {"Query": "release", "Cnt": 10})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(mock_post.call_args.args[0], WSA_ENDPOINT)

    def test_missing_api_key_fails_without_network_request(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "source_discovery.search.tencent_wsa.requests.post"
        ) as mock_post:
            with self.assertRaisesRegex(SearchProviderError, "WSA_API_KEY") as context:
                TencentWsaSearchProvider().search("release", 5)

        self.assertEqual(context.exception.error_type, "missing_api_key")
        mock_post.assert_not_called()

    @patch("source_discovery.search.tencent_wsa.requests.post")
    def test_timeout_has_stable_error_type(self, mock_post: Mock) -> None:
        mock_post.side_effect = requests.Timeout("slow")

        with self.assertRaises(SearchProviderError) as context:
            TencentWsaSearchProvider(api_key="test-key", timeout=2).search("release", 5)

        self.assertEqual(context.exception.error_type, "timeout")

    @patch("source_discovery.search.tencent_wsa.requests.post")
    def test_provider_error_code_is_converted(self, mock_post: Mock) -> None:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "Response": {
                "Error": {
                    "Code": "UnauthorizedOperation",
                    "Message": "invalid credential",
                }
            }
        }

        with self.assertRaises(SearchProviderError) as context:
            TencentWsaSearchProvider(api_key="test-key").search("release", 5)

        self.assertEqual(context.exception.error_type, "authentication_failed")


if __name__ == "__main__":
    unittest.main()
