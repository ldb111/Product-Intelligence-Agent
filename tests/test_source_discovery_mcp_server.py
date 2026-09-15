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
