"""安全切换已确认的 Tab，并验证点击没有产生越界副作用。

本模块属于 Stage 1 V0.3-3B。它只负责一次 Tab 点击、点击后安全校验和恢复默认 Tab；
不会遍历全部状态、抽取 Local Scope、生成 Interactive State 内容或进入 Snapshot 链路。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from backend.interactive_state import discover_safe_tab_groups


TAB_CLICK_TIMEOUT_MS = 5_000
TAB_SELECTION_TIMEOUT_MS = 3_000
LOCAL_DOM_STABILITY_TIMEOUT_MS = 3_000
LOCAL_DOM_STABILITY_INTERVAL_MS = 100
LOCAL_DOM_STABLE_OBSERVATIONS = 3


TARGET_TAB_RUNTIME_PATH_SCRIPT = r"""
({ tablistPath, targetTabIndex }) => {
    const hasRole = (element, expectedRole) => {
        const roles = (element.getAttribute("role") || "")
            .split(/\s+/)
            .map((role) => role.toLowerCase())
            .filter(Boolean);
        return roles.includes(expectedRole);
    };
    const nearestOwningTablist = (tab) => {
        let current = tab.parentElement;
        while (current) {
            if (hasRole(current, "tablist")) {
                return current;
            }
            current = current.parentElement;
        }
        return null;
    };
    const buildRuntimeDomPath = (element) => {
        const parts = [];
        let current = element;
        while (current && current.nodeType === Node.ELEMENT_NODE) {
            const tag = current.tagName.toLowerCase();
            if (tag === "body") {
                parts.unshift("body");
                break;
            }
            const sameTagSiblings = current.parentElement
                ? Array.from(current.parentElement.children)
                    .filter((sibling) => sibling.tagName === current.tagName)
                : [current];
            parts.unshift(`${tag}:nth-of-type(${sameTagSiblings.indexOf(current) + 1})`);
            current = current.parentElement;
        }
        return parts;
    };

    const tablist = document.querySelector(tablistPath.join(" > "));
    if (!tablist || !hasRole(tablist, "tablist")) {
        return null;
    }
    const tabs = Array.from(tablist.querySelectorAll("[role]"))
        .filter((element) =>
            hasRole(element, "tab") && nearestOwningTablist(element) === tablist
        );
    const target = tabs[targetTabIndex];
    return target ? buildRuntimeDomPath(target) : null;
}
"""


LOCAL_DOM_CONTENT_SCRIPT = r"""
({ tablistPath }) => {
    const tablist = document.querySelector(tablistPath.join(" > "));
    if (!tablist) {
        return null;
    }

    // 当前阶段不定义 Local Scope。这里只使用 Tablist 的父元素作为有限、通用的运行时
    // 稳定性观察窗口，并补充 aria-controls 指向的面板 DOM；结果只计算指纹，不会持久化。
    const nearbyRoot = tablist.parentElement || tablist;
    const controlledPanels = Array.from(tablist.querySelectorAll('[role~="tab"]'))
        .map((tab) => (tab.getAttribute("aria-controls") || "").trim())
        .filter(Boolean)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((panel) => panel.outerHTML);
    return [nearbyRoot.outerHTML, ...controlledPanels].join("\n");
}
"""


SELECTION_SWITCHED_SCRIPT = r"""
({ originalTabPath, targetTabPath }) => {
    const originalTab = document.querySelector(originalTabPath.join(" > "));
    const targetTab = document.querySelector(targetTabPath.join(" > "));
    if (!originalTab || !targetTab) {
        return false;
    }
    return (originalTab.getAttribute("aria-selected") || "").trim().toLowerCase()
            === "false"
        && (targetTab.getAttribute("aria-selected") || "").trim().toLowerCase()
            === "true";
}
"""


def _runtime_tab_index(tab: dict[str, Any]) -> int | None:
    """读取 Tab 的运行时索引；该索引只用于当前 DOM 定位，不参与语义身份。"""
    runtime_locator = tab.get("runtime_locator")
    if not isinstance(runtime_locator, dict):
        return None
    tab_index = runtime_locator.get("tab_index")
    return tab_index if isinstance(tab_index, int) else None


def _find_current_safe_group(
    page: Any, expected_group: dict[str, Any]
) -> dict[str, Any] | None:
    """重新发现当前安全组，避免使用点击前已经失效的 DOM 状态。

    输入：浏览器 Page 和先前由 3A 返回的安全组。
    处理：重新执行 Safe Tab Group Discovery，并用运行时 DOM 路径匹配同一个组。
    输出：当前仍安全的组；无法确认时返回 None，调用方不得点击。
    """
    expected_runtime = expected_group.get("runtime_locator")
    if not isinstance(expected_runtime, dict):
        return None
    expected_path = expected_runtime.get("dom_path")
    if not isinstance(expected_path, list):
        return None

    for current_group in discover_safe_tab_groups(page):
        current_runtime = current_group.get("runtime_locator")
        if (
            isinstance(current_runtime, dict)
            and current_runtime.get("dom_path") == expected_path
        ):
            return current_group
    return None


def _find_tab(
    group: dict[str, Any], target_tab_index: int
) -> dict[str, Any] | None:
    """按运行时索引从安全组中找到目标 Tab。"""
    tabs = group.get("tabs")
    if not isinstance(tabs, list):
        return None
    for tab in tabs:
        if isinstance(tab, dict) and _runtime_tab_index(tab) == target_tab_index:
            return tab
    return None


def _selected_state(group: dict[str, Any]) -> list[dict[str, Any]]:
    """提取审计所需的 Tab 文字和 selected 状态，不包含持久化业务数据。"""
    states: list[dict[str, Any]] = []
    tabs = group.get("tabs")
    if not isinstance(tabs, list):
        return states
    for tab in tabs:
        if isinstance(tab, dict):
            states.append(
                {
                    "tab_index": _runtime_tab_index(tab),
                    "text": tab.get("text"),
                    "aria_selected": tab.get("aria_selected"),
                }
            )
    return states


def _dom_fingerprint(page: Any, tablist_path: list[str]) -> str | None:
    """计算目标组附近 DOM 的运行时 SHA-256 指纹，不保存原始 DOM。"""
    local_dom = page.evaluate(
        LOCAL_DOM_CONTENT_SCRIPT,
        {"tablistPath": tablist_path},
    )
    if not isinstance(local_dom, str):
        return None
    return hashlib.sha256(local_dom.encode("utf-8")).hexdigest()


def _wait_for_local_dom_stability(
    page: Any,
    tablist_path: list[str],
    *,
    timeout_ms: int,
    interval_ms: int,
    stable_observations: int,
) -> str | None:
    """有限等待点击附近 DOM 连续多次保持相同，不依赖 networkidle。

    输入：目标组运行时路径，以及总超时、采样间隔和连续稳定次数。
    处理：重复计算局部 DOM 指纹；连续达到阈值即认为短时间稳定。
    输出：稳定后的指纹；超时或目标 DOM 消失时返回 None。
    """
    deadline = time.monotonic() + timeout_ms / 1_000
    previous_fingerprint: str | None = None
    stable_count = 0

    while time.monotonic() <= deadline:
        fingerprint = _dom_fingerprint(page, tablist_path)
        if fingerprint is None:
            return None
        if fingerprint == previous_fingerprint:
            stable_count += 1
        else:
            previous_fingerprint = fingerprint
            stable_count = 1
        if stable_count >= stable_observations:
            return fingerprint
        page.wait_for_timeout(interval_ms)

    return None


def _failure(
    code: str,
    message: str,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成稳定失败结构，让调用方无需解析 Playwright traceback。"""
    return {
        "success": False,
        "error": {"code": code, "message": message},
        "before": before,
        "after": after,
        "runtime": runtime,
    }


