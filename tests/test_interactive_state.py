"""Stage 1 V0.3-3 安全 Tab Group 发现与状态骨架测试。

测试使用本地可控 HTML 和已安装的 Chromium，不访问公网。这样能够真实验证浏览器
computed style、hidden/disabled 状态和标准 ARIA role，同时避免外部页面变化影响结果。
"""

from __future__ import annotations

import unittest

from playwright.sync_api import sync_playwright

from backend.interactive_state import (
    build_interactive_state_key,
    discover_safe_tab_groups,
)


class InteractiveStateTests(unittest.TestCase):
    """验证只读发现规则；所有测试都不调用 click。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def _discover(self, html: str) -> list[dict[str, object]]:
        """把本地 HTML 放入真实 DOM，返回生产发现函数生成的标签组。"""
        page = self.browser.new_page()
        try:
            page.set_content(html)
            return discover_safe_tab_groups(page)
        finally:
            page.close()

    def test_visible_standard_tabs_create_default_state_skeleton(self) -> None:
        groups = self._discover(
            """
            <div role="tablist" aria-label="Product section">
              <button role="tab" aria-selected="true">Overview</button>
              <button role="tab" aria-selected="false">Pricing Plans</button>
            </div>
            """
        )

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["runtime_locator"]["selected_tab_index"], 0)
        self.assertIsInstance(group["scope_path"], list)
        self.assertEqual(group["scope_path"], ["Product section"])
        self.assertTrue(group["stable_state_available"])
        self.assertIsNone(group["interactive_state_unavailable_reason"])
        self.assertEqual(
            group["tabs"],
            [
                {
                    "runtime_locator": {"tab_index": 0},
                    "text": "Overview",
                    "aria_selected": True,
                    "visible": True,
                    "disabled": False,
                },
                {
                    "runtime_locator": {"tab_index": 1},
                    "text": "Pricing Plans",
                    "aria_selected": False,
                    "visible": True,
                    "disabled": False,
                },
            ],
        )
        self.assertEqual(len(group["states"]), 1)
        state = group["states"][0]
        self.assertEqual(
            set(state),
            {
                "state_key",
                "scope_path",
                "state_path",
                "is_default",
                "blocks",
                "content_hash",
                "captured_at",
            },
        )
        self.assertTrue(state["is_default"])
        self.assertEqual(state["scope_path"], group["scope_path"])
        self.assertEqual(state["state_path"], ["Overview"])
        self.assertEqual(
            state["state_key"],
            build_interactive_state_key(["Product section"], ["Overview"]),
        )
        self.assertNotIn("nth-of-type", str(state))
        self.assertNotIn("tab[", str(state))
        self.assertEqual(state["blocks"], [])
        self.assertIsNone(state["content_hash"])
        self.assertIsNone(state["captured_at"])

    def test_aria_labelledby_provides_semantic_scope(self) -> None:
        groups = self._discover(
            """
            <h2 id="billing-heading">Billing cycle</h2>
            <div role="tablist" aria-labelledby="billing-heading">
              <button role="tab" aria-selected="true">Monthly</button>
              <button role="tab" aria-selected="false">Yearly</button>
            </div>
            """
        )

        self.assertEqual(groups[0]["scope_path"], ["Billing cycle"])
        self.assertEqual(groups[0]["states"][0]["state_path"], ["Monthly"])

    def test_visible_preceding_heading_path_is_semantic_scope_fallback(self) -> None:
        groups = self._discover(
            """
            <h1>Plans</h1>
            <section>
              <h2>Team subscription</h2>
              <div role="tablist">
                <button role="tab" aria-selected="true">Monthly</button>
                <button role="tab" aria-selected="false">Yearly</button>
              </div>
            </section>
            """
        )

        self.assertEqual(groups[0]["scope_path"], ["Plans", "Team subscription"])

    def test_group_without_semantic_scope_has_no_stable_state(self) -> None:
        groups = self._discover(
            """
            <div role="tablist">
              <button role="tab" aria-selected="true">Overview</button>
              <button role="tab" aria-selected="false">Pricing</button>
            </div>
            """
        )

        self.assertEqual(len(groups), 1)
        self.assertFalse(groups[0]["stable_state_available"])
        self.assertEqual(
            groups[0]["interactive_state_unavailable_reason"],
            "semantic_scope_unavailable",
        )
        self.assertEqual(groups[0]["scope_path"], [])
        self.assertEqual(groups[0]["states"], [])

    def test_unrelated_preceding_dom_node_does_not_change_state_key(self) -> None:
        original_groups = self._discover(
            """
            <section>
              <div role="tablist" aria-label="Billing cycle">
                <button role="tab" aria-selected="true">Monthly</button>
                <button role="tab" aria-selected="false">Yearly</button>
              </div>
            </section>
            """
        )
        shifted_groups = self._discover(
            """
            <section>
              <div>Unrelated introduction</div>
              <div role="tablist" aria-label="Billing cycle">
                <button role="tab" aria-selected="true">Monthly</button>
                <button role="tab" aria-selected="false">Yearly</button>
              </div>
            </section>
            """
        )

        original_group = original_groups[0]
        shifted_group = shifted_groups[0]
        self.assertNotEqual(
            original_group["runtime_locator"]["dom_path"],
            shifted_group["runtime_locator"]["dom_path"],
        )
        self.assertEqual(
            original_group["states"][0]["state_key"],
            shifted_group["states"][0]["state_key"],
        )

    def test_hidden_tab_is_recorded_but_not_counted_as_actionable(self) -> None:
        groups = self._discover(
            """
            <div role="tablist">
              <button role="tab" aria-selected="true">Current</button>
              <button role="tab" aria-selected="false">Alternative</button>
              <button role="tab" aria-selected="false" style="display:none">Hidden</button>
            </div>
            """
        )

        self.assertEqual(len(groups), 1)
        hidden_tab = groups[0]["tabs"][2]
        self.assertEqual(hidden_tab["text"], "Hidden")
        self.assertFalse(hidden_tab["visible"])
        self.assertFalse(hidden_tab["disabled"])

    def test_disabled_tab_is_recorded_and_marked_disabled(self) -> None:
        groups = self._discover(
            """
            <div role="tablist">
              <button role="tab" aria-selected="true">Current</button>
              <button role="tab" aria-selected="false">Alternative</button>
              <button role="tab" aria-selected="false" disabled>Unavailable</button>
            </div>
            """
        )

        self.assertEqual(len(groups), 1)
        disabled_tab = groups[0]["tabs"][2]
        self.assertEqual(disabled_tab["text"], "Unavailable")
        self.assertTrue(disabled_tab["visible"])
        self.assertTrue(disabled_tab["disabled"])

    def test_group_without_clear_selected_tab_is_rejected(self) -> None:
        groups = self._discover(
            """
            <div role="tablist">
              <button role="tab">Overview</button>
              <button role="tab" aria-selected="false">Pricing</button>
            </div>
            """
        )

        self.assertEqual(groups, [])

    def test_group_with_one_selected_and_one_missing_selection_is_rejected(self) -> None:
        groups = self._discover(
            """
            <div role="tablist">
              <button role="tab" aria-selected="true">Overview</button>
              <button role="tab">Pricing</button>
            </div>
            """
        )

        self.assertEqual(groups, [])

    def test_ordinary_buttons_are_not_recognized_as_tabs(self) -> None:
        groups = self._discover(
            """
            <div role="tablist">
              <button aria-selected="true">Overview</button>
              <button>Pricing</button>
            </div>
            <button>Ordinary action</button>
            """
        )

        self.assertEqual(groups, [])


if __name__ == "__main__":
    unittest.main()
