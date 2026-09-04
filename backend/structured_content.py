"""把清理噪声后的 HTML 页面树转换为 Structured Blocks（结构化内容块）。

本模块负责结构提取、完整重复 DOM 序列去重和稳定文本序列化，不请求网页、不删除噪声
标签，也不处理 Snapshot、Change Detection 或 Diff。调用方应先删除已经确定的结构
噪声，再把 BeautifulSoup 页面树传入 extract_structured_blocks。

基础 Block 包括 heading、paragraph、list、table、code；结构相似的重复兄弟卡片会额外
保留为 group/card。BeautifulSoup 本身不执行 JavaScript，因此动态结构必须先由浏览器
采集模块取得渲染后 HTML，才能进入这里解析。
"""

from __future__ import annotations

import difflib
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

# 只有同一父节点至少出现“3 个元素 × 完整两套”时才考虑去重；同时要求一套序列内部
# 至少包含两种不同子树，避免 6 个合法相同按钮被误认为 A B C / A B C 轮播副本。
MIN_REPEATED_SEQUENCE_LENGTH = 3
MIN_DISTINCT_SUBTREES_IN_SEQUENCE = 2

# Group/Card 识别只把 article 和普通 div 当作候选，不依赖站点域名或 CSS class。
# 至少两个相邻候选才能形成 group；结构相似阈值集中定义，避免规则散落在遍历代码中。
CARD_CONTAINER_TAG_NAMES = {"article", "div"}
MIN_CARD_GROUP_SIZE = 2
CARD_STRUCTURE_SIMILARITY_THRESHOLD = 0.8

# 这些标签自身带有明确文本语义。card 仍保留原始 blocks，同时额外记录标记文本，避免
# 价格删除线等信息在转成普通字符串后丢失。多个标签可映射到同一种稳定业务标记。
SEMANTIC_TEXT_MARKS = {
    "del": "strikethrough",
    "s": "strikethrough",
    "strike": "strikethrough",
    "strong": "strong",
    "b": "strong",
    "em": "emphasis",
    "i": "emphasis",
    "mark": "highlight",
    "ins": "insertion",
}


def _normalize_inline_whitespace(text: str) -> str:
    """整理一个逻辑文本块内部的空白，同时保持行内节点连续。"""
    return " ".join(text.split())


def _normalize_attribute_value(value: Any) -> str | tuple[str, ...]:
    """把 BeautifulSoup 属性值转换成可稳定比较、可哈希的简单类型。"""
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return "" if value is None else str(value)


def _build_subtree_fingerprint(tag: Tag) -> tuple[Any, ...]:
    """为一个元素生成同时包含 DOM 结构和规范文本的子树指纹。

    输入：父容器中的一个直接元素子节点。
    处理：递归记录标签名、全部 HTML 属性、规范化文本和子节点顺序；注释及纯排版空白
    不参与比较。属性也参与指纹，避免文字相同但链接地址、图片来源或语义属性不同的
    卡片被误认为同一副本。
    输出：可直接进行相等比较和集合去重的嵌套 tuple（元组）。
    """
    attributes = tuple(
        sorted(
            (
                str(attribute_name),
                _normalize_attribute_value(attribute_value),
            )
            for attribute_name, attribute_value in tag.attrs.items()
        )
    )
    children: list[tuple[Any, ...]] = []

    for child in tag.children:
        if isinstance(child, (Comment, Doctype)):
            continue
        if isinstance(child, NavigableString):
            normalized_text = _normalize_inline_whitespace(str(child))
            if normalized_text:
                children.append(("text", normalized_text))
            continue
        if isinstance(child, Tag):
            children.append(_build_subtree_fingerprint(child))

    return ("tag", tag.name, attributes, tuple(children))


def _deduplicate_one_parent(parent: BeautifulSoup | Tag) -> int:
    """删除一个父容器中完全重复的后半段元素序列，并返回删除数量。

    该函数只接受“全部直接元素子节点恰好由两个相同半段组成”的强证据。父节点中如果
    混有有意义的直接文本、元素数量为奇数、序列太短、两半任一指纹不同，都会保持原样。
    """
    direct_children: list[Tag] = []
    for child in parent.children:
        if isinstance(child, (Comment, Doctype)):
            continue
        if isinstance(child, NavigableString):
            if str(child).strip():
                return 0
            continue
        if isinstance(child, Tag):
            direct_children.append(child)

    child_count = len(direct_children)
    if (
        child_count < MIN_REPEATED_SEQUENCE_LENGTH * 2
        or child_count % 2 != 0
    ):
        return 0

    midpoint = child_count // 2
    first_sequence = [
        _build_subtree_fingerprint(child)
        for child in direct_children[:midpoint]
    ]
    second_sequence = [
        _build_subtree_fingerprint(child)
        for child in direct_children[midpoint:]
    ]

    if first_sequence != second_sequence:
        return 0
    if len(set(first_sequence)) < MIN_DISTINCT_SUBTREES_IN_SEQUENCE:
        return 0

    # 只删除能够由完整前半段逐项证明的后半副本；第一套原始 DOM 保持不变。
    for duplicated_child in direct_children[midpoint:]:
        duplicated_child.decompose()
    return midpoint


