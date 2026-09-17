"""Event Core 业务边界测试；模型输出为 Mock，不代表真实模型语义能力。"""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from pydantic import ValidationError

from backend.snapshot import EXTRACTION_VERSION, compute_content_hash
from backend.structured_content import serialize_blocks
from event_core.dedup import deduplicate_event
from event_core.detector import detect_event
from event_core.evidence import EvidenceValidationError, content_fingerprint
from event_core.inputs import build_explicit_statement_input, build_state_comparison_input
from event_core.prompts import EVENT_DETECTION_PROMPT, EVENT_DEDUP_PROMPT
from event_core.schemas import (
    ChangeItem, DedupDecision, DetectionDecisions, DetectionInput,
    EventDecision, Quote, StateContext,
)
from llm.client import LLMCallError, RuntimeRecord


def runtime() -> RuntimeRecord:
    return RuntimeRecord(role="reasoning", provider="test_provider", model="test_model",
                         response_model="test_model", latency_seconds=0, token_usage=None)


def data(path="state_comparison", old="Export supports CSV only.", new="Export supports CSV and PDF."):
    return DetectionInput(
        competitor="Example", source_url="https://example.com/product", source_title="Example Product",
        source_type="product_page", source_credibility="official", detection_path=path,
        state_context=StateContext(comparison_status="comparable"),
        previous_state=old, current_state=new, contextual_diff=[{"previous": old, "current": new}],
        previous_observed_at="2026-09-01T10:00:00+08:00" if old else None,
        current_observed_at="2026-09-02T10:00:00+08:00",
    )


def decision_for(input_data, event_type="capability", action="增加 PDF 导出"):
    """构造可逐字追踪的测试输出，只有语义决定来自 Mock。"""
    old, new = input_data.previous_state, input_data.current_state
    q = Quote(input_field="current_state", excerpt=new)
    citations = {key: [q] for key in ("event_type", "event_subject", "change_action", "change_items.0.item", "change_items.0.current_state")}
    if old:
        citations["change_items.0.previous_state"] = [Quote(input_field="previous_state", excerpt=old)]
    return EventDecision(
        decision="event", event_type=event_type, event_subject="Export", change_action=action,
        change_items=[ChangeItem(item="Export", previous_state=old, current_state=new)],
        decision_reason="原文描述产品状态变化。", citations=citations,
    )


def detect_with_mock(input_data, decision=None):
    client = Mock()
    decisions = DetectionDecisions(candidates=[decision or decision_for(input_data)])
    client.decide.return_value = decisions, runtime()
    with patch("event_core.detector.get_llm", return_value=client) as get:
        result = detect_event(input_data)
    return result, get, client


def snapshot(text, at="2026-09-02T10:00:00+08:00"):
    blocks = [{"type": "heading", "level": 1, "text": "Export", "links": []},
              {"type": "paragraph", "text": text, "links": []}]
    content = serialize_blocks(blocks)
    return dict(url="https://example.com/product", final_url="https://example.com/product",
                title="Example", content=content, blocks=blocks, captured_at=at,
                extraction_version=EXTRACTION_VERSION, content_hash=compute_content_hash(content))


