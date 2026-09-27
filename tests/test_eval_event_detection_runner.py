"""Event Detection Independent Eval Runner 的离线控制测试。

这些测试只验证数据装载、输入隔离、计分和报告规则，不调用真实模型或网络服务。
"""

from __future__ import annotations

import copy
import unittest

from evals.runners import event_detection_independent as runner


def _record(
    case_id: str,
    decision: str,
    *,
    path: str = "state_comparison",
    event_type: str | None = None,
    event_time: str | None = None,
    previous_state: bool = True,
) -> dict:
    """构造最小控制 Case，让测试只关注某一项评测规则。"""
    return {
        "metadata": {"case_id": case_id, "path": path},
        "input": {"previous_state": {"content": "旧状态"} if previous_state else None},
        "gold": {
            "decision": decision,
            "event_type": event_type,
            "event_subject": "测试对象" if decision == "event" else None,
            "change_action": "发生变化" if decision == "event" else None,
            "change_items": (
                [{"field": "测试字段", "previous_state": "旧状态", "current_state": "新状态"}]
                if decision == "event" else []
            ),
            "event_time": event_time,
            "effective_time": None,
            "affected_scope": None,
        },
    }


def _candidate(
    decision: str,
    *,
    event_type: str | None = None,
    event_time: str | None = None,
    previous_state: str | None = "旧状态",
    current_state: str | None = "新状态",
) -> dict:
    """构造已经通过生产 Schema 后的候选视图，供纯评分测试使用。"""
    is_event = decision == "event"
    return {
        "decision": decision,
        "event_type": event_type if is_event else None,
        "event_subject": "测试对象" if is_event else None,
        "change_action": "发生变化" if is_event else None,
        "change_items": (
            [{"field": "测试字段", "previous_state": previous_state, "current_state": current_state}]
            if is_event else []
        ),
        "event_time": event_time if is_event else None,
        "effective_time": None,
        "affected_scope": None,
        "decision_reason": "控制测试",
        "uncertainty_reason": "信息不足" if decision == "uncertain" else None,
        "citations": {},
    }


def _actual(
    decision: str,
    *,
    event_type: str | None = None,
    event_time: str | None = None,
    previous_state: str | None = "旧状态",
    current_state: str | None = "新状态",
) -> dict:
    """把单个候选包装成 Runner 的 Case 级归一化结果。"""
    candidate = _candidate(
        decision,
        event_type=event_type,
        event_time=event_time,
        previous_state=previous_state,
        current_state=current_state,
    )
    return {
        "decision": decision,
        "event_type": candidate["event_type"],
        "event_subject": candidate["event_subject"],
        "change_action": candidate["change_action"],
        "change_items": candidate["change_items"],
        "event_time": candidate["event_time"],
        "effective_time": candidate["effective_time"],
        "affected_scope": candidate["affected_scope"],
        "candidate_count": 1,
        "event_candidate_count": int(decision == "event"),
        "candidates": [candidate],
        "runtime": None,
    }


def _scored(record: dict, actual: dict) -> dict:
    """生成可直接进入汇总器的成功结果，并标明一次模型调用。"""
    value = runner.score_success_case(record, actual)
    value["model_calls"] = 1
    return value


