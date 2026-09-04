"""使用 Playwright Chromium 获取 JavaScript 渲染后的最终页面 HTML。

本模块只负责浏览器采集：打开 URL、等待有限时间、读取最终 URL、标题和当前 DOM HTML。
Structured Blocks、Content Quality Gate、Snapshot 和 Change Detection 仍由现有模块负责，
避免浏览器路径复制另一套解析或业务规则。
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from backend.interactive_state import discover_safe_tab_groups
from backend.structured_content import COMPUTED_STYLE_TEXT_MARK_ATTRIBUTE


# 所有等待都有明确上限。DOMContentLoaded 只等待基础 DOM 就绪，随后固定等待一小段
# 时间让常见前端框架完成首屏渲染；不使用可能被长连接和统计请求持续阻塞的 networkidle。
BROWSER_LAUNCH_TIMEOUT_MS = 30_000
BROWSER_NAVIGATION_TIMEOUT_MS = 30_000
BROWSER_RENDER_WAIT_MS = 5_000

# 页面采集与 Interactive State 的局部采集必须使用同一套 Rendered Visibility（渲染
# 可见性）语义。把判断集中在一个脚本片段中，可以避免全页与局部范围逐渐出现两套规则。
_RENDERED_VISIBILITY_HELPERS_SCRIPT = """
    const isRenderedHidden = (element) => {
        const style = window.getComputedStyle(element);
        const ariaHidden = (element.getAttribute("aria-hidden") || "")
            .trim()
            .toLowerCase() === "true";
        const hiddenByStyle = style.display === "none"
            || style.visibility === "hidden"
            || style.visibility === "collapse";
        // opacity=0 也可能只是正文滚动显现动画的初始状态，不能单独作为删除依据。
        // 永久关闭的菜单面板稳定表现为透明且脱离普通文档流；static/relative 的透明
        // 内容仍可能是等待滚动显现的真实正文，因此必须保留。
        const hiddenTransparentOverlay = Number.parseFloat(style.opacity) === 0
            && (style.position === "absolute" || style.position === "fixed");

        return element.hidden
            || element.hasAttribute("hidden")
            || ariaHidden
            || hiddenByStyle
            || hiddenTransparentOverlay;
    };

    const enrichRenderedClone = (sourceElements, clonedElements) => {
        const hiddenIndexes = [];
        let computedTextMarkCount = 0;

        sourceElements.forEach((element, index) => {
            const clonedElement = clonedElements[index];
            if (!clonedElement) {
                return;
            }
            if (isRenderedHidden(element)) {
                hiddenIndexes.push(index);
                return;
            }

            // 这里只保留浏览器已经计算出的中性展示事实，不读取 class，也不推断价格、
            // 折扣等业务语义。空元素没有可保存文本，因此不增加内部标记。
            const style = window.getComputedStyle(element);
            const textDecorationLines = (style.textDecorationLine || "")
                .split(/\\s+/)
                .filter(Boolean);
            if (
                textDecorationLines.includes("line-through")
                && (element.textContent || "").trim()
            ) {
                clonedElement.setAttribute(
                    "__COMPUTED_TEXT_MARK_ATTRIBUTE__",
                    "strikethrough"
                );
                computedTextMarkCount += 1;
            }
        });

        // 倒序删除可保持 source 与 clone 的初始索引对应关系。所有修改只发生在 clone，
        // 不会影响真实页面后续 Tab 操作或浏览器肉眼显示。
        hiddenIndexes.reverse().forEach((index) => {
            const clonedElement = clonedElements[index];
            if (clonedElement) {
                clonedElement.remove();
            }
        });

        return {
            hiddenElementCount: hiddenIndexes.length,
            computedTextMarkCount,
        };
    };
