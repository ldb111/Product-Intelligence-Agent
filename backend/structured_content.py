"""把清理噪声后的 HTML 页面树转换为 Structured Blocks（结构化内容块）。

本模块只负责 Stage 1 V0.2-1 的结构提取和稳定文本序列化，不请求网页、不删除噪声标签，
也不处理 Snapshot、Change Detection 或 Diff。调用方应先删除已经确定的结构噪声，再把
BeautifulSoup 页面树传入 extract_structured_blocks。

第一版只产生 heading、paragraph、list、table、code 五类 Block。BeautifulSoup 只处理
服务器返回的 HTML，不执行 JavaScript，因此浏览器运行脚本后才出现的结构不在输入范围内。
"""

from __future__ import annotations

from typing import Any, Iterable

from bs4 import BeautifulSoup, Comment, Doctype, NavigableString, Tag


StructuredBlock = dict[str, Any]

HEADING_TAG_NAMES = {f"h{level}" for level in range(1, 7)}
LIST_TAG_NAMES = {"ul", "ol"}
STRUCTURED_BLOCK_TAG_NAMES = HEADING_TAG_NAMES | {
    "p",
    "ul",
    "ol",
    "table",
    "pre",
    "code",
}

# 这些标签自身不是本版本的 Block，但通常承担页面分区作用。递归进入它们可以保持
# 页面中的先后顺序，并把其中没有块级标签包裹的可见文字转成 paragraph。
CONTAINER_TAG_NAMES = {
    "html",
    "body",
    "main",
    "header",
    "aside",
    "section",
    "article",
    "div",
    "blockquote",
    "figure",
    "figcaption",
    "details",
    "summary",
    "form",
    "address",
    "dl",
    "dt",
    "dd",
}

# title 已由 page_reader 单独提取；其余标签不承载本任务需要的正文结构。跳过它们可以
# 避免把文档元数据或 SVG 图标内部文字误当成 paragraph，但不增加网站专用规则。
IGNORED_CONTENT_TAG_NAMES = {
    "head",
    "title",
    "meta",
    "link",
    "svg",
    "path",
    "template",
}

# 在 list item 或 table cell 内遇到这些块级容器时补一个空格边界，避免多个段落被直接
# 拼成“第一段第二段”。strong、span、em、a 等行内标签不在此处，因此不会把单词拆开。
TEXT_BOUNDARY_TAG_NAMES = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "aside",
    "blockquote",
}


def _normalize_inline_whitespace(text: str) -> str:
    """整理一个逻辑文本块内部的空白，同时保持行内节点连续。"""
    return " ".join(text.split())


def _collect_text_parts(
    node: object,
    parts: list[str],
    skipped_tag_names: set[str],
) -> None:
    """递归收集文字，不在 strong/span/a 等行内标签边界主动插入换行。"""
    if isinstance(node, (Comment, Doctype)):
        return
    if isinstance(node, NavigableString):
        parts.append(str(node))
        return
    if not isinstance(node, Tag) or node.name in skipped_tag_names:
        return

    if node.name == "br":
        parts.append("\n")
        return

    has_text_boundary = node.name in TEXT_BOUNDARY_TAG_NAMES
    if has_text_boundary:
        parts.append(" ")
    for child in node.children:
        _collect_text_parts(child, parts, skipped_tag_names)
    if has_text_boundary:
        parts.append(" ")


def _extract_inline_text(
    nodes: Iterable[object], skipped_tag_names: set[str] | None = None
) -> str:
    """合并一组节点的可见文字，并在全部节点合并后统一清理空白。

    为什么最后才清理：如果分别处理 ``Av``、``<span>ail</span>``、``able``，再人为
    加分隔符就会破坏单词。先按 DOM 顺序无缝收集，能够得到正确的 ``Available``。
    """
    parts: list[str] = []
    skipped_names = skipped_tag_names or set()
    for node in nodes:
        _collect_text_parts(node, parts, skipped_names)
    return _normalize_inline_whitespace("".join(parts))


def _is_inside_skipped_descendant(
    tag: Tag, root: Tag, skipped_tag_names: set[str]
) -> bool:
    """判断链接是否位于当前 Block 要交给子结构处理的容器中。"""
    parent = tag.parent
    while isinstance(parent, Tag) and parent is not root:
        if parent.name in skipped_tag_names:
            return True
        parent = parent.parent
    return False


