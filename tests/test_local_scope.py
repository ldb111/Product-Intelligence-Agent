"""Stage 1 V0.3-3C-1 Local Scope Resolver 自动化测试。

测试使用本地 Chromium 构造可控交互页面，并真实调用冻结的 3A 发现与 3B 安全点击。
Local Scope 只输出运行时定位和诊断，不生成 blocks、Hash 或 Snapshot。
"""

from __future__ import annotations

import unittest

from playwright.sync_api import sync_playwright

from backend.interactive_state import discover_safe_tab_groups
from backend.local_scope import capture_local_scope_baseline, resolve_local_scope
from backend.tab_interaction import click_safe_tab


class LocalScopeResolverTests(unittest.TestCase):
    """验证 ARIA 优先、非控制内容差异兜底和文档根节点拒绝规则。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def _new_page(self, html: str):
        """为每个场景创建独立 context，避免弹窗或页面状态影响其他测试。"""
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_content(html)
        return page

    @staticmethod
    def _click_options() -> dict[str, int]:
        return {
            "selection_timeout_ms": 300,
            "stability_timeout_ms": 800,
            "stability_interval_ms": 20,
            "stable_observations": 2,
        }

    @staticmethod
    def _selector(result: dict[str, object]) -> str:
        """把 Resolver 返回的运行时分段路径转换为测试定位器。"""
        return " > ".join(result["runtime_locator"]["dom_path"])

    def test_aria_controls_and_tabpanel_are_preferred(self) -> None:
        page = self._new_page(
            """
            <section id="billing-scope">
              <div role="tablist" aria-label="Billing cycle">
                <button id="monthly-tab" role="tab" aria-selected="true"
                        aria-controls="monthly-panel"
                        onclick="switchPanel(this)">Monthly</button>
                <button id="yearly-tab" role="tab" aria-selected="false"
                        aria-controls="yearly-panel"
                        onclick="switchPanel(this)">Yearly</button>
              </div>
              <div id="monthly-panel" role="tabpanel"
                   aria-labelledby="monthly-tab">Monthly price</div>
              <div id="yearly-panel" role="tabpanel"
                   aria-labelledby="yearly-tab" hidden>Yearly price</div>
            </section>
            <script>
              function switchPanel(target) {
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
        )
        group = discover_safe_tab_groups(page)[0]
        baseline = capture_local_scope_baseline(page, group)
        click_result = click_safe_tab(page, group, 1, **self._click_options())

        result = resolve_local_scope(page, group, baseline, click_result)

        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "aria_controls")
        self.assertEqual(
            page.locator(self._selector(result)).get_attribute("id"),
            "yearly-panel",
        )
        self.assertNotIn("blocks", result)
        self.assertNotIn("content_hash", result)

    def test_changed_local_business_text_finds_smallest_common_container(self) -> None:
        page = self._new_page(
            """
            <section id="plan-scope">
              <div role="tablist" aria-label="Plan period">
                <button role="tab" aria-selected="true"
                        onclick="switchPlan(this, 'Monthly business content')">
                  Monthly
                </button>
                <button role="tab" aria-selected="false"
                        onclick="switchPlan(this, 'Yearly business content')">
                  Yearly
                </button>
              </div>
              <p id="business-content">Monthly business content</p>
            </section>
            <script>
              function switchPlan(target, content) {
                target.closest('[role="tablist"]')
                  .querySelectorAll('[role="tab"]')
                  .forEach((tab) => tab.setAttribute(
                    'aria-selected', tab === target ? 'true' : 'false'
                  ));
                document.getElementById('business-content').textContent = content;
              }
            </script>
            """
        )
        group = discover_safe_tab_groups(page)[0]
        baseline = capture_local_scope_baseline(page, group)
        click_result = click_safe_tab(page, group, 1, **self._click_options())

        result = resolve_local_scope(page, group, baseline, click_result)

        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "non_control_content_change")
        self.assertEqual(
            page.locator(self._selector(result)).get_attribute("id"),
            "plan-scope",
        )

    def test_tabpanel_aria_labelledby_relation_is_supported(self) -> None:
        page = self._new_page(
            """
            <section>
              <div role="tablist" aria-label="Report period">
                <button id="current-tab" role="tab" aria-selected="true"
                        onclick="switchPanel(this)">Current</button>
                <button id="history-tab" role="tab" aria-selected="false"
                        onclick="switchPanel(this)">History</button>
              </div>
              <div role="tabpanel" aria-labelledby="current-tab">Current report</div>
              <div role="tabpanel" aria-labelledby="history-tab" hidden>
                History report
              </div>
            </section>
            <script>
              function switchPanel(target) {
                target.closest('[role="tablist"]')
                  .querySelectorAll('[role="tab"]')
                  .forEach((tab) => tab.setAttribute(
                    'aria-selected', tab === target ? 'true' : 'false'
                  ));
                document.querySelectorAll('[role="tabpanel"]')
                  .forEach((panel) => {
                    panel.hidden = panel.getAttribute('aria-labelledby') !== target.id;
                  });
              }
            </script>
            """
        )
        group = discover_safe_tab_groups(page)[0]
        baseline = capture_local_scope_baseline(page, group)
        click_result = click_safe_tab(page, group, 1, **self._click_options())

        result = resolve_local_scope(page, group, baseline, click_result)

        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "tabpanel_aria_labelledby")
        self.assertEqual(
            page.locator(self._selector(result)).get_attribute("aria-labelledby"),
            "history-tab",
        )

    def test_only_aria_selected_change_is_rejected(self) -> None:
        page = self._new_page(
            """
            <section>
              <div role="tablist" aria-label="Display mode">
                <button role="tab" aria-selected="true"
                        onclick="selectOnly(this)">One</button>
                <button role="tab" aria-selected="false"
                        onclick="selectOnly(this)">Two</button>
              </div>
              <p>Same business content</p>
            </section>
            <script>
              function selectOnly(target) {
                target.closest('[role="tablist"]')
                  .querySelectorAll('[role="tab"]')
                  .forEach((tab) => tab.setAttribute(
                    'aria-selected', tab === target ? 'true' : 'false'
                  ));
              }
            </script>
            """
        )
        group = discover_safe_tab_groups(page)[0]
        baseline = capture_local_scope_baseline(page, group)
        click_result = click_safe_tab(page, group, 1, **self._click_options())

        result = resolve_local_scope(page, group, baseline, click_result)

        self.assertFalse(result["success"])
        self.assertEqual(
            result["error"]["code"], "no_non_control_content_change"
        )

    def test_scope_requiring_document_root_is_rejected(self) -> None:
        page = self._new_page(
            """
            <div role="tablist" aria-label="Root-level mode">
              <button role="tab" aria-selected="true"
                      onclick="switchRoot(this, 'First content')">First</button>
              <button role="tab" aria-selected="false"
                      onclick="switchRoot(this, 'Second content')">Second</button>
            </div>
            <p id="root-content">First content</p>
            <script>
              function switchRoot(target, content) {
                target.closest('[role="tablist"]')
                  .querySelectorAll('[role="tab"]')
                  .forEach((tab) => tab.setAttribute(
                    'aria-selected', tab === target ? 'true' : 'false'
                  ));
                document.getElementById('root-content').textContent = content;
              }
            </script>
            """
        )
        group = discover_safe_tab_groups(page)[0]
        baseline = capture_local_scope_baseline(page, group)
        click_result = click_safe_tab(page, group, 1, **self._click_options())

        result = resolve_local_scope(page, group, baseline, click_result)

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "document_root_only")

    def test_unchanged_unrelated_region_is_not_included(self) -> None:
        page = self._new_page(
            """
            <section id="target-scope">
              <div role="tablist" aria-label="Target mode">
                <button role="tab" aria-selected="true"
                        onclick="switchTarget(this, 'Old value')">Old</button>
                <button role="tab" aria-selected="false"
                        onclick="switchTarget(this, 'New value')">New</button>
              </div>
              <p id="target-content">Old value</p>
            </section>
            <aside id="unrelated-region">Unchanged unrelated information</aside>
            <script>
              function switchTarget(target, content) {
                target.closest('[role="tablist"]')
                  .querySelectorAll('[role="tab"]')
                  .forEach((tab) => tab.setAttribute(
                    'aria-selected', tab === target ? 'true' : 'false'
                  ));
                document.getElementById('target-content').textContent = content;
              }
            </script>
            """
        )
        group = discover_safe_tab_groups(page)[0]
        baseline = capture_local_scope_baseline(page, group)
        click_result = click_safe_tab(page, group, 1, **self._click_options())

        result = resolve_local_scope(page, group, baseline, click_result)

        self.assertTrue(result["success"])
        scope = page.locator(self._selector(result))
        self.assertEqual(scope.get_attribute("id"), "target-scope")
        self.assertNotIn("Unchanged unrelated information", scope.inner_text())


if __name__ == "__main__":
    unittest.main()
