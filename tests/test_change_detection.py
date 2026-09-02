"""Task 6 Change Detection（变化检测）的自动化测试。"""

from __future__ import annotations

import unittest

from backend.change_detection import ChangeDetectionError, detect_change


class ChangeDetectionTests(unittest.TestCase):
    """验证变化结论只由前后 content_hash 决定。"""

    def _snapshot(
        self,
        content_hash: str,
        *,
        captured_at: str,
        title: str = "页面标题",
        status_code: int = 200,
    ) -> dict[str, object]:
        """构造字段可控的合法快照，避免测试依赖文件或真实网页。"""
        return {
            "url": "https://example.com/product",
            "status_code": status_code,
            "title": title,
            "content": "标准化正文",
            "captured_at": captured_at,
            "content_hash": content_hash,
            "other_field": "不参与变化判断",
        }

    def test_first_scan_returns_unknown_change_without_previous_hash(self) -> None:
        current_snapshot = self._snapshot(
            "a" * 64, captured_at="2026-09-02T12:00:00+08:00"
        )
        history = {
            "is_first_scan": True,
            "current_snapshot": current_snapshot,
            "previous_snapshot": None,
        }

        result = detect_change(history)

        self.assertTrue(result["is_first_scan"])
        self.assertIsNone(result["changed"])
        self.assertIsNone(result["previous_content_hash"])
        self.assertEqual(result["current_content_hash"], "a" * 64)
        self.assertEqual(result["current_snapshot"], current_snapshot)
        self.assertIsNone(result["previous_snapshot"])

    def test_same_hash_means_unchanged_even_when_other_fields_differ(self) -> None:
        same_hash = "b" * 64
        previous_snapshot = self._snapshot(
            same_hash,
            captured_at="2026-09-02T11:00:00+08:00",
            title="旧标题",
            status_code=201,
        )
        current_snapshot = self._snapshot(
            same_hash,
            captured_at="2026-09-02T12:00:00+08:00",
            title="新标题",
            status_code=200,
        )
        current_snapshot["other_field"] = "其他字段也发生变化"

        result = detect_change(
            {
                "is_first_scan": False,
                "current_snapshot": current_snapshot,
                "previous_snapshot": previous_snapshot,
            }
        )

        self.assertFalse(result["is_first_scan"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["previous_content_hash"], same_hash)
        self.assertEqual(result["current_content_hash"], same_hash)

    def test_different_hash_means_changed(self) -> None:
        previous_snapshot = self._snapshot(
            "c" * 64, captured_at="2026-09-02T11:00:00+08:00"
        )
        current_snapshot = self._snapshot(
            "d" * 64, captured_at="2026-09-02T12:00:00+08:00"
        )

        result = detect_change(
            {
                "is_first_scan": False,
                "current_snapshot": current_snapshot,
                "previous_snapshot": previous_snapshot,
            }
        )

        self.assertTrue(result["changed"])
        self.assertEqual(result["previous_content_hash"], "c" * 64)
        self.assertEqual(result["current_content_hash"], "d" * 64)

    def test_non_first_scan_requires_both_content_hashes(self) -> None:
        valid_snapshot = self._snapshot(
            "e" * 64, captured_at="2026-09-02T12:00:00+08:00"
        )

        for missing_snapshot_name in ("current_snapshot", "previous_snapshot"):
            with self.subTest(missing_snapshot=missing_snapshot_name):
                snapshot_without_hash = dict(valid_snapshot)
                del snapshot_without_hash["content_hash"]
                history = {
                    "is_first_scan": False,
                    "current_snapshot": valid_snapshot,
                    "previous_snapshot": valid_snapshot,
                }
                history[missing_snapshot_name] = snapshot_without_hash

                with self.assertRaises(ChangeDetectionError) as raised:
                    detect_change(history)

                self.assertEqual(
                    raised.exception.error_type,
                    "invalid_change_detection_input",
                )
                self.assertIn("content_hash", str(raised.exception))