def _extract_links(root: Tag, skipped_tag_names: set[str] | None = None) -> list[dict[str, str]]:
    """提取一个标题、段落或列表项内部的链接文本与原始 href。"""
    skipped_names = skipped_tag_names or set()
    links: list[dict[str, str]] = []

    candidates = list(root.find_all("a"))
    if root.name == "a":
        candidates.insert(0, root)

    for link in candidates:
        if _is_inside_skipped_descendant(link, root, skipped_names):
            continue
        href_value = link.get("href", "")
        links.append(
            {
                "text": _extract_inline_text(link.children),
                "href": str(href_value) if href_value is not None else "",
            }
        )

    return links


def _build_heading(tag: Tag) -> StructuredBlock | None:
    """将 h1～h6 转成保留层级、完整文字和链接信息的 heading Block。"""
    text = _extract_inline_text(tag.children)
    if not text:
        return None
    return {
        "type": "heading",
        "level": int(tag.name[1]),
        "text": text,
        "links": _extract_links(tag),
    }


def _build_paragraph_from_nodes(nodes: list[object]) -> StructuredBlock | None:
    """把一组连续行内节点转换为 paragraph，避免 DOM 节点边界制造错误换行。"""
    text = _extract_inline_text(nodes)
    if not text:
        return None

    links: list[dict[str, str]] = []
    for node in nodes:
        if isinstance(node, Tag):
            links.extend(_extract_links(node))

    return {"type": "paragraph", "text": text, "links": links}


def _build_paragraph(tag: Tag) -> StructuredBlock | None:
    """把一个 p 标签转换为 paragraph，并保留其中的链接。"""
    return _build_paragraph_from_nodes([tag])


def _build_list(tag: Tag) -> StructuredBlock:
    """递归提取 ul/ol，确保嵌套列表仍属于对应的父列表项。

    输入：当前 ul 或 ol 标签。
    处理：只读取当前层的直接 li；父项文字排除内部子列表，再递归构造 children。
    输出：包含 ordered 和 items 的 list Block，每个 item 都保存 text、links、children。
    """
    items: list[dict[str, Any]] = []

    for item_tag in tag.find_all("li", recursive=False):
        nested_lists = [
            nested_list
            for nested_list in item_tag.find_all(["ul", "ol"])
            if nested_list.find_parent(["ul", "ol"]) is tag
        ]
        item = {
            "text": _extract_inline_text(item_tag.children, LIST_TAG_NAMES),
            "links": _extract_links(item_tag, LIST_TAG_NAMES),
            "children": [_build_list(nested_list) for nested_list in nested_lists],
        }
        # 没有文字但包含子列表的 li 仍有层级意义，不能直接丢弃。
        if item["text"] or item["children"]:
            items.append(item)

    return {
        "type": "list",
        "ordered": tag.name == "ol",
        "items": items,
    }


def _build_table(tag: Tag) -> StructuredBlock:
    """直接从 tr/th/td 构造二维表格，不经过纯文本反向猜测。

    第一行包含 th 时作为 headers；其余行按原始单元格顺序进入 rows。嵌套 table 的 tr
    不属于当前表格，会留给其自身处理。第一版不展开 rowspan/colspan。
    """
    headers: list[str] = []
    rows: list[list[str]] = []

    for row_tag in tag.find_all("tr"):
        if row_tag.find_parent("table") is not tag:
            continue
        cell_tags = row_tag.find_all(["th", "td"], recursive=False)
        if not cell_tags:
            continue
        cells = [
            _extract_inline_text(cell_tag.children, {"table"})
            for cell_tag in cell_tags
        ]
        if not headers and any(cell_tag.name == "th" for cell_tag in cell_tags):
            headers = cells
        else:
            rows.append(cells)

    return {"type": "table", "headers": headers, "rows": rows}


def _build_code(tag: Tag) -> StructuredBlock:
    """整体保存 pre/code 的文字，语法高亮 span 不会切碎代码。

    get_text 使用空分隔符，仅移除 HTML 标签本身，代码中的原始换行和缩进不会像普通
    paragraph 那样被空白标准化。pre 内部的 code 由外层 pre 一次处理，避免重复 Block。
    """
    return {"type": "code", "text": tag.get_text("", strip=False)}


