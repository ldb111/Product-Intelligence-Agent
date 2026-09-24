"""Legacy Gold 迁移数据的数量、标签和禁止字段完整性测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

from evals.schema import FORBIDDEN_STAGE_EXPECTED_FIELDS, load_and_validate
from source_discovery.schemas import MonitoringSource


ROOT = Path(__file__).resolve().parents[1]
SOURCE_GOLD = ROOT / "evals/gold/source_discovery_legacy_regression_v0.2.jsonl"
STAGE1_GOLD = ROOT / "evals/gold/stage1_legacy_regression_v0.2.jsonl"


def nested_keys(value):
    """递归收集 JSON 对象字段，确认 Stage 1 没有补造历史比较答案。"""
    keys = set()
    if isinstance(value, dict):
        keys.update(value)
        for child in value.values():
            keys.update(nested_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(nested_keys(child))
    return keys


class LegacyGoldIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_records = load_and_validate(SOURCE_GOLD, "source_discovery")
        cls.stage1_records = load_and_validate(STAGE1_GOLD, "stage1")

    def test_source_discovery_contains_four_legacy_cases(self):
        self.assertEqual(len(self.source_records), 4)
        self.assertEqual(
            {record["metadata"]["legacy_case_id"] for record in self.source_records},
            {"B01", "B02", "B03", "T01"},
        )

    def test_stage1_contains_eight_page_cases(self):
        self.assertEqual(len(self.stage1_records), 8)
        self.assertEqual(
            {record["metadata"]["legacy_case_id"] for record in self.stage1_records},
            {f"S1-{number:02d}" for number in range(1, 9)},
        )

    def test_case_ids_are_unique_within_each_asset(self):
        for records in (self.source_records, self.stage1_records):
            case_ids = [record["metadata"]["legacy_case_id"] for record in records]
            self.assertEqual(len(case_ids), len(set(case_ids)))

    def test_every_record_is_regression_only(self):
        for record in [*self.source_records, *self.stage1_records]:
            self.assertEqual(
                record["metadata"]["evaluation_use"], "regression_only"
            )

    def test_source_discovery_contains_seventeen_human_sources(self):
        self.assertEqual(
            sum(len(record["expected"]["sources"]) for record in self.source_records),
            17,
        )

    def test_legacy_importance_stays_evaluation_only(self):
        self.assertNotIn("legacy_importance", MonitoringSource.model_fields)
        for record in self.source_records:
            for source in record["expected"]["sources"]:
                self.assertIn(source["legacy_importance"], {"core", "supplementary"})

    def test_stage1_has_no_invented_historical_comparison_answers(self):
        for record in self.stage1_records:
            self.assertTrue(
                FORBIDDEN_STAGE_EXPECTED_FIELDS.isdisjoint(nested_keys(record))
            )


if __name__ == "__main__":
    unittest.main()

