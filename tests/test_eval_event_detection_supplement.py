"""补充候选的本地完整性测试；不调用模型、Event Detector 或网络。"""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from evals.event_detection_schema import (
    EventDetectionGoldValidationError,
    read_jsonl,
    validate_supplement_records,
)


ROOT = Path(__file__).resolve().parents[1]
SUPPLEMENT_PATH = ROOT / "evals" / "gold" / "event_detection_fresh_supplement_v0.1.jsonl"


class EventDetectionSupplementTests(unittest.TestCase):
    """保护五种选材目标与空 Gold 的边界，不判断任何候选是否真是事件。"""

    @classmethod
    def setUpClass(cls) -> None:
        """读取正式补充候选并校验，供测试副本使用，不写回原文件。"""
        cls.records = validate_supplement_records(read_jsonl(SUPPLEMENT_PATH))

    def test_five_unique_selection_targets(self) -> None:
        """真实补充集必须恰好五条，五种目标各一条；目标不是答案。"""
        self.assertEqual(len(self.records), 5)
        self.assertEqual({r["metadata"]["target_coverage_type"] for r in self.records},
                         {"pricing", "plan_packaging", "access_permission", "commercialization", "positioning"})

    def test_all_gold_fields_and_annotators_remain_empty(self) -> None:
        """包括备注在内的所有 Gold 都为空，防止用选材目标自动填写答案。"""
        for record in self.records:
            self.assertEqual(record["metadata"]["annotation_status"], "pending_human_review")
            self.assertIsNone(record["metadata"]["annotated_by"])
            self.assertIsNone(record["metadata"]["annotation_method"])
            for field, value in record["gold"].items():
                self.assertEqual(value, [] if field == "change_items" else None)

    def test_holdout_created_before_evaluation_with_traceable_sources(self) -> None:
        """来源和污染核查保留，来源日期不能冒充事件时间或虚构旧页面。"""
        for record in self.records:
            metadata = record["metadata"]
            self.assertIs(metadata["created_before_model_evaluation"], True)
            self.assertIs(metadata["contaminated"], False)
            self.assertEqual(metadata["evaluation_use"], "independent_holdout")
            self.assertEqual(metadata["path"], "explicit_statement")
            self.assertIsNone(record["input"]["previous_state"])
            self.assertIsNone(record["provenance"]["previous_source"])
            self.assertEqual(record["input"]["explicit_statement"]["content"],
                             record["provenance"]["current_source"]["excerpt"])
            self.assertEqual(metadata["freshness_check"]["history_matches"], 0)

    def test_missing_case_is_rejected(self) -> None:
        """少一条不能宣称五种覆盖目标已经全部备齐。"""
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_supplement_records(copy.deepcopy(self.records[:-1]))

    def test_duplicate_coverage_target_is_rejected(self) -> None:
        """不能把不同 ID 对应到同一选材目标以凑足数量。"""
        records = copy.deepcopy(self.records)
        records[1]["metadata"]["target_coverage_type"] = "pricing"
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_supplement_records(records)

    def test_prefilled_gold_is_rejected(self) -> None:
        """无论预填决策或类型，待标注数据都必须拒绝。"""
        for field, value in (("decision", "event"), ("event_type", "pricing")):
            records = copy.deepcopy(self.records)
            records[0]["gold"][field] = value
            with self.subTest(field=field), self.assertRaises(EventDetectionGoldValidationError):
                validate_supplement_records(records)

    def test_not_created_before_evaluation_is_rejected(self) -> None:
        """缺失或否认评测前创建的候选不能进入本轮补充集。"""
        records = copy.deepcopy(self.records)
        records[0]["metadata"]["created_before_model_evaluation"] = False
        with self.assertRaises(EventDetectionGoldValidationError):
            validate_supplement_records(records)


if __name__ == "__main__":
    unittest.main()
