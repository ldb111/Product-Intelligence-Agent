"""把已验证的浏览器交互状态采集为可信 Interactive State。

本模块属于 Stage 1 V0.3-3C-2。它只处理一次已经完成安全点击和 Local Scope 解析的
当前状态：读取局部可见 DOM，复用现有 Structured Blocks、Group/Card 和 DOM 去重，
再生成稳定内容哈希。它不遍历其他 Tab、不保存 Snapshot，也不参与 Change Detection。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from backend.browser_reader import BrowserReadError, extract_visible_scope_html
from backend.interactive_state import (
    create_interactive_state_skeleton,
    discover_safe_tab_groups,
)
from backend.page_reader import build_structured_content


def compute_interactive_state_content_hash(blocks: list[dict[str, Any]]) -> str:
    """只根据 Structured Blocks 计算 Interactive State 的 SHA-256 哈希。

    在业务链路中的职责：为同一个交互状态的业务内容建立稳定摘要，后续即使采集时间、
    DOM 路径或网页实现细节变化，只要抽取出的业务 Blocks 相同，摘要就保持相同。

    输入：Local Scope 经现有结构抽取器生成的 blocks。
    处理：使用键排序和固定分隔符转成规范 JSON，再以 UTF-8 编码并计算 SHA-256。
    输出：64 位小写十六进制字符串。captured_at、运行时定位和 Raw DOM 均不在输入中，
    因此不会参与内容身份。
    """
    canonical_blocks = json.dumps(
        blocks,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_blocks.encode("utf-8")).hexdigest()


def _current_captured_at() -> str:
    """生成带时区的 ISO 8601 审计时间，不参与状态内容哈希。"""
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _failure(code: str, message: str) -> dict[str, Any]:
    """生成字段稳定的 State Capture 失败结果，且绝不返回半成品 State。"""
    return {
        "success": False,
        "error": {"code": code, "message": message},
        "interactive_state": None,
        "diagnostics": None,
    }


def _runtime_group_path(group: dict[str, Any]) -> list[str] | None:
    """读取只供当前浏览器会话定位的 Tab Group DOM 路径。"""
    runtime_locator = group.get("runtime_locator")
    if not isinstance(runtime_locator, dict):
        return None
    dom_path = runtime_locator.get("dom_path")
    if not isinstance(dom_path, list) or not all(
        isinstance(part, str) and part for part in dom_path
    ):
        return None
    return list(dom_path)


def _find_current_group(
    page: Any, original_group: dict[str, Any]
) -> dict[str, Any] | None:
    """重新执行冻结的 3A 发现，并用运行时路径找到点击后的同一个控件组。

    DOM 路径只用于这次浏览器会话内把操作结果与控件对应起来；真正写入 State 的身份
    仍完全来自 3A 生成的 scope_path、state_path 和 state_key。
    """
    expected_path = _runtime_group_path(original_group)
    if expected_path is None:
        return None
    for current_group in discover_safe_tab_groups(page):
        if _runtime_group_path(current_group) == expected_path:
            return current_group
    return None


def _stable_state_skeleton(group: dict[str, Any]) -> dict[str, Any] | None:
    """从 3A 结果读取唯一稳定语义 State 骨架；不可用时不自行猜测身份。"""
    states = group.get("states")
    if group.get("stable_state_available") is not True:
        return None
    if not isinstance(states, list) or len(states) != 1:
        return None
    state = states[0]
    if not isinstance(state, dict):
        return None
    if not state.get("scope_path") or not state.get("state_path") or not state.get(
        "state_key"
    ):
        return None
    return state


def capture_interactive_state(
    page: Any,
    tab_group: dict[str, Any],
    safe_click_result: dict[str, Any],
    local_scope_result: dict[str, Any],
) -> dict[str, Any]:
    """采集一次已验证交互状态的 Local Scope，生成可信 Interactive State。

    输入：当前 Playwright Page、点击前由 3A 发现的 Tab Group、3B 安全点击成功结果，
    以及 3C-1 Local Scope 成功结果。
    处理：再次用 3A 确认当前语义状态；只读取 Local Scope 的渲染可见 DOM；复用现有
    噪声清理、Structured Blocks、Group/Card 和 DOM 去重；最后对规范 blocks JSON 计算
    SHA-256，并生成带时区采集时间。
    输出：成功时返回完整 Interactive State；任何前置验证、语义身份、范围定位或结构
    抽取失败时返回稳定错误，interactive_state 为 None，不产生可信半成品。
    """
    if safe_click_result.get("success") is not True:
        return _failure(
            "safe_click_not_validated",
            "Interactive State requires a successfully validated Tab switch.",
        )
    if local_scope_result.get("success") is not True:
        return _failure(
            "local_scope_not_resolved",
            "Interactive State requires a successfully resolved Local Scope.",
        )

    original_state = _stable_state_skeleton(tab_group)
    if original_state is None:
        return _failure(
            "stable_semantic_state_unavailable",
            "The original Tab group has no stable semantic Interactive State identity.",
        )

    current_group = _find_current_group(page, tab_group)
    if current_group is None:
        return _failure(
            "current_safe_tab_group_unavailable",
            "The Tab group no longer passes Safe Tab Group Discovery.",
        )
    current_state = _stable_state_skeleton(current_group)
    if current_state is None:
        return _failure(
            "stable_semantic_state_unavailable",
            "The current Tab selection has no stable semantic Interactive State identity.",
        )
    if current_state["scope_path"] != original_state["scope_path"]:
        return _failure(
            "semantic_scope_changed",
            "The Tab group's semantic scope changed during the validated interaction.",
        )

    runtime_locator = local_scope_result.get("runtime_locator")
    runtime_dom_path = (
        runtime_locator.get("dom_path")
        if isinstance(runtime_locator, dict)
        else None
    )
    if not isinstance(runtime_dom_path, list) or not runtime_dom_path:
        return _failure(
            "local_scope_runtime_locator_invalid",
            "Resolved Local Scope has no valid runtime DOM path.",
        )

    try:
        visible_scope = extract_visible_scope_html(page, runtime_dom_path)
        soup = BeautifulSoup(visible_scope["html"], "html.parser")
        blocks, _ = build_structured_content(soup)
    except BrowserReadError as exc:
        return _failure(exc.error_type, str(exc))
    except Exception as exc:
        # BeautifulSoup 和结构抽取异常统一转换为稳定边界，避免 traceback 被误当业务输出。
        return _failure(
            "state_structure_extraction_failed",
            f"Could not extract Structured Blocks from Local Scope: {exc}",
        )

    if not blocks:
        return _failure(
            "state_blocks_unavailable",
            "Local Scope did not produce any usable Structured Blocks.",
        )

    # 复用 3A 的构造器，而不是从 DOM 索引重新制造身份。当前 state_key、scope_path 和
    # state_path 因而继续遵守已冻结的规范化规则；is_default 也用语义 key 与默认 key 比较。
    interactive_state = create_interactive_state_skeleton(
        scope_path=current_state["scope_path"],
        state_path=current_state["state_path"],
        is_default=current_state["state_key"] == original_state["state_key"],
    )
    interactive_state["blocks"] = blocks
    interactive_state["content_hash"] = compute_interactive_state_content_hash(
        blocks
    )
    interactive_state["captured_at"] = _current_captured_at()

    return {
        "success": True,
        "error": None,
        "interactive_state": interactive_state,
        # 诊断字段可以包含运行过程信息，但与 interactive_state 分开，也不参与内容哈希。
        "diagnostics": {
            "local_scope_method": local_scope_result.get("method"),
            "hidden_element_count": visible_scope["hidden_element_count"],
            "computed_text_mark_count": visible_scope[
                "computed_text_mark_count"
            ],
            "block_count": len(blocks),
        },
    }
