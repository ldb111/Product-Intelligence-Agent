"""最终 35 条 Event Detection 独立留出集的本地完整性测试。

测试只读取两份冻结 JSONL 并校验人工 Gold，不调用 Event Detector、LLM 或网络。
"""

from __future__ import annotations

import copy
import unittest
from collections import Counter
from pathlib import Path

from evals.event_detection_schema import (
    EVENT_TYPES,
    EventDetectionGoldValidationError,
    load_and_validate,
    read_jsonl,
    validate_supplement_records,
)


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_PATH = ROOT / "evals" / "gold" / "event_detection_fresh_v0.1.jsonl"
SUPPLEMENT_PATH = ROOT / "evals" / "gold" / "event_detection_fresh_supplement_v0.1.jsonl"


class EventDetectionSupplementTests(unittest.TestCase):
    """保护五条 Supplement Gold 及最终 35 条留出集的冻结事实。"""

    @classmethod
    def setUpClass(cls) -> None:
        """读取并校验两份正式 Gold；测试过程只使用内存副本，不写回文件。"""
        cls.original = load_and_validate(ORIGINAL_PATH)
        cls.supplement = validate_supplement_records(read_jsonl(SUPPLEMENT_PATH))

    def test_five_unique_selection_targets(self) -> None:
        """五条补充记录保留最初的选材目标；选材字段不等同于人工 Gold。"""
        self.assertEqual(len(self.supplement), 5)
        self.assertEqual(
            {record["metadata"]["target_coverage_type"] for record in self.supplement},
            {"pricing", "plan_packaging", "access_permission", "commercialization", "positioning"},
        )

    def test_supplement_gold_is_ready_with_human_review(self) -> None:
        """五条用户确认结果均为 event，并如实记录 AI 辅助与最终人工复核。"""
        for record in self.supplement:
            metadata = record["metadata"]
            self.assertEqual(metadata["annotation_status"], "ready")
            self.assertEqual(metadata["annotated_by"], "ai_assisted_human_review")
            self.assertEqual(metadata["annotation_method"], "ai_assisted_human_review")
            self.assertEqual(record["gold"]["decision"], "event")
        self.assertEqual(
            Counter(record["gold"]["event_type"] for record in self.supplement),
            {"pricing": 1, "plan_packaging": 1, "access_permission": 1,
             "commercialization": 1, "positioning": 1},
        )

    def test_frozen_evidence_and_unknown_fields_are_preserved(self) -> None:
        """Gold 只取自冻结摘录；未知旧状态保留空，WPS 回顾材料不虚构事件日期。"""
        event_times = {}
        for record in self.supplement:
            metadata = record["metadata"]
            self.assertIs(metadata["created_before_model_evaluation"], True)
            self.assertIs(metadata["contaminated"], False)
            self.assertEqual(metadata["evaluation_use"], "independent_holdout")
            self.assertEqual(metadata["path"], "explicit_statement")
            self.assertIsNone(record["input"]["previous_state"])
            self.assertIsNone(record["provenance"]["previous_source"])
            excerpt = record["provenance"]["current_source"]["excerpt"]
            self.assertEqual(record["input"]["explicit_statement"]["content"], excerpt)
            for item in record["gold"]["change_items"]:
                self.assertIsNone(item["previous_state"])
                self.assertIn(item["current_state"], excerpt)
            event_times[metadata["case_id"]] = record["gold"]["event_time"]
        self.assertEqual(
            event_times,
            {"EDS-001": "2024-08-02", "EDS-002": "2025-05-13",
             "EDS-003": "2026-08-23", "EDS-004": "2024-08-01", "EDS-005": None},
        )

    def test_final_35_integrity_and_event_type_coverage(self) -> None:
        """两份 Gold 合计 35 条、身份全局唯一，并覆盖全部八种正例类型。"""
        records = self.original + self.supplement
        case_ids = [record["metadata"]["case_id"] for record in records]
        self.assertEqual(len(records), 35)
        self.assertEqual(len(case_ids), len(set(case_ids)))
        self.assertEqual(
            Counter(record["gold"]["decision"] for record in records),
            {"event": 17, "no_event": 13, "uncertain": 5},
        )
        self.assertEqual(
            {record["gold"]["event_type"] for record in records
             if record["gold"]["decision"] == "event"},
            EVENT_TYPES,
        )
        for record in records:
            metadata = record["metadata"]
            self.assertEqual(metadata["annotation_status"], "ready")
            self.assertEqual(metadata["evaluation_use"], "independent_holdout")
            self.assertIs(metadata["contaminated"], False)

    def test_missing_case_is_rejected(self) -> None:
        """少一条不能宣称五种补充覆盖已完整冻结。"""
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_supplement_records(copy.deepcopy(self.supplement[:-1]))

    def test_duplicate_coverage_target_is_rejected(self) -> None:
        """不同 Case 不能被改成同一个选材目标来伪造覆盖。"""
        records = copy.deepcopy(self.supplement)
        records[1]["metadata"]["target_coverage_type"] = "pricing"
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_supplement_records(records)

    def test_not_created_before_evaluation_is_rejected(self) -> None:
        """补充记录必须保留模型评测前创建的冻结事实。"""
        records = copy.deepcopy(self.supplement)
        records[0]["metadata"]["created_before_model_evaluation"] = False
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_supplement_records(records)


if __name__ == "__main__":
    unittest.main()
