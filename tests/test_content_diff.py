"""Task 7 Diff（差异比较）的自动化测试。"""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from backend.content_diff import build_content_diff, build_diff_result


class ContentDiffTests(unittest.TestCase):
    """验证只有 changed=true 时才生成清晰的行级 Unified Diff。"""

    def _change_result(
        self,
        *,
        is_first_scan: bool,
        changed: bool | None,
        previous_content: str | None,
        current_content: str,
        comparison_skipped_reason: str | None = None,
    ) -> dict[str, object]:
        """构造字段可控的 Task 6 结果，避免依赖文件或真实网页。"""
        previous_snapshot = (
            None if previous_content is None else {"content": previous_content}
        )
        return {
            "is_first_scan": is_first_scan,
            "changed": changed,
            "comparison_skipped_reason": comparison_skipped_reason,
            "previous_content_hash": None if is_first_scan else "a" * 64,
            "current_content_hash": "b" * 64,
            "previous_snapshot": previous_snapshot,
            "current_snapshot": {"content": current_content},
        }

    def test_first_scan_does_not_generate_diff(self) -> None:
        change_result = self._change_result(
            is_first_scan=True,
            changed=None,
            previous_content=None,
            current_content="功能 A",
        )

        # patch 证明首次采集分支没有偷偷调用实际 Diff 算法。
        with patch("backend.content_diff.build_content_diff") as mocked_diff:
            result = build_diff_result(change_result)

        self.assertIsNone(result["diff"])
        mocked_diff.assert_not_called()

    def test_unchanged_content_does_not_generate_diff(self) -> None:
        change_result = self._change_result(
            is_first_scan=False,
            changed=False,
            previous_content="功能 A",
            current_content="功能 A",
        )

        with patch("backend.content_diff.build_content_diff") as mocked_diff:
            result = build_diff_result(change_result)

        self.assertIsNone(result["diff"])
        mocked_diff.assert_not_called()

    def test_extraction_version_change_does_not_generate_false_diff(self) -> None:
        change_result = self._change_result(
            is_first_scan=False,
            changed=None,
            previous_content="旧抽取算法文本",
            current_content="新抽取算法文本",
            comparison_skipped_reason="extraction_version_changed",
        )

        with patch("backend.content_diff.build_content_diff") as mocked_diff:
            result = build_diff_result(change_result)

        self.assertFalse(result["is_first_scan"])
        self.assertIsNone(result["changed"])
        self.assertEqual(
            result["comparison_skipped_reason"], "extraction_version_changed"
        )
        self.assertIsNone(result["diff"])
        mocked_diff.assert_not_called()

    def test_diff_shows_added_line(self) -> None:
        diff = build_content_diff(
            "功能 A\n功能 B",
            "功能 A\n功能 B\n功能 C",
        )

        self.assertIn("+功能 C", diff)
        self.assertNotIn("-功能 C", diff)

    def test_diff_shows_deleted_line(self) -> None:
        diff = build_content_diff(
            "功能 A\n功能 B\n功能 C",
            "功能 A\n功能 B",
        )

        self.assertIn("-功能 C", diff)
        self.assertNotIn("+功能 C", diff)

    def test_diff_shows_replacement_and_addition(self) -> None:
        change_result = self._change_result(
            is_first_scan=False,
            changed=True,
            previous_content="支持 10 个项目",
            current_content="支持 20 个项目\n新增团队协作功能",
        )

        result = build_diff_result(change_result)

        self.assertIn("-支持 10 个项目", result["diff"])
        self.assertIn("+支持 20 个项目", result["diff"])
        self.assertIn("+新增团队协作功能", result["diff"])

    def test_diff_does_not_modify_snapshot_content(self) -> None:
        change_result = self._change_result(
            is_first_scan=False,
            changed=True,
            previous_content="旧内容",
            current_content="新内容",
        )
        original_change_result = copy.deepcopy(change_result)

        result = build_diff_result(change_result)

        self.assertEqual(change_result, original_change_result)
        self.assertEqual(
            result["previous_snapshot"]["content"],
            original_change_result["previous_snapshot"]["content"],
        )
        self.assertEqual(
            result["current_snapshot"]["content"],
            original_change_result["current_snapshot"]["content"],
        )
