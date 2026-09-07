"""编排顶层安全 Tab Group 的 Interactive State Traversal。

本模块属于 Stage 1 V0.3-3D-1，只负责把已经冻结的 3A（发现）、3B（安全点击与
恢复）、3C-1（Local Scope）和 3C-2（State Capture）串成一个有限流程。它不重新
实现任何底层判断，不递归发现嵌套状态，也不写 Snapshot 或参与 Change Detection。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.interactive_state import (
    build_interactive_state_key,
    discover_safe_tab_groups,
)
from backend.local_scope import capture_local_scope_baseline, resolve_local_scope
from backend.state_capture import capture_interactive_state
from backend.tab_interaction import click_safe_tab, restore_default_tab


# 这三类结果说明点击已经越过当前页面的安全边界。即使随后尝试点回原 Tab，也不能证明
# 新页面、下载或导航副作用已被撤销，因此编排器必须立即中止整页遍历。
UNRECOVERABLE_CLICK_ERROR_CODES = {
    "url_changed",
    "new_page_opened",
    "download_triggered",
}

# V0.3-3D-2 的限制属于安全边界，而不是抓取质量判断。达到限制时保留已经采集的状态，
# 并明确记录截断原因；绝不能把尚未遍历的状态解释为页面已删除或业务能力不存在。
MAX_TRAVERSAL_DEPTH = 2
MAX_ACTIONABLE_OPTIONS_PER_GROUP = 6
MAX_NON_DEFAULT_STATES_PER_PAGE = 12


@dataclass
class _TraversalContext:
    """保存一次整页遍历的运行时状态，不进入任何持久化 Interactive State。"""

    page: Any
    result: dict[str, Any]
    click_options: dict[str, Any]
    state_hashes: dict[str, str | None] = field(default_factory=dict)
    visited_state_keys: set[str] = field(default_factory=set)
    visited_group_contexts: set[tuple[Any, ...]] = field(default_factory=set)
    nested_group_paths: set[tuple[str, ...]] = field(default_factory=set)
    nested_exclusion_paths_by_parent_state: dict[
        str, set[tuple[str, ...]]
    ] = field(default_factory=dict)
    state_limit_recorded: bool = False


def _runtime_group_path(group: dict[str, Any]) -> list[str] | None:
    """读取只在当前浏览器会话内使用的 Tab Group DOM 路径。"""
    runtime_locator = group.get("runtime_locator")
    if not isinstance(runtime_locator, dict):
        return None
    dom_path = runtime_locator.get("dom_path")
    if not isinstance(dom_path, list) or not all(
        isinstance(part, str) and part for part in dom_path
    ):
        return None
    return list(dom_path)


def _default_tab_index(group: dict[str, Any]) -> int | None:
    """读取初次发现时的默认 Tab 索引；该索引不会写入 Interactive State。"""
    runtime_locator = group.get("runtime_locator")
    if not isinstance(runtime_locator, dict):
        return None
    selected_tab_index = runtime_locator.get("selected_tab_index")
    return selected_tab_index if isinstance(selected_tab_index, int) else None


def _actionable_non_default_tabs(
    group: dict[str, Any], default_tab_index: int
) -> list[dict[str, Any]]:
    """按 DOM 顺序返回当前组中能够安全点击的非默认 Tab。"""
    actionable_tabs: list[dict[str, Any]] = []
    for tab in group.get("tabs", []):
        if not isinstance(tab, dict) or not tab.get("visible") or tab.get("disabled"):
            continue
        runtime_locator = tab.get("runtime_locator")
        tab_index = (
            runtime_locator.get("tab_index")
            if isinstance(runtime_locator, dict)
            else None
        )
        if isinstance(tab_index, int) and tab_index != default_tab_index:
            actionable_tabs.append(tab)
    return actionable_tabs


def _bounded_non_default_tabs(
    group: dict[str, Any], default_tab_index: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把单组可处理选项限制在六个，并始终为默认选项保留一个名额。

    限制统计所有可见且未禁用的选项，包括默认选项。因此 V1 每组最多点击五个
    非默认选项；超出的选项只进入截断审计，不会被点击或被解释为缺失。
    """
    non_default_tabs = _actionable_non_default_tabs(group, default_tab_index)
    allowed_count = max(MAX_ACTIONABLE_OPTIONS_PER_GROUP - 1, 0)
    return non_default_tabs[:allowed_count], non_default_tabs[allowed_count:]


