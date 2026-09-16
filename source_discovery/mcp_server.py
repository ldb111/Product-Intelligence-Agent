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
    """搜索公开网页，发现与查询词相关的候选页面。

    输入为搜索词和最大结果数；返回每个候选页面的标题、网址和摘要。本工具只负责
    搜索和发现页面，不读取页面正文；需要查看某个结果的完整内容时，应使用 read_page。
    """
    try:
        return _search_provider.search(query, limit)
    except SearchProviderError as exc:
        raise ToolError(f"{exc.error_type}: {exc}") from exc


@mcp.tool()
def read_page(url: str) -> ReadPageResult:
    """读取一个公开网页并返回可信的主要文本与标题。

    输入为一个明确的 HTTP/HTTPS URL；返回请求网址、最终网址、标题、主要文本、页面
    标题列表、采集方式和质量状态。本工具只读取指定页面，不负责搜索其他候选页面。
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
