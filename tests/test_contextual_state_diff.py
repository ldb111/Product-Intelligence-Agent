"""Stage 1 V0.3-4B Contextual State Diff 自动化测试。"""

from __future__ import annotations

import unittest

from backend.contextual_state_diff import build_contextual_state_diff
from backend.snapshot import EXTRACTION_VERSION, INTERACTIVE_STATE_SCHEMA_VERSION
from backend.state_matching import match_interactive_states


def _heading(level: int, text: str) -> dict:
    return {"type": "heading", "level": level, "text": text, "links": []}


def _paragraph(text: str) -> dict:
    return {"type": "paragraph", "text": text, "links": []}


def _state(
    state_key: str,
    content_hash: str | None,
    blocks: list[dict],
    *,
    comparison_eligible: bool | None = None,
) -> dict:
    state = {
        "state_key": state_key,
        "scope_path": ["Membership"],
        "state_path": [state_key],
        "is_default": state_key == "monthly",
        "blocks": blocks,
        "content_hash": content_hash,
        "captured_at": "2026-09-07T10:00:00+08:00",
    }
    if comparison_eligible is not None:
        state["comparison_eligible"] = comparison_eligible
    return state


def _snapshot(states: list[dict]) -> dict:
    return {
        "extraction_version": EXTRACTION_VERSION,
        "interactive_state_schema_version": INTERACTIVE_STATE_SCHEMA_VERSION,
        "interactive_states": states,
        "interactive_state_traversal": {
            "status": "complete",
            "page_restored": True,
        },
    }


def _history(previous: dict | None, current: dict, *, first_scan: bool = False) -> dict:
    return {
        "is_first_scan": first_scan,
        "previous_snapshot": previous,
        "current_snapshot": current,
    }


def _build_diff(previous: dict, current: dict) -> tuple[dict, list[dict]]:
    history = _history(previous, current)
    comparison = match_interactive_states(history)
    return comparison, build_contextual_state_diff(history, comparison)