def _error_details(result: dict[str, Any], fallback_code: str) -> tuple[str, str]:
    """从冻结能力的失败返回中提取稳定错误码和说明。"""
    error = result.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        return (
            code if isinstance(code, str) and code else fallback_code,
            message if isinstance(message, str) and message else fallback_code,
        )
    return fallback_code, fallback_code


def _append_error(
    errors: list[dict[str, Any]],
    group: dict[str, Any],
    *,
    phase: str,
    code: str,
    message: str,
    target_tab: dict[str, Any] | None = None,
) -> None:
    """记录有限且可审计的遍历错误，不把大段 DOM 或 traceback 放入结果。"""
    runtime_locator = group.get("runtime_locator")
    error: dict[str, Any] = {
        "group_index": (
            runtime_locator.get("group_index")
            if isinstance(runtime_locator, dict)
            else None
        ),
        "scope_path": list(group.get("scope_path", [])),
        "phase": phase,
        "code": code,
        "message": message,
    }
    if isinstance(target_tab, dict):
        error["target_tab"] = target_tab.get("text")
    errors.append(error)


def _current_group_is_default(
    page: Any, original_group: dict[str, Any], default_tab_index: int
) -> bool:
    """点击未成功时重新发现组，确认页面是否仍停留在原始默认状态。

    这里只调用冻结的 Safe Tab Discovery 读取 selected 状态，不自行点击或猜测页面状态。
    如果无法找到同一路径的安全组，就不能证明页面已经恢复。
    """
    expected_path = _runtime_group_path(original_group)
    if expected_path is None:
        return False
    for current_group in discover_safe_tab_groups(page):
        if _runtime_group_path(current_group) != expected_path:
            continue
        current_runtime = current_group.get("runtime_locator")
        return bool(
            isinstance(current_runtime, dict)
            and current_runtime.get("selected_tab_index") == default_tab_index
        )
    return False


def _register_state(
    states: list[dict[str, Any]],
    state_hashes: dict[str, str | None],
    state: dict[str, Any],
) -> str:
    """以语义 state_key 和内容哈希登记状态，并检测不稳定重复采集。

    返回 ``added`` 表示新增，``duplicate`` 表示相同身份和内容已存在，
    ``unstable`` 表示同一语义身份出现不同内容。后者不能覆盖先前证据。
    """
    state_key = state.get("state_key")
    content_hash = state.get("content_hash")
    non_comparable = state.get("comparison_eligible") is False
    if not isinstance(state_key, str) or (
        not isinstance(content_hash, str) and not (non_comparable and content_hash is None)
    ):
        return "invalid"
    if state_key not in state_hashes:
        state_hashes[state_key] = content_hash
        states.append(state)
        return "added"
    previous_hash = state_hashes[state_key]
    if previous_hash == content_hash:
        return "duplicate"
    return "unstable"


def _abort_result(result: dict[str, Any]) -> dict[str, Any]:
    """把当前结果标为整页中止；此时不能声称页面已恢复。"""
    result["status"] = "aborted"
    result["page_restored"] = False
    return result


def _group_audit(group: dict[str, Any]) -> dict[str, Any]:
    """提取足够定位被跳过组的信息，不把完整 DOM 放入审计结果。"""
    runtime_locator = group.get("runtime_locator")
    return {
        "group_index": (
            runtime_locator.get("group_index")
            if isinstance(runtime_locator, dict)
            else None
        ),
        "scope_path": list(group.get("scope_path", [])),
        "runtime_dom_path": _runtime_group_path(group),
    }


