"""Stage 1 V0.2-3 Browser Rendering Fallback 的浏览器读取单元测试。

测试只模拟 Playwright 对象，不启动真实 Chromium，也不访问公网。真实浏览器与公网
页面留给任务完成后的手动验收，从而让自动化测试稳定、快速。
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from backend.browser_reader import (
    BROWSER_LAUNCH_TIMEOUT_MS,
    BrowserReadError,
    read_browser_page,
)


class BrowserReaderTests(unittest.TestCase):
    """验证等待策略、浏览器返回结构和稳定错误，不依赖浏览器运行环境。"""

    def _mock_playwright(self) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
        """构造 sync_playwright 上下文、browser、page 和主文档响应。"""
        manager = MagicMock()
        playwright = manager.__enter__.return_value
        browser = playwright.chromium.launch.return_value
        page = browser.new_page.return_value
        response = page.goto.return_value
        return manager, browser, page, response

    def test_reads_rendered_dom_after_bounded_wait(self) -> None:
        manager, browser, page, response = self._mock_playwright()
        response.status = 200
        page.url = "https://www.example.com/final"
        page.title.return_value = "Rendered title"
        page.content.return_value = "<html><body><p>Rendered content</p></body></html>"

        with patch("backend.browser_reader.sync_playwright", return_value=manager):
            result = read_browser_page(
                "https://example.com", navigation_timeout_ms=12_345, render_wait_ms=678
            )

        manager.__enter__.return_value.chromium.launch.assert_called_once_with(
            headless=True,
            timeout=BROWSER_LAUNCH_TIMEOUT_MS,
        )
        page.goto.assert_called_once_with(
            "https://example.com",
            wait_until="domcontentloaded",
            timeout=12_345,
        )
        page.wait_for_timeout.assert_called_once_with(678)
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["final_url"], "https://www.example.com/final")
        self.assertEqual(result["title"], "Rendered title")
        self.assertIn(b"Rendered content", result["html"])
        browser.close.assert_called_once()

    def test_navigation_timeout_becomes_stable_browser_error(self) -> None:
        manager, browser, page, _ = self._mock_playwright()
        page.goto.side_effect = PlaywrightTimeoutError("simulated timeout")

        with patch("backend.browser_reader.sync_playwright", return_value=manager):
            with self.assertRaises(BrowserReadError) as raised:
                read_browser_page("https://example.com")

        self.assertEqual(raised.exception.error_type, "browser_timeout")
        browser.close.assert_called_once()

    def test_browser_start_failure_becomes_stable_browser_error(self) -> None:
        manager, _, _, _ = self._mock_playwright()
        manager.__enter__.return_value.chromium.launch.side_effect = PlaywrightError(
            "Chromium executable is unavailable"
        )

        with patch("backend.browser_reader.sync_playwright", return_value=manager):
            with self.assertRaises(BrowserReadError) as raised:
                read_browser_page("https://example.com")

        self.assertEqual(raised.exception.error_type, "browser_failed")
        self.assertIn("Chromium executable", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
