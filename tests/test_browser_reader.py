"""Stage 1 Browser Rendering 的浏览器读取与可见 DOM 过滤测试。

网络读取流程继续模拟 Playwright 对象；可见 DOM 规则使用本地 HTML 和 Chromium 验证，
不访问公网，从而既能检查真实 computed style，又保持测试输入稳定可控。
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from backend.browser_reader import (
    BROWSER_LAUNCH_TIMEOUT_MS,
    VISIBLE_DOM_EXTRACTION_SCRIPT,
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
        page.evaluate.return_value = {
            "html": "<html><body><p>Rendered content</p></body></html>",
            "hidden_element_count": 4,
        }

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
        page.evaluate.assert_called_once_with(VISIBLE_DOM_EXTRACTION_SCRIPT)
        page.click.assert_not_called()
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["final_url"], "https://www.example.com/final")
        self.assertEqual(result["title"], "Rendered title")
        self.assertIn(b"Rendered content", result["html"])
        self.assertEqual(result["hidden_element_count"], 4)
        browser.close.assert_called_once()

    def test_visible_dom_filter_uses_hidden_semantics_not_viewport_position(self) -> None:
        """过滤脚本应覆盖明确隐藏规则，但不能用坐标误删视口外正文。"""
        normalized_script = " ".join(VISIBLE_DOM_EXTRACTION_SCRIPT.split()).lower()

        self.assertIn('style.display === "none"', normalized_script)
        self.assertIn('style.visibility === "hidden"', normalized_script)
        self.assertIn('style.visibility === "collapse"', normalized_script)
        self.assertIn("number.parsefloat(style.opacity) === 0", normalized_script)
        self.assertIn('style.position === "absolute"', normalized_script)
        self.assertIn('style.position === "fixed"', normalized_script)
        self.assertIn('element.hasattribute("hidden")', normalized_script)
        self.assertIn('element.getattribute("aria-hidden")', normalized_script)
        self.assertNotIn("getboundingclientrect", normalized_script)
        self.assertNotIn("offsetparent", normalized_script)
        self.assertNotIn("scroll", normalized_script)
        self.assertNotIn("checkvisibility", normalized_script)

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


class VisibleDomExtractionScriptTests(unittest.TestCase):
    """用本地页面验证过滤脚本的真实 DOM 删除结果，不依赖任何外部网站。"""

    @classmethod
    def setUpClass(cls) -> None:
        """整组测试共用一个 Chromium，减少重复启动带来的时间开销。"""
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        """无论测试是否通过，都关闭浏览器进程和 Playwright 驱动。"""
        cls.browser.close()
        cls.playwright.stop()

    def _filter_local_html(self, html: str) -> dict[str, object]:
        """把可控 HTML 放入浏览器，执行生产过滤脚本并返回过滤结果。"""
        page = self.browser.new_page(viewport={"width": 800, "height": 600})
        try:
            page.set_content(html)
            return page.evaluate(VISIBLE_DOM_EXTRACTION_SCRIPT)
        finally:
            page.close()

    def test_absolute_opacity_zero_parent_removes_child_subtree(self) -> None:
        """透明绝对定位浮层无需额外裁剪证据，也应删除完整隐藏子树。"""
        result = self._filter_local_html(
            """
            <main>VISIBLE_MAIN</main>
            <section style="opacity: 0; position: absolute">
              <p style="opacity: 1">HIDDEN_MENU_CHILD</p>
            </section>
            """
        )

        self.assertIn("VISIBLE_MAIN", result["html"])
        self.assertNotIn("HIDDEN_MENU_CHILD", result["html"])
        self.assertEqual(result["hidden_element_count"], 1)

    def test_opacity_one_content_is_preserved(self) -> None:
        result = self._filter_local_html(
            '<section style="opacity: 1"><p>NORMAL_VISIBLE_CONTENT</p></section>'
        )

        self.assertIn("NORMAL_VISIBLE_CONTENT", result["html"])
        self.assertEqual(result["hidden_element_count"], 0)

    def test_static_opacity_zero_content_with_normal_height_is_preserved(self) -> None:
        """普通文档流中的透明正文可能等待滚动显现，不能仅因 opacity=0 删除。"""
        result = self._filter_local_html(
            '<section style="opacity:0; position:static; height:200px">'
            "STATIC_REVEAL_CONTENT</section>"
        )

        self.assertIn("STATIC_REVEAL_CONTENT", result["html"])
        self.assertEqual(result["hidden_element_count"], 0)

    def test_static_opacity_zero_translate_reveal_content_is_preserved(self) -> None:
        """opacity 与 translateY 组合是常见滚动动画初态，应保留真实正文。"""
        result = self._filter_local_html(
            '<section style="opacity:0; position:static; transform:translateY(20px)">'
            "TRANSLATED_REVEAL_CONTENT</section>"
        )

        self.assertIn("TRANSLATED_REVEAL_CONTENT", result["html"])
        self.assertEqual(result["hidden_element_count"], 0)

    def test_fixed_opacity_zero_parent_removes_child_subtree(self) -> None:
        """透明 fixed 浮层同样脱离文档流，应作为关闭面板删除。"""
        result = self._filter_local_html(
            '<section style="opacity:0; position:fixed">'
            "HIDDEN_FIXED_CONTENT</section>"
        )

        self.assertNotIn("HIDDEN_FIXED_CONTENT", result["html"])
        self.assertEqual(result["hidden_element_count"], 1)

    def test_relative_opacity_zero_content_is_preserved(self) -> None:
        """relative 仍保留原有文档流位置，不能仅因透明而删除。"""
        result = self._filter_local_html(
            '<section style="opacity:0; position:relative">'
            "RELATIVE_REVEAL_CONTENT</section>"
        )

        self.assertIn("RELATIVE_REVEAL_CONTENT", result["html"])
        self.assertEqual(result["hidden_element_count"], 0)

    def test_content_outside_viewport_is_preserved(self) -> None:
        """页面下方未进入 800×600 视口的正文仍属于真实可见内容。"""
        result = self._filter_local_html(
            '<p style="position:absolute; top:5000px">BELOW_VIEWPORT_CONTENT</p>'
        )

        self.assertIn("BELOW_VIEWPORT_CONTENT", result["html"])
        self.assertEqual(result["hidden_element_count"], 0)

    def test_existing_hidden_semantics_still_remove_complete_subtrees(self) -> None:
        result = self._filter_local_html(
            """
            <div style="display:none"><span>DISPLAY_NONE_CHILD</span></div>
            <div style="visibility:hidden"><span>VISIBILITY_HIDDEN_CHILD</span></div>
            <div style="visibility:collapse"><span>VISIBILITY_COLLAPSE_CHILD</span></div>
            <div hidden><span>HIDDEN_ATTRIBUTE_CHILD</span></div>
            <div aria-hidden="true"><span>ARIA_HIDDEN_CHILD</span></div>
            <div>STILL_VISIBLE</div>
            """
        )

        self.assertIn("STILL_VISIBLE", result["html"])
        for hidden_text in (
            "DISPLAY_NONE_CHILD",
            "VISIBILITY_HIDDEN_CHILD",
            "VISIBILITY_COLLAPSE_CHILD",
            "HIDDEN_ATTRIBUTE_CHILD",
            "ARIA_HIDDEN_CHILD",
        ):
            self.assertNotIn(hidden_text, result["html"])


if __name__ == "__main__":
    unittest.main()