def _compose_nested_state(
    state: dict[str, Any], parent_state_path: list[str]
) -> dict[str, Any]:
    """把 3C-2 的当前组状态身份扩展为真实父→子语义路径。

    Blocks、content_hash 和 captured_at 仍完全来自冻结的 State Capture。这里只组合语义
    state_path，并复用 3A 的稳定 key 生成器；DOM path、group/tab index 不进入持久身份。
    """
    composed = dict(state)
    # 顶层状态完全保留 3C-2 已生成的身份，确保 D-1 行为不发生变化；只有真正存在父状态
    # 时才扩展路径并重新计算组合身份。
    if not parent_state_path:
        return composed
    own_state_path = state.get("state_path")
    scope_path = state.get("scope_path")
    if not isinstance(own_state_path, list) or not isinstance(scope_path, list):
        return composed
    composed_path = [*parent_state_path, *own_state_path]
    composed["state_path"] = composed_path
    composed["state_key"] = build_interactive_state_key(scope_path, composed_path)
    return composed


def _is_descendant_path(path: list[str], ancestor: list[str]) -> bool:
    """判断运行时 DOM 路径是否严格位于 Local Scope 内。"""
    return len(path) > len(ancestor) and path[: len(ancestor)] == ancestor


def _discover_child_groups(
    context: _TraversalContext,
    parent_group: dict[str, Any],
    local_scope: dict[str, Any],
) -> list[dict[str, Any]]:
    """在当前父状态 Local Scope 内筛选 Safe Tab Group。

    Safe Tab 的可见性、disabled 和 selected 判断仍由 3A 完成。编排器只用 3C-1 返回的
    Local Scope DOM 路径做包含关系过滤，因此页面其他独立组不会进入当前父状态路径。
    """
    runtime_locator = local_scope.get("runtime_locator")
    scope_path = (
        runtime_locator.get("dom_path")
        if isinstance(runtime_locator, dict)
        else None
    )
    parent_path = _runtime_group_path(parent_group)
    if not isinstance(scope_path, list):
        return []

    child_groups: list[dict[str, Any]] = []
    for candidate in discover_safe_tab_groups(context.page):
        candidate_path = _runtime_group_path(candidate)
        if (
            candidate_path is not None
            and candidate_path != parent_path
            and _is_descendant_path(candidate_path, scope_path)
        ):
            context.nested_group_paths.add(tuple(candidate_path))
            child_groups.append(candidate)
    return child_groups


def _record_truncation(
    context: _TraversalContext,
    *,
    reason: str,
    group: dict[str, Any],
    state_path: list[str],
    details: dict[str, Any],
) -> None:
    """记录边界截断；截断只表示未继续探索，不表示业务状态不存在。"""
    context.result["truncations"].append(
        {
            "reason": reason,
            "group": _group_audit(group),
            "state_path": list(state_path),
            **details,
        }
    )


def _record_state_limit(
    context: _TraversalContext,
    group: dict[str, Any],
    state_path: list[str],
) -> None:
    """整页只记录一次状态上限，避免后续每个未点击 Tab 重复制造日志。"""
    if context.state_limit_recorded:
        return
    context.state_limit_recorded = True
    _record_truncation(
        context,
        reason="state_limit_reached",
        group=group,
        state_path=state_path,
        details={"limit": MAX_NON_DEFAULT_STATES_PER_PAGE},
    )


