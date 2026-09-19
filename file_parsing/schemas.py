"""文件解析层的数据契约；解析状态与可入库材料保持明确分离。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from knowledge.schemas import KnowledgeDocument


ParsingStatus = Literal["pending", "parsing", "parsed", "failed", "unsupported", "scanned_or_no_text"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ParsedUnit(Contract):
    """一段可独立追踪的正文；格式相关位置只在适用时填写。"""

    text: str = Field(min_length=1)
    heading_path: list[str] = Field(default_factory=list)
    page_number: int | None = Field(default=None, ge=1)
    sheet_name: str | None = None
    cell_range: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ParsedDocument(Contract):
    """不同文件格式的统一结果，正文使用结构化 Markdown 风格文本。"""

    file_name: str
    file_type: Literal["txt", "md", "docx", "pdf", "xlsx"]
    title: str
    normalized_text: str
    units: list[ParsedUnit] = Field(default_factory=list)
    source_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ParsingResult(Contract):
    """UI 可直接展示的解析状态；用户文件问题不会泄露本机异常正文。"""

    status: ParsingStatus
    document: ParsedDocument | None = None
    warnings: list[str] = Field(default_factory=list)


class PreparedDocument(Contract):
    """稳定后端入口的返回值：解析结果和现有 KnowledgeDocument 输入。"""

    status: ParsingStatus
    document_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    normalized_content: str = ""
    ingestion_input: list[KnowledgeDocument] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
