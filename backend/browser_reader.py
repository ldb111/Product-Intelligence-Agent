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
    处理：无界面启动 Chromium；等待 DOMContentLoaded；固定等待有限渲染时间；读取主
    文档状态码、最终 URL、页面标题和包含 JavaScript 渲染结果的当前 DOM HTML。
    输出：包含 status_code、final_url、title、html bytes 的字典；失败时抛出
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

                return {
                    "status_code": response.status,
                    "final_url": page.url,
                    "title": page.title(),
                    # page.content() 返回当前 DOM 的完整 HTML 字符串，包括 JavaScript 已经
                    # 插入的节点；转成 UTF-8 bytes 后可直接复用现有 BeautifulSoup 入口。
                    "html": page.content().encode("utf-8"),
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