def _register_captured_state(
    context: _TraversalContext,
    group: dict[str, Any],
    raw_state: Any,
    parent_state_path: list[str],
    *,
    target_tab: dict[str, Any] | None,
    is_non_default: bool,
) -> tuple[dict[str, Any] | None, bool]:
    """组合嵌套身份、执行去重并更新非默认状态计数。

    输出为 ``(state, usable)``。相同 key/hash 会返回已有语义状态但不重复扩展；相同 key
    不同 hash 则记录 unstable_state_capture，且绝不覆盖已经采集的内容。
    """
    if not isinstance(raw_state, dict):
        _append_error(
            context.result["errors"], group, phase="state_capture",
            code="invalid_interactive_state",
            message="State Capture returned no Interactive State.",
            target_tab=target_tab,
        )
        return None, False

    state = _compose_nested_state(raw_state, parent_state_path)
    state_key = state.get("state_key")
    already_visited = isinstance(state_key, str) and state_key in context.visited_state_keys
    registration = _register_state(
        context.result["interactive_states"], context.state_hashes, state
    )
    if registration == "unstable":
        _append_error(
            context.result["errors"], group, phase="state_deduplication",
            code="unstable_state_capture",
            message="The same state_key produced a different content_hash.",
            target_tab=target_tab,
        )
        return None, False
    if registration == "invalid":
        _append_error(
            context.result["errors"], group, phase="state_capture",
            code="invalid_interactive_state",
            message="State Capture returned no stable state_key/content_hash.",
            target_tab=target_tab,
        )
        return None, False

    if already_visited or registration == "duplicate":
        context.result["skipped"].append(
            {
                "reason": "visited_state",
                "group": _group_audit(group),
                "state_path": list(state.get("state_path", [])),
                "state_key": state_key,
            }
        )
        return state, True

    if isinstance(state_key, str):
        context.visited_state_keys.add(state_key)
    if is_non_default and registration == "added":
        context.result["non_default_states_captured"] += 1
    return state, True


def _local_scope_path(local_scope: dict[str, Any]) -> list[str] | None:
    """读取 3C-1 已确认的 Local Scope 运行时路径。"""
    runtime_locator = local_scope.get("runtime_locator")
    dom_path = (
        runtime_locator.get("dom_path")
        if isinstance(runtime_locator, dict)
        else None
    )
    if not isinstance(dom_path, list) or not all(
        isinstance(part, str) and part for part in dom_path
    ):
        return None
    return list(dom_path)


def _has_meaningful_independent_blocks(blocks: list[dict[str, Any]]) -> bool:
    """判断排除子范围后，父状态是否仍有可独立比较的业务内容。

    单独的 heading 只承担导航和上下文作用，不能据此制造父级内容 Hash；段落、列表、
    表格、代码或真实 Group/Card 则属于可独立比较内容。判断只看 Structured Blocks 类型，
    不使用网站文本或 DOM class。
    """
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in {"paragraph", "list", "table", "code"}:
            return True
        if block_type == "group" and block.get("cards"):
            return True
    return False


def _mark_parent_non_comparable(
    context: _TraversalContext,
    state: dict[str, Any],
    *,
    reason: str,
    excluded_scope_count: int,
    residual_blocks: list[dict[str, Any]] | None = None,
) -> None:
    """把只有导航意义的父节点保留下来，但从内容比较集合中明确排除。"""
    state["blocks"] = list(residual_blocks or [])
    state["content_hash"] = None
    state["comparison_eligible"] = False
    state["non_comparable_reason"] = reason
    state["content_ownership"] = {
        "role": "navigation_only",
        "excluded_nested_scope_count": excluded_scope_count,
    }
    state_key = state.get("state_key")
    if isinstance(state_key, str):
        context.state_hashes[state_key] = None


