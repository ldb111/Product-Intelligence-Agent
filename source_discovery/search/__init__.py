"""网页搜索供应商适配模块。"""

from .base import SearchProvider, SearchProviderError
from .tencent_wsa import TencentWsaSearchProvider

__all__ = ["SearchProvider", "SearchProviderError", "TencentWsaSearchProvider"]