class ContextualStateDiffTests(unittest.TestCase):
    """使用可控 State Blocks 验证 4B，不访问网页或浏览器。"""

    def test_modified_paragraph_keeps_state_and_heading_context(self) -> None:
        previous = _snapshot(
            [
                _state(
                    "monthly",
                    "old-hash",
                    [_heading(2, "Monthly Plan"), _paragraph("Price 10")],
                )
            ]
        )
        current = _snapshot(
            [
                _state(
                    "monthly",
                    "new-hash",
                    [_heading(2, "Monthly Plan"), _paragraph("Price 12")],
                )
            ]
        )

        comparison, state_diffs = _build_diff(previous, current)

        self.assertTrue(comparison["changed"])
        self.assertEqual(len(state_diffs), 1)
        state_diff = state_diffs[0]
        paragraph_change = state_diff["contextual_diff"][0]
        self.assertEqual(state_diff["state_key"], "monthly")
        self.assertEqual(state_diff["scope_path"], ["Membership"])
        self.assertEqual(state_diff["state_path"], ["monthly"])
        self.assertEqual(state_diff["previous_hash"], "old-hash")
        self.assertEqual(state_diff["current_hash"], "new-hash")
        self.assertEqual(paragraph_change["previous"], "Price 10")
        self.assertEqual(paragraph_change["current"], "Price 12")
        self.assertEqual(
            paragraph_change["heading_path"][-1]["text"], "Monthly Plan"
        )

    def test_card_field_change_keeps_card_title_and_structured_values(self) -> None:
        def card_group(value: str) -> dict:
            return {
                "type": "group",
                "cards": [
                    {
                        "type": "card",
                        "title": "Pro",
                        "blocks": [
                            _heading(3, "Pro"),
                            _paragraph(f"Projects {value}"),
                        ],
                        "links": [],
                        "key_values": [{"key": "Projects", "value": value}],
                        "text_marks": [],
                    }
                ],
            }

        previous = _snapshot(
            [_state("yearly", "old-card-hash", [card_group("100")])]
        )
        current = _snapshot(
            [_state("yearly", "new-card-hash", [card_group("120")])]
        )

        _, state_diffs = _build_diff(previous, current)

        card_change = state_diffs[0]["contextual_diff"][0]
        self.assertEqual(card_change["block_type"], "card")
        self.assertEqual(card_change["card_title"], "Pro")
        self.assertEqual(
            card_change["previous"]["key_values"],
            [{"key": "Projects", "value": "100"}],
        )
        self.assertEqual(
            card_change["current"]["key_values"],
            [{"key": "Projects", "value": "120"}],
        )
        self.assertEqual(
            card_change["block_changes"][0]["previous"], "Projects 100"
        )
        self.assertEqual(
            card_change["block_changes"][0]["current"], "Projects 120"
        )

    def test_table_cell_change_reuses_headers_row_and_column_diff(self) -> None:
        headers = ["Feature", "Basic", "Pro"]
        previous_table = {
            "type": "table",
            "headers": headers,
            "rows": [["Projects", "20", "100"]],
        }
        current_table = {
            "type": "table",
            "headers": headers,
            "rows": [["Projects", "20", "120"]],
        }
        previous = _snapshot(
            [_state("yearly", "old-table-hash", [previous_table])]
        )
        current = _snapshot(
            [_state("yearly", "new-table-hash", [current_table])]
        )

        _, state_diffs = _build_diff(previous, current)

        table_change = state_diffs[0]["contextual_diff"][0]
        cell_change = table_change["changed_rows"][0]["changed_cells"][0]
        self.assertEqual(table_change["headers"], headers)
        self.assertEqual(cell_change["column"], "Pro")
        self.assertEqual(cell_change["previous"], "100")
        self.assertEqual(cell_change["current"], "120")

    def test_list_item_change_keeps_item_level_structure(self) -> None:
        previous_list = {
            "type": "list",
            "ordered": False,
            "items": [{"text": "Credits 100", "links": [], "children": []}],
        }
        current_list = {
            "type": "list",
            "ordered": False,
            "items": [{"text": "Credits 120", "links": [], "children": []}],
        }
        previous = _snapshot(
            [_state("monthly", "old-list-hash", [previous_list])]
        )
        current = _snapshot(
            [_state("monthly", "new-list-hash", [current_list])]
        )

        _, state_diffs = _build_diff(previous, current)

        item_change = state_diffs[0]["contextual_diff"][0]["item_changes"][0]
        self.assertEqual(item_change["change_type"], "modified")
        self.assertEqual(item_change["previous"], "Credits 100")
        self.assertEqual(item_change["current"], "Credits 120")

    def test_text_marks_change_is_preserved_inside_card(self) -> None:
        def marked_card(marks: list[dict]) -> dict:
            return {
                "type": "group",
                "cards": [
                    {
                        "type": "card",
                        "title": "Standard",
                        "blocks": [_paragraph("Price 100")],
                        "links": [],
                        "key_values": [],
                        "text_marks": marks,
                    }
                ],
            }

        previous = _snapshot(
            [_state("monthly", "plain-hash", [marked_card([])])]
        )
        current_marks = [{"text": "100", "marks": ["strikethrough"]}]
        current = _snapshot(
            [_state("monthly", "marked-hash", [marked_card(current_marks)])]
        )

        _, state_diffs = _build_diff(previous, current)

        card_change = state_diffs[0]["contextual_diff"][0]
        self.assertEqual(card_change["card_title"], "Standard")
        self.assertEqual(card_change["previous"]["text_marks"], [])
        self.assertEqual(card_change["current"]["text_marks"], current_marks)
        # 文字相同，因此基础段落没有变化；Card 结构仍准确保留展示语义变化。
        self.assertEqual(card_change["block_changes"], [])

    def test_only_modified_state_generates_detailed_diff(self) -> None:
        previous = _snapshot(
            [
                _state("monthly", "same-hash", [_paragraph("Price 10")]),
                _state("yearly", "old-hash", [_paragraph("Price 100")]),
            ]
        )
        current = _snapshot(
            [
                _state("monthly", "same-hash", [_paragraph("Price 10")]),
                _state("yearly", "new-hash", [_paragraph("Price 120")]),
            ]
        )

        comparison, state_diffs = _build_diff(previous, current)

        self.assertEqual(
            {change["state_key"]: change["change_type"] for change in comparison["state_changes"]},
            {"monthly": "unchanged", "yearly": "modified"},
        )
        self.assertEqual([diff["state_key"] for diff in state_diffs], ["yearly"])

    def test_navigation_unchanged_and_baseline_do_not_generate_diff(self) -> None:
        previous = _snapshot(
            [
                _state("navigation", None, [], comparison_eligible=False),
                _state("monthly", "same-hash", [_paragraph("Price 10")]),
            ]
        )
        current = _snapshot(
            [
                _state("navigation", None, [], comparison_eligible=False),
                _state("monthly", "same-hash", [_paragraph("Price 10")]),
            ]
        )

        comparison, state_diffs = _build_diff(previous, current)
        baseline_history = _history(None, current, first_scan=True)
        baseline_comparison = match_interactive_states(baseline_history)
        baseline_diffs = build_contextual_state_diff(
            baseline_history, baseline_comparison
        )

        self.assertFalse(comparison["changed"])
        self.assertEqual(state_diffs, [])
        self.assertEqual(baseline_diffs, [])


if __name__ == "__main__":
    unittest.main()