def _finalize_parent_content_ownership(
    context: _TraversalContext,
    group: dict[str, Any],
    state: dict[str, Any],
    click_result: dict[str, Any],
    local_scope: dict[str, Any],
    parent_state_path: list[str],
) -> bool:
    """从父 State 的可比较内容中排除已由嵌套子 State 拥有的 Local Scope。

    子范围必须来自 3C-1 的成功结果，并且在同一父状态遍历期间登记。排除动作重新调用
    3C-2 的统一可见 DOM 与 Structured Blocks 链路，不对已生成文本做字符串去重。
    """
    state_key = state.get("state_key")
    if not isinstance(state_key, str):
        return False
    child_paths = sorted(
        context.nested_exclusion_paths_by_parent_state.get(state_key, set())
    )
    if not child_paths:
        return True

    parent_scope_path = _local_scope_path(local_scope)
    if parent_scope_path is None:
        _mark_parent_non_comparable(
            context,
            state,
            reason="nested_content_exclusion_failed",
            excluded_scope_count=len(child_paths),
        )
        return False

    # 子 Local Scope 若与父范围完全相同，无法在 DOM 上安全切开父子内容。此时宁可把父
    # 节点降级为导航节点，也不能让它重复持有子 State 的完整内容。
    if any(list(path) == parent_scope_path for path in child_paths):
        _mark_parent_non_comparable(
            context,
            state,
            reason="content_owned_by_nested_states",
            excluded_scope_count=len(child_paths),
        )
        return True

    recapture = capture_interactive_state(
        context.page,
        group,
        click_result,
        local_scope,
        excluded_runtime_paths=[list(path) for path in child_paths],
        allow_empty_blocks=True,
    )
    if recapture.get("success") is not True:
        code, message = _error_details(
            recapture, "parent_content_ownership_failed"
        )
        _append_error(
            context.result["errors"],
            group,
            phase="content_ownership",
            code=code,
            message=message,
        )
        _mark_parent_non_comparable(
            context,
            state,
            reason="nested_content_exclusion_failed",
            excluded_scope_count=len(child_paths),
        )
        return False

    recaptured_state = recapture.get("interactive_state")
    if not isinstance(recaptured_state, dict):
        _mark_parent_non_comparable(
            context,
            state,
            reason="nested_content_exclusion_failed",
            excluded_scope_count=len(child_paths),
        )
        return False
    final_state = _compose_nested_state(recaptured_state, parent_state_path)
    if final_state.get("state_key") != state_key:
        _append_error(
            context.result["errors"],
            group,
            phase="content_ownership",
            code="parent_state_identity_changed",
            message="Parent semantic identity changed while excluding nested content.",
        )
        _mark_parent_non_comparable(
            context,
            state,
            reason="nested_content_exclusion_failed",
            excluded_scope_count=len(child_paths),
        )
        return False

    residual_blocks = final_state.get("blocks")
    if not isinstance(residual_blocks, list):
        residual_blocks = []
    state.clear()
    state.update(final_state)
    if not _has_meaningful_independent_blocks(residual_blocks):
        _mark_parent_non_comparable(
            context,
            state,
            reason="content_owned_by_nested_states",
            excluded_scope_count=len(child_paths),
            residual_blocks=residual_blocks,
        )
        return True

    state["comparison_eligible"] = True
    state["non_comparable_reason"] = None
    state["content_ownership"] = {
        "role": "parent_with_independent_content",
        "excluded_nested_scope_count": len(child_paths),
    }
    content_hash = state.get("content_hash")
    if isinstance(content_hash, str):
        context.state_hashes[state_key] = content_hash
    return True


