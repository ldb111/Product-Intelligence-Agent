"""通过 MCP Python SDK v2 暴露 Source Discovery 的两个只读工具。"""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from dotenv import load_dotenv

from source_discovery.schemas import ReadPageResult, SearchResponse
from source_discovery.search import SearchProvider, SearchProviderError, TencentWsaSearchProvider
from source_discovery.stage1_adapter import Stage1AdapterError, read_page_for_discovery


mcp = MCPServer("product-intelligence-source-discovery-tools")
_search_provider: SearchProvider = TencentWsaSearchProvider()


@mcp.tool()
def web_search(query: str, limit: int = 5) -> SearchResponse:
    """搜索公开网页并返回标题、网址和摘要。

    输入为搜索词和最大结果数；返回供应商无关的结构化结果。当前供应商需要配置
    ``WSA_API_KEY``，且最多返回 50 条；本工具只搜索，不读取结果页面正文。
    """
    try:
        return _search_provider.search(query, limit)
    except SearchProviderError as exc:
        raise ToolError(f"{exc.error_type}: {exc}") from exc


@mcp.tool()
def read_page(url: str) -> ReadPageResult:
    """读取一个公开网页并返回可信的主要文本与标题。

    输入为 HTTP/HTTPS URL；返回 Stage 1 采集方式、质量状态和精简页面内容。工具复用
    已冻结的 Stage 1，可能启动浏览器；不会保存 Snapshot，也不会执行历史变化比较。
    """
    try:
        return read_page_for_discovery(url)
    except Stage1AdapterError as exc:
        raise ToolError(f"{exc.error_type}: {exc}") from exc


def main() -> None:
    """加载本地环境变量并启动 Source Discovery MCP 服务。

    ``load_dotenv()`` 只放在统一启动入口执行一次，业务模块仍然只从操作系统环境读取
    配置。这样既避免多层重复加载，也不会让搜索 Provider 承担应用启动职责。
    """
    load_dotenv()
    mcp.run()


if __name__ == "__main__":
    # MCP v2 默认使用 stdio；保持 stdout 专用于协议消息，工具代码本身不打印内容。
    main()
