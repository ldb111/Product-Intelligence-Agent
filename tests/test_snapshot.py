"""Task 3 Snapshot（页面快照）创建与保存功能的自动化测试。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.snapshot import create_snapshot, save_snapshot


class SnapshotTests(unittest.TestCase):
    """使用临时目录验证快照，不向正式 data/snapshots 写入任何测试文件。"""

    def setUp(self) -> None:
        self.page_data = {
            "url": "https://example.com/docs/product?id=1&language=zh",
            "status_code": 200,
            "title": "中文产品文档",
            "content": "第一行产品内容\n\n第二行产品内容",
        }

    def test_snapshot_can_be_saved_and_read_as_utf8_json(self) -> None:
        snapshot = create_snapshot(self.page_data)

        with TemporaryDirectory() as temporary_directory:
            snapshot_path = save_snapshot(snapshot, temporary_directory)
            raw_json = snapshot_path.read_text(encoding="utf-8")
            saved_snapshot = json.loads(raw_json)

            self.assertTrue(snapshot_path.exists())
            self.assertEqual(snapshot_path.parent, Path(temporary_directory))

        self.assertEqual(
            set(saved_snapshot),
            {"url", "status_code", "title", "content", "captured_at"},
        )
        for field in ("url", "status_code", "title", "content"):
            self.assertEqual(saved_snapshot[field], self.page_data[field])

        # fromisoformat 能解析该值，并且 utcoffset 不为 None，说明时间符合 ISO 8601
        # 且确实包含时区信息，而不是无法确定时区的本地时间字符串。
        captured_at = datetime.fromisoformat(saved_snapshot["captured_at"])
        self.assertIsNotNone(captured_at.utcoffset())

        # 中文直接存在于 UTF-8 文件中，证明没有被 JSON 转义成难以阅读的 \uXXXX。
        self.assertIn("中文产品文档", raw_json)
        self.assertIn("第一行产品内容", raw_json)

        windows_invalid_characters = '<>:"/\\|?*'
        self.assertTrue(
            all(character not in snapshot_path.name for character in windows_invalid_characters)
        )
        self.assertTrue(all(ord(character) >= 32 for character in snapshot_path.name))

    def test_saving_same_url_and_time_twice_does_not_overwrite(self) -> None:
        # 重复保存同一个 snapshot 会让 URL 和 captured_at 完全相同，能够稳定覆盖最容易
        # 发生文件名冲突的情况，比依赖两次系统时间恰好相同更可靠。
        snapshot = create_snapshot(self.page_data)

        with TemporaryDirectory() as temporary_directory:
            first_path = save_snapshot(snapshot, temporary_directory)
            second_path = save_snapshot(snapshot, temporary_directory)
            snapshot_files = list(Path(temporary_directory).glob("*.json"))

            self.assertNotEqual(first_path, second_path)
            self.assertTrue(first_path.exists())
            self.assertTrue(second_path.exists())
            self.assertEqual(len(snapshot_files), 2)
            self.assertEqual(
                json.loads(first_path.read_text(encoding="utf-8")),
                json.loads(second_path.read_text(encoding="utf-8")),
            )