def _traverse_children(
    context: _TraversalContext,
    parent_group: dict[str, Any],
    parent_state: dict[str, Any],
    local_scope: dict[str, Any],
    depth: int,
) -> bool:
    """只在父状态 Local Scope 内扩展下一层组；False 表示整页已中止。"""
    child_groups = _discover_child_groups(context, parent_group, local_scope)
    if not child_groups:
        return True

    parent_state_path = parent_state.get("state_path")
    if not isinstance(parent_state_path, list):
        return True
    if depth >= MAX_TRAVERSAL_DEPTH:
        for child_group in child_groups:
            _record_truncation(
                context,
                reason="depth_limit_reached",
                group=child_group,
                state_path=parent_state_path,
                details={"limit": MAX_TRAVERSAL_DEPTH, "next_depth": depth + 1},
            )
        return True
    if (
        context.result["non_default_states_captured"]
        >= MAX_NON_DEFAULT_STATES_PER_PAGE
    ):
        _record_state_limit(context, parent_group, parent_state_path)
        return True

    for child_group in child_groups:
        if (
            context.result["non_default_states_captured"]
            >= MAX_NON_DEFAULT_STATES_PER_PAGE
        ):
            _record_state_limit(context, child_group, parent_state_path)
            break
        child_group_path = _runtime_group_path(child_group)
        if child_group_path is not None:
            # 子 tablist 只负责状态导航。若仅排除其 panel，按钮文字可能被结构抽取器当成
            # 父级段落，因此也要从父级可比较克隆中排除该已确认的子控制组。
            context.nested_exclusion_paths_by_parent_state.setdefault(
                parent_state.get("state_key"), set()
            ).add(tuple(child_group_path))
        if not _traverse_group(
            context,
            child_group,
            depth=depth + 1,
            parent_state_path=parent_state_path,
            owner_parent_state_key=parent_state.get("state_key"),
        ):
            return False
    return True


def _capture_switched_state(
    context: _TraversalContext,
    group: dict[str, Any],
    baseline: dict[str, Any],
    click_result: dict[str, Any],
    parent_state_path: list[str],
    depth: int,
    *,
    target_tab: dict[str, Any] | None,
    is_non_default: bool,
    owner_parent_state_key: str | None,
) -> tuple[bool, bool]:
    """让一次已完成的点击继续通过 3C-1、3C-2，并按需扩展子组。

    返回 ``(state_ok, traversal_can_continue)``；第二项为 False 只表示嵌套恢复失败等原因
    已使整页中止，调用方必须立即开始向上恢复或结束。
    """
    local_scope = resolve_local_scope(context.page, group, baseline, click_result)
    if local_scope.get("success") is not True:
        code, message = _error_details(local_scope, "local_scope_resolution_failed")
        _append_error(
            context.result["errors"], group, phase="local_scope",
            code=code, message=message, target_tab=target_tab,
        )
        return False, True

    capture = capture_interactive_state(
        context.page, group, click_result, local_scope
    )
    if capture.get("success") is not True:
        code, message = _error_details(capture, "state_capture_failed")
        _append_error(
            context.result["errors"], group, phase="state_capture",
            code=code, message=message, target_tab=target_tab,
        )
        return False, True

    state, usable = _register_captured_state(
        context,
        group,
        capture.get("interactive_state"),
        parent_state_path,
        target_tab=target_tab,
        is_non_default=is_non_default,
    )
    if not usable or state is None:
        return False, True

    # 只有成功形成子 Interactive State 后，它的 Local Scope 才能成为父级内容排除证据。
    # 失败或被拒绝的子采集不会改变父 State 的内容归属。
    child_scope_path = _local_scope_path(local_scope)
    if owner_parent_state_key and child_scope_path is not None:
        context.nested_exclusion_paths_by_parent_state.setdefault(
            owner_parent_state_key, set()
        ).add(tuple(child_scope_path))

    state_key = state.get("state_key")
    if (
        isinstance(state_key, str)
        and context.result["skipped"]
        and context.result["skipped"][-1].get("state_key") == state_key
    ):
        # 已处理的相同状态不再递归，以免页面通过循环嵌套重复进入同一路径。
        return True, True

    if not _traverse_children(context, group, state, local_scope, depth):
        return True, False
    ownership_ok = _finalize_parent_content_ownership(
        context,
        group,
        state,
        click_result,
        local_scope,
        parent_state_path,
    )
    if not ownership_ok:
        return False, True
    return True, True


