"""Stage 1 V0.3-3A～3D-1 人工浏览器验收入口。

本脚本只负责编排阿里云真实页面的人工观察流程。Safe Tab Group Discovery、点击安全
校验、Local Scope Resolver 和 State Capture 全部调用 backend 中已经实现的生产能力；
这里不复制判断规则、不保存 Snapshot，也不会替用户自动判断项目是否通过验收。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from backend.interactive_state import discover_safe_tab_groups
from backend.interactive_traversal import traverse_top_level_interactive_states
from backend.local_scope import capture_local_scope_baseline, resolve_local_scope
from backend.state_capture import capture_interactive_state
from backend.structured_content import serialize_blocks
from backend.tab_interaction import click_safe_tab, restore_default_tab


TARGET_URL = "https://www.aliyun.com/benefit?utm_content=m_20000000458"
INITIAL_RENDER_WAIT_MS = 5_000
NAVIGATION_TIMEOUT_MS = 30_000


def _parse_args() -> argparse.Namespace:
    """解析人工验收模式，默认直接进入 3D-1 整页自动遍历。"""
    parser = argparse.ArgumentParser(
        description="Stage 1 V0.3-3 浏览器人工验收入口。"
    )
    parser.add_argument(
        "--mode",
        choices=("state-capture", "traversal"),
        default="traversal",
        help=(
            "traversal（默认）直接运行正式 3D-1 Traversal Orchestrator；"
            "state-capture 仅保留旧版逐步人工诊断入口。"
        ),
    )
    return parser.parse_args()


def _configure_utf8_output() -> None:
    """让 Windows 终端按 UTF-8 输出中文、人民币符号和 JSON。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _print_json(label: str, value: Any) -> None:
    """以便于人工阅读的 UTF-8 JSON 打印一次发现或交互结果。"""
    print(f"\n===== {label} =====")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _tab_index(group: dict[str, Any], text_prefix: str) -> int:
    """按当前页面显示文字找到实验目标 Tab 的运行时索引。

    该索引只用于把人工指定的目标交给生产点击函数，不作为 Interactive State 身份。
    页面缺少目标时抛出明确错误，避免误点其他控件。
    """
    for tab in group.get("tabs", []):
        text = tab.get("text")
        runtime_locator = tab.get("runtime_locator")
        if (
            isinstance(text, str)
            and text.startswith(text_prefix)
            and isinstance(runtime_locator, dict)
            and isinstance(runtime_locator.get("tab_index"), int)
        ):
            return runtime_locator["tab_index"]
    raise RuntimeError(f"未找到目标 Tab：{text_prefix}")


def _find_group(
    groups: list[dict[str, Any]],
    matcher: Callable[[dict[str, Any]], bool],
    description: str,
) -> dict[str, Any]:
    """从生产发现结果中选择本次人工验收指定的标签组。"""
    for group in groups:
        if matcher(group):
            return group
    raise RuntimeError(f"未发现安全 Tab Group：{description}")


def _selected_tabs(group: dict[str, Any]) -> list[str]:
    """从生产发现结果中提取当前选中的可见 Tab 文本，供终端展示。"""
    return [
        tab["text"]
        for tab in group.get("tabs", [])
        if tab.get("aria_selected") is True and isinstance(tab.get("text"), str)
    ]


def _wait_for_manual_confirmation(message: str) -> None:
    """打印当前步骤的验收提示，并等待用户确认后再继续下一步。"""
    print(f"\n人工验收提示：{message}")
    input("确认当前页面状态后，请按 Enter 继续：")


