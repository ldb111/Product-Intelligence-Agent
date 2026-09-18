"""确定性 Markdown/纯文本切片：结构边界优先，长度上限兜底。"""

from __future__ import annotations

import re

from .schemas import EmptyChunkError, KnowledgeChunk, KnowledgeDocument, KnowledgeError, content_hash

CHUNKING_VERSION = "structure-v1"


def normalize(content: str) -> str:
    """统一换行和首尾空白，保留段落、列表缩进与表格；返回稳定的入库原文。"""
    return content.replace("\r\n", "\n").replace("\r", "\n").strip()


def _bounded_spans(text: str, start: int, end: int, limit: int):
    """把单个超长结构块分成原文区间，优先行/句子边界，最后才按字符截取。"""
    while end - start > limit:
        window = text[start:start + limit]
        boundaries = [m.end() for m in re.finditer(r"\n|[。！？.!?]\s*|\s+", window)]
        split = max((i for i in boundaries if i >= limit // 2), default=limit)
        yield start, start + split
        start += split
    if start < end:
        yield start, end


def chunk_document(document: KnowledgeDocument, max_chars: int = 1200) -> list[KnowledgeChunk]:
    """将规范化文档按章节和空行拆成块，再打包成有长度上限的切片。

    输入是显式知识文档；输出携带完整来源、章节、正文区间及稳定 ID。列表/表格在
    上限内保持完整，超长表格的表头独立重复为上下文。支持 ATX 标题和围栏代码；
    不是完整 Markdown 解析器，不支持 PDF/DOCX/HTML 的隐式解析。
    """
    if type(max_chars) is not int or not 100 <= max_chars <= 2000:
        raise KnowledgeError("切片长度须在 100 到 2000 字符之间。")
    text = normalize(document.content)
    if not text:
        raise EmptyChunkError("规范化后没有可切片正文。")
    lines = text.splitlines(keepends=True)
    sections, headings, block_start = [], [], None
    offset, fence = 0, None
    block_headings = []
    for line in lines:
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line.rstrip("\n")) if fence is None else None
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if heading or (not line.strip() and fence is None):
            if block_start is not None:
                sections.append((block_start, offset, block_headings))
                block_start = None
            if heading:
                level = len(heading[1])
                headings = [(n, h) for n, h in headings if n < level] + [(level, heading[2])]
        else:
            if block_start is None:
                block_start, block_headings = offset, [h for _, h in headings]
            if marker:
                if fence is None:
                    fence = marker[1]
                elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                    fence = None
        offset += len(line)
    if block_start is not None:
        sections.append((block_start, len(text), block_headings))
    spans = []
    for start, end, path in sections:
        while end > start and text[end - 1].isspace():
            end -= 1
        if not text[start:end].strip():
            continue
        block_lines = text[start:end].splitlines()
        table = None
        if len(block_lines) >= 2 and "|" in block_lines[0] and re.fullmatch(r"[\s|:\-]+", block_lines[1]):
            table = "\n".join(block_lines[:2])
        if len("\n".join([document.title, *path, table or ""])) > 800:
            raise KnowledgeError("标题、章节或表头过长，请先缩小文档结构范围。")
        for lo, hi in _bounded_spans(text, start, end, max_chars):
            # 只合并同一章节且无表格上下文的相邻小段；不把不同章节事实混到一起。
            if (spans and not table and spans[-1][3] is None and spans[-1][2] == path
                    and hi - spans[-1][0] <= max_chars):
                spans[-1] = (spans[-1][0], hi, path, None)
            else:
                spans.append((lo, hi, path, table))
    source = document.model_dump(exclude={"content"})
    doc_hash = content_hash(text)
    chunks = []
    for index, (start, end, path, table) in enumerate(spans):
        body = text[start:end]
        if not body.strip():
            continue
        # 域参与 ID，防止两个域中相同 document_id/content 发生碰撞。
        identity = f"{document.knowledge_domain}\0{document.document_id}\0{doc_hash}\0{max_chars}\0{CHUNKING_VERSION}\0{index}"
        chunks.append(KnowledgeChunk(
            **source, chunk_id=content_hash(identity), content=body, content_hash=content_hash(body),
            document_content_hash=doc_hash, chunk_index=index, heading_path=path,
            table_context=table, start_offset=start, end_offset=end,
        ))
    if not chunks:
        raise EmptyChunkError("文档只有标题或空白，没有可检索正文。")
    return chunks
