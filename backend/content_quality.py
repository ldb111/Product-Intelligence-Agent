"""使用确定性规则判断一次静态网页采集是否足够可信。

Content Quality Gate（内容质量门槛）位于 Structured Blocks 生成之后、可信 Snapshot
保存之前。HTTP 200 只说明服务器成功返回响应，不能证明响应中包含真实页面正文；本模块
负责拦截空内容、动态加载占位页和异常稀疏结果，但不判断内容价值或竞争意义。

第一版有意保持简单、可解释，不调用 LLM，也不执行浏览器渲染。
"""

from __future__ import annotations

from typing import Any


QUALITY_PASS = "PASS"
QUALITY_WARNING = "WARNING"
QUALITY_FAIL = "FAIL"

# 动态占位词只有同时满足“正文短、Block 少”时才触发 FAIL，避免正常长页面偶然提到
# Loading 或 JavaScript 就被误判。所有阈值集中在这里，后续可根据真实 Bad Case 调整。
DYNAMIC_PLACEHOLDER_MAX_CONTENT_CHARS = 200
DYNAMIC_PLACEHOLDER_MAX_BLOCK_COUNT = 5
SPARSE_CONTENT_CHAR_THRESHOLD = 120
SPARSE_BLOCK_COUNT_THRESHOLD = 3

SUPPORTED_BLOCK_TYPES = ("heading", "paragraph", "list", "table", "code", "group")
DYNAMIC_PLACEHOLDER_PHRASES = (
    "加载中",
    "正在加载",
    "loading",
    "please wait",
    "enable javascript",
    "javascript required",
)


def _block_has_content(block: Any) -> bool:
    """判断一个 Block 是否包含可用于质量判断的真实文字或子结构。"""
    if not isinstance(block, dict):
        return False

    block_type = block.get("type")
    if block_type in {"heading", "paragraph", "code"}:
        return bool(str(block.get("text", "")).strip())

    if block_type == "list":
        items = block.get("items", [])
        if not isinstance(items, list):
            return False
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("text", "")).strip():
                return True
            children = item.get("children", [])
            if isinstance(children, list) and any(
                _block_has_content(child) for child in children
            ):
                return True
        return False

    if block_type == "table":
        table_rows = [block.get("headers", []), *block.get("rows", [])]
        return any(
            str(cell).strip()
            for row in table_rows
            if isinstance(row, list)
            for cell in row
        )

    if block_type == "group":
        cards = block.get("cards", [])
        if not isinstance(cards, list):
            return False
        for card in cards:
            if not isinstance(card, dict):
                continue
            if str(card.get("title", "")).strip():
                return True
            child_blocks = card.get("blocks", [])
            if isinstance(child_blocks, list) and any(
                _block_has_content(child_block) for child_block in child_blocks
            ):
                return True
        return False

    return False


def _build_metrics(content: str, blocks: list[Any]) -> dict[str, int]:
    """计算 Quality Gate 使用并对外解释的简单结构指标。

    content_chars 排除空格和换行，避免 Markdown 序列化中的排版空白夸大正文长度；
    block_count 是收到的 Block 总数，valid_block_count 则只统计确实含有内容的 Block。
    """
    metrics = {
        "content_chars": len("".join(content.split())),
        "block_count": len(blocks),
        "valid_block_count": sum(_block_has_content(block) for block in blocks),
    }
    for block_type in SUPPORTED_BLOCK_TYPES:
        metrics[f"{block_type}_count"] = sum(
            isinstance(block, dict) and block.get("type") == block_type
            for block in blocks
        )
    return metrics


def _contains_dynamic_placeholder(content: str) -> bool:
    """以不区分英文大小写的方式检查第一版动态加载占位词。"""
    comparable_content = content.casefold()
    return any(
        phrase.casefold() in comparable_content
        for phrase in DYNAMIC_PLACEHOLDER_PHRASES
    )


def evaluate_content_quality(
    content: str, blocks: list[dict[str, Any]]
) -> dict[str, Any]:
    """评估 Structured Blocks 结果能否进入可信 Snapshot 和变化检测。

    输入：由 Structured Blocks 稳定序列化的 content，以及原始 Block 列表。
    处理：依次检查空结果、动态占位页和异常稀疏内容；规则按严重程度短路，避免同一次
    采集产生互相冲突的 PASS/WARNING/FAIL 解释。
    输出：状态、下游许可、浏览器回退建议、原因列表和可审计指标。

    业务规则：只有 PASS 的 downstream_allowed 为 True。WARNING 也采用 Fail Closed，
    因为“可能不完整”不应被保存成可信历史并触发虚假 Change Detection。
    """
    safe_content = content if isinstance(content, str) else ""
    safe_blocks = blocks if isinstance(blocks, list) else []
    metrics = _build_metrics(safe_content, safe_blocks)
    reasons: list[dict[str, str]] = []

    if metrics["content_chars"] == 0:
        reasons.append(
            {
                "code": "empty_content",
                "message": "标准化后的 content 为空，未获得可用页面正文。",
            }
        )
    if metrics["valid_block_count"] == 0:
        reasons.append(
            {
                "code": "no_valid_blocks",
                "message": "Structured Blocks 为空或不包含有效内容。",
            }
        )
    if reasons:
        status = QUALITY_FAIL
    elif (
        _contains_dynamic_placeholder(safe_content)
        and metrics["content_chars"] <= DYNAMIC_PLACEHOLDER_MAX_CONTENT_CHARS
        and metrics["block_count"] <= DYNAMIC_PLACEHOLDER_MAX_BLOCK_COUNT
    ):
        status = QUALITY_FAIL
        reasons.append(
            {
                "code": "dynamic_placeholder",
                "message": (
                    "页面内容很少且包含动态加载占位提示，静态请求可能未获得真实正文。"
                ),
            }
        )
    elif (
        metrics["content_chars"] < SPARSE_CONTENT_CHAR_THRESHOLD
        and metrics["block_count"] < SPARSE_BLOCK_COUNT_THRESHOLD
    ):
        status = QUALITY_WARNING
        reasons.append(
            {
                "code": "sparse_content",
                "message": "已经获得部分内容，但正文和结构都异常稀疏，完整性仍然可疑。",
            }
        )
    else:
        status = QUALITY_PASS

    downstream_allowed = status == QUALITY_PASS
    return {
        "status": status,
        "downstream_allowed": downstream_allowed,
        # 下一阶段才会真正实现 Browser Rendering；这里只给出建议，不自动启动浏览器。
        "browser_fallback_recommended": not downstream_allowed,
        "reasons": reasons,
        "metrics": metrics,
    }