def _print_interaction_summary(
    label: str,
    result: dict[str, Any],
    page: Any,
) -> None:
    """打印生产点击结果中的 selected、URL、窗口、下载和指纹信息。

    本函数只负责展示，不重新判断点击是否安全。最终成功与失败完全使用生产函数返回的
    success/error；如果生产函数因失败没有 after 数据，则显示当前浏览器状态方便人工排查。
    """
    before = result.get("before") or {}
    after = result.get("after") or {}
    error = result.get("error") or {}
    before_selected = [
        tab.get("text")
        for tab in before.get("selected_state", [])
        if tab.get("aria_selected") is True
    ]
    after_selected = [
        tab.get("text")
        for tab in after.get("selected_state", [])
        if tab.get("aria_selected") is True
    ]
    before_fingerprint = before.get("local_dom_fingerprint")
    after_fingerprint = after.get("local_dom_fingerprint")

    _print_json(
        label,
        {
            "success": result.get("success"),
            "failure_reason": result.get("error"),
            "selected_before": before_selected,
            "selected_after": after_selected,
            "url_before": before.get("url"),
            "url_after": after.get("url", page.url),
            "url_changed": (
                before.get("url") != after.get("url")
                if after
                else error.get("code") == "url_changed"
            ),
            "page_count_before": before.get("page_count"),
            "page_count_after": after.get("page_count", len(page.context.pages)),
            "new_page_or_window": error.get("code") == "new_page_opened",
            "download_triggered": error.get("code") == "download_triggered",
            "local_dom_fingerprint_before": before_fingerprint,
            "local_dom_fingerprint_after": after_fingerprint,
            "local_dom_fingerprint_changed": (
                before_fingerprint != after_fingerprint if after else None
            ),
            "local_dom_stable": bool(
                result.get("success") and after_fingerprint
            ),
        },
    )


