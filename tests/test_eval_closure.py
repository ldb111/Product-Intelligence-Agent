"""最终收尾评测的离线边界测试，不请求外部模型。"""

import unittest
import copy
import json
from unittest.mock import Mock, patch

from evals.event_metrics import dates_in_text, event_time_check, summarize
from evals.runners import event_detection_closure as closure
from evals.runners import event_detection_independent as base
from event_core.evidence import EvidenceValidationError, validate_claims
from tests.test_event_core import data, decision_for


class ClosureEvaluationTests(unittest.TestCase):
    def test_final_freeze_guard_rejects_changed_evidence_or_nonempty_candidate(self):
        record = copy.deepcopy(base.load_dataset()[0])
        candidate = copy.deepcopy(record)
        candidate['metadata'].update(annotation_status='pending_expert_review', annotation_method=None, annotated_by=None)
        candidate['gold'] = {key: [] if key == 'change_items' else None for key in candidate['gold']}
        def git_result(args, **kwargs):
            return Mock(returncode=0, stdout=args[2] if args[1] == 'rev-parse' else '')
        with patch.object(closure.subprocess, 'run', side_effect=git_result), patch.object(base, '_git_text_at_commit', return_value=json.dumps(candidate)):
            closure.verify_freeze_order(base.ORIGINAL_GOLD, [record], 'rc', 'candidate', 'gold')
            changed = copy.deepcopy(record)
            changed['input']['competitor'] = 'changed'
            with self.assertRaisesRegex(ValueError, '模型输入'):
                closure.verify_freeze_order(base.ORIGINAL_GOLD, [changed], 'rc', 'candidate', 'gold')
        with patch.object(closure.subprocess, 'run', side_effect=git_result), patch.object(base, '_git_text_at_commit', return_value=json.dumps(record)):
            with self.assertRaisesRegex(ValueError, '答案为空'):
                closure.verify_freeze_order(base.ORIGINAL_GOLD, [record], 'rc', 'candidate', 'gold')

    def test_final_freeze_guard_rejects_same_or_reversed_commits(self):
        with patch.object(closure.subprocess, 'run', return_value=Mock(returncode=0, stdout='same')):
            with self.assertRaisesRegex(ValueError, '不同'):
                closure.verify_freeze_order(base.ORIGINAL_GOLD, [], 'rc', 'candidate', 'gold')
        def git_result(args, **kwargs):
            return Mock(returncode=1 if args[1] == 'merge-base' else 0,
                        stdout=args[2] if args[1] == 'rev-parse' else '')
        with patch.object(closure.subprocess, 'run', side_effect=git_result):
            with self.assertRaisesRegex(ValueError, '冻结顺序'):
                closure.verify_freeze_order(base.ORIGINAL_GOLD, [], 'rc', 'candidate', 'gold')

    def test_dates_are_calendar_valid_and_language_independent(self):
        self.assertEqual(dates_in_text("2026-09-20"), dates_in_text("September 20, 2026"))
        self.assertEqual(dates_in_text("2026年9月20日"), {"2026-09-20"})
        self.assertEqual(dates_in_text("2026-02-30; version 3.0.2; September 2026"), set())

    def test_actual_body_controls_date_eligibility(self):
        records = {r["metadata"]["case_id"]: r for r in base.load_dataset()}
        self.assertTrue(event_time_check(records["EDF-A-012"], {"event_time": "September 20, 2026"})["normalized_match"])
        for case_id in ("EDS-001", "EDS-002", "EDS-003", "EDS-004"):
            self.assertFalse(event_time_check(records[case_id], None)["eligible"])

    def test_missing_citation_diagnostic_has_field_without_original_text(self):
        inp = data()
        decision = decision_for(inp)
        decision.citations.pop("event_type")
        with self.assertRaises(EvidenceValidationError) as caught:
            validate_claims(inp, decision)
        diagnostic = caught.exception.diagnostic
        self.assertEqual(diagnostic["validation_rule"], "citation_missing")
        self.assertEqual(diagnostic["field_path"], "event_type")
        self.assertNotIn(inp.current_state, str(diagnostic))

    def test_missing_previous_is_not_relaxed(self):
        inp = data()
        decision = decision_for(inp)
        decision.change_items[0].previous_state = None
        decision.citations.pop("change_items.0.previous_state")
        with self.assertRaises(EvidenceValidationError) as caught:
            validate_claims(inp, decision)
        self.assertEqual(caught.exception.diagnostic["validation_rule"], "comparison_side_missing")

    def test_quote_wrong_side_and_invented_quote_remain_rejected(self):
        inp = data()
        decision = decision_for(inp)
        decision.citations["change_items.0.previous_state"][0].input_field = "current_state"
        with self.assertRaises(EvidenceValidationError):
            validate_claims(inp, decision)
        decision = decision_for(inp)
        decision.citations["event_type"][0].excerpt = "invented quote"
        with self.assertRaises(EvidenceValidationError) as caught:
            validate_claims(inp, decision)
        self.assertEqual(caught.exception.diagnostic["validation_rule"], "excerpt_not_found")

    def test_historical_rescore_uses_no_detector_and_exposes_all_denominators(self):
        records = base.load_dataset()
        source = base.REPOSITORY_ROOT / "tmp/evals/event_detection/20260927T093830303930Z/results.json"
        if not source.exists():
            self.skipTest("历史动态结果不随仓库分发")
        _, cases = closure.rescore(source, records)
        result = summarize(cases, records)
        self.assertEqual(result["execution_success_rate"], base._ratio(31, 35))
        self.assertEqual(result["end_to_end_correct_rate"], base._ratio(19, 35))
        self.assertEqual(result["event_time_eligible"]["eligible_case_ids"], ["EDF-A-012"])
        self.assertEqual(result["raw_semantic_diagnostic"]["coverage"], base._ratio(31, 35))

    def test_execute_captures_diagnostic_without_retry(self):
        item = base.build_execution_plan(base.load_dataset()[:1])[0]
        exc = EvidenceValidationError("固定约束", rule="citation_missing", field_path="event_type")
        exc.raw_decisions = {"candidates": [{"decision": "event"}]}
        detector = Mock(side_effect=exc)
        result = closure.execute(item, detector)
        detector.assert_called_once()
        self.assertEqual(result["execution_status"], "ERROR")
        self.assertEqual(result["validation_diagnostic"]["field_path"], "event_type")
        self.assertEqual(result["raw_decisions"], exc.raw_decisions)

    def test_blocked_does_not_become_semantic_no_event(self):
        item = base.build_execution_plan(base.load_dataset()[:1])[0]
        detector = Mock(side_effect=closure.LLMCallError("provider unavailable"))
        result = closure.execute(item, detector)
        self.assertEqual(result["execution_status"], "BLOCKED")
        self.assertIsNone(result["actual"])
        self.assertIsNone(result["decision_correct"])


if __name__ == "__main__":
    unittest.main()