class EventDetectionIndependentRunnerTests(unittest.TestCase):
    """覆盖 Runner V0.1 的数据、隔离、评分、失败与输出安全边界。"""

    @classmethod
    def setUpClass(cls) -> None:
        """一次读取冻结 Gold，避免每个测试重复启动 Git 子进程。"""
        cls.records = runner.load_dataset(verify_freeze=True)

    def test_a_loader_combines_30_original_and_5_supplement(self) -> None:
        self.assertEqual(len(self.records), 35)
        self.assertEqual(sum(r["metadata"]["case_id"].startswith("EDF-") for r in self.records), 30)
        self.assertEqual(sum(r["metadata"]["case_id"].startswith("EDS-") for r in self.records), 5)

    def test_b_loader_requires_ready_independent_holdout(self) -> None:
        for record in self.records:
            self.assertEqual(record["metadata"]["annotation_status"], "ready")
            self.assertEqual(record["metadata"]["evaluation_use"], "independent_holdout")
            self.assertIs(record["metadata"]["contaminated"], False)

    def test_c_case_ids_are_globally_unique(self) -> None:
        ids = [record["metadata"]["case_id"] for record in self.records]
        self.assertEqual(len(ids), len(set(ids)))

    def test_d_model_input_excludes_supplement_target_metadata(self) -> None:
        supplement = next(r for r in self.records if r["metadata"]["case_id"] == "EDS-001")
        model_input = runner.adapt_case_to_detection_input(supplement)
        keys = runner._walk_keys(model_input.model_dump(mode="json"))
        self.assertNotIn("target_coverage_type", keys)
        self.assertNotIn("selection_notes", keys)

    def test_e_model_input_excludes_gold_and_annotation_fields(self) -> None:
        for item in runner.build_execution_plan(self.records):
            keys = runner._walk_keys(item["model_input"].model_dump(mode="json"))
            self.assertTrue(keys.isdisjoint(runner.FORBIDDEN_MODEL_KEYS))
            self.assertEqual(item["leakage"]["status"], "PASS")

    def test_f_decision_exact_match_scoring(self) -> None:
        case = _record("CASE-F", "event", event_type="pricing")
        self.assertTrue(_scored(case, _actual("event", event_type="pricing"))["decision_correct"])
        self.assertFalse(_scored(case, _actual("uncertain"))["decision_correct"])

    def test_g_confusion_matrix(self) -> None:
        cases = [
            _scored(_record("G1", "event", event_type="pricing"), _actual("no_event")),
            _scored(_record("G2", "no_event"), _actual("event", event_type="pricing")),
            _scored(_record("G3", "uncertain"), _actual("uncertain")),
        ]
        matrix = runner.summarize_results(cases)["confusion_matrix"]
        self.assertEqual(matrix["event"]["no_event"], 1)
        self.assertEqual(matrix["no_event"]["event"], 1)
        self.assertEqual(matrix["uncertain"]["uncertain"], 1)

    def test_h_macro_f1(self) -> None:
        cases = [
            _scored(_record("H1", "event", event_type="pricing"), _actual("event", event_type="pricing")),
            _scored(_record("H2", "no_event"), _actual("no_event")),
            _scored(_record("H3", "uncertain"), _actual("uncertain")),
        ]
        self.assertEqual(runner.summarize_results(cases)["decision"]["macro_f1"], 1.0)

    def test_i_event_binary_metrics(self) -> None:
        cases = [
            _scored(_record("I1", "event", event_type="pricing"), _actual("event", event_type="pricing")),
            _scored(_record("I2", "event", event_type="pricing"), _actual("no_event")),
            _scored(_record("I3", "no_event"), _actual("event", event_type="pricing")),
        ]
        metric = runner.summarize_results(cases)["event_binary"]
        self.assertEqual((metric["true_positive"], metric["false_positive"], metric["false_negative"]), (1, 1, 1))
        self.assertEqual(metric["f1"], 0.5)

    def test_j_path_metrics(self) -> None:
        cases = [
            _scored(_record("J1", "no_event"), _actual("no_event")),
            _scored(_record("J2", "no_event", path="explicit_statement"), _actual("event", event_type="pricing")),
        ]
        paths = runner.summarize_results(cases)["path_metrics"]
        self.assertEqual(paths["state_comparison"]["accuracy"]["rate"], 1.0)
        self.assertEqual(paths["explicit_statement"]["accuracy"]["rate"], 0.0)

    def test_k_strict_event_type_accuracy_uses_all_gold_events(self) -> None:
        cases = [
            _scored(_record("K1", "event", event_type="pricing"), _actual("event", event_type="pricing")),
            _scored(_record("K2", "event", event_type="feature"), _actual("no_event")),
        ]
        strict = runner.summarize_results(cases)["event_type"]["strict_accuracy"]
        self.assertEqual((strict["matched"], strict["total"], strict["rate"]), (1, 2, 0.5))

    def test_l_conditional_event_type_accuracy_uses_predicted_events(self) -> None:
        cases = [
            _scored(_record("L1", "event", event_type="pricing"), _actual("event", event_type="pricing")),
            _scored(_record("L2", "event", event_type="feature"), _actual("no_event")),
        ]
        conditional = runner.summarize_results(cases)["event_type"]["conditional_accuracy"]
        self.assertEqual((conditional["matched"], conditional["total"], conditional["rate"]), (1, 1, 1.0))

    def test_m_event_time_exact_match(self) -> None:
        case = _record("CASE-M", "event", event_type="pricing", event_time="2026-10-01T00:00:00Z")
        scored = _scored(case, _actual("event", event_type="pricing", event_time="2026-10-01T00:00:00Z"))
        self.assertTrue(scored["checks"]["event_time_exact_match"])

    def test_n_unsupported_event_time_is_hard_violation(self) -> None:
        case = _record("CASE-N", "event", event_type="pricing", event_time=None)
        scored = _scored(case, _actual("event", event_type="pricing", event_time="2026-10-01T00:00:00Z"))
        self.assertIn("unsupported_event_time", scored["checks"]["hard_constraint_violations"])

    def test_o_unsupported_previous_state_is_hard_violation(self) -> None:
        case = _record("CASE-O", "event", event_type="pricing", previous_state=False)
        case["gold"]["change_items"][0]["previous_state"] = None
        scored = _scored(case, _actual("event", event_type="pricing", previous_state="模型虚构的旧状态"))
        self.assertIn("unsupported_previous_state", scored["checks"]["hard_constraint_violations"])

    def test_p_blocked_case_is_excluded_from_quality_denominator(self) -> None:
        success = _scored(_record("P1", "no_event"), _actual("no_event"))
        blocked_record = _record("P2", "event", event_type="pricing")
        blocked = runner._non_success_case(blocked_record, "BLOCKED", "LLMCallError", {"status": "PASS"}, model_calls=1)
        summary = runner.summarize_results([success, blocked])
        self.assertEqual((summary["evaluated_cases"], summary["blocked_cases"]), (1, 1))
        self.assertEqual(summary["decision"]["accuracy"]["total"], 1)

    def test_q_error_case_is_excluded_from_quality_denominator(self) -> None:
        success = _scored(_record("Q1", "no_event"), _actual("no_event"))
        error_record = _record("Q2", "event", event_type="pricing")
        error = runner._non_success_case(error_record, "ERROR", "EvidenceValidationError", {"status": "PASS"}, model_calls=1)
        summary = runner.summarize_results([success, error])
        self.assertEqual((summary["evaluated_cases"], summary["error_cases"]), (1, 1))
        self.assertEqual(summary["decision"]["accuracy"]["total"], 1)

    def test_r_all_bad_case_categories(self) -> None:
        expected = {
            ("event", "no_event", False): "event_missed_as_no_event",
            ("event", "uncertain", False): "event_missed_as_uncertain",
            ("no_event", "event", None): "no_event_false_positive_event",
            ("no_event", "uncertain", None): "no_event_predicted_uncertain",
            ("uncertain", "event", None): "uncertain_overclaimed_as_event",
            ("uncertain", "no_event", None): "uncertain_collapsed_to_no_event",
            ("event", "event", False): "event_type_mismatch",
        }
        for arguments, category in expected.items():
            with self.subTest(arguments=arguments):
                self.assertEqual(runner.classify_bad_case(*arguments), category)

    def test_s_field_review_queue_contains_semantic_fields_and_reasons(self) -> None:
        case = _record("CASE-S", "event", event_type="pricing")
        scored = _scored(case, _actual("event", event_type="feature"))
        item = runner.build_field_review_queue([scored])[0]
        self.assertEqual(item["case_id"], "CASE-S")
        self.assertIn("event_type_mismatch", item["review_reason"])
        self.assertIn("gold_change_items", item)
        self.assertIn("actual_change_items", item)

    def test_t_secret_field_guard(self) -> None:
        runner.assert_no_secret_fields({"runtime": {"token_usage": {"total_tokens": 10}}})
        with self.assertRaises(ValueError):
            runner.assert_no_secret_fields({"api_key": "must-never-be-written"})

    def test_filter_case_and_path(self) -> None:
        selected = runner.filter_cases(self.records, case_id="EDS-001", path="explicit_statement")
        self.assertEqual([item["metadata"]["case_id"] for item in selected], ["EDS-001"])
        with self.assertRaises(ValueError):
            runner.filter_cases(self.records, case_id="UNKNOWN")

    def test_gold_freeze_guard_detects_changed_objects(self) -> None:
        original_reader = runner.read_jsonl
        try:
            runner.read_jsonl = lambda path: [*original_reader(path), {"tampered": True}]
            with self.assertRaises(ValueError):
                runner.verify_gold_freeze()
        finally:
            runner.read_jsonl = original_reader

    def test_scoring_does_not_mutate_gold(self) -> None:
        case = _record("MUTATION", "event", event_type="pricing")
        before = copy.deepcopy(case)
        _scored(case, _actual("event", event_type="pricing"))
        self.assertEqual(case, before)


if __name__ == "__main__":
    unittest.main()