def deduplicate_repeated_sibling_sequences(
    soup: BeautifulSoup | Tag,
) -> dict[str, int]:
    """保守删除同一父容器中的完整重复 DOM 序列。

    在业务链路中的职责：在 Structured Blocks 生成前移除轮播组件为视觉循环复制的整套
    DOM，避免同一批真实内容两次进入 Canonical Content、Diff 和 Stage 2 输入。

    输入：已完成确定性噪声删除、仍保留 DOM 结构的 BeautifulSoup 页面树或 Tag。
    处理：先递归处理子容器，再检查每个父容器的直接元素子节点是否严格等于“两套完整
    相同序列”；比较只发生在同一 parent 内，不跨页面区域查找相同文本。
    输出：包含去重父容器数、重复序列数和删除子树数的诊断计数；页面树会原地更新。

    已知边界：第一版只识别整个父容器恰好由两套序列组成的情况。三套副本、带前后装饰
    节点、属性不一致的视觉副本会保守保留，宁可漏删也不猜测。
    """
    metrics = {
        "deduplicated_parent_count": 0,
        "deduplicated_sequence_count": 0,
        "removed_subtree_count": 0,
    }

    def visit(parent: BeautifulSoup | Tag) -> None:
        # 先检查父节点，保证“两个半段完全一致”的结论来自未经子级去重改写的原始子树；
        # 随后只递归仍保留的第一套节点，不会操作已经 decompose 的后半副本。
        removed_count = _deduplicate_one_parent(parent)
        if removed_count:
            metrics["deduplicated_parent_count"] += 1
            metrics["deduplicated_sequence_count"] += 1
            metrics["removed_subtree_count"] += removed_count

        for child in list(parent.children):
            if isinstance(child, Tag):
                visit(child)

    visit(soup)
    return metrics


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


def _build_structure_tokens(tag: Tag) -> list[str]:
    """把候选卡片子树转换成忽略文案和样式属性的 DOM 结构序列。

    输入：article 或 div 候选容器。
    处理：按 DOM 顺序记录开始/结束标签，不记录文字、class、id 等站点实现细节。
    输出：供相似度比较的字符串序列；文案不同但模板接近的卡片仍可被归为一组。
    """
    tokens = [f"<{tag.name}>"]
    for child in tag.children:
        if isinstance(child, Tag):
            tokens.extend(_build_structure_tokens(child))
    tokens.append(f"</{tag.name}>")
    return tokens


def _card_structure_similarity(first: Tag, second: Tag) -> float:
    """计算两个候选容器的通用 DOM 结构相似度，不比较具体业务文本。"""
    return difflib.SequenceMatcher(
        None,
        _build_structure_tokens(first),
        _build_structure_tokens(second),
        autojunk=False,
    ).ratio()


def _find_card_title_tag(tag: Tag) -> Tag | None:
    """返回候选卡片中的第一个标题标签；没有明确标题时不猜测。"""
    return tag.find(list(HEADING_TAG_NAMES))


def _is_card_candidate(tag: Tag) -> bool:
    """保守判断一个 article/div 是否具备“标题 + 正文”的卡片基本证据。

    仅有相似结构并不足以证明它是卡片，例如多个纯布局 div 也可能结构一致。因此这里
    要求明确 heading，并要求标题之外仍有可见文字；没有证据时继续走原有扁平提取。
    """
    if tag.name not in CARD_CONTAINER_TAG_NAMES:
        return False

    title_tag = _find_card_title_tag(tag)
    if title_tag is None:
        return False

    title = _extract_inline_text(title_tag.children)
    whole_text = _extract_inline_text(tag.children)
    return bool(title and whole_text and whole_text != title)


def _find_card_group_runs(container: BeautifulSoup | Tag) -> list[list[Tag]]:
    """查找同一父容器内连续、结构相似的 article/div 卡片序列。

    输入：当前共同父容器。
    处理：忽略兄弟之间的排版空白；有意义的直接文本或非候选元素会结束当前序列；
    候选卡片与序列首项达到统一相似度阈值时才进入同一 group。
    输出：每个至少包含两个卡片根节点的连续序列。
    """
    groups: list[list[Tag]] = []
    current_run: list[Tag] = []

    def finish_run() -> None:
        if len(current_run) >= MIN_CARD_GROUP_SIZE:
            groups.append(list(current_run))
        current_run.clear()

    for child in container.children:
        if isinstance(child, (Comment, Doctype)):
            continue
        if isinstance(child, NavigableString):
            if str(child).strip():
                finish_run()
            continue
        if not isinstance(child, Tag) or not _is_card_candidate(child):
            finish_run()
            continue

        if not current_run:
            current_run.append(child)
            continue

        similarity = _card_structure_similarity(current_run[0], child)
        if similarity >= CARD_STRUCTURE_SIMILARITY_THRESHOLD:
            current_run.append(child)
        else:
            finish_run()
            current_run.append(child)

    finish_run()
    return groups