"""
_RENDERED_VISIBILITY_HELPERS_SCRIPT = (
    _RENDERED_VISIBILITY_HELPERS_SCRIPT.replace(
        "__COMPUTED_TEXT_MARK_ATTRIBUTE__",
        COMPUTED_STYLE_TEXT_MARK_ATTRIBUTE,
    )
)

# 该脚本在浏览器已经完成默认状态渲染后执行，只删除 DOM 明确声明或计算样式确认隐藏的
# 元素。它不读取元素坐标、尺寸或是否进入当前视口，因此页面下方尚未滚动到的正文会保留。
VISIBLE_DOM_EXTRACTION_SCRIPT = """
() => {
""" + _RENDERED_VISIBILITY_HELPERS_SCRIPT + """
    if (!document.documentElement || !document.body) {
        return { error: "browser_dom_unavailable" };
    }

    const documentClone = document.documentElement.cloneNode(true);
    const clonedBody = documentClone.querySelector("body");
    const sourceElements = [document.body, ...document.body.querySelectorAll("*")];
    const clonedElements = [clonedBody, ...clonedBody.querySelectorAll("*")];
    const enrichment = enrichRenderedClone(sourceElements, clonedElements);

    return {
        error: null,
        html: documentClone.outerHTML,
        hidden_element_count: enrichment.hiddenElementCount,
        computed_text_mark_count: enrichment.computedTextMarkCount,
    };
}
"""

# State Capture 只需要 Local Scope，而不是全页 HTML。这里在内存中克隆已经解析出的局部
# 子树，再从克隆中删除隐藏节点；真实页面 DOM 不会被改写，因此不会影响后续安全交互。
VISIBLE_SCOPE_EXTRACTION_SCRIPT = """
({ scopeDomPath }) => {
""" + _RENDERED_VISIBILITY_HELPERS_SCRIPT + """
    if (!Array.isArray(scopeDomPath) || scopeDomPath.length === 0) {
        return { error: "local_scope_runtime_locator_invalid" };
    }

    const scope = document.querySelector(scopeDomPath.join(" > "));
    if (!scope) {
        return { error: "local_scope_not_found" };
    }
    if (scope === document.body || scope === document.documentElement) {
        return { error: "local_scope_document_root" };
    }
    if (isRenderedHidden(scope)) {
        return { error: "local_scope_hidden" };
    }

    const clone = scope.cloneNode(true);
    const sourceElements = [scope, ...scope.querySelectorAll("*")];
    const clonedElements = [clone, ...clone.querySelectorAll("*")];
    const enrichment = enrichRenderedClone(sourceElements, clonedElements);

    return {
        error: null,
        html: clone.outerHTML,
        hidden_element_count: enrichment.hiddenElementCount,
        computed_text_mark_count: enrichment.computedTextMarkCount,
    };
}
"""


class BrowserReadError(Exception):
    """表示浏览器启动、导航、超时或响应状态异常等可预期采集失败。

    输入：稳定错误类型和面向用户的错误说明。
    处理：保存错误信息，不让 Playwright traceback 成为正常业务输出。
    输出：由上层 CLI 捕获并转换成结构化 acquisition/browser 错误 JSON。
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        # 当浏览器由静态请求失败触发时，上层采集编排会填入该审计信息。浏览器读取模块
        # 本身仍只负责 Chromium，不需要知道 requests 为什么失败。
        self.static_acquisition_error: dict[str, str] | None = None


def extract_visible_scope_html(
    page: Any, runtime_dom_path: list[str]
) -> dict[str, Any]:
    """读取 Local Scope 当前可见 DOM，同时保持真实页面不变。

    在业务链路中的职责：为 State Capture 提供与浏览器全页采集一致的可见性过滤结果，
    但只返回 Local Scope，不把页面其他区域交给 Structured Blocks。

    输入：当前 Playwright Page，以及 3C-1 Resolver 返回的运行时 DOM 分段路径。
    处理：在浏览器内定位范围、克隆子树，并复用全页采集的隐藏节点规则清理克隆。
    输出：UTF-8 HTML bytes 和隐藏节点计数；定位无效或浏览器执行失败时抛出稳定的
    BrowserReadError。DOM 路径只用于本次定位，不进入返回 HTML 或内容身份。
    """
    if not isinstance(runtime_dom_path, list) or not runtime_dom_path or not all(
        isinstance(part, str) and part for part in runtime_dom_path
    ):
        raise BrowserReadError(
            "local_scope_runtime_locator_invalid",
            "Local Scope runtime DOM path must be a non-empty string list.",
        )

    try:
        result = page.evaluate(
            VISIBLE_SCOPE_EXTRACTION_SCRIPT,
            {"scopeDomPath": list(runtime_dom_path)},
        )
    except (PlaywrightError, OSError) as exc:
        raise BrowserReadError(
            "browser_scope_dom_error",
            f"Browser could not extract the visible Local Scope DOM: {exc}",
        ) from exc

    if not isinstance(result, dict):
        raise BrowserReadError(
            "browser_scope_dom_error",
            "Browser returned an invalid Local Scope DOM result.",
        )
    if result.get("error"):
        raise BrowserReadError(
            str(result["error"]),
            f"Browser could not use the resolved Local Scope: {result['error']}.",
        )
    if not isinstance(result.get("html"), str):
        raise BrowserReadError(
            "browser_scope_dom_error",
            "Browser returned Local Scope content without valid HTML.",
        )

    return {
        "html": result["html"].encode("utf-8"),
        "hidden_element_count": int(result.get("hidden_element_count", 0)),
        "computed_text_mark_count": int(
            result.get("computed_text_mark_count", 0)
        ),
    }