def _collect_card_text_marks(
    blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """收集所有 group/card 的原始 text_marks，供人工对照浏览器展示。

    输入：State Capture 已经生成的 Structured Blocks。
    处理：只遍历现有 group/card 结构，不解释 mark 的业务含义，也不改写生产结果。
    输出：按 Block 和 Card 顺序排列的标题与原始 text_marks；空列表也保留，便于确认
    每张卡是否确实没有文本标记。
    """
    collected: list[dict[str, Any]] = []

    def visit(current_blocks: list[dict[str, Any]]) -> None:
        for block_index, block in enumerate(current_blocks):
            if not isinstance(block, dict) or block.get("type") != "group":
                continue
            cards = block.get("cards")
            if not isinstance(cards, list):
                continue
            for card_index, card in enumerate(cards):
                if not isinstance(card, dict):
                    continue
                collected.append(
                    {
                        "block_index": block_index,
                        "card_index": card_index,
                        "title": card.get("title"),
                        "text_marks": card.get("text_marks", []),
                    }
                )
                nested_blocks = card.get("blocks")
                if isinstance(nested_blocks, list):
                    visit(nested_blocks)

    visit(blocks)
    return collected


def _print_state_capture(
    label: str,
    capture_result: dict[str, Any],
) -> None:
    """打印 State、可读 blocks，以及每张 card 的原始 text_marks。

    本函数只负责人工展示。Structured Blocks 的生成和文本序列化都调用生产模块，不在
    experiments 中复制解析规则；text_marks 不做格式转换或业务解释。失败时原样打印
    生产错误，不制造半成品 State。
    """
    state = capture_result.get("interactive_state")
    if not isinstance(state, dict):
        _print_json(
            f"{label}｜Interactive State Capture",
            {
                "success": capture_result.get("success"),
                "error": capture_result.get("error"),
                "diagnostics": capture_result.get("diagnostics"),
                "interactive_state": None,
            },
        )
        return

    blocks = state.get("blocks")
    readable_blocks = serialize_blocks(blocks) if isinstance(blocks, list) else ""
    card_text_marks = (
        _collect_card_text_marks(blocks) if isinstance(blocks, list) else []
    )
    _print_json(
        f"{label}｜Interactive State Capture",
        {
            "success": capture_result.get("success"),
            "error": capture_result.get("error"),
            "diagnostics": capture_result.get("diagnostics"),
            "state_key": state.get("state_key"),
            "scope_path": state.get("scope_path"),
            "state_path": state.get("state_path"),
            "is_default": state.get("is_default"),
            "content_hash": state.get("content_hash"),
            "captured_at": state.get("captured_at"),
            "card_text_marks": card_text_marks,
        },
    )
    print(f"\n----- {label}｜blocks 可读文本 -----")
    print(readable_blocks or "（没有可读 Block 内容）")


def _print_traversal_result(result: dict[str, Any]) -> None:
    """完整展示 Traversal Result 摘要和按采集顺序排列的状态内容。

    输入：正式 Traversal Orchestrator 返回的原始结果。
    处理：摘要字段和 errors 原样输出；每个 Interactive State 继续使用生产
    ``serialize_blocks`` 生成可读文本，并额外列出 card 的原始 text_marks。
    输出：只打印到终端，不修改结果、不保存 Snapshot，也不判断是否通过验收。
    """
    _print_json(
        "V0.3-3D-1 Traversal Result",
        {
            "status": result.get("status"),
            "groups_discovered": result.get("groups_discovered"),
            "groups_completed": result.get("groups_completed"),
            "non_default_states_captured": result.get(
                "non_default_states_captured"
            ),
            "page_restored": result.get("page_restored"),
            "errors": result.get("errors"),
        },
    )

    states = result.get("interactive_states")
    if not isinstance(states, list):
        states = []
    print(f"\n共采集 Interactive State：{len(states)} 个")
    for state_index, state in enumerate(states, start=1):
        if not isinstance(state, dict):
            _print_json(f"Interactive State #{state_index}", state)
            continue
        blocks = state.get("blocks")
        readable_blocks = (
            serialize_blocks(blocks) if isinstance(blocks, list) else ""
        )
        text_marks = (
            _collect_card_text_marks(blocks) if isinstance(blocks, list) else []
        )
        _print_json(
            f"Interactive State #{state_index}",
            {
                "state_key": state.get("state_key"),
                "scope_path": state.get("scope_path"),
                "state_path": state.get("state_path"),
                "is_default": state.get("is_default"),
                "content_hash": state.get("content_hash"),
                "captured_at": state.get("captured_at"),
                "text_marks": text_marks,
            },
        )
        print(f"\n----- Interactive State #{state_index}｜blocks 可读文本 -----")
        print(readable_blocks or "（没有可读 Block 内容）")


def _run_traversal_acceptance() -> int:
    """显示真实浏览器并把整页交给正式 Traversal Orchestrator 自动遍历。

    本模式不在实验脚本中选择或点击具体 Tab。所有发现顺序、点击、局部范围解析、状态
    采集和默认态恢复均由 backend 的 3D-1 编排器决定；脚本只负责打开目标页和展示结果。
    """
    _configure_utf8_output()
    playwright = None
    browser = None
    context = None
    exit_code = 0

    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        response = page.goto(
            TARGET_URL,
            wait_until="domcontentloaded",
            timeout=NAVIGATION_TIMEOUT_MS,
        )
        page.wait_for_timeout(INITIAL_RENDER_WAIT_MS)
        print(f"已打开：{page.url}")
        print(f"HTTP 状态码：{response.status if response else '无响应'}")
        print("\n正式 Traversal Orchestrator 正在自动遍历顶层安全 Tab Group……")

        traversal_result = traverse_top_level_interactive_states(page)
        _print_traversal_result(traversal_result)
    except (PlaywrightError, RuntimeError) as exc:
        exit_code = 1
        print(f"\nTraversal 人工验收流程中断：{exc}", file=sys.stderr)
    except KeyboardInterrupt:
        exit_code = 1
        print("\n用户中断了 Traversal 人工验收。", file=sys.stderr)
    finally:
        # 无论完成或中断，都让用户先核对浏览器最终状态和终端结果，再关闭窗口。
        if browser is not None:
            try:
                input("\n人工观察完成后，请按 Enter 关闭浏览器并退出：")
            except EOFError:
                pass
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()

    return exit_code


def _resolve_and_capture(
    page: Any,
    group: dict[str, Any],
    baseline: dict[str, Any],
    interaction_result: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """调用生产 Resolver 与 State Capture，并完整打印当前状态数据。"""
    local_scope_result = resolve_local_scope(
        page, group, baseline, interaction_result
    )
    _print_json(f"{label}｜Local Scope Resolver", local_scope_result)
    capture_result = capture_interactive_state(
        page, group, interaction_result, local_scope_result
    )
    _print_state_capture(label, capture_result)
    return {
        "local_scope_result": local_scope_result,
        "capture_result": capture_result,
    }


def _click_capture_and_observe(
    page: Any,
    group: dict[str, Any],
    target_text_prefix: str,
    label: str,
) -> dict[str, Any]:
    """依次调用生产基线、点击、Local Scope 和 State Capture，并等待人工确认。"""
    current_groups = discover_safe_tab_groups(page)
    _print_json(
        f"{label}｜点击前重新发现 Safe Tab Groups",
        current_groups,
    )
    baseline = capture_local_scope_baseline(page, group)
    _print_json(f"{label}｜Local Scope 点击前基线", baseline)
    target_index = _tab_index(group, target_text_prefix)
    interaction_result = click_safe_tab(page, group, target_index)
    _print_interaction_summary(label, interaction_result, page)
    state_step = _resolve_and_capture(
        page, group, baseline, interaction_result, label
    )
    _wait_for_manual_confirmation(
        f"请对照“{label}”的浏览器页面、Local Scope 与 Interactive State 数据。"
    )
    return {
        "interaction_result": interaction_result,
        **state_step,
    }


def _restore_capture_and_observe(
    page: Any,
    group: dict[str, Any],
    source_click_result: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """恢复默认 Tab 后解析范围并采集默认 Interactive State。"""
    baseline = capture_local_scope_baseline(page, group)
    _print_json(f"{label}｜Local Scope 恢复前基线", baseline)
    interaction_result = restore_default_tab(page, group, source_click_result)
    _print_interaction_summary(label, interaction_result, page)
    state_step = _resolve_and_capture(
        page, group, baseline, interaction_result, label
    )
    _wait_for_manual_confirmation(
        f"请对照“{label}”的默认页面与默认 Interactive State。"
    )
    return {
        "interaction_result": interaction_result,
        **state_step,
    }


def _repeat_current_state_capture(
    page: Any,
    group: dict[str, Any],
    restored_step: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """不再次点击，复用已验证的默认状态结果重复采集并打印哈希。"""
    capture_result = capture_interactive_state(
        page,
        group,
        restored_step["interaction_result"],
        restored_step["local_scope_result"],
    )
    _print_state_capture(label, capture_result)
    previous_state = restored_step["capture_result"].get("interactive_state") or {}
    current_state = capture_result.get("interactive_state") or {}
    _print_json(
        f"{label}｜相同默认状态 Hash 对照",
        {
            "previous_content_hash": previous_state.get("content_hash"),
            "repeated_content_hash": current_state.get("content_hash"),
            "hashes_equal": (
                previous_state.get("content_hash")
                == current_state.get("content_hash")
            ),
        },
    )
    _wait_for_manual_confirmation(
        f"请检查“{label}”两次采集的业务 Blocks 与 content_hash 是否一致。"
    )
    return capture_result


def main() -> int:
    """打开真实页面，逐状态演示安全交互、局部解析与 State Capture。

    输入：无命令行参数，目标 URL 和人工等待时间由本实验脚本固定。
    处理：显示 Chromium，发现安全组；每次状态切换都运行 3B、3C-1、3C-2，并在恢复
    月付和包月后各重复采集一次相同状态。每一步打印完成后等待用户按 Enter。
    输出：发现、交互、Local Scope、Interactive State 和 Hash 对照打印到终端；退出码
    只说明脚本是否执行完，不自动代表项目验收结论。
    """
    args = _parse_args()
    if args.mode == "traversal":
        return _run_traversal_acceptance()

    _configure_utf8_output()
    playwright = None
    browser = None
    context = None
    exit_code = 0

    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        response = page.goto(
            TARGET_URL,
            wait_until="domcontentloaded",
            timeout=NAVIGATION_TIMEOUT_MS,
        )
        page.wait_for_timeout(INITIAL_RENDER_WAIT_MS)
        print(f"已打开：{page.url}")
        print(f"HTTP 状态码：{response.status if response else '无响应'}")

        groups = discover_safe_tab_groups(page)
        _print_json("初次 Safe Tab Group Discovery", groups)

        token_group = _find_group(
            groups,
            lambda group: group.get("scope_path") == ["个人版付款周期"],
            "Token Plan 个人版付款周期",
        )
        all_model_group = _find_group(
            groups,
            lambda group: any(
                "全模型通享" in segment
                for segment in group.get("scope_path", [])
            ),
            "全模型通享",
        )
        print(f"\nToken Plan 初始选中：{_selected_tabs(token_group)}")
        print(f"全模型通享初始选中：{_selected_tabs(all_model_group)}")
        _wait_for_manual_confirmation(
            "请先检查默认状态：Token Plan 应为月付，全模型通享应为包月。"
        )

        quarter_step = _click_capture_and_observe(
            page, token_group, "季付", "Token Plan：月付 → 季付"
        )
        if not (
            quarter_step["interaction_result"].get("success")
            and quarter_step["local_scope_result"].get("success")
            and quarter_step["capture_result"].get("success")
        ):
            raise RuntimeError("季付 State Capture 失败，已停止后续点击。")

        year_step = _click_capture_and_observe(
            page, token_group, "年付", "Token Plan：季付 → 年付"
        )
        if not (
            year_step["interaction_result"].get("success")
            and year_step["local_scope_result"].get("success")
            and year_step["capture_result"].get("success")
        ):
            raise RuntimeError("年付 State Capture 失败，已停止后续点击。")

        _print_json(
            "Token Plan：恢复前重新发现 Safe Tab Groups",
            discover_safe_tab_groups(page),
        )
        token_restore_step = _restore_capture_and_observe(
            page,
            token_group,
            quarter_step["interaction_result"],
            "Token Plan：年付 → 恢复月付",
        )
        if not (
            token_restore_step["interaction_result"].get("success")
            and token_restore_step["local_scope_result"].get("success")
            and token_restore_step["capture_result"].get("success")
        ):
            raise RuntimeError("月付恢复或 State Capture 失败，已停止后续点击。")
        repeated_monthly = _repeat_current_state_capture(
            page,
            token_group,
            token_restore_step,
            "Token Plan：恢复月付后重复采集",
        )
        if not repeated_monthly.get("success"):
            raise RuntimeError("月付重复 State Capture 失败，已停止后续点击。")

        package_quarter_step = _click_capture_and_observe(
            page, all_model_group, "包季", "全模型通享：包月 → 包季"
        )
        if not (
            package_quarter_step["interaction_result"].get("success")
            and package_quarter_step["local_scope_result"].get("success")
            and package_quarter_step["capture_result"].get("success")
        ):
            raise RuntimeError("包季 State Capture 失败，已停止后续点击。")

        _print_json(
            "全模型通享：恢复前重新发现 Safe Tab Groups",
            discover_safe_tab_groups(page),
        )
        package_restore_step = _restore_capture_and_observe(
            page,
            all_model_group,
            package_quarter_step["interaction_result"],
            "全模型通享：包季 → 恢复包月",
        )
        if not (
            package_restore_step["interaction_result"].get("success")
            and package_restore_step["local_scope_result"].get("success")
            and package_restore_step["capture_result"].get("success")
        ):
            raise RuntimeError("包月恢复或 State Capture 失败，已停止后续点击。")
        repeated_package_monthly = _repeat_current_state_capture(
            page,
            all_model_group,
            package_restore_step,
            "全模型通享：恢复包月后重复采集",
        )
        if not repeated_package_monthly.get("success"):
            raise RuntimeError("包月重复 State Capture 失败，已停止后续点击。")

        final_groups = discover_safe_tab_groups(page)
        _print_json("恢复后 Safe Tab Group Discovery", final_groups)
    except (PlaywrightError, RuntimeError) as exc:
        exit_code = 1
        print(f"\n人工验收流程中断：{exc}", file=sys.stderr)
    except KeyboardInterrupt:
        exit_code = 1
        print("\n用户中断了人工验收。", file=sys.stderr)
    finally:
        # 成功或中途失败都先保留窗口，避免异常日志出现后浏览器立即消失、无法人工检查。
        if browser is not None:
            try:
                input("\n人工观察完成后，请按 Enter 关闭浏览器并退出：")
            except EOFError:
                pass
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
