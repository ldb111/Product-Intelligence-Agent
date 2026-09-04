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


# 所有等待都有明确上限。DOMContentLoaded 只等待基础 DOM 就绪，随后固定等待一小段
# 时间让常见前端框架完成首屏渲染；不使用可能被长连接和统计请求持续阻塞的 networkidle。
BROWSER_LAUNCH_TIMEOUT_MS = 30_000
BROWSER_NAVIGATION_TIMEOUT_MS = 30_000
BROWSER_RENDER_WAIT_MS = 5_000

# 该脚本在浏览器已经完成默认状态渲染后执行，只删除 DOM 明确声明或计算样式确认隐藏的
# 元素。它不读取元素坐标、尺寸或是否进入当前视口，因此页面下方尚未滚动到的正文会保留。
VISIBLE_DOM_EXTRACTION_SCRIPT = """
() => {
    const elements = document.body
        ? Array.from(document.body.querySelectorAll("*"))
        : [];
    const hiddenElements = [];
    let hiddenElementCount = 0;

    for (const element of elements) {
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

        if (
            element.hidden
            || element.hasAttribute("hidden")
            || ariaHidden
            || hiddenByStyle
            || hiddenTransparentOverlay
        ) {
            hiddenElements.push(element);
        }
    }

    // 先完成全部计算样式检查，再统一删除。若边检查边删除样式表或祖先节点，后续元素的
    // computed style 可能变化，导致原本 opacity:0 的菜单被错误读取为可见。
    for (const element of hiddenElements) {
        if (element.isConnected) {
            element.remove();
            hiddenElementCount += 1;
        }
    }

    return {
        html: document.documentElement.outerHTML,
        hidden_element_count: hiddenElementCount,
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
