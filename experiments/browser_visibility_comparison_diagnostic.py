"""对照永久隐藏菜单与滚动显现正文的浏览器可见性证据。

本脚本仅用于人工诊断：使用项目现有 Playwright Chromium 等待参数打开阿里云与 CaSee，
读取目标元素及其祖先链的计算样式、布局矩形和 checkVisibility()。CaSee 会额外执行一次
不带点击的程序化滚动并等待动画稳定，用于判断 opacity=0 是否只是滚动显现的初始状态。
脚本不调用生产过滤脚本、不保存 Snapshot，也不修改 backend 逻辑。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.browser_reader import (  # noqa: E402
    BROWSER_LAUNCH_TIMEOUT_MS,
    BROWSER_NAVIGATION_TIMEOUT_MS,
    BROWSER_RENDER_WAIT_MS,
)
from playwright.sync_api import sync_playwright  # noqa: E402


ALIYUN_URL = "https://www.aliyun.com/benefit?utm_content=m_20000000458"
CASee_URL = "https://casee.me/"

ALIYUN_HIDDEN_MENU_TARGETS = ("成为销售伙伴", "开发者社区")
CASEE_REVEAL_TARGETS = ("我们擅长", "竞争态势实时感知")
CASEE_CONTROL_TARGETS = ("凯见动态", "上兵伐谋觉察先机 · 决胜千里")

MAX_MATCHES_PER_TARGET = 4
MAX_ANCESTOR_DEPTH = 20
REVEAL_STABILIZATION_WAIT_MS = 2_000

INSPECT_TARGETS_SCRIPT = """
({ targets, maxMatches, maxDepth }) => {
    const normalizeText = (value) => (value || "").replace(/\\s+/g, " ").trim();
    const numberOrNull = (value) => {
        const parsed = Number.parseFloat(value);
        return Number.isFinite(parsed) ? parsed : null;
    };

    const describe = (element, depth) => {
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        const rects = element.getClientRects();
        const overflowClips = [style.overflow, style.overflowX, style.overflowY]
            .some((value) => value === "hidden" || value === "clip");
        const zeroHeight = rect.height === 0 || numberOrNull(style.height) === 0;
        let checkVisibility = null;
        let checkVisibilitySupported = false;
        let checkVisibilityError = null;

        if (typeof element.checkVisibility === "function") {
            checkVisibilitySupported = true;
            try {
                checkVisibility = element.checkVisibility({
                    checkOpacity: true,
                    checkVisibilityCSS: true,
                });
            } catch (error) {
                checkVisibilityError = String(error);
            }
        }

        return {
            depth,
            tag: element.tagName.toLowerCase(),
            role: element.getAttribute("role"),
            aria_hidden: element.getAttribute("aria-hidden"),
            hidden: Boolean(element.hidden || element.hasAttribute("hidden")),
            opacity: style.opacity,
            display: style.display,
            visibility: style.visibility,
            position: style.position,
            width: style.width,
            height: style.height,
            overflow: style.overflow,
            overflow_x: style.overflowX,
            overflow_y: style.overflowY,
            transform: style.transform,
            clip: style.clip,
            clip_path: style.clipPath,
            has_client_rect: rects.length > 0,
            client_rect_count: rects.length,
            client_rect: {
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height,
                top: rect.top,
                right: rect.right,
                bottom: rect.bottom,
                left: rect.left,
            },
            check_visibility_supported: checkVisibilitySupported,
            check_visibility: checkVisibility,
            check_visibility_error: checkVisibilityError,
            // absolute/fixed 脱离普通文档流；static/relative/sticky 仍保留正常布局位置。
            in_normal_document_flow: (
                style.position === "static"
                || style.position === "relative"
                || style.position === "sticky"
            ) && style.cssFloat === "none",
            is_zero_height_overflow_clip: zeroHeight && overflowClips,
            text_preview: normalizeText(element.innerText || element.textContent).slice(0, 180),
        };
    };

    const matchesFor = (target) => {
        const containing = Array.from(document.querySelectorAll("body *"))
            .filter((element) => normalizeText(element.textContent).includes(target));
        const leaves = containing.filter((element) =>
            !Array.from(element.children).some((child) =>
                normalizeText(child.textContent).includes(target)
            )
        );
        const candidates = leaves.length > 0 ? leaves : containing;

        return candidates.slice(0, maxMatches).map((element, matchIndex) => {
            const ancestors = [];
            let current = element;
            let depth = 0;
            while (current && depth < maxDepth) {
                ancestors.push(describe(current, depth));
                if (current.tagName.toLowerCase() === "html") {
                    break;
                }
                current = current.parentElement;
                depth += 1;
            }
            return {
                match_index: matchIndex,
                has_zero_height_overflow_hidden_ancestor: ancestors
                    .slice(1)
                    .some((ancestor) => ancestor.is_zero_height_overflow_clip),
                ancestors,
            };
        });
    };

    return Object.fromEntries(targets.map((target) => [target, matchesFor(target)]));
}
"""

SCROLL_TARGET_INTO_VIEW_SCRIPT = """
(target) => {
    const normalizeText = (value) => (value || "").replace(/\\s+/g, " ").trim();
    const containing = Array.from(document.querySelectorAll("body *"))
        .filter((element) => normalizeText(element.textContent).includes(target));
    const leaves = containing.filter((element) =>
        !Array.from(element.children).some((child) =>
            normalizeText(child.textContent).includes(target)
        )
    );
    const element = (leaves.length > 0 ? leaves : containing)[0];
    if (!element) {
        return false;
    }
    element.scrollIntoView({ behavior: "instant", block: "center", inline: "nearest" });
    return true;
}
"""


def inspect_targets(page: Any, targets: tuple[str, ...]) -> dict[str, Any]:
    """读取目标与祖先链，输出可判断布局、裁剪和可见性的完整证据。"""
    return page.evaluate(
        INSPECT_TARGETS_SCRIPT,
        {
            "targets": list(targets),
            "maxMatches": MAX_MATCHES_PER_TARGET,
            "maxDepth": MAX_ANCESTOR_DEPTH,
        },
    )


def open_default_page(browser: Any, url: str) -> tuple[Any, int]:
    """使用生产浏览器相同的默认视口、DOMContentLoaded 和有限等待策略打开页面。"""
    page = browser.new_page()
    response = page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=BROWSER_NAVIGATION_TIMEOUT_MS,
    )
    page.wait_for_timeout(BROWSER_RENDER_WAIT_MS)
    if response is None:
        page.close()
        raise RuntimeError(f"Browser returned no main response for {url}.")
    return page, response.status


def run_diagnostic() -> dict[str, Any]:
    """完成阿里云默认状态与 CaSee 滚动前后的只读可见性对照。"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            timeout=BROWSER_LAUNCH_TIMEOUT_MS,
        )
        try:
            aliyun_page, aliyun_status = open_default_page(browser, ALIYUN_URL)
            try:
                aliyun_result = {
                    "status_code": aliyun_status,
                    "final_url": aliyun_page.url,
                    "interaction_performed": False,
                    "targets": inspect_targets(
                        aliyun_page, ALIYUN_HIDDEN_MENU_TARGETS
                    ),
                }
            finally:
                aliyun_page.close()

            casee_page, casee_status = open_default_page(browser, CASee_URL)
            try:
                casee_targets = CASEE_REVEAL_TARGETS + CASEE_CONTROL_TARGETS
                before_scroll = inspect_targets(casee_page, casee_targets)

                # 两个疑似滚动显现目标可能位于相邻但不同的动画容器，因此分别滚动并在每次
                # 等待后记录全组状态。操作只改变滚动位置，不点击或切换任何页面状态。
                after_scroll: dict[str, Any] = {}
                for target in CASEE_REVEAL_TARGETS:
                    found = casee_page.evaluate(SCROLL_TARGET_INTO_VIEW_SCRIPT, target)
                    casee_page.wait_for_timeout(REVEAL_STABILIZATION_WAIT_MS)
                    after_scroll[target] = {
                        "scroll_target_found": bool(found),
                        "scroll_y": casee_page.evaluate("window.scrollY"),
                        "targets": inspect_targets(casee_page, casee_targets),
                    }

                casee_result = {
                    "status_code": casee_status,
                    "final_url": casee_page.url,
                    "click_performed": False,
                    "before_scroll": before_scroll,
                    "after_scroll": after_scroll,
                }
            finally:
                casee_page.close()

            return {
                "aliyun_hidden_menu": aliyun_result,
                "casee_visibility_transition": casee_result,
            }
        finally:
            browser.close()


def main() -> int:
    """执行诊断并输出中文可读的 UTF-8 JSON。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    try:
        result = run_diagnostic()
    except Exception as exc:  # 实验脚本需要把浏览器失败转换为简短 JSON，便于人工排查。
        print(
            json.dumps(
                {"error": {"type": type(exc).__name__, "message": str(exc)}},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