def read_browser_page(
    url: str,
    navigation_timeout_ms: int = BROWSER_NAVIGATION_TIMEOUT_MS,
    render_wait_ms: int = BROWSER_RENDER_WAIT_MS,
) -> dict[str, Any]:
    """用 Chromium 渲染页面，并返回现有结构提取链路需要的浏览器结果。

    输入：已经过 URL 校验的地址，以及可选的导航超时和渲染等待毫秒数。
    处理：无界面启动 Chromium；等待 DOMContentLoaded；固定等待有限渲染时间；删除
    display:none、visibility:hidden/collapse、hidden、aria-hidden=true 元素；opacity:0 只有
    同时满足 absolute/fixed 时才删除。不会点击 Tab，也不会根据尺寸或视口位置删除。
    输出：包含状态码、最终 URL、标题、可见 DOM HTML 和隐藏元素计数；失败时抛出
    BrowserReadError。浏览器总会在 finally 中关闭，避免异常后残留进程。
    """
    if navigation_timeout_ms <= 0 or render_wait_ms < 0:
        raise BrowserReadError(
            "invalid_browser_timeout",
            "Browser navigation timeout must be positive and render wait cannot be negative.",
        )

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                timeout=BROWSER_LAUNCH_TIMEOUT_MS,
            )
            try:
                page = browser.new_page()
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=navigation_timeout_ms,
                )
                page.wait_for_timeout(render_wait_ms)

                if response is None:
                    raise BrowserReadError(
                        "browser_no_response",
                        "Browser navigation finished without a main document response.",
                    )
                if not 200 <= response.status < 300:
                    raise BrowserReadError(
                        "browser_http_error",
                        "Browser returned unexpected HTTP status code "
                        f"{response.status}.",
                    )

                # 标题在清理 DOM 前读取，因为浏览器计算样式可能把 head/title 视为不展示
                # 元素；正文 HTML 则必须来自清理后的默认可见状态。
                page_title = page.title()
                # Tab Group 必须在过滤 DOM 之前发现，才能记录隐藏或禁用 Tab 的状态；该
                # 函数只读当前默认状态，不会点击或触发页面交互。
                interactive_tab_groups = discover_safe_tab_groups(page)
                visible_dom = page.evaluate(VISIBLE_DOM_EXTRACTION_SCRIPT)
                if (
                    not isinstance(visible_dom, dict)
                    or not isinstance(visible_dom.get("html"), str)
                ):
                    raise BrowserReadError(
                        "browser_dom_error",
                        "Browser did not return a valid visible DOM HTML result.",
                    )

                return {
                    "status_code": response.status,
                    "final_url": page.url,
                    "title": page_title,
                    # evaluate 返回已经排除明确隐藏节点的默认 DOM。转成 UTF-8 bytes 后
                    # 继续复用 BeautifulSoup、Structured Blocks 和 Group/Card 抽取逻辑。
                    "html": visible_dom["html"].encode("utf-8"),
                    "hidden_element_count": int(
                        visible_dom.get("hidden_element_count", 0)
                    ),
                    "computed_text_mark_count": int(
                        visible_dom.get("computed_text_mark_count", 0)
                    ),
                    "interactive_tab_groups": interactive_tab_groups,
                }
            finally:
                browser.close()
    except BrowserReadError:
        raise
    except PlaywrightTimeoutError as exc:
        raise BrowserReadError(
            "browser_timeout",
            "Browser rendering timed out before a bounded acquisition completed.",
        ) from exc
    except (PlaywrightError, OSError) as exc:
        raise BrowserReadError(
            "browser_failed", f"Browser rendering failed: {exc}"
        ) from exc