class EventDetectionTests(unittest.TestCase):
    def test_first_scan_is_baseline_without_model(self):
        value = build_state_comparison_input(
            dict(is_first_scan=True, previous_snapshot=None, current_snapshot=snapshot("Supports PDF.")),
            competitor="Example", source_type="product_page",
        )
        result, get, _ = detect_with_mock(value)
        self.assertEqual(result.candidates[0].decision, "no_event")
        self.assertEqual(result.candidates[0].change_items, [])
        get.assert_not_called()

    def test_two_sided_capability_change_uses_reasoning_role(self):
        result, get, _ = detect_with_mock(data())
        get.assert_called_once_with(role="reasoning")
        candidate = result.candidates[0]
        self.assertEqual(candidate.decision, "event")
        self.assertEqual(candidate.change_items[0].previous_state, "Export supports CSV only.")
        self.assertEqual(candidate.detection_path, "state_comparison")

    def test_copywriting_change_can_be_no_event(self):
        value = data(old="Export CSV files.", new="Easily export CSV files.")
        negative = EventDecision(decision="no_event", decision_reason="仅修饰文案，没有产品状态变化。")
        result, _, _ = detect_with_mock(value, negative)
        self.assertIsNone(result.candidates[0].event_type)
        self.assertEqual(result.candidates[0].decision, "no_event")

    def test_missing_previous_state_becomes_uncertain(self):
        result, get, _ = detect_with_mock(data(old=None))
        candidate = result.candidates[0]
        self.assertEqual(candidate.decision, "uncertain")
        self.assertIsNone(candidate.event_subject)
        self.assertTrue(all(e.evidence_status == "pending" for e in candidate.evidence))
        get.assert_not_called()

    def test_official_changelog_statement_without_previous(self):
        value = data("explicit_statement", None, "We released PDF export in public beta.")
        result, _, _ = detect_with_mock(value, decision_for(value, action="发布公开 Beta"))
        candidate = result.candidates[0]
        self.assertEqual(candidate.decision, "event")
        self.assertIsNone(candidate.change_items[0].previous_state)
        self.assertIn("beta", candidate.change_items[0].current_state)

    def test_announcement_does_not_get_ga_state_added_by_program(self):
        value = data("explicit_statement", None, "We announced PDF export, planned for next quarter.")
        result, _, _ = detect_with_mock(value, decision_for(value, "availability", "宣布未来推出"))
        self.assertNotIn("GA", result.model_dump_json())
        self.assertEqual(result.candidates[0].change_action, "宣布未来推出")
        self.assertIsNone(result.candidates[0].event_time)

    def test_unproved_time_is_null_not_observed_at(self):
        result, _, _ = detect_with_mock(data())
        self.assertIsNone(result.candidates[0].event_time)
        self.assertIsNone(result.candidates[0].effective_time)
        self.assertEqual(result.candidates[0].evidence[-1].observed_at, data().current_observed_at)

    def test_observed_time_cannot_be_cited_as_event_time(self):
        value = data()
        decision = decision_for(value)
        decision.event_time = "2026-09-02"
        decision.citations["event_time"] = [Quote(input_field="current_state", excerpt=value.current_state)]
        with self.assertRaises(EvidenceValidationError):
            detect_with_mock(value, decision)
        with self.assertRaises(ValidationError):
            Quote(input_field="current_observed_at", excerpt="2026-09-02")

    def test_explicit_time_requires_body_evidence(self):
        value = data("explicit_statement", None, "On 2026-08-20 we released PDF export.")
        decision = decision_for(value)
        decision.event_time = "2026-08-20"
        decision.citations["event_time"] = [Quote(input_field="current_state", excerpt=value.current_state)]
        result, _, _ = detect_with_mock(value, decision)
        self.assertEqual(result.candidates[0].event_time, "2026-08-20")

    def test_pricing_permission_and_deprecation_are_supported(self):
        cases = [
            ("pricing", "Pro costs $10 per month.", "Pro costs $12 per month.", "调整价格"),
            ("access_permission", "Only admins can export.", "Members can also export.", "开放成员导出权限"),
            ("deprecation", "Legacy API is supported.", "Legacy API is deprecated.", "弃用 Legacy API"),
        ]
        for kind, old, new, action in cases:
            with self.subTest(kind=kind):
                value = data(old=old, new=new)
                result, _, _ = detect_with_mock(value, decision_for(value, kind, action))
                self.assertEqual(result.candidates[0].event_type, kind)

    def test_one_sided_state_observations_never_mean_absence(self):
        for status in ("current_only", "previous_only", "unresolved", "missing_unconfirmed", "not_comparable"):
            with self.subTest(status=status):
                value = data()
                value.state_context.comparison_status = status
                result, get, _ = detect_with_mock(value)
                self.assertEqual(result.candidates[0].decision, "uncertain")
                self.assertEqual(result.candidates[0].change_items, [])
                get.assert_not_called()

    def test_pending_and_conflict_evidence_remain_unconfirmed(self):
        for status in ("pending", "conflict"):
            value = data()
            value.evidence_status = status
            result, get, _ = detect_with_mock(value)
            self.assertEqual(result.candidates[0].decision, "uncertain")
            self.assertEqual({e.evidence_status for e in result.candidates[0].evidence}, {status})
            get.assert_not_called()

    def test_unverified_source_cannot_confirm_path_b(self):
        value = data("explicit_statement", None, "We launched PDF export.")
        value.source_credibility = "unverified"
        result, get, _ = detect_with_mock(value)
        self.assertEqual(result.candidates[0].decision, "uncertain")
        get.assert_not_called()

    def test_no_event_and_uncertain_cannot_carry_event_facts(self):
        for verdict in ("no_event", "uncertain"):
            with self.assertRaises(ValidationError):
                EventDecision(decision=verdict, event_subject="Invented launch", decision_reason="x")
        with self.assertRaises(ValidationError):
            EventDecision(decision="uncertain", decision_reason="x")

    def test_model_cannot_invent_history(self):
        value = data()
        decision = decision_for(value)
        decision.change_items[0].previous_state = "PDF did not exist."
        with self.assertRaises(EvidenceValidationError):
            detect_with_mock(value, decision)

    def test_announcement_cannot_be_rewritten_as_confirmed_ga_state(self):
        value = data("explicit_statement", None, "We announced PDF export for a future release.")
        decision = decision_for(value)
        decision.change_items[0].current_state = "PDF export is generally available to all users."
        with self.assertRaises(EvidenceValidationError):
            detect_with_mock(value, decision)

    def test_scope_cannot_be_invented_from_official_credibility(self):
        value = data()
        decision = decision_for(value)
        decision.affected_scope = "All enterprise users worldwide"
        decision.citations["affected_scope"] = [Quote(input_field="current_state", excerpt=value.current_state)]
        with self.assertRaises(EvidenceValidationError):
            detect_with_mock(value, decision)

    def test_model_cannot_use_current_quote_as_historical_observation(self):
        value = data()
        decision = decision_for(value)
        decision.citations["change_items.0.previous_state"] = [Quote(input_field="current_state", excerpt=value.current_state)]
        with self.assertRaises(EvidenceValidationError):
            detect_with_mock(value, decision)

    def test_fake_quote_and_uncited_fact_fail(self):
        value = data()
        for mutate in (lambda d: d.citations.pop("event_type"),
                       lambda d: d.citations.update(event_type=[Quote(input_field="current_state", excerpt="invented evidence")])):
            decision = decision_for(value)
            mutate(decision)
            with self.assertRaises(EvidenceValidationError):
                detect_with_mock(value, decision)

    def test_evidence_metadata_is_copied_from_input(self):
        value = data()
        result, _, _ = detect_with_mock(value)
        for evidence in result.candidates[0].evidence:
            self.assertEqual(evidence.source_url, value.source_url)
            self.assertEqual(evidence.source_title, value.source_title)
            self.assertEqual(evidence.evidence_status, "confirmed")
            self.assertNotIn("event_time", evidence.evidence_capability)

    def test_model_cannot_supply_source_or_path_or_confidence(self):
        for name in ("source_url", "detection_path", "confidence"):
            with self.assertRaises(ValidationError):
                EventDecision.model_validate({**decision_for(data()).model_dump(), name: "invented"})

    def test_uncertain_from_semantic_judgment_is_pending(self):
        d = EventDecision(decision="uncertain", decision_reason="范围含糊。", uncertainty_reason="无法确认适用套餐。")
        result, _, _ = detect_with_mock(data(), d)
        self.assertEqual(result.candidates[0].evidence[0].evidence_status, "pending")

    def test_independent_actions_can_produce_two_candidates(self):
        value = data(old="Admins only; Pro $10.", new="Members too; Pro $12.")
        decisions = DetectionDecisions(candidates=[
            decision_for(value, "access_permission", "开放权限"),
            decision_for(value, "pricing", "调整价格"),
        ])
        with patch("event_core.detector.get_llm") as get:
            get.return_value.decide.return_value = decisions, runtime()
            result = detect_event(value)
        self.assertEqual(len(result.candidates), 2)
        self.assertNotEqual(result.candidates[0].candidate_id, result.candidates[1].candidate_id)

    def test_model_call_failure_propagates_without_fallback(self):
        with patch("event_core.detector.get_llm") as get:
            get.return_value.decide.side_effect = LLMCallError("failed")
            with self.assertRaises(LLMCallError):
                detect_event(data())
            get.assert_called_once_with(role="reasoning")


