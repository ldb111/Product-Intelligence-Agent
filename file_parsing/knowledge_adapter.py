"""把统一解析结果适配为冻结 Knowledge Layer 的现有输入，不改变入库规则。"""

from pathlib import Path

from knowledge.schemas import KnowledgeDocument

from .parser import file_sha256, parse_document
from .schemas import ParsedDocument, ParsingResult, PreparedDocument


def prepare_for_knowledge(result: ParsingResult, path: str | Path, *, document_id: str,
                          product_id: str | None, source_type: str = "product_document") -> list[KnowledgeDocument]:
    """将每个可追踪单元转换为一份现有 KnowledgeDocument。

    输入是解析结果、原文件和由可信调用方生成的业务 ID；每页、Sheet 或 Section
    独立保留 provenance，正文仍交给既有 Chunking。非 parsed 状态不会伪造入库材料。
    """
    if result.status != "parsed" or result.document is None:
        return []
    parsed: ParsedDocument = result.document
    source_path = Path(path).resolve()
    if (not source_path.is_file() or source_path.name != parsed.file_name
            or file_sha256(source_path) != parsed.source_metadata.get("file_sha256")):
        raise ValueError("解析结果与来源文件不一致。")
    source_reference = str(source_path)
    documents = []
    for index, unit in enumerate(parsed.units, 1):
        location = (f"Page {unit.page_number}" if unit.page_number else
                    f"Sheet {unit.sheet_name}" if unit.sheet_name else
                    " / ".join(unit.heading_path))
        title = parsed.title + (" · " + location if location else "")
        metadata = {"parsed_file_name": parsed.file_name, "parsed_file_type": parsed.file_type,
                    "parsed_unit_index": index, "heading_path": unit.heading_path,
                    "page_number": unit.page_number, "sheet_name": unit.sheet_name,
                    "cell_range": unit.cell_range, **unit.metadata}
        documents.append(KnowledgeDocument(
            knowledge_domain="own_product", document_id=f"{document_id}:unit:{index}",
            product_id=product_id, source_type=source_type, source_reference=source_reference,
            title=title[:300], content=unit.text, source_credibility="user_provided", metadata=metadata,
        ))
    return documents


def parse_and_prepare(path: str | Path, *, document_id: str, product_id: str | None,
                      source_type: str = "product_document", max_file_bytes: int | None = None,
                      max_text_chars: int | None = None) -> PreparedDocument:
    """供后续 UI/后端调用的一步入口，返回状态、展示元数据和正式入库输入。"""
    options = {}
    if max_file_bytes is not None:
        options["max_file_bytes"] = max_file_bytes
    if max_text_chars is not None:
        options["max_text_chars"] = max_text_chars
    result = parse_document(path, **options)
    document = result.document
    return PreparedDocument(
        status=result.status,
        document_metadata=document.source_metadata if document else {},
        normalized_content=document.normalized_text if document else "",
        ingestion_input=prepare_for_knowledge(result, path, document_id=document_id,
                                              product_id=product_id, source_type=source_type),
        warnings=result.warnings,
    )