def _extract_definition_pairs(root: Tag) -> list[dict[str, str]]:
    """从 card 内的 dl/dt/dd 直接保留 key-value 关系。

    一个 dt 后连续出现的多个 dd 仍属于同一个 key，使用换行连接到 value；这比先压成
    paragraph 再猜测字段关系可靠。嵌套 dl 各自处理，不会把内外层定义混在一起。
    """
    pairs: list[dict[str, str]] = []

    for definition_list in root.find_all("dl"):
        current_key: str | None = None
        current_values: list[str] = []

        def finish_pair() -> None:
            if current_key and current_values:
                pairs.append(
                    {"key": current_key, "value": "\n".join(current_values)}
                )

        for definition_part in definition_list.find_all(["dt", "dd"]):
            # 只处理最近 dl 就是当前 definition_list 的节点，避免外层重复读取嵌套 dl。
            if definition_part.find_parent("dl") is not definition_list:
                continue
            text = _extract_inline_text(definition_part.children)
            if definition_part.name == "dt":
                finish_pair()
                current_key = text
                current_values = []
            elif current_key is not None and text:
                current_values.append(text)

        finish_pair()

    return pairs


def _extract_text_marks(root: Tag) -> list[dict[str, Any]]:
    """提取删除线、强调、插入等由 HTML 标签明确表达的文本语义。"""
    marked_text: list[dict[str, Any]] = []
    for marked_tag in root.find_all(list(SEMANTIC_TEXT_MARKS)):
        text = _extract_inline_text(marked_tag.children)
        if text:
            marked_text.append(
                {
                    "text": text,
                    "marks": [SEMANTIC_TEXT_MARKS[marked_tag.name]],
                }
            )
    return marked_text


def _build_card(tag: Tag) -> dict[str, Any]:
    """把一个已确认的卡片容器转换为保留原有 Blocks 和附加语义的 card。"""
    title_tag = _find_card_title_tag(tag)
    assert title_tag is not None
    return {
        "type": "card",
        "title": _extract_inline_text(title_tag.children),
        # 原有 heading/paragraph/list/table/code 仍完整保存在 card.blocks 中，保证信息不丢失。
        "blocks": _walk_container(tag),
        "links": _extract_links(tag),
        "key_values": _extract_definition_pairs(tag),
        "text_marks": _extract_text_marks(tag),
    }


def _build_group(card_tags: list[Tag]) -> StructuredBlock:
    """把同一父容器下的结构相似卡片组成一个 group Block。"""
    return {
        "type": "group",
        "cards": [_build_card(card_tag) for card_tag in card_tags],
    }


def _walk_container(container: BeautifulSoup | Tag) -> list[StructuredBlock]:
    """按 DOM 顺序遍历容器，优先保留 group/card，再提取原有基础 Block。"""
    blocks: list[StructuredBlock] = []
    pending_inline_nodes: list[object] = []
    card_groups = _find_card_group_runs(container)
    group_by_first_card = {id(group[0]): group for group in card_groups}
    grouped_card_ids = {
        id(card_tag) for group in card_groups for card_tag in group
    }

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

        group = group_by_first_card.get(id(child))
        if group is not None:
            flush_inline_nodes()
            blocks.append(_build_group(group))
            continue
        if id(child) in grouped_card_ids:
            # 同组后续 card 已经由第一个兄弟节点一次构造，不能再扁平输出造成正文重复。
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
    """从已删除噪声的页面树提取基础 Blocks，并保留可确认的 group/card。

    输入：仍保留 HTML 结构、但已由调用方删除确定性噪声标签的页面树。
    处理：先保守删除同一父容器中的完整重复 DOM 序列，再识别结构相似的兄弟卡片；
    非卡片区域继续按原有顺序提取标题、段落、列表、表格和代码。
    输出：顺序稳定、可直接 JSON 序列化的 Block 字典列表。
    """
    deduplicate_repeated_sibling_sequences(soup)
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
        elif block_type == "group":
            # group/card 是新增结构层，不应让同一份正文重复进入 content。递归序列化每张
            # card 中原样保留的基础 blocks，可以维持旧版扁平提取的文本顺序和格式。
            card_contents = [
                serialize_blocks(card.get("blocks", []))
                for card in block.get("cards", [])
                if isinstance(card, dict)
            ]
            sections.append("\n\n".join(content for content in card_contents if content))

    return "\n\n".join(section for section in sections if section)