class Stage1InputTests(unittest.TestCase):
    def test_matched_interactive_state_keeps_local_identity_and_timestamps(self):
        from backend.snapshot import INTERACTIVE_STATE_SCHEMA_VERSION
        previous = snapshot("Price 10", "2026-09-01T10:00:00+08:00")
        current = snapshot("Price 12")
        for item, text in ((previous, "Price 10"), (current, "Price 12")):
            item.update(
                interactive_state_schema_version=INTERACTIVE_STATE_SCHEMA_VERSION,
                interactive_state_traversal={"status": "complete", "page_restored": True},
                interactive_states=[{
                    "state_key": "monthly", "scope_path": ["Plans"], "state_path": ["Monthly"],
                    "blocks": [{"type": "paragraph", "text": text, "links": []}],
                    "captured_at": item["captured_at"], "content_hash": compute_content_hash(text),
                }],
            )
        value = build_state_comparison_input(
            dict(is_first_scan=False, previous_snapshot=previous, current_snapshot=current),
            competitor="Example", source_type="pricing", state_key="monthly",
        )
        self.assertEqual(value.state_context.state_path, ["Monthly"])
        self.assertEqual(value.state_context.comparison_status, "comparable")
        self.assertEqual(value.previous_state, "Price 10")
        self.assertEqual(value.current_state, "Price 12")

    def test_real_stage1_diff_feeds_local_two_sided_input(self):
        previous = snapshot("CSV only.", "2026-09-01T10:00:00+08:00")
        current = snapshot("CSV and PDF.")
        history = dict(is_first_scan=False, previous_snapshot=previous, current_snapshot=current)
        original = deepcopy(history)
        result = build_state_comparison_input(history, competitor="Example", source_type="product_page")
        self.assertEqual(result.previous_state, "CSV only.")
        self.assertEqual(result.current_state, "CSV and PDF.")
        self.assertTrue(result.contextual_diff)
        self.assertEqual(history, original)

    def test_extraction_version_change_cannot_create_event(self):
        previous = snapshot("CSV only.", "2026-09-01T10:00:00+08:00")
        previous["extraction_version"] = "legacy"
        value = build_state_comparison_input(
            dict(is_first_scan=False, previous_snapshot=previous, current_snapshot=snapshot("CSV and PDF.")),
            competitor="Example", source_type="product_page",
        )
        self.assertEqual(detect_event(value).candidates[0].decision, "uncertain")

    def test_path_b_excerpt_must_exist_in_stage1_snapshot(self):
        with self.assertRaises(ValueError):
            build_explicit_statement_input(snapshot("Supports CSV."), "Launched PDF.", competitor="Example", source_type="changelog")

    def test_path_b_preserves_observation_time_without_event_time(self):
        value = build_explicit_statement_input(snapshot("We released PDF export."), "We released PDF export.", competitor="Example", source_type="changelog", source_credibility="official")
        self.assertIsNone(value.previous_state)
        self.assertEqual(value.current_observed_at.isoformat(), "2026-09-02T10:00:00+08:00")

    def test_large_input_is_rejected_not_silently_truncated(self):
        with self.assertRaises(ValidationError):
            data(new="x" * 12001)

    def test_different_source_snapshots_cannot_be_compared(self):
        previous = snapshot("CSV.", "2026-09-01T10:00:00+08:00")
        previous["url"] = "https://other.example.com/"
        with self.assertRaises(ValueError):
            build_state_comparison_input(dict(is_first_scan=False, previous_snapshot=previous, current_snapshot=snapshot("PDF.")), competitor="Example", source_type="product_page")


