"""MCP v2 工具发现、Schema 和调用测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp import Client

from source_discovery.mcp_server import main, mcp


class _MockSearchProvider:
    def search(self, query: str, limit: int) -> dict:
        return {
            "query": query,
            "results": [
                {"title": "Result", "url": "https://example.com", "snippet": "Summary"}
            ][:limit],
        }


class McpServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_discovery_and_schemas(self) -> None:
        async with Client(mcp, raise_exceptions=True) as client:
            response = await client.list_tools()

        tools = {tool.name: tool for tool in response.tools}
        self.assertEqual(set(tools), {"web_search", "read_page"})
        self.assertEqual(set(tools["web_search"].input_schema["properties"]), {"query", "limit"})
        self.assertEqual(set(tools["read_page"].input_schema["properties"]), {"url"})
        self.assertEqual(
            set(tools["web_search"].output_schema["properties"]), {"query", "results"}
        )
        self.assertEqual(
            set(tools["read_page"].output_schema["properties"]),
            {
                "requested_url",
                "final_url",
                "title",
                "main_content",
                "headings",
                "acquisition_method",
                "quality_status",
            },
        )

    async def test_tool_descriptions_keep_implementation_details_out_of_model_context(
        self,
    ) -> None:
        """工具描述属于模型输入，只保留模型选择工具所需的业务语义。"""
        async with Client(mcp, raise_exceptions=True) as client:
            response = await client.list_tools()

        tools = {tool.name: tool for tool in response.tools}
        search_description = tools["web_search"].description or ""
        page_description = tools["read_page"].description or ""

        for implementation_detail in (
            "WSA_API_KEY",
            ".env",
            "Bearer",
            "腾讯云",
            "Tencent",
            "WSA",
            "Provider",
            "供应商",
        ):
            self.assertNotIn(implementation_detail.casefold(), search_description.casefold())

        for business_term in ("搜索", "网址", "摘要", "read_page"):
            self.assertIn(business_term.casefold(), search_description.casefold())

        for implementation_detail in (
            "Stage 1",
            "backend",
            "page_reader",
            "acquire_page_with_browser_fallback",
            "Playwright",
            "BeautifulSoup",
        ):
            self.assertNotIn(implementation_detail.casefold(), page_description.casefold())

        for business_term in ("读取", "URL", "主要文本", "质量状态"):
            self.assertIn(business_term.casefold(), page_description.casefold())

    async def test_web_search_tool_call_uses_provider_contract(self) -> None:
        with patch("source_discovery.mcp_server._search_provider", _MockSearchProvider()):
            async with Client(mcp, raise_exceptions=True) as client:
                result = await client.call_tool(
                    "web_search", {"query": "product update", "limit": 1}
                )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["query"], "product update")
        self.assertEqual(
            result.structured_content["results"][0]["url"], "https://example.com"
        )

    async def test_read_page_tool_call_returns_adapter_schema(self) -> None:
        page_result = {
            "requested_url": "https://example.com",
            "final_url": "https://www.example.com/",
            "title": "Example",
            "main_content": "Product information",
            "headings": ["Overview"],
            "acquisition_method": "static",
            "quality_status": "PASS",
        }
        with patch(
            "source_discovery.mcp_server.read_page_for_discovery",
            return_value=page_result,
        ):
            async with Client(mcp, raise_exceptions=True) as client:
                result = await client.call_tool(
                    "read_page", {"url": "https://example.com"}
                )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, page_result)

    @patch("source_discovery.mcp_server.mcp.run")
    @patch("source_discovery.mcp_server.load_dotenv")
    async def test_main_loads_dotenv_once_before_server_start(
        self,
        mock_load_dotenv,
        mock_run,
    ) -> None:
        main()

        mock_load_dotenv.assert_called_once_with()
        mock_run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
