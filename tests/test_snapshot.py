"""Task 3 Snapshot（页面快照）创建与保存功能的自动化测试。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.snapshot import (
    build_snapshot_history,
    compute_content_hash,
    create_snapshot,
    find_previous_snapshot,
    save_snapshot,
)


class SnapshotTests(unittest.TestCase):
    """使用临时目录验证快照，不向正式 data/snapshots 写入任何测试文件。"""

    def setUp(self) -> None:
        self.page_data = {
            "url": "https://example.com/docs/product?id=1&language=zh",
            "status_code": 200,
            "title": "中文产品文档",
            "content": "第一行产品内容\n\n第二行产品内容",
        }

    def _snapshot_at(
        self, url: str, captured_at: str, content: str
    ) -> dict[str, object]:
        """构造时间可控的测试快照，避免测试依赖真实时钟。"""
        return {
            "url": url,
            "status_code": 200,
            "title": "测试页面",
            "content": content,
            "captured_at": captured_at,
            "content_hash": compute_content_hash(content),
        }

    def _write_named_snapshot(
        self, directory: Path, filename: str, snapshot: dict[str, object]
    ) -> None:
        """用指定文件名写入测试快照，证明查询结果不依赖文件名顺序。"""
        (directory / filename).write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
        )

    def test_compute_content_hash_returns_standard_sha256_hex_digest(self) -> None:
        # "hello" 的 SHA-256 是公开、固定的测试值。与它直接比较可以同时验证算法、
        # UTF-8 编码和十六进制输出，而不是只检查字符串长度。
        content_hash = compute_content_hash("hello")

        self.assertEqual(
            content_hash,
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        )
        self.assertRegex(content_hash, r"^[0-9a-f]{64}$")

    def test_content_hash_is_stable_and_changes_with_content(self) -> None:
        first_hash = compute_content_hash("相同的标准化正文")
        repeated_hash = compute_content_hash("相同的标准化正文")
        changed_hash = compute_content_hash("不同的标准化正文")

        self.assertEqual(first_hash, repeated_hash)
        self.assertNotEqual(first_hash, changed_hash)

    def test_snapshot_hash_ignores_metadata_and_capture_time(self) -> None:
        changed_metadata = {
            "url": "https://another.example.com/other-page",
            "status_code": 201,
            "title": "不同标题",
            "content": self.page_data["content"],
        }
        china_timezone = timezone(timedelta(hours=8))

        # 固定两次不同的采集时间，避免测试依赖计算机执行速度，并明确证明 captured_at
        # 不参与 content_hash 计算。
        with patch("backend.snapshot.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(
                2026, 9, 2, 14, 35, 20, tzinfo=china_timezone
            )
            first_snapshot = create_snapshot(self.page_data)

            mocked_datetime.now.return_value = datetime(
                2026, 9, 2, 14, 36, 20, tzinfo=china_timezone
            )
            second_snapshot = create_snapshot(changed_metadata)

        self.assertNotEqual(first_snapshot["url"], second_snapshot["url"])
        self.assertNotEqual(first_snapshot["status_code"], second_snapshot["status_code"])
        self.assertNotEqual(first_snapshot["title"], second_snapshot["title"])
        self.assertNotEqual(first_snapshot["captured_at"], second_snapshot["captured_at"])
        self.assertEqual(first_snapshot["content"], second_snapshot["content"])
        self.assertEqual(first_snapshot["content_hash"], second_snapshot["content_hash"])

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
            {
                "url",
                "status_code",
                "title",
                "content",
                "captured_at",
                "content_hash",
            },
        )
        for field in ("url", "status_code", "title", "content"):
            self.assertEqual(saved_snapshot[field], self.page_data[field])

        # fromisoformat 能解析该值，并且 utcoffset 不为 None，说明时间符合 ISO 8601
        # 且确实包含时区信息，而不是无法确定时区的本地时间字符串。
        captured_at = datetime.fromisoformat(saved_snapshot["captured_at"])
        self.assertIsNotNone(captured_at.utcoffset())
        self.assertEqual(
            saved_snapshot["content_hash"],
            compute_content_hash(self.page_data["content"]),
        )

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
                json.loads(first_path.read_text(encoding="utf-8"))["content_hash"],
                json.loads(second_path.read_text(encoding="utf-8"))["content_hash"],
            )
            self.assertEqual(
                json.loads(first_path.read_text(encoding="utf-8")),
                json.loads(second_path.read_text(encoding="utf-8")),
            )

    def test_finds_nearest_previous_snapshot_using_json_time(self) -> None:
        url = "https://example.com/product"
        snapshot_a = self._snapshot_at(url, "2026-09-02T10:00:00+08:00", "A")
        snapshot_b = self._snapshot_at(url, "2026-09-02T11:00:00+08:00", "B")
        current_snapshot = self._snapshot_at(
            url, "2026-09-02T12:00:00+08:00", "C"
        )
        future_snapshot = self._snapshot_at(
            url, "2026-09-02T13:00:00+08:00", "Future"
        )
        other_url_snapshot = self._snapshot_at(
            "https://other.example.com/product",
            "2026-09-02T11:59:00+08:00",
            "Other URL",
        )

        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            # 文件名字母顺序故意与时间顺序不一致，并把当前和未来快照也放进目录。
            self._write_named_snapshot(directory, "z_oldest.json", snapshot_a)
            self._write_named_snapshot(directory, "a_nearest.json", snapshot_b)
            self._write_named_snapshot(directory, "m_current.json", current_snapshot)
            self._write_named_snapshot(directory, "b_future.json", future_snapshot)
            self._write_named_snapshot(directory, "c_other_url.json", other_url_snapshot)

            previous_snapshot = find_previous_snapshot(current_snapshot, directory)
            history = build_snapshot_history(current_snapshot, directory)

        self.assertEqual(previous_snapshot, snapshot_b)
        self.assertFalse(history["is_first_scan"])
        self.assertEqual(history["current_snapshot"], current_snapshot)
        self.assertEqual(history["previous_snapshot"], snapshot_b)

    def test_first_scan_when_directory_missing_or_only_contains_current(self) -> None:
        current_snapshot = self._snapshot_at(
            "https://example.com/new-page",
            "2026-09-02T12:00:00+08:00",
            "First content",
        )

        with TemporaryDirectory() as temporary_directory:
            missing_directory = Path(temporary_directory) / "not-created"
            missing_directory_history = build_snapshot_history(
                current_snapshot, missing_directory
            )

            existing_directory = Path(temporary_directory) / "snapshots"
            existing_directory.mkdir()
            self._write_named_snapshot(
                existing_directory, "current-only.json", current_snapshot
            )
            current_only_history = build_snapshot_history(
                current_snapshot, existing_directory
            )

        for history in (missing_directory_history, current_only_history):
            self.assertTrue(history["is_first_scan"])
            self.assertEqual(history["current_snapshot"], current_snapshot)
            self.assertIsNone(history["previous_snapshot"])
