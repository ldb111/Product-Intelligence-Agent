"""本地文件解析入口：把用户文件转换为现有 Knowledge Layer 可接收的资料。"""

from .knowledge_adapter import parse_and_prepare, prepare_for_knowledge
from .parser import parse_document
from .schemas import ParsedDocument, ParsedUnit, ParsingResult, PreparedDocument

__all__ = [
    "ParsedDocument", "ParsedUnit", "ParsingResult", "PreparedDocument",
    "parse_document", "prepare_for_knowledge", "parse_and_prepare",
]
