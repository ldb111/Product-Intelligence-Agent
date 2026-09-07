"""Stage 1 V0.3-3D-1 顶层 Interactive State Traversal 测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from backend.interactive_state import discover_safe_tab_groups
from backend.interactive_traversal import traverse_top_level_interactive_states


class InteractiveTraversalTests(unittest.TestCase):
    """验证编排顺序、恢复边界、状态去重和整页中止策略。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def _new_page(self, html: str):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_content(html)
        return page

    @staticmethod
    def _fast_options() -> dict[str, int]:
        return {
            "selection_timeout_ms": 300,
            "stability_timeout_ms": 800,
            "stability_interval_ms": 20,
            "stable_observations": 2,
        }

    @staticmethod
    def _two_group_fixture() -> str:
        """构造两个独立顶层组，面板用标准 aria-controls 关联。"""
        return """
        <main>
          <section>
            <div role="tablist" aria-label="Billing cycle">
              <button role="tab" aria-selected="true" aria-controls="g1-monthly"
                      onclick="selectTab(this)">Monthly</button>
              <button role="tab" aria-selected="false" aria-controls="g1-quarterly"
                      onclick="selectTab(this)">Quarterly</button>
              <button role="tab" aria-selected="false" aria-controls="g1-yearly"
                      onclick="selectTab(this)">Yearly</button>
            </div>
            <div id="g1-monthly" role="tabpanel"><p>Monthly 10</p></div>
            <div id="g1-quarterly" role="tabpanel" hidden><p>Quarterly 27</p></div>
            <div id="g1-yearly" role="tabpanel" hidden><p>Yearly 100</p></div>
          </section>
          <section>
            <div role="tablist" aria-label="Credit package">
              <button role="tab" aria-selected="true" aria-controls="g2-small"
                      onclick="selectTab(this)">Small</button>
              <button role="tab" aria-selected="false" aria-controls="g2-large"
                      onclick="selectTab(this)">Large</button>
            </div>
            <div id="g2-small" role="tabpanel"><p>Small 20</p></div>
            <div id="g2-large" role="tabpanel" hidden><p>Large 100</p></div>
          </section>
        </main>
        <script>
          function selectTab(target) {
            const tabs = target.closest('[role="tablist"]')
              .querySelectorAll('[role="tab"]');
            tabs.forEach((tab) => {
              const selected = tab === target;
              tab.setAttribute('aria-selected', selected ? 'true' : 'false');
              document.getElementById(tab.getAttribute('aria-controls')).hidden =
                !selected;
            });
          }
        </script>
        """

    @staticmethod
    def _nested_fixture(
        *,
        include_third_level: bool = False,
        include_parent_explanation: bool = False,
    ) -> str:
        """构造父状态中各自存在子组的页面，并可加入第三层用于深度截断。"""
        third_level = """
          <div role="tablist" aria-label="Add-on mode">
            <button role="tab" aria-selected="true" aria-controls="addon-a"
                    onclick="selectTab(this)">Add-on A</button>
            <button role="tab" aria-selected="false" aria-controls="addon-b"
                    onclick="selectTab(this)">Add-on B</button>
          </div>
          <div id="addon-a" role="tabpanel"><p>Add-on A content</p></div>
          <div id="addon-b" role="tabpanel" hidden><p>Add-on B content</p></div>
        """ if include_third_level else ""
        personal_explanation = (
            "<p>Personal accounts include private workspace controls.</p>"
            if include_parent_explanation
            else ""
        )
        team_explanation = (
            "<p>Team accounts include shared administration controls.</p>"
            if include_parent_explanation
            else ""
        )
        return f"""
        <main>
          <div role="tablist" aria-label="Edition">
            <button role="tab" aria-selected="true" aria-controls="personal"
                    onclick="selectTab(this)">Personal</button>
            <button role="tab" aria-selected="false" aria-controls="team"
                    onclick="selectTab(this)">Team</button>
          </div>
          <section id="personal" role="tabpanel">
            {personal_explanation}
            <div role="tablist" aria-label="Personal billing">
              <button role="tab" aria-selected="true" aria-controls="personal-month"
                      onclick="selectTab(this)">Monthly</button>
              <button role="tab" aria-selected="false" aria-controls="personal-quarter"
                      onclick="selectTab(this)">Quarterly</button>
            </div>
            <div id="personal-month" role="tabpanel"><p>Personal month</p></div>
            <div id="personal-quarter" role="tabpanel" hidden><p>Personal quarter</p></div>
          </section>
          <section id="team" role="tabpanel" hidden>
            {team_explanation}
            <div role="tablist" aria-label="Team billing">
              <button role="tab" aria-selected="true" aria-controls="team-month"
                      onclick="selectTab(this)">Monthly</button>
              <button role="tab" aria-selected="false" aria-controls="team-year"
                      onclick="selectTab(this)">Yearly</button>
            </div>
            <div id="team-month" role="tabpanel"><p>Team month</p></div>
            <div id="team-year" role="tabpanel" hidden>
              <p>Team year</p>
              {third_level}
            </div>
          </section>
        </main>
        <script>
          function selectTab(target) {{
            const tabs = target.closest('[role="tablist"]')
              .querySelectorAll(':scope > [role="tab"]');
            tabs.forEach((tab) => {{
              const selected = tab === target;
              tab.setAttribute('aria-selected', selected ? 'true' : 'false');
              document.getElementById(tab.getAttribute('aria-controls')).hidden =
                !selected;
            }});
          }}
        </script>
        """

    @staticmethod
    def _many_groups_fixture(group_count: int, option_count: int) -> str:
        """构造多个独立组，用于 option/state 两类硬上限测试。"""
        sections: list[str] = []
        for group_index in range(group_count):
            tabs: list[str] = []
            panels: list[str] = []
            for option_index in range(option_count):
                panel_id = f"g{group_index}-p{option_index}"
                tabs.append(
                    f'<button role="tab" aria-selected="{str(option_index == 0).lower()}" '
                    f'aria-controls="{panel_id}" onclick="selectTab(this)">'
                    f'G{group_index} Option {option_index}</button>'
                )
                hidden = "" if option_index == 0 else " hidden"
                panels.append(
                    f'<div id="{panel_id}" role="tabpanel"{hidden}>'
                    f'<p>G{group_index} content {option_index}</p></div>'
                )
            sections.append(
                f'<section><div role="tablist" aria-label="Group {group_index}">'
                f'{"".join(tabs)}</div>{"".join(panels)}</section>'
            )
        return f"""
        <main>{''.join(sections)}</main>
        <script>
          function selectTab(target) {{
            const tabs = target.closest('[role="tablist"]')
              .querySelectorAll(':scope > [role="tab"]');
            tabs.forEach((tab) => {{
              const selected = tab === target;
              tab.setAttribute('aria-selected', selected ? 'true' : 'false');
              document.getElementById(tab.getAttribute('aria-controls')).hidden =
                !selected;
            }});
          }}
        </script>
        """

    @staticmethod
    def _group(index: int, scope: str = "Scope") -> dict[str, object]:
        """为纯编排测试构造冻结 3A 的最小组结构。"""
        return {
            "scope_path": [scope],
            "stable_state_available": True,
            "runtime_locator": {
                "group_index": index,
                "dom_path": ["body", f"div:nth-of-type({index + 1})"],
                "selected_tab_index": 0,
            },
            "tabs": [
                {
                    "text": "Default",
                    "aria_selected": True,
                    "visible": True,
                    "disabled": False,
                    "runtime_locator": {"tab_index": 0},
                },
                {
                    "text": "Other",
                    "aria_selected": False,
                    "visible": True,
                    "disabled": False,
                    "runtime_locator": {"tab_index": 1},
                },
            ],
        }

    @staticmethod
    def _success_click(group_index: int = 0) -> dict[str, object]:
        return {
            "success": True,
            "error": None,
            "runtime": {
                "tablist_dom_path": [
                    "body", f"div:nth-of-type({group_index + 1})"
                ],
                "original_selected_tab_index": 0,
                "target_tab_index": 1,
            },
        }

    @staticmethod
    def _state(key: str, content_hash: str, *, is_default: bool) -> dict[str, object]:
        return {
            "state_key": key,
            "scope_path": ["Scope"],
            "state_path": ["Default" if is_default else "Other"],
            "is_default": is_default,
            "blocks": [{"type": "paragraph", "text": content_hash}],
            "content_hash": content_hash,
            "captured_at": "2026-09-05T10:00:00+08:00",
        }

    def test_complete_traversal_captures_states_and_restores_every_group(self) -> None:
        page = self._new_page(self._two_group_fixture())

        result = traverse_top_level_interactive_states(
            page, click_options=self._fast_options()
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["groups_discovered"], 2)
        self.assertEqual(result["groups_completed"], 2)
        self.assertEqual(result["non_default_states_captured"], 3)
        self.assertEqual(len(result["interactive_states"]), 5)
        self.assertTrue(result["page_restored"])
        self.assertEqual(result["errors"], [])
        groups_after = discover_safe_tab_groups(page)
        self.assertEqual(
            [group["runtime_locator"]["selected_tab_index"] for group in groups_after],
            [0, 0],
        )
        self.assertEqual(
            [state["state_path"] for state in result["interactive_states"]],
            [["Quarterly"], ["Yearly"], ["Monthly"], ["Large"], ["Small"]],
        )
        self.assertTrue(
            all(len(state["state_path"]) == 1 for state in result["interactive_states"])
        )

    def test_nested_tab_group_builds_parent_to_child_state_paths(self) -> None:
        page = self._new_page(self._nested_fixture())

        result = traverse_top_level_interactive_states(
            page, click_options=self._fast_options()
        )

        state_paths = [state["state_path"] for state in result["interactive_states"]]
        self.assertEqual(result["status"], "complete")
        self.assertIn(["Team", "Yearly"], state_paths)
        self.assertIn(["Team", "Monthly"], state_paths)
        self.assertIn(["Personal", "Quarterly"], state_paths)
        self.assertIn(["Personal", "Monthly"], state_paths)
        self.assertNotIn(["Team", "Quarterly"], state_paths)
        self.assertNotIn(["Personal", "Yearly"], state_paths)
        self.assertTrue(result["page_restored"])
        self.assertEqual(
            discover_safe_tab_groups(page)[0]["runtime_locator"]["selected_tab_index"],
            0,
        )

    def test_parent_and_child_do_not_both_own_default_child_content(self) -> None:
        page = self._new_page(self._nested_fixture())

        result = traverse_top_level_interactive_states(
            page, click_options=self._fast_options()
        )

        states = {
            tuple(state["state_path"]): state
            for state in result["interactive_states"]
        }
        team_parent = states[("Team",)]
        team_monthly = states[("Team", "Monthly")]
        self.assertFalse(team_parent["comparison_eligible"])
        self.assertIsNone(team_parent["content_hash"])
        self.assertEqual(
            team_parent["non_comparable_reason"],
            "content_owned_by_nested_states",
        )
        self.assertNotIn("Team month", str(team_parent["blocks"]))
        self.assertIn("Team month", str(team_monthly["blocks"]))

    def test_parent_independent_explanation_remains_comparable(self) -> None:
        page = self._new_page(
            self._nested_fixture(include_parent_explanation=True)
        )

        result = traverse_top_level_interactive_states(
            page, click_options=self._fast_options()
        )

        states = {
            tuple(state["state_path"]): state
            for state in result["interactive_states"]
        }
        team_parent = states[("Team",)]
        self.assertTrue(team_parent["comparison_eligible"])
        self.assertIsInstance(team_parent["content_hash"], str)
        self.assertIn("shared administration controls", str(team_parent["blocks"]))
        self.assertNotIn("Team month", str(team_parent["blocks"]))

    def test_sibling_child_states_do_not_contain_each_others_content(self) -> None:
        page = self._new_page(self._nested_fixture())

        result = traverse_top_level_interactive_states(
            page, click_options=self._fast_options()
        )

        states = {
            tuple(state["state_path"]): state
            for state in result["interactive_states"]
        }
        monthly_blocks = str(states[("Personal", "Monthly")]["blocks"])
        quarterly_blocks = str(states[("Personal", "Quarterly")]["blocks"])
        self.assertIn("Personal month", monthly_blocks)
        self.assertNotIn("Personal quarter", monthly_blocks)
        self.assertIn("Personal quarter", quarterly_blocks)
        self.assertNotIn("Personal month", quarterly_blocks)

    def test_non_nested_d1_states_keep_existing_comparable_shape(self) -> None:
        page = self._new_page(self._two_group_fixture())

        result = traverse_top_level_interactive_states(
            page, click_options=self._fast_options()
        )

        self.assertEqual(result["status"], "complete")
        self.assertTrue(
            all(
                "comparison_eligible" not in state
                for state in result["interactive_states"]
            )
        )
        self.assertTrue(
            all(
                isinstance(state["content_hash"], str)
                for state in result["interactive_states"]
            )
        )

    def test_third_level_group_is_reported_at_depth_limit(self) -> None:
        page = self._new_page(self._nested_fixture(include_third_level=True))

        result = traverse_top_level_interactive_states(
            page, click_options=self._fast_options()
        )

        self.assertEqual(result["status"], "partial")
        self.assertIn(
            "depth_limit_reached",
            [item["reason"] for item in result["truncations"]],
        )
        self.assertFalse(
            any(len(state["state_path"]) > 2 for state in result["interactive_states"])
        )
        self.assertTrue(result["page_restored"])

    def test_more_than_six_actionable_options_is_truncated(self) -> None:
        page = self._new_page(self._many_groups_fixture(1, 8))

        result = traverse_top_level_interactive_states(
            page, click_options=self._fast_options()
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["non_default_states_captured"], 5)
        self.assertEqual(len(result["interactive_states"]), 6)
        option_limits = [
            item for item in result["truncations"]
            if item["reason"] == "option_limit_reached"
        ]
        self.assertEqual(len(option_limits), 1)
        self.assertEqual(len(option_limits[0]["skipped_options"]), 2)
        self.assertTrue(result["page_restored"])

    def test_page_stops_expanding_after_twelve_non_default_states(self) -> None:
        page = self._new_page(self._many_groups_fixture(3, 6))

        result = traverse_top_level_interactive_states(
            page, click_options=self._fast_options()
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["non_default_states_captured"], 12)
        self.assertIn(
            "state_limit_reached",
            [item["reason"] for item in result["truncations"]],
        )
        self.assertTrue(result["page_restored"])

    def test_visited_semantic_state_is_not_clicked_twice(self) -> None:
        page = self._new_page("""
        <main><section>
          <div role="tablist" aria-label="Repeated labels">
            <button role="tab" aria-selected="true" aria-controls="default"
                    onclick="selectTab(this)">Default</button>
            <button role="tab" aria-selected="false" aria-controls="repeat-a"
                    onclick="selectTab(this)">Repeat</button>
            <button role="tab" aria-selected="false" aria-controls="repeat-b"
                    onclick="selectTab(this)">Repeat</button>
          </div>
          <div id="default" role="tabpanel"><p>Default content</p></div>
          <div id="repeat-a" role="tabpanel" hidden><p>Repeated content</p></div>
          <div id="repeat-b" role="tabpanel" hidden><p>Repeated content</p></div>
        </section></main>
        <script>
          window.clickCount = 0;
          function selectTab(target) {
            window.clickCount += 1;
            const tabs = target.closest('[role="tablist"]')
              .querySelectorAll(':scope > [role="tab"]');
            tabs.forEach((tab) => {
              const selected = tab === target;
              tab.setAttribute('aria-selected', selected ? 'true' : 'false');
              document.getElementById(tab.getAttribute('aria-controls')).hidden =
                !selected;
            });
          }
        </script>
        """)

        result = traverse_top_level_interactive_states(
            page, click_options=self._fast_options()
        )

        self.assertEqual(result["non_default_states_captured"], 1)
        self.assertEqual(page.evaluate("window.clickCount"), 2)
        self.assertIn("visited_state", [item["reason"] for item in result["skipped"]])
        self.assertTrue(result["page_restored"])

    def test_child_restore_failure_aborts_whole_page(self) -> None:
        page = self._new_page("""
        <main>
          <div role="tablist" aria-label="Parent">
            <button role="tab" aria-selected="true" aria-controls="base"
                    onclick="selectTab(this)">Base</button>
            <button role="tab" aria-selected="false" aria-controls="advanced"
                    onclick="selectTab(this)">Advanced</button>
          </div>
          <div id="base" role="tabpanel"><p>Base content</p></div>
          <div id="advanced" role="tabpanel" hidden>
            <div role="tablist" aria-label="Child">
              <button id="child-default" role="tab" aria-selected="true"
                      aria-controls="child-month" onclick="selectTab(this)">Month</button>
              <button id="child-other" role="tab" aria-selected="false"
                      aria-controls="child-year" onclick="selectTab(this)">Year</button>
            </div>
            <div id="child-month" role="tabpanel"><p>Child month</p></div>
            <div id="child-year" role="tabpanel" hidden><p>Child year</p></div>
          </div>
        </main>
        <script>
          window.childLeftDefault = false;
          function selectTab(target) {
            if (target.id === 'child-default' && window.childLeftDefault) return;
            if (target.id === 'child-other') window.childLeftDefault = true;
            const tabs = target.closest('[role="tablist"]')
              .querySelectorAll(':scope > [role="tab"]');
            tabs.forEach((tab) => {
              const selected = tab === target;
              tab.setAttribute('aria-selected', selected ? 'true' : 'false');
              document.getElementById(tab.getAttribute('aria-controls')).hidden =
                !selected;
            });
          }
        </script>
        """)

        result = traverse_top_level_interactive_states(
            page, click_options=self._fast_options()
        )

        self.assertEqual(result["status"], "aborted")
        self.assertFalse(result["page_restored"])
        self.assertIn("selection_switch_timeout", [error["code"] for error in result["errors"]])

    def test_group_failure_restores_default_and_continues_next_group(self) -> None:
        groups = [self._group(0, "First"), self._group(1, "Second")]
        click_results = [self._success_click(0), self._success_click(1)]
        restore_results = [self._success_click(0), self._success_click(1)]
        capture_results = [
            {"success": False, "error": {"code": "state_blocks_unavailable", "message": "empty"}},
            {"success": True, "interactive_state": self._state("first-default", "a", is_default=True)},
            {"success": True, "interactive_state": self._state("second-other", "b", is_default=False)},
            {"success": True, "interactive_state": self._state("second-default", "c", is_default=True)},
        ]
        success = {"success": True, "error": None, "runtime_locator": {"dom_path": ["body", "main"]}}

        with (
            patch("backend.interactive_traversal.discover_safe_tab_groups", return_value=groups),
            patch("backend.interactive_traversal.capture_local_scope_baseline", return_value={"success": True}),
            patch("backend.interactive_traversal.click_safe_tab", side_effect=click_results),
            patch("backend.interactive_traversal.restore_default_tab", side_effect=restore_results),
            patch("backend.interactive_traversal.resolve_local_scope", return_value=success),
            patch("backend.interactive_traversal.capture_interactive_state", side_effect=capture_results),
        ):
            result = traverse_top_level_interactive_states(object())

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["groups_completed"], 1)
        self.assertTrue(result["page_restored"])
        self.assertEqual(result["non_default_states_captured"], 1)
        self.assertIn("state_blocks_unavailable", [error["code"] for error in result["errors"]])

    def test_restore_failure_aborts_whole_page(self) -> None:
        group = self._group(0)
        success = {"success": True, "runtime_locator": {"dom_path": ["body", "main"]}}
        with (
            patch("backend.interactive_traversal.discover_safe_tab_groups", return_value=[group]),
            patch("backend.interactive_traversal.capture_local_scope_baseline", return_value={"success": True}),
            patch("backend.interactive_traversal.click_safe_tab", return_value=self._success_click()),
            patch("backend.interactive_traversal.resolve_local_scope", return_value=success),
            patch("backend.interactive_traversal.capture_interactive_state", return_value={"success": True, "interactive_state": self._state("other", "a", is_default=False)}),
            patch("backend.interactive_traversal.restore_default_tab", return_value={"success": False, "error": {"code": "selection_switch_timeout", "message": "restore failed"}}),
        ):
            result = traverse_top_level_interactive_states(object())

        self.assertEqual(result["status"], "aborted")
        self.assertFalse(result["page_restored"])
        self.assertEqual(result["groups_completed"], 0)

    def test_identical_state_key_and_hash_is_deduplicated(self) -> None:
        group = self._group(0)
        success = {"success": True, "runtime_locator": {"dom_path": ["body", "main"]}}
        duplicate = self._state("same", "same-hash", is_default=False)
        with (
            patch("backend.interactive_traversal.discover_safe_tab_groups", return_value=[group]),
            patch("backend.interactive_traversal.capture_local_scope_baseline", return_value={"success": True}),
            patch("backend.interactive_traversal.click_safe_tab", return_value=self._success_click()),
            patch("backend.interactive_traversal.restore_default_tab", return_value=self._success_click()),
            patch("backend.interactive_traversal.resolve_local_scope", return_value=success),
            patch("backend.interactive_traversal.capture_interactive_state", side_effect=[
                {"success": True, "interactive_state": duplicate},
                {"success": True, "interactive_state": dict(duplicate)},
            ]),
        ):
            result = traverse_top_level_interactive_states(object())

        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["interactive_states"]), 1)
        self.assertEqual(result["non_default_states_captured"], 1)

    def test_same_state_key_with_different_hash_reports_unstable_capture(self) -> None:
        group = self._group(0)
        success = {"success": True, "runtime_locator": {"dom_path": ["body", "main"]}}
        with (
            patch("backend.interactive_traversal.discover_safe_tab_groups", return_value=[group]),
            patch("backend.interactive_traversal.capture_local_scope_baseline", return_value={"success": True}),
            patch("backend.interactive_traversal.click_safe_tab", return_value=self._success_click()),
            patch("backend.interactive_traversal.restore_default_tab", return_value=self._success_click()),
            patch("backend.interactive_traversal.resolve_local_scope", return_value=success),
            patch("backend.interactive_traversal.capture_interactive_state", side_effect=[
                {"success": True, "interactive_state": self._state("same", "hash-a", is_default=False)},
                {"success": True, "interactive_state": self._state("same", "hash-b", is_default=True)},
            ]),
        ):
            result = traverse_top_level_interactive_states(object())

        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["interactive_states"]), 1)
        self.assertEqual(result["errors"][-1]["code"], "unstable_state_capture")
        self.assertTrue(result["page_restored"])

    def test_navigation_risk_aborts_without_trying_restore(self) -> None:
        group = self._group(0)
        with (
            patch("backend.interactive_traversal.discover_safe_tab_groups", return_value=[group]),
            patch("backend.interactive_traversal.capture_local_scope_baseline", return_value={"success": True}),
            patch("backend.interactive_traversal.click_safe_tab", return_value={"success": False, "error": {"code": "url_changed", "message": "navigated"}}),
            patch("backend.interactive_traversal.restore_default_tab") as restore,
        ):
            result = traverse_top_level_interactive_states(object())

        self.assertEqual(result["status"], "aborted")
        self.assertFalse(result["page_restored"])
        restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