def _walk_container(container: BeautifulSoup | Tag) -> list[StructuredBlock]:
    """按 DOM 顺序遍历一个页面容器，并避免父子 Block 重复输出。"""
    blocks: list[StructuredBlock] = []
    pending_inline_nodes: list[object] = []

    def flush_inline_nodes() -> None:
        paragraph = _build_paragraph_from_nodes(pending_inline_nodes)
        if paragraph is not None:
            blocks.append(paragraph)
        pending_inline_nodes.clear()

    for child in container.children:
        if isinstance(child, (Comment, Doctype)):
            continue
        if isinstance(child, NavigableString):
            pending_inline_nodes.append(child)
            continue
        if not isinstance(child, Tag):
            continue
        if child.name in IGNORED_CONTENT_TAG_NAMES:
            continue

        if child.name in STRUCTURED_BLOCK_TAG_NAMES:
            flush_inline_nodes()
            block: StructuredBlock | None
            if child.name in HEADING_TAG_NAMES:
                block = _build_heading(child)
            elif child.name == "p":
                block = _build_paragraph(child)
            elif child.name in LIST_TAG_NAMES:
                block = _build_list(child)
            elif child.name == "table":
                block = _build_table(child)
            else:
                block = _build_code(child)
            if block is not None:
                blocks.append(block)
            continue

        # 常见页面容器，或包含受支持 Block 的自定义容器，需要递归处理。否则把它当作
        # 行内节点的一部分，才能正确合并 span/strong/em/a 等标签中的连续文字。
        contains_structured_block = child.find(
            list(STRUCTURED_BLOCK_TAG_NAMES)
        ) is not None
        if child.name in CONTAINER_TAG_NAMES or contains_structured_block:
            flush_inline_nodes()
            blocks.extend(_walk_container(child))
        else:
            pending_inline_nodes.append(child)

    flush_inline_nodes()
    return blocks


def extract_structured_blocks(soup: BeautifulSoup) -> list[StructuredBlock]:
    """从已删除噪声的 BeautifulSoup 页面树提取五类 Structured Blocks。

    输入：仍保留 HTML 结构、但已由调用方删除确定性噪声标签的页面树。
    处理：按 DOM 顺序识别标题、段落、列表、表格和代码，并为无块标签包裹的可见文字
    生成 paragraph；行内标签只贡献连续文字和链接信息。
    输出：顺序稳定、可直接 JSON 序列化的 Block 字典列表。
    """
    return _walk_container(soup)


def _serialize_list(block: StructuredBlock, depth: int = 0) -> list[str]:
    """把递归 list Block 转成带缩进的可读文本行。"""
    lines: list[str] = []
    for index, item in enumerate(block["items"], start=1):
        marker = f"{index}." if block["ordered"] else "-"
        lines.append(f"{'  ' * depth}{marker} {item['text']}".rstrip())
        for child_list in item["children"]:
            lines.extend(_serialize_list(child_list, depth + 1))
    return lines


def _escape_table_cell(cell: str) -> str:
    """转义文本表格分隔符，避免单元格中的竖线被误认为新列。"""
    return cell.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _serialize_table(block: StructuredBlock) -> list[str]:
    """把 headers/rows 序列化为列关系清晰的 Markdown 风格表格。"""
    lines: list[str] = []
    headers = block["headers"]
    if headers:
        lines.append("| " + " | ".join(_escape_table_cell(cell) for cell in headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in block["rows"]:
        lines.append("| " + " | ".join(_escape_table_cell(cell) for cell in row) + " |")
    return lines


def serialize_blocks(blocks: list[StructuredBlock]) -> str:
    """将 Structured Blocks 稳定序列化为兼容现有链路的 content 字符串。

    heading 使用 Markdown 层级标记；paragraph 保留连续文字；list 使用缩进表达父子层级；
    table 使用行列明确的管道格式；code 使用围栏并保持 Block 内原始换行和缩进。各 Block
    之间固定空一行，使 Snapshot、Content Hash、Change Detection 和行级 Diff 仍可继续
    使用字符串 content，同时不再以 soup.get_text("\\n") 作为最终事实底座。
    """
    sections: list[str] = []

    for block in blocks:
        block_type = block["type"]
        if block_type == "heading":
            sections.append(f"{'#' * block['level']} {block['text']}")
        elif block_type == "paragraph":
            sections.append(block["text"])
        elif block_type == "list":
            sections.append("\n".join(_serialize_list(block)))
        elif block_type == "table":
            sections.append("\n".join(_serialize_table(block)))
        elif block_type == "code":
            code_text = block["text"]
            closing_prefix = "" if code_text.endswith("\n") else "\n"
            sections.append(f"```\n{code_text}{closing_prefix}```")

    return "\n\n".join(section for section in sections if section)
