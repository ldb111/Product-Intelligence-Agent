"""V0.3-3D-2 Nested Traversal 的本地 Playwright 人工验收入口。

本脚本只打开可控 HTML fixture，并把页面直接交给正式 Traversal Orchestrator。Safe Tab
发现、点击、Local Scope、State Capture、嵌套关系、边界与恢复均由 backend 实现；实验
代码不决定点击顺序、不保存 Snapshot，也不自动判断验收是否通过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from backend.interactive_state import discover_safe_tab_groups
from backend.interactive_traversal import traverse_top_level_interactive_states


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "v033d2_nested_traversal.html"


def _configure_utf8_output() -> None:
    """让 Windows 终端稳定显示中文、英文状态和 JSON。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _print_json(label: str, value: Any) -> None:
    """使用 UTF-8、缩进 JSON 打印人工验收数据，不修改生产结果。"""
    print(f"\n===== {label} =====")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _selected_state_summary(page: Any) -> list[dict[str, Any]]:
    """重新调用正式 3A，展示遍历结束后各可见组的默认选中状态。

    该摘要只用于人工核对恢复结果，不负责重新点击或自行判断 PASS。
    """
    summaries: list[dict[str, Any]] = []
    for group in discover_safe_tab_groups(page):
        summaries.append(
            {
                "scope_path": group.get("scope_path"),
                "selected_tabs": [
                    tab.get("text")
                    for tab in group.get("tabs", [])
                    if isinstance(tab, dict) and tab.get("aria_selected") is True
                ],
            }
        )
    return summaries


def main() -> int:
    """显示本地页面，运行正式嵌套遍历并打印完整端到端验收信息。

    输入：固定的本地 ARIA fixture，不访问公网。
    处理：以可视 Chromium 打开页面并仅调用一次正式 Traversal Orchestrator；完成后展示
    原始结果、各状态路径及其深度、截断/跳过记录和最终默认选中状态。
    输出：信息打印到终端；关闭前等待一次 Enter，退出码不代表自动验收结论。
    """
    _configure_utf8_output()
    playwright = None
    browser = None
    context = None
    exit_code = 0

    try:
        if not FIXTURE_PATH.is_file():
            raise RuntimeError(f"找不到本地 fixture：{FIXTURE_PATH}")

        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(FIXTURE_PATH.resolve().as_uri(), wait_until="domcontentloaded")

        print(f"已打开本地 fixture：{FIXTURE_PATH.resolve()}")
        print("Traversal Orchestrator 正在自动执行嵌套状态遍历……")
        traversal_result = traverse_top_level_interactive_states(page)

        _print_json("Traversal Result", traversal_result)
        states = traversal_result.get("interactive_states")
        state_paths = [
            {
                "state_path": state.get("state_path"),
                "depth": (
                    len(state.get("state_path"))
                    if isinstance(state.get("state_path"), list)
                    else None
                ),
            }
            for state in states or []
            if isinstance(state, dict)
        ]
        _print_json("所有 state_path 与 depth", state_paths)
        _print_json("truncations", traversal_result.get("truncations"))
        _print_json("skipped", traversal_result.get("skipped"))
        _print_json("page_restored", traversal_result.get("page_restored"))
        _print_json("最终可见 Tab 默认状态", _selected_state_summary(page))
    except (PlaywrightError, RuntimeError, OSError) as exc:
        exit_code = 1
        print(f"\nD-2 本地人工验收流程中断：{exc}", file=sys.stderr)
    except KeyboardInterrupt:
        exit_code = 1
        print("\n用户中断了 D-2 本地人工验收。", file=sys.stderr)
    finally:
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