def _traverse_group(
    context: _TraversalContext,
    group: dict[str, Any],
    *,
    depth: int,
    parent_state_path: list[str],
    owner_parent_state_key: str | None = None,
) -> bool:
    """遍历一个组的有限选项，并保证退出前恢复该组的原始默认状态。"""
    group_context_key = (
        tuple(parent_state_path),
        tuple(group.get("scope_path", [])),
        tuple(_runtime_group_path(group) or []),
    )
    if group_context_key in context.visited_group_contexts:
        context.result["skipped"].append(
            {
                "reason": "visited_group_state_path",
                "group": _group_audit(group),
                "state_path": list(parent_state_path),
            }
        )
        return True
    context.visited_group_contexts.add(group_context_key)
    context.result["groups_discovered"] += 1

    default_tab_index = _default_tab_index(group)
    if default_tab_index is None:
        _append_error(
            context.result["errors"], group, phase="discovery",
            code="default_tab_unavailable",
            message="Safe Tab Group has no original selected Tab index.",
        )
        return True
    if group.get("stable_state_available") is not True:
        _append_error(
            context.result["errors"], group, phase="discovery",
            code="stable_semantic_state_unavailable",
            message="Safe Tab Group has no stable semantic state identity.",
        )
        return True

    target_tabs, skipped_tabs = _bounded_non_default_tabs(group, default_tab_index)
    group_failed = False
    if skipped_tabs:
        _record_truncation(
            context,
            reason="option_limit_reached",
            group=group,
            state_path=parent_state_path,
            details={
                "limit": MAX_ACTIONABLE_OPTIONS_PER_GROUP,
                "skipped_options": [tab.get("text") for tab in skipped_tabs],
            },
        )
        group_failed = True

    first_successful_click: dict[str, Any] | None = None
    for target_tab in target_tabs:
        if (
            context.result["non_default_states_captured"]
            >= MAX_NON_DEFAULT_STATES_PER_PAGE
        ):
            _record_state_limit(context, group, parent_state_path)
            group_failed = True
            break

        target_runtime = target_tab.get("runtime_locator")
        target_index = (
            target_runtime.get("tab_index")
            if isinstance(target_runtime, dict)
            else None
        )
        if not isinstance(target_index, int):
            _append_error(
                context.result["errors"], group, phase="discovery",
                code="target_tab_locator_unavailable",
                message="Actionable Tab has no runtime index.",
                target_tab=target_tab,
            )
            group_failed = True
            break

        target_text = target_tab.get("text")
        predicted_path = [
            *parent_state_path,
            *([target_text] if isinstance(target_text, str) else []),
        ]
        predicted_key = build_interactive_state_key(
            list(group.get("scope_path", [])), predicted_path
        )
        if predicted_key in context.visited_state_keys:
            context.result["skipped"].append(
                {
                    "reason": "visited_state",
                    "group": _group_audit(group),
                    "state_path": predicted_path,
                    "state_key": predicted_key,
                }
            )
            continue

        baseline = capture_local_scope_baseline(context.page, group)
        if baseline.get("success") is not True:
            code, message = _error_details(baseline, "local_scope_baseline_failed")
            _append_error(
                context.result["errors"], group, phase="local_scope_baseline",
                code=code, message=message, target_tab=target_tab,
            )
            group_failed = True
            break

        click_result = click_safe_tab(
            context.page, group, target_index, **context.click_options
        )
        if click_result.get("success") is not True:
            code, message = _error_details(click_result, "tab_click_failed")
            _append_error(
                context.result["errors"], group, phase="safe_click",
                code=code, message=message, target_tab=target_tab,
            )
            if code in UNRECOVERABLE_CLICK_ERROR_CODES:
                _abort_result(context.result)
                return False
            group_failed = True
            if first_successful_click is None and not _current_group_is_default(
                context.page, group, default_tab_index
            ):
                _append_error(
                    context.result["errors"], group, phase="restore",
                    code="default_state_not_confirmed",
                    message="Failed click left the original default state unconfirmed.",
                )
                _abort_result(context.result)
                return False
            break

        if first_successful_click is None:
            first_successful_click = click_result
        state_ok, can_continue = _capture_switched_state(
            context,
            group,
            baseline,
            click_result,
            parent_state_path,
            depth,
            target_tab=target_tab,
            is_non_default=True,
            owner_parent_state_key=owner_parent_state_key,
        )
        if not can_continue:
            return False
        if not state_ok:
            group_failed = True
            break

    default_state_captured = False
    if first_successful_click is not None:
        # 即使达到任何遍历上限，也必须先恢复当前组；子组会在各自返回前先完成恢复，
        # 因而递归退出顺序天然是“先子后父”。
        restore_baseline = capture_local_scope_baseline(context.page, group)
        restored = restore_default_tab(
            context.page, group, first_successful_click, **context.click_options
        )
        if restored.get("success") is not True:
            code, message = _error_details(restored, "default_restore_failed")
            _append_error(
                context.result["errors"], group, phase="restore",
                code=code, message=message,
            )
            _abort_result(context.result)
            return False

        if restore_baseline.get("success") is not True:
            code, message = _error_details(
                restore_baseline, "restore_local_scope_baseline_failed"
            )
            _append_error(
                context.result["errors"], group,
                phase="restore_local_scope_baseline", code=code, message=message,
            )
            group_failed = True
        else:
            state_ok, can_continue = _capture_switched_state(
                context,
                group,
                restore_baseline,
                restored,
                parent_state_path,
                depth,
                target_tab=None,
                is_non_default=False,
                owner_parent_state_key=owner_parent_state_key,
            )
            if not can_continue:
                return False
            default_state_captured = state_ok
            if not state_ok:
                group_failed = True

    if not group_failed and first_successful_click is not None and default_state_captured:
        context.result["groups_completed"] += 1
    return True


