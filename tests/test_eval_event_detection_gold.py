"""Event Detection Fresh Holdout 数据契约测试。

这些测试只验证本地 JSONL、来源与人工标注状态，不调用 Event Detector、LLM 或网络。
"""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from evals.event_detection_schema import (
    EventDetectionGoldValidationError,
    load_and_validate,
    validate_records,
)


ROOT = Path(__file__).resolve().parents[1]
GOLD_PATH = ROOT / "evals" / "gold" / "event_detection_fresh_v0.1.jsonl"


class EventDetectionFreshGoldTests(unittest.TestCase):
    """验证 Fresh Candidate 与未来人工 Gold 的最小完整性边界。"""

    @classmethod
    def setUpClass(cls) -> None:
        """读取一次正式候选集，供各测试深拷贝使用，避免修改磁盘数据。"""
        cls.records = load_and_validate(GOLD_PATH)

    def sample(self) -> dict:
        """返回一条隔离副本，便于故障注入不污染其他测试。"""
        return copy.deepcopy(self.records[0])

    def mark_ready(self, record: dict, decision: str) -> None:
        """把测试副本切换为人工已完成状态；调用方再填写对应答案字段。"""
        record["metadata"]["annotation_status"] = "ready"
        record["metadata"]["annotated_by"] = "human-reviewer"
        record["gold"]["decision"] = decision
        record["gold"]["annotation_notes"] = "本地契约测试"

    def test_candidate_file_has_unique_ids_and_expected_paths(self) -> None:
        """30 条候选必须保持唯一身份以及 18/12 的两路径配额。"""
        case_ids = [record["metadata"]["case_id"] for record in self.records]
        self.assertEqual(len(case_ids), 30)
        self.assertEqual(len(case_ids), len(set(case_ids)))
        self.assertEqual(
            sum(record["metadata"]["path"] == "state_comparison" for record in self.records),
            18,
        )
        self.assertEqual(
            sum(record["metadata"]["path"] == "explicit_statement" for record in self.records),
            12,
        )

    def test_invalid_path_is_rejected(self) -> None:
        """路径名必须使用当前 Event Core 的两条正式发现路径。"""
        record = self.sample()
        record["metadata"]["path"] = "unknown_path"
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_records([record])

    def test_duplicate_case_id_is_rejected(self) -> None:
        """同一文件内的 Case ID 必须唯一，避免标注与评分错位。"""
        first = self.sample()
        second = copy.deepcopy(self.records[1])
        second["metadata"]["case_id"] = first["metadata"]["case_id"]
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_records([first, second])

    def test_contaminated_case_is_rejected(self) -> None:
        """被开发资产污染的 Case 不能伪装成 Fresh Holdout。"""
        record = self.sample()
        record["metadata"]["contaminated"] = True
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_records([record])

    def test_pending_human_review_allows_empty_gold(self) -> None:
        """待人工标注记录可以保留空答案，但不能夹带模型或脚本结论。"""
        record = self.sample()
        self.assertEqual(record["metadata"]["annotation_status"], "pending_human_review")
        self.assertIsNone(record["gold"]["decision"])
        self.assertEqual(validate_records([record]), [record])

    def test_pending_human_review_rejects_prefilled_decision(self) -> None:
        """Candidate 阶段只要出现预填 decision 就立即失败。"""
        record = self.sample()
        record["gold"]["decision"] = "event"
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_records([record])

    def test_ready_event_requires_event_fields(self) -> None:
        """人工确认事件后必须提供合法类型、对象、动作和至少一个变化项。"""
        record = self.sample()
        self.mark_ready(record, "event")
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_records([record])

        record["gold"].update(
            {
                "event_type": "capability",
                "event_subject": "示例能力",
                "change_action": "新增",
                "change_items": [
                    {
                        "item": "示例能力",
                        "previous_state": None,
                        "current_state": "官方明确提供",
                    }
                ],
            }
        )
        self.assertEqual(validate_records([record]), [record])

    def test_ready_event_rejects_unknown_event_type(self) -> None:
        """人工 Event 只能使用当前 Event Core 已定义的业务类型。"""
        record = self.sample()
        self.mark_ready(record, "event")
        record["gold"].update(
            {
                "event_type": "marketing_campaign",
                "event_subject": "示例能力",
                "change_action": "新增",
                "change_items": [
                    {
                        "item": "示例能力",
                        "previous_state": None,
                        "current_state": "官方明确提供",
                    }
                ],
            }
        )
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_records([record])

    def test_no_event_rejects_event_fields(self) -> None:
        """no_event 不能同时携带看似已确认的 Event 事实。"""
        record = self.sample()
        self.mark_ready(record, "no_event")
        record["gold"]["event_subject"] = "不应存在的对象"
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_records([record])

    def test_uncertain_allows_partial_fields_to_remain_empty(self) -> None:
        """证据不足时允许 uncertain 保留未知事件字段为空。"""
        record = self.sample()
        self.mark_ready(record, "uncertain")
        self.assertIsNone(record["gold"]["event_type"])
        self.assertEqual(record["gold"]["change_items"], [])
        self.assertEqual(validate_records([record]), [record])

    def test_uncertain_rejects_confirmed_event_facts(self) -> None:
        """uncertain 只记录不确定原因，不能夹带已确认 Event 字段。"""
        record = self.sample()
        self.mark_ready(record, "uncertain")
        record["gold"]["event_subject"] = "证据不足的对象"
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_records([record])

    def test_event_time_may_be_null_for_ready_event(self) -> None:
        """来源未给出事件时间时不得拿 observation time 代填。"""
        record = self.sample()
        self.mark_ready(record, "event")
        record["gold"].update(
            {
                "event_type": "availability",
                "event_subject": "示例服务",
                "change_action": "正式开放",
                "change_items": [
                    {
                        "item": "示例服务",
                        "previous_state": None,
                        "current_state": "正式开放",
                    }
                ],
                "event_time": None,
            }
        )
        self.assertEqual(validate_records([record]), [record])

    def test_provenance_is_required(self) -> None:
        """Path A 缺少任一历史来源时，人工无法复核真实前后状态。"""
        record = self.sample()
        record["provenance"]["previous_source"] = None
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_records([record])


if __name__ == "__main__":
    unittest.main()
