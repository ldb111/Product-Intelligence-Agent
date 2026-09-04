"""Stage 1 V0.3-3C-2 Interactive State Capture 自动化测试。

测试在本地 Chromium 中完成真实安全点击和 Local Scope 解析，再调用生产 State Capture。
所有 HTML 都是可控夹具，不访问公网，也不写 Snapshot。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from backend.interactive_state import discover_safe_tab_groups
from backend.local_scope import capture_local_scope_baseline, resolve_local_scope
from backend.state_capture import capture_interactive_state
from backend.tab_interaction import click_safe_tab


class StateCaptureTests(unittest.TestCase):
    """验证局部结构抽取、语义身份边界和 Structured Blocks 内容哈希。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def _new_page(self, html: str):
        """为每个场景创建独立浏览器上下文，避免 DOM 状态互相影响。"""
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
    def _fixture(
        *,
        price: str = "200",
        price_markup: str | None = None,
        stylesheet: str = "",
        unrelated_before_panel: bool = False,
        semantic_scope: bool = True,
    ) -> str:
        """构造两状态三卡片页面，可调整业务值、Raw DOM 和运行时位置。"""
        scope_attribute = 'aria-label="Plan billing"' if semantic_scope else ""
        unrelated = (
            "<div>UNRELATED_BEFORE_LOCAL_SCOPE</div>"
            if unrelated_before_panel
            else ""
        )
        displayed_price = price_markup or f"Price {price}"
        cards = "".join(
            f"""
            <article>
              <h3>{title}</h3>
              <p>{displayed_price}</p>
              <dl><dt>Credits</dt><dd>{credits}</dd></dl>
            </article>
            """
            for title, credits in (
                ("Lite", "2,500"),
                ("Standard", "10,000"),
                ("Pro", "40,000"),
            )
        )
        return f"""
        {stylesheet}
        <main>
          <p>OUTSIDE_UNRELATED_CONTENT</p>
          <section>
            <div role="tablist" {scope_attribute}>
              <button role="tab" aria-selected="true"
                      aria-controls="monthly-panel"
                      onclick="switchPanel(this)">Monthly</button>
              <button role="tab" aria-selected="false"
                      aria-controls="quarterly-panel"
                      onclick="switchPanel(this)">Quarterly</button>
            </div>
            <div id="monthly-panel" role="tabpanel">
              <p>Monthly default</p>
            </div>
            {unrelated}
            <div id="quarterly-panel" role="tabpanel" hidden>
              <div>{cards}</div>
              <div style="display:none">HIDDEN_LOCAL_CONTENT</div>
            </div>
          </section>
        </main>
        <script>
          function switchPanel(target) {{
            const tabs = target.closest('[role="tablist"]')
              .querySelectorAll('[role="tab"]');
            tabs.forEach((tab) => {{
              const selected = tab === target;
              tab.setAttribute('aria-selected', selected ? 'true' : 'false');
              document.getElementById(tab.getAttribute('aria-controls')).hidden =
                !selected;
            }});
          }}
        </script>
        """

    def _run_capture(self, html: str) -> dict[str, object]:
        """串联 3A、3B、3C-1 与本次 3C-2，返回全部关键中间结果。"""
        page = self._new_page(html)
        group = discover_safe_tab_groups(page)[0]
        baseline = capture_local_scope_baseline(page, group)
        click_result = click_safe_tab(
            page, group, 1, **self._click_options()
        )
        local_scope_result = resolve_local_scope(
            page, group, baseline, click_result
        )
        capture_result = capture_interactive_state(
            page, group, click_result, local_scope_result
        )
        return {
            "page": page,
            "group": group,
            "click_result": click_result,
            "local_scope_result": local_scope_result,
            "capture_result": capture_result,
        }

    def test_local_scope_three_cards_become_structured_group(self) -> None:
        result = self._run_capture(self._fixture())

        capture = result["capture_result"]
        self.assertTrue(capture["success"])
        state = capture["interactive_state"]
        groups = [block for block in state["blocks"] if block["type"] == "group"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(
            [card["title"] for card in groups[0]["cards"]],
            ["Lite", "Standard", "Pro"],
        )
        self.assertEqual(state["scope_path"], ["Plan billing"])
        self.assertEqual(state["state_path"], ["Quarterly"])
        self.assertFalse(state["is_default"])
        self.assertEqual(len(state["content_hash"]), 64)
        self.assertIn("+", state["captured_at"])

    def test_scope_outside_and_hidden_content_do_not_enter_blocks(self) -> None:
        result = self._run_capture(self._fixture())

        state_text = str(result["capture_result"]["interactive_state"]["blocks"])
        self.assertNotIn("OUTSIDE_UNRELATED_CONTENT", state_text)
        self.assertNotIn("HIDDEN_LOCAL_CONTENT", state_text)
        # 局部可见性过滤在克隆上执行，不能删除真实页面中的隐藏节点。
        self.assertEqual(
            result["page"].locator("text=HIDDEN_LOCAL_CONTENT").count(), 1
        )

    def test_business_text_change_changes_content_hash(self) -> None:
        previous = self._run_capture(self._fixture(price="100"))
        current = self._run_capture(self._fixture(price="200"))

        self.assertNotEqual(
            previous["capture_result"]["interactive_state"]["content_hash"],
            current["capture_result"]["interactive_state"]["content_hash"],
        )

    def test_captured_at_does_not_change_content_hash(self) -> None:
        flow = self._run_capture(self._fixture())
        with patch(
            "backend.state_capture._current_captured_at",
            side_effect=[
                "2026-09-04T10:00:00+08:00",
                "2026-09-04T11:00:00+08:00",
            ],
        ):
            first = capture_interactive_state(
                flow["page"],
                flow["group"],
                flow["click_result"],
                flow["local_scope_result"],
            )["interactive_state"]
            second = capture_interactive_state(
                flow["page"],
                flow["group"],
                flow["click_result"],
                flow["local_scope_result"],
            )["interactive_state"]

        self.assertNotEqual(first["captured_at"], second["captured_at"])
        self.assertEqual(first["content_hash"], second["content_hash"])

    def test_runtime_dom_path_change_does_not_change_content_hash(self) -> None:
        original = self._run_capture(self._fixture())
        shifted = self._run_capture(
            self._fixture(unrelated_before_panel=True)
        )

        original_scope = original["local_scope_result"]["runtime_locator"]["dom_path"]
        shifted_scope = shifted["local_scope_result"]["runtime_locator"]["dom_path"]
        self.assertNotEqual(original_scope, shifted_scope)
        self.assertEqual(
            original["capture_result"]["interactive_state"]["content_hash"],
            shifted["capture_result"]["interactive_state"]["content_hash"],
        )
        self.assertEqual(
            original["capture_result"]["interactive_state"]["state_key"],
            shifted["capture_result"]["interactive_state"]["state_key"],
        )

    def test_different_raw_dom_with_same_blocks_has_same_hash(self) -> None:
        first = self._run_capture(
            self._fixture(price_markup="Price <span>200</span>")
        )
        second = self._run_capture(
            self._fixture(price_markup="<span>Price</span> 200")
        )

        first_state = first["capture_result"]["interactive_state"]
        second_state = second["capture_result"]["interactive_state"]
        self.assertEqual(first_state["blocks"], second_state["blocks"])
        self.assertEqual(first_state["content_hash"], second_state["content_hash"])

    def test_computed_strikethrough_participates_in_state_content_hash(self) -> None:
        """可见文字相同但展示语义不同，blocks 与 State 内容哈希都必须不同。"""
        plain = self._run_capture(
            self._fixture(price_markup='<span class="display-value">Price 200</span>')
        )
        struck = self._run_capture(
            self._fixture(
                price_markup='<span class="display-value">Price 200</span>',
                stylesheet=(
                    "<style>.display-value { "
                    "text-decoration-line: line-through; }</style>"
                ),
            )
        )

        plain_state = plain["capture_result"]["interactive_state"]
        struck_state = struck["capture_result"]["interactive_state"]
        struck_marks = struck_state["blocks"][0]["cards"][0]["text_marks"]
        self.assertNotIn(
            {"text": "Price 200", "marks": ["strikethrough"]},
            plain_state["blocks"][0]["cards"][0]["text_marks"],
        )
        self.assertIn(
            {"text": "Price 200", "marks": ["strikethrough"]},
            struck_marks,
        )
        self.assertNotEqual(plain_state["blocks"], struck_state["blocks"])
        self.assertNotEqual(plain_state["content_hash"], struck_state["content_hash"])

    def test_failed_local_scope_is_rejected(self) -> None:
        flow = self._run_capture(self._fixture())
        failed_scope = {
            "success": False,
            "error": {"code": "document_root_only", "message": "rejected"},
        }

        result = capture_interactive_state(
            flow["page"], flow["group"], flow["click_result"], failed_scope
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "local_scope_not_resolved")
        self.assertIsNone(result["interactive_state"])

    def test_unstable_semantic_state_is_rejected(self) -> None:
        flow = self._run_capture(self._fixture(semantic_scope=False))

        result = flow["capture_result"]

        self.assertFalse(result["success"])
        self.assertEqual(
            result["error"]["code"], "stable_semantic_state_unavailable"
        )
        self.assertIsNone(result["interactive_state"])

    def test_unvalidated_safe_click_is_rejected(self) -> None:
        flow = self._run_capture(self._fixture())

        result = capture_interactive_state(
            flow["page"],
            flow["group"],
            {"success": False, "error": {"code": "selection_switch_timeout"}},
            flow["local_scope_result"],
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "safe_click_not_validated")


if __name__ == "__main__":
    unittest.main()