def traverse_top_level_interactive_states(
    page: Any,
    *,
    click_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """遍历当前页面顶层安全组，以及它们 Local Scope 内真实出现的一层子组。

    输入：完成基础渲染的 Playwright Page，以及可选的 3B 点击等待参数。
    处理：按初次 DOM 顺序进入顶层组；每个父状态采集成功后，只在对应 Local Scope 内
    发现子组。最大状态深度为 2，不组合页面上的独立组；每一层都复用 3B/3C 与 Restore。
    输出：保留 D-1 字段，并增加 bounds、truncations、skipped 审计信息。
    """
    initial_groups = discover_safe_tab_groups(page)
    result: dict[str, Any] = {
        "status": "complete",
        "interactive_states": [],
        "groups_discovered": 0,
        "groups_completed": 0,
        "non_default_states_captured": 0,
        "page_restored": True,
        "errors": [],
        "bounds": {
            "max_depth": MAX_TRAVERSAL_DEPTH,
            "max_actionable_options_per_group": MAX_ACTIONABLE_OPTIONS_PER_GROUP,
            "max_non_default_states_per_page": MAX_NON_DEFAULT_STATES_PER_PAGE,
        },
        "truncations": [],
        "skipped": [],
    }
    context = _TraversalContext(
        page=page,
        result=result,
        click_options=dict(click_options or {}),
    )

    for group in initial_groups:
        group_path = _runtime_group_path(group)
        # 初始 DOM 中可见的子组会在前方父组的 Local Scope 内被识别并处理；到达初始
        # 清单中的旧位置时跳过，避免把它再次当成独立顶层组并制造错误组合。
        if group_path is not None and tuple(group_path) in context.nested_group_paths:
            result["skipped"].append(
                {
                    "reason": "already_traversed_as_nested_group",
                    "group": _group_audit(group),
                    "state_path": [],
                }
            )
            continue
        if result["non_default_states_captured"] >= MAX_NON_DEFAULT_STATES_PER_PAGE:
            _record_state_limit(context, group, [])
            result["skipped"].append(
                {
                    "reason": "state_limit_reached",
                    "group": _group_audit(group),
                    "state_path": [],
                }
            )
            continue
        if not _traverse_group(context, group, depth=1, parent_state_path=[]):
            return result

    if result["errors"] or result["truncations"]:
        result["status"] = "partial"
    return result