def click_safe_tab(
    page: Any,
    tab_group: dict[str, Any],
    target_tab_index: int,
    *,
    click_timeout_ms: int = TAB_CLICK_TIMEOUT_MS,
    selection_timeout_ms: int = TAB_SELECTION_TIMEOUT_MS,
    stability_timeout_ms: int = LOCAL_DOM_STABILITY_TIMEOUT_MS,
    stability_interval_ms: int = LOCAL_DOM_STABILITY_INTERVAL_MS,
    stable_observations: int = LOCAL_DOM_STABLE_OBSERVATIONS,
) -> dict[str, Any]:
    """安全点击一个非当前 Tab，并验证没有导航、弹窗或下载。

    输入：Playwright Page、3A 已发现的安全组和目标 Tab 运行时索引。
    处理：重新确认组仍安全，记录 URL、页面数、selected 状态和局部 DOM 指纹；注册新页面
    与下载监听后只点击一次；等待 ARIA 正确切换和局部 DOM 短暂稳定，再逐项验证安全边界。
    输出：成功时返回点击前后审计信息；任一条件失败时返回稳定错误码并停止，不继续点击。
    """
    current_group = _find_current_safe_group(page, tab_group)
    if current_group is None:
        return _failure(
            "unsafe_tab_group",
            "Tab group is no longer present or no longer passes safe discovery.",
        )

    target_tab = _find_tab(current_group, target_tab_index)
    if target_tab is None:
        return _failure("target_tab_not_found", "Target Tab was not found in the group.")
    if not target_tab.get("visible") or target_tab.get("disabled"):
        return _failure(
            "target_tab_not_actionable",
            "Target Tab must be visible and enabled before clicking.",
        )
    if target_tab.get("aria_selected") is True:
        return _failure(
            "target_tab_already_selected",
            "Target Tab is already selected; no click was performed.",
        )

    selected_tabs = [
        tab
        for tab in current_group.get("tabs", [])
        if isinstance(tab, dict)
        and tab.get("visible")
        and not tab.get("disabled")
        and tab.get("aria_selected") is True
    ]
    if len(selected_tabs) != 1:
        return _failure(
            "selected_state_invalid",
            "Safe click requires exactly one currently selected actionable Tab.",
        )
    original_tab_index = _runtime_tab_index(selected_tabs[0])
    if original_tab_index is None:
        return _failure(
            "selected_state_invalid", "Selected Tab has no runtime locator."
        )

    runtime_locator = current_group.get("runtime_locator")
    tablist_path = runtime_locator.get("dom_path")
    if not isinstance(tablist_path, list):
        return _failure("tab_locator_unavailable", "Tab group runtime path is missing.")

    original_tab_path = page.evaluate(
        TARGET_TAB_RUNTIME_PATH_SCRIPT,
        {"tablistPath": tablist_path, "targetTabIndex": original_tab_index},
    )
    target_tab_path = page.evaluate(
        TARGET_TAB_RUNTIME_PATH_SCRIPT,
        {"tablistPath": tablist_path, "targetTabIndex": target_tab_index},
    )
    if not isinstance(original_tab_path, list) or not isinstance(target_tab_path, list):
        return _failure(
            "tab_locator_unavailable", "Could not resolve a unique runtime Tab path."
        )

    context = page.context
    before = {
        "url": page.url,
        "page_count": len(context.pages),
        "selected_state": _selected_state(current_group),
        "local_dom_fingerprint": _dom_fingerprint(page, tablist_path),
    }
    runtime = {
        "tablist_dom_path": list(tablist_path),
        "original_selected_tab_index": original_tab_index,
        "target_tab_index": target_tab_index,
    }
    if before["local_dom_fingerprint"] is None:
        return _failure(
            "local_dom_unavailable",
            "Target Tab group local DOM could not be fingerprinted.",
            before=before,
            runtime=runtime,
        )

    opened_pages: list[Any] = []
    downloads: list[Any] = []

    def record_opened_page(opened_page: Any) -> None:
        opened_pages.append(opened_page)

    def record_download(download: Any) -> None:
        downloads.append(download)

    context.on("page", record_opened_page)
    page.on("download", record_download)

    def safety_failure() -> tuple[str, str] | None:
        if page.url != before["url"]:
            return "url_changed", "Tab click changed the page URL."
        if opened_pages or len(context.pages) != before["page_count"]:
            return "new_page_opened", "Tab click opened a new page or window."
        if downloads:
            return "download_triggered", "Tab click triggered a download."
        return None

    try:
        target_locator = page.locator(" > ".join(target_tab_path))
        if target_locator.count() != 1:
            return _failure(
                "tab_locator_unavailable",
                "Target Tab runtime locator did not resolve exactly one element.",
                before=before,
                runtime=runtime,
            )

        # 不使用 force，Playwright 必须确认元素在真实页面上可操作；每次调用只点击一次。
        target_locator.click(timeout=click_timeout_ms)
        try:
            page.wait_for_function(
                SELECTION_SWITCHED_SCRIPT,
                arg={
                    "originalTabPath": original_tab_path,
                    "targetTabPath": target_tab_path,
                },
                timeout=selection_timeout_ms,
            )
        except PlaywrightTimeoutError:
            violation = safety_failure()
            if violation is not None:
                return _failure(
                    violation[0],
                    violation[1],
                    before=before,
                    runtime=runtime,
                )
            return _failure(
                "selection_switch_timeout",
                "aria-selected did not switch to the target Tab within the timeout.",
                before=before,
                runtime=runtime,
            )

        stable_fingerprint = _wait_for_local_dom_stability(
            page,
            tablist_path,
            timeout_ms=stability_timeout_ms,
            interval_ms=stability_interval_ms,
            stable_observations=stable_observations,
        )
        violation = safety_failure()
        if violation is not None:
            return _failure(
                violation[0],
                violation[1],
                before=before,
                runtime=runtime,
            )
        if stable_fingerprint is None:
            return _failure(
                "local_dom_stability_timeout",
                "Target Tab group local DOM did not reach short-term stability.",
                before=before,
                runtime=runtime,
            )

        after_group = _find_current_safe_group(page, current_group)
        if after_group is None:
            return _failure(
                "selected_state_invalid",
                "Tab group no longer has one valid selected state after clicking.",
                before=before,
                runtime=runtime,
            )
        selected_after = [
            tab
            for tab in after_group.get("tabs", [])
            if isinstance(tab, dict) and tab.get("aria_selected") is True
        ]
        if (
            len(selected_after) != 1
            or _runtime_tab_index(selected_after[0]) != target_tab_index
        ):
            return _failure(
                "selected_state_invalid",
                "Target Tab was not the unique selected Tab after clicking.",
                before=before,
                runtime=runtime,
            )

        after = {
            "url": page.url,
            "page_count": len(context.pages),
            "selected_state": _selected_state(after_group),
            "local_dom_fingerprint": stable_fingerprint,
        }
        return {
            "success": True,
            "error": None,
            "before": before,
            "after": after,
            "runtime": runtime,
        }
    except (PlaywrightError, OSError) as exc:
        violation = safety_failure()
        if violation is not None:
            return _failure(
                violation[0],
                violation[1],
                before=before,
                runtime=runtime,
            )
        return _failure(
            "tab_click_failed",
            f"Tab click failed: {exc}",
            before=before,
            runtime=runtime,
        )
    finally:
        try:
            context.remove_listener("page", record_opened_page)
            page.remove_listener("download", record_download)
        except PlaywrightError:
            # 页面若在违规导航中被销毁，监听器清理失败不应覆盖真正的安全失败原因。
            pass


def restore_default_tab(
    page: Any,
    tab_group: dict[str, Any],
    click_result: dict[str, Any],
    **click_options: Any,
) -> dict[str, Any]:
    """恢复一次成功点击前的默认 Tab，并复用全部点击安全校验。

    输入：原始安全组和 click_safe_tab 的成功结果。
    处理：读取点击前 selected Tab 的运行时索引，再通过同一安全点击函数切回并验证。
    输出：恢复操作结果；原点击失败或缺少运行时信息时返回明确错误且不点击。
    """
    if click_result.get("success") is not True:
        return _failure(
            "restore_source_invalid",
            "Only a successful Tab click can be restored.",
        )
    runtime = click_result.get("runtime")
    if not isinstance(runtime, dict):
        return _failure(
            "restore_source_invalid", "Click result has no runtime restore metadata."
        )
    original_tab_index = runtime.get("original_selected_tab_index")
    if not isinstance(original_tab_index, int):
        return _failure(
            "restore_source_invalid", "Original selected Tab index is missing."
        )
    return click_safe_tab(
        page,
        tab_group,
        original_tab_index,
        **click_options,
    )
