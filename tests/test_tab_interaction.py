"""Stage 1 V0.3-3B 安全 Tab 点击和恢复测试。

测试使用本地 Chromium 与可控 DOM，不访问公网。每个场景都从 3A 实际发现的安全组开始，
用于验证点击前审计、ARIA 切换、安全副作用拦截和默认状态恢复。
"""

from __future__ import annotations

import unittest

from playwright.sync_api import sync_playwright

from backend.interactive_state import discover_safe_tab_groups
from backend.tab_interaction import click_safe_tab, restore_default_tab


class SafeTabInteractionTests(unittest.TestCase):
    """验证每次调用只点击一个目标 Tab，并在异常后停止。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def _new_page(self, tab_markup: str, extra_script: str = ""):
        """创建带统一 ARIA 切换函数的独立页面；测试结束自动关闭整个 context。"""
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_content(
            f"""
            <main>
              <h2>Subscription</h2>
              <section id="tab-scope">
                <div role="tablist" aria-label="Billing cycle">
                  {tab_markup}
                </div>
                <div id="panel">Monthly content</div>
              </section>
            </main>
            <script>
              function selectTab(target) {{
                const tablist = target.closest('[role="tablist"]');
                tablist.querySelectorAll('[role="tab"]').forEach((tab) => {{
                  tab.setAttribute('aria-selected', tab === target ? 'true' : 'false');
                }});
                document.getElementById('panel').textContent =
                  target.textContent.trim() + ' content';
              }}
              {extra_script}
            </script>
            """
        )
        return page

    @staticmethod
    def _fast_options() -> dict[str, int]:
        """缩短可控本地页面等待时间，同时仍执行真实轮询和稳定性判断。"""
        return {
            "selection_timeout_ms": 300,
            "stability_timeout_ms": 800,
            "stability_interval_ms": 20,
            "stable_observations": 2,
        }

    def test_successful_tab_switch_records_and_validates_audit_state(self) -> None:
        page = self._new_page(
            """
            <button role="tab" aria-selected="true"
                    onclick="selectTab(this)">Monthly</button>
            <button role="tab" aria-selected="false"
                    onclick="selectTab(this)">Yearly</button>
            """
        )
        group = discover_safe_tab_groups(page)[0]

        result = click_safe_tab(page, group, 1, **self._fast_options())

        self.assertTrue(result["success"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["before"]["url"], result["after"]["url"])
        self.assertEqual(result["before"]["page_count"], 1)
        self.assertEqual(result["after"]["page_count"], 1)
        self.assertNotEqual(
            result["before"]["local_dom_fingerprint"],
            result["after"]["local_dom_fingerprint"],
        )
        self.assertEqual(
            [tab["aria_selected"] for tab in result["after"]["selected_state"]],
            [False, True],
        )

    def test_selected_state_not_changing_returns_clear_failure(self) -> None:
        page = self._new_page(
            """
            <button role="tab" aria-selected="true">Monthly</button>
            <button role="tab" aria-selected="false">Yearly</button>
            """
        )
        group = discover_safe_tab_groups(page)[0]

        result = click_safe_tab(page, group, 1, **self._fast_options())

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "selection_switch_timeout")

    def test_url_navigation_is_rejected(self) -> None:
        page = self._new_page(
            """
            <button role="tab" aria-selected="true"
                    onclick="selectTab(this)">Monthly</button>
            <button role="tab" aria-selected="false"
                    onclick="selectTab(this); history.pushState({}, '', '#changed')">
              Yearly
            </button>
            """
        )
        group = discover_safe_tab_groups(page)[0]

        result = click_safe_tab(page, group, 1, **self._fast_options())

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "url_changed")

    def test_opening_new_page_is_rejected(self) -> None:
        page = self._new_page(
            """
            <button role="tab" aria-selected="true"
                    onclick="selectTab(this)">Monthly</button>
            <button role="tab" aria-selected="false"
                    onclick="selectTab(this); window.open('about:blank', '_blank')">
              Yearly
            </button>
            """
        )
        group = discover_safe_tab_groups(page)[0]

        result = click_safe_tab(page, group, 1, **self._fast_options())

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "new_page_opened")

    def test_download_is_rejected(self) -> None:
        page = self._new_page(
            """
            <button role="tab" aria-selected="true"
                    onclick="selectTab(this)">Monthly</button>
            <a role="tab" aria-selected="false" download="plans.txt"
               href="data:text/plain,plans"
               onclick="selectTab(this)">Download plans</a>
            """
        )
        group = discover_safe_tab_groups(page)[0]

        result = click_safe_tab(page, group, 1, **self._fast_options())

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "download_triggered")

    def test_hidden_or_disabled_target_is_not_clicked(self) -> None:
        for target_attribute in ("style='display:none'", "disabled"):
            with self.subTest(target_attribute=target_attribute):
                page = self._new_page(
                    f"""
                    <button role="tab" aria-selected="true"
                            onclick="selectTab(this)">Current</button>
                    <button role="tab" aria-selected="false"
                            onclick="selectTab(this)">Alternative</button>
                    <button role="tab" aria-selected="false" {target_attribute}
                            onclick="window.restrictedClicks += 1">Restricted</button>
                    """,
                    "window.restrictedClicks = 0;",
                )
                group = discover_safe_tab_groups(page)[0]

                result = click_safe_tab(page, group, 2, **self._fast_options())

                self.assertFalse(result["success"])
                self.assertEqual(
                    result["error"]["code"], "target_tab_not_actionable"
                )
                self.assertEqual(page.evaluate("window.restrictedClicks"), 0)

    def test_successful_switch_can_restore_original_default_tab(self) -> None:
        page = self._new_page(
            """
            <button role="tab" aria-selected="true"
                    onclick="selectTab(this)">Monthly</button>
            <button role="tab" aria-selected="false"
                    onclick="selectTab(this)">Yearly</button>
            """
        )
        group = discover_safe_tab_groups(page)[0]
        switched = click_safe_tab(page, group, 1, **self._fast_options())

        restored = restore_default_tab(
            page,
            group,
            switched,
            **self._fast_options(),
        )

        self.assertTrue(switched["success"])
        self.assertTrue(restored["success"])
        self.assertEqual(
            [tab["aria_selected"] for tab in restored["after"]["selected_state"]],
            [True, False],
        )


if __name__ == "__main__":
    unittest.main()