class EventDedupTests(unittest.TestCase):
    def setUp(self):
        self.first = detect_with_mock(data())[0].candidates[0]
        second = data()
        second.source_url = "https://example.com/changelog"
        self.second = detect_with_mock(second)[0].candidates[0]

    def verdict(self, value, candidate=None):
        with patch("event_core.dedup.get_llm") as get:
            get.return_value.decide.return_value = DedupDecision(decision=value, reason="语义比较结果。"), runtime()
            result = deduplicate_event(candidate or self.second, [self.first])
            get.assert_called_once_with(role="reasoning")
        return result

    def test_same_event_different_sources_adds_evidence(self):
        result = self.verdict("SAME_EVENT")
        self.assertEqual(result.matched_event_id, self.first.candidate_id)
        self.assertEqual(len(result.event.evidence), 4)
        self.assertEqual(len(self.first.evidence), 2)

    def test_supplemental_evidence_is_same_event_without_overwriting_facts(self):
        result = self.verdict("SAME_EVENT", self.first)
        self.assertEqual(len(result.event.evidence), 2)
        self.assertEqual(result.event.change_items, self.first.change_items)

    def test_later_ga_after_announcement_can_be_new_event(self):
        announced = data("explicit_statement", None, "We announced PDF export, planned for next quarter.")
        self.first = detect_with_mock(announced, decision_for(announced, "availability", "宣布未来推出"))[0].candidates[0]
        value = data("explicit_statement", None, "PDF export is now generally available.")
        candidate = detect_with_mock(value, decision_for(value, "availability", "正式上线"))[0].candidates[0]
        result = self.verdict("NEW_EVENT", candidate)
        self.assertEqual(result.event.candidate_id, candidate.candidate_id)
        self.assertIsNone(result.matched_event_id)

    def test_same_subject_later_price_transition_is_new_event(self):
        value = data(old="Pro costs $12.", new="Pro costs $15.")
        candidate = detect_with_mock(value, decision_for(value, "pricing", "再次调价"))[0].candidates[0]
        self.assertEqual(self.verdict("NEW_EVENT", candidate).decision, "NEW_EVENT")

    def test_new_time_evidence_can_merge_without_replacing_existing_time(self):
        value = data("explicit_statement", None, "On 2026-08-20 we released PDF export.")
        decision = decision_for(value)
        decision.event_time = "2026-08-20"
        decision.citations["event_time"] = [Quote(input_field="current_state", excerpt=value.current_state)]
        candidate = detect_with_mock(value, decision)[0].candidates[0]
        merged = self.verdict("SAME_EVENT", candidate).event
        self.assertIsNone(merged.event_time)
        self.assertTrue(any("event_time" in e.evidence_capability for e in merged.evidence))

    def test_uncertain_dedup_keeps_pending_candidate_without_merge(self):
        result = self.verdict("UNCERTAIN")
        self.assertIsNone(result.event)
        self.assertEqual(result.pending_candidate.candidate_id, self.second.candidate_id)

    def test_pending_detection_does_not_enter_confirmed_store(self):
        pending = detect_event(data(old=None)).candidates[0]
        result = deduplicate_event(pending, [self.first])
        self.assertEqual(result.decision, "UNCERTAIN")

    def test_no_event_is_not_a_dedup_candidate(self):
        value = data()
        value.state_context.comparison_status = "unchanged"
        with self.assertRaises(ValueError):
            deduplicate_event(detect_event(value).candidates[0], [])

    def test_empty_event_store_is_new_without_model(self):
        with patch("event_core.dedup.get_llm") as get:
            self.assertEqual(deduplicate_event(self.first, []).decision, "NEW_EVENT")
            get.assert_not_called()

    def test_content_identity_does_not_bypass_semantic_dedup(self):
        self.assertEqual(content_fingerprint("text"), content_fingerprint("text"))
        self.assertEqual(self.verdict("UNCERTAIN", self.first).decision, "UNCERTAIN")

    def test_multiple_possible_matches_do_not_merge_automatically(self):
        third = data()
        third.source_url = "https://example.com/another"
        candidate = detect_with_mock(third)[0].candidates[0]
        with patch("event_core.dedup.get_llm") as get:
            get.return_value.decide.return_value = DedupDecision(decision="SAME_EVENT", reason="same"), runtime()
            result = deduplicate_event(candidate, [self.first, self.second])
        self.assertEqual(result.decision, "UNCERTAIN")

    def test_dedup_call_failure_is_not_success(self):
        with patch("event_core.dedup.get_llm") as get:
            get.return_value.decide.side_effect = LLMCallError("failed")
            with self.assertRaises(LLMCallError):
                deduplicate_event(self.second, [self.first])


class PromptContractTests(unittest.TestCase):
    def test_prompts_keep_anti_hallucination_and_business_boundaries(self):
        self.assertIn("Not Observed ≠ Absent", EVENT_DETECTION_PROMPT)
        self.assertIn("Observation Time ≠ Event Time", EVENT_DETECTION_PROMPT)
        self.assertIn("独立业务动作", EVENT_DETECTION_PROMPT)
        self.assertIn("后续实际 GA", EVENT_DEDUP_PROMPT)
        visible = EVENT_DETECTION_PROMPT + EVENT_DEDUP_PROMPT + json.dumps(DetectionDecisions.model_json_schema())
        for forbidden in ("DEEPSEEK_API_KEY", "LLM_REASONING", "base_url", "Bearer", "confidence"):
            self.assertNotIn(forbidden, visible)


if __name__ == "__main__":
    unittest.main()
