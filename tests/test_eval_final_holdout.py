"""最终留出数据的确定性边界检查；不调用模型，也不把答案反馈给生产系统。"""
import json
from pathlib import Path
import unittest

from evals.event_detection_schema import read_jsonl, validate_records
from evals.runners.event_detection_independent import build_execution_plan, load_dataset

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "evals/gold/event_detection_final_holdout_v1.jsonl"


class FinalHoldoutTests(unittest.TestCase):
    """分别验证数量、历史隔离、输入投影、原文对齐与标注生命周期。"""

    def setUp(self):
        self.records = validate_records(read_jsonl(DATA))

    def test_count_and_paths(self):
        self.assertEqual(len(self.records), 12)
        self.assertEqual(sum(r["metadata"]["path"] == "state_comparison" for r in self.records), 4)

    def test_separate_from_development_materials(self):
        old = load_dataset()
        old_ids = {r["metadata"]["case_id"] for r in old}
        old_urls = {s["url"] for r in old for s in r["provenance"].values() if s}
        for r in self.records:
            self.assertNotIn(r["metadata"]["case_id"], old_ids)
            self.assertNotIn(r["provenance"]["current_source"]["url"], old_urls)

    def test_leakage_guard_and_annotation_independence(self):
        original = build_execution_plan(self.records)
        # 故意替换评测答案和标注身份；实际生产输入哈希必须完全不受影响。
        changed = json.loads(json.dumps(self.records))
        for r in changed:
            r["gold"] = {"decision": "SENTINEL_GOLD"}
            r["metadata"]["annotated_by"] = "SENTINEL_REVIEWER"
        projected = build_execution_plan(changed)
        for a, b in zip(original, projected):
            self.assertEqual(a["leakage"]["status"], "PASS")
            self.assertEqual(a["leakage"]["model_input_sha256"], b["leakage"]["model_input_sha256"])

    def test_body_matches_preserved_excerpt(self):
        for r in self.records:
            for key, source_key in (("previous_state", "previous_source"), ("current_state", "current_source"), ("explicit_statement", "current_source")):
                if r["input"][key]:
                    self.assertEqual(r["input"][key]["content"], r["provenance"][source_key]["excerpt"])

    def test_consistent_annotation_stage(self):
        stages = {r["metadata"]["annotation_status"] for r in self.records}
        self.assertTrue(stages in ({"pending_expert_review"}, {"ready"}))
        for r in self.records:
            self.assertFalse(r["metadata"]["contaminated"])
            if stages == {"ready"}:
                self.assertEqual(r["metadata"]["annotation_method"], "independent_expert_model_review")
            else:
                self.assertIsNone(r["gold"]["decision"])


if __name__ == "__main__":
    unittest.main()
