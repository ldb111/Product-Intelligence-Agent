"""Stage 1 V0.2-5 Contextual Diff（带上下文差异）的自动化测试。"""

from __future__ import annotations

import unittest

from backend.content_diff import build_diff_result


def _heading(level: int, text: str) -> dict:
    return {"type": "heading", "level": level, "text": text, "links": []}


def _paragraph(text: str) -> dict:
    return {"type": "paragraph", "text": text, "links": []}


def _list(items: list[dict], ordered: bool = False) -> dict:
    return {"type": "list", "ordered": ordered, "items": items}


def _item(text: str, children: list[dict] | None = None) -> dict:
    return {"text": text, "links": [], "children": children or []}


class ContextualDiffTests(unittest.TestCase):
    """用通用 Block 数据验证上下文恢复，不依赖真实网站或专用解析器。"""

    def _changed_result(
        self,
        previous_blocks: list[dict] | None,
        current_blocks: list[dict] | None,
        *,
        previous_content: str = "previous content",
        current_content: str = "current content",
        changed: bool | None = True,
        is_first_scan: bool = False,
        comparison_skipped_reason: str | None = None,
    ) -> dict:
        """构造可直接交给 build_diff_result 的 Change Detection 输出。"""
        previous_snapshot = None
        if not is_first_scan:
            previous_snapshot = {"content": previous_content}
            if previous_blocks is not None:
                previous_snapshot["blocks"] = previous_blocks

        current_snapshot = {"content": current_content}
        if current_blocks is not None:
            current_snapshot["blocks"] = current_blocks

        return {
            "is_first_scan": is_first_scan,
            "changed": changed,
            "comparison_skipped_reason": comparison_skipped_reason,
            "previous_content_hash": None if is_first_scan else "a" * 64,
            "current_content_hash": "b" * 64,
            "previous_snapshot": previous_snapshot,
            "current_snapshot": current_snapshot,
        }

    def test_kimi_work_paragraph_change_keeps_heading_path(self) -> None:
        previous_blocks = [
            _heading(1, "Products"),
            _heading(2, "Kimi Work"),
            _paragraph("Handle complex projects."),
        ]
        current_blocks = [
            _heading(1, "Products"),
            _heading(2, "Kimi Work"),
            _paragraph("Handle complex multi-file engineering projects end to end."),
        ]

        result = build_diff_result(
            self._changed_result(previous_blocks, current_blocks)
        )

        change = result["contextual_diff"][0]
        self.assertEqual(change["block_type"], "paragraph")
        self.assertEqual(change["change_type"], "modified")
        self.assertEqual(change["previous"], "Handle complex projects.")
        self.assertIn("multi-file", change["current"])
        self.assertEqual(
            [heading["text"] for heading in change["heading_path"]],
            ["Products", "Kimi Work"],
        )

    def test_table_cell_change_keeps_headers_row_and_column(self) -> None:
        headers = ["Feature", "Andante", "Moderato", "Allegretto", "Allegro"]
        previous_table = {
            "type": "table",
            "headers": headers,
            "rows": [["Number of projects", "20", "20", "20", "100"]],
        }
        current_table = {
            "type": "table",
            "headers": headers,
            "rows": [["Number of projects", "20", "20", "20", "200"]],
        }

        result = build_diff_result(
            self._changed_result(
                [_heading(2, "Membership plans"), previous_table],
                [_heading(2, "Membership plans"), current_table],
            )
        )

        table_change = result["contextual_diff"][0]
        row_change = table_change["changed_rows"][0]
        cell_change = row_change["changed_cells"][0]
        self.assertEqual(table_change["headers"], headers)
        self.assertEqual(row_change["row_key"], "Number of projects")
        self.assertEqual(cell_change["column"], "Allegro")
        self.assertEqual(cell_change["previous"], "100")
        self.assertEqual(cell_change["current"], "200")
        self.assertEqual(table_change["heading_path"][0]["text"], "Membership plans")

    def test_release_notes_added_item_keeps_version_and_category_context(self) -> None:
        shared_prefix = [
            _heading(1, "Release Notes"),
            _heading(2, "Version 2.1.0"),
            _paragraph("New"),
        ]
        previous_list = _list([_item("Existing feature")])
        current_list = _list(
            [_item("Existing feature"), _item("Added scheduled task support")]
        )

        result = build_diff_result(
            self._changed_result(
                [*shared_prefix, previous_list],
                [*shared_prefix, current_list],
            )
        )

        list_change = result["contextual_diff"][0]
        added_item = list_change["item_changes"][0]
        self.assertEqual(
            [heading["text"] for heading in list_change["heading_path"]],
            ["Release Notes", "Version 2.1.0"],
        )
        self.assertEqual(list_change["preceding_context"], "New")
        self.assertEqual(added_item["change_type"], "added")
        self.assertEqual(added_item["current"], "Added scheduled task support")

    def test_changelog_added_item_keeps_date_and_subject_context(self) -> None:
        shared_prefix = [
            _heading(1, "我们最近做了什么。"),
            _paragraph("2026-07-14"),
            _heading(2, "营销与信任面更新"),
        ]
        previous_list = _list([_item("原有更新项")])
        current_list = _list(
            [_item("原有更新项"), _item("新增企业微信推送能力。")]
        )

        result = build_diff_result(
            self._changed_result(
                [*shared_prefix, previous_list],
                [*shared_prefix, current_list],
            )
        )

        list_change = result["contextual_diff"][0]
        self.assertEqual(list_change["heading_path"][-1]["text"], "营销与信任面更新")
        self.assertEqual(list_change["preceding_context"], "营销与信任面更新")
        self.assertIn(
            {"type": "paragraph", "text": "2026-07-14"},
            list_change["context_blocks"],
        )
        self.assertEqual(
            list_change["item_changes"][0]["current"],
            "新增企业微信推送能力。",
        )

    def test_nested_list_child_change_keeps_parent_item(self) -> None:
        previous_list = _list(
            [_item("Fixed", [_list([_item("Resolve old routing issue")])])]
        )
        current_list = _list(
            [_item("Fixed", [_list([_item("Resolve multi-provider routing issue")])])]
        )

        result = build_diff_result(
            self._changed_result(
                [_heading(2, "Version 3.0"), previous_list],
                [_heading(2, "Version 3.0"), current_list],
            )
        )

        item_change = result["contextual_diff"][0]["item_changes"][0]
        self.assertEqual(item_change["change_type"], "modified")
        self.assertEqual(item_change["parent_item"], "Fixed")
        self.assertEqual(item_change["previous"], "Resolve old routing issue")
        self.assertEqual(item_change["current"], "Resolve multi-provider routing issue")

    def test_group_card_blocks_keep_existing_contextual_diff_behavior(self) -> None:
        def group_with_description(description: str) -> dict:
            return {
                "type": "group",
                "cards": [
                    {
                        "type": "card",
                        "title": "Product A",
                        "blocks": [
                            _heading(2, "Product A"),
                            _paragraph(description),
                        ],
                        "links": [],
                        "key_values": [],
                        "text_marks": [],
                    }
                ],
            }

        result = build_diff_result(
            self._changed_result(
                [group_with_description("Old description")],
                [group_with_description("New description")],
            )
        )

        change = result["contextual_diff"][0]
        self.assertEqual(change["block_type"], "paragraph")
        self.assertEqual(change["previous"], "Old description")
        self.assertEqual(change["current"], "New description")
        self.assertEqual(change["heading_path"][-1]["text"], "Product A")

    def test_non_comparable_and_legacy_snapshots_return_null_safely(self) -> None:
        scenarios = (
            self._changed_result(
                None,
                [_paragraph("First content")],
                is_first_scan=True,
                changed=None,
                previous_content="",
                current_content="First content",
            ),
            self._changed_result(
                [_paragraph("Old extraction")],
                [_paragraph("New extraction")],
                changed=None,
                comparison_skipped_reason="extraction_version_changed",
            ),
            self._changed_result(
                None,
                [_paragraph("New content with blocks")],
                previous_content="Legacy content without blocks",
            ),
        )

        for change_result in scenarios:
            with self.subTest(change_result=change_result):
                result = build_diff_result(change_result)
                self.assertIsNone(result["contextual_diff"])


if __name__ == "__main__":
    unittest.main()
