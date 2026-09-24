"""Legacy Regression Runner 的确定性单元测试。

所有测试只使用 Fake 输出，不访问真实网页、不调用真实模型，也不读取 .env。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from evals.runners.legacy_regression import (
    SOURCE_GOLD,
    STAGE1_GOLD,
    build_result,
    load_plan,
    write_outputs,
)
from evals.runners.source_discovery_regression import run_source_case
from evals.runners.stage1_regression import run_stage1_case
from evals.schema import load_and_validate
from evals.scoring import match_url, score_source_discovery, score_stage1, summarize_cases


def _source_gold(case_id: str) -> dict[str, Any]:
    records = load_and_validate(SOURCE_GOLD, "source_discovery")
    return next(record for record in records if record["metadata"]["legacy_case_id"] == case_id)


def _stage_gold(case_id: str) -> dict[str, Any]:
    records = load_and_validate(STAGE1_GOLD, "stage1")
    return next(record for record in records if record["metadata"]["legacy_case_id"] == case_id)


def _source_actual(gold: dict[str, Any]) -> dict[str, Any]:
    """把 Gold 转成形状正确的受控生产输出，专用于比较器测试。"""
    return {
        "competitor_name": gold["input"]["competitor_name"],
        "official_domains": copy.deepcopy(gold["expected"]["official_domains"]),
        "sources": [
            {
                "url": source["url"],
                "title": source["title"],
                "source_type": source["source_type"],
                "monitoring_reason": source["monitoring_reason"],
            }
            for source in gold["expected"]["sources"]
        ],
        "discovery_complete": gold["expected"]["discovery_complete"],
        "remaining_gaps": copy.deepcopy(gold["expected"]["remaining_gaps"]),
    }


def _stage_actual(gold: dict[str, Any], *, content: str | None = None) -> dict[str, Any]:
    """构造包含真实格式 Snapshot 的受控 Stage 1 输出。"""
    body = content or "\n".join(gold["expected"].get("content_anchors", []))
    page = {
        "url": gold["input"]["url"],
        "requested_url": gold["input"]["url"],
        "final_url": gold["input"]["url"],
        "status_code": 200,
        "title": gold["expected"]["title"],
        "content": body,
        "blocks": [{"type": "heading", "text": gold["expected"]["title"]}],
        "acquisition_method": gold["expected"].get("acquisition_method", "static"),
    }
    return {
        "page_data": page,
        "quality_gate": {"status": gold["expected"]["quality_status"]},
        "snapshot": {
            **page,
            "captured_at": "2026-09-24T12:00:00+08:00",
            "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        },
    }


class LegacyScoringTests(unittest.TestCase):
    def test_a_url_exact_match(self) -> None:
        self.assertEqual(match_url("https://example.com/a", "https://example.com/a"), "exact")

    def test_b_url_equivalence_uses_declared_alias_and_trailing_slash(self) -> None:
        equivalence = {"host_aliases": ["example.com", "www.example.com"]}
        self.assertEqual(
            match_url("https://example.com/a/", "https://www.example.com/a", equivalence),
            "equivalent",
        )
        self.assertEqual(
            match_url("https://example.com/a?x=1", "https://example.com/a?x=2", equivalence),
            "unmatched",
        )

    def test_c_expected_source_recall(self) -> None:
        gold = _source_gold("B02")
        scored = score_source_discovery(gold, _source_actual(gold))
        self.assertEqual(scored["metrics"]["expected_source_recall"], {"matched": 6, "total": 6, "rate": 1.0})

    def test_d_core_and_supplementary_recall_have_separate_denominators(self) -> None:
        gold = _source_gold("B02")
        actual = _source_actual(gold)
        actual["sources"] = actual["sources"][1:-1]
        scored = score_source_discovery(gold, actual)
        self.assertEqual(scored["metrics"]["core_source_recall"]["total"], 3)
        self.assertEqual(scored["metrics"]["supplementary_source_recall"]["total"], 3)
        self.assertEqual(scored["metrics"]["core_source_recall"]["matched"], 2)
        self.assertEqual(scored["metrics"]["supplementary_source_recall"]["matched"], 2)

    def test_e_unexpected_source_is_review(self) -> None:
        gold = _source_gold("B01")
        actual = _source_actual(gold)
        actual["sources"].append({
            "url": "https://casee.me/new",
            "title": "New",
            "source_type": "other",
            "monitoring_reason": "Current page",
        })
        scored = score_source_discovery(gold, actual)
        self.assertEqual(scored["status"], "REVIEW")
        self.assertIn("new_source_detected", {item["reason"] for item in scored["review_queue"]})

    def test_f_missing_expected_source_is_review(self) -> None:
        gold = _source_gold("B01")
        actual = _source_actual(gold)
        actual["sources"] = []
        scored = score_source_discovery(gold, actual)
        self.assertEqual(scored["status"], "REVIEW")
        self.assertIn("expected_source_missing", {item["reason"] for item in scored["review_queue"]})

    def test_g_title_changed_is_review(self) -> None:
        gold = _source_gold("B01")
        actual = _source_actual(gold)
        actual["sources"][0]["title"] = "Current title"
        scored = score_source_discovery(gold, actual)
        self.assertEqual(scored["status"], "REVIEW")
        self.assertIn("title_changed", {item["reason"] for item in scored["review_queue"]})

    def test_h_partial_stage_anchor_match_is_review(self) -> None:
        gold = _stage_gold("S1-02")
        first_anchor = gold["expected"]["content_anchors"][0]
        scored = score_stage1(gold, _stage_actual(gold, content=first_anchor))
        metric = scored["metrics"]["content_anchor_recall"]
        self.assertEqual(metric["matched"], 1)
        self.assertEqual(metric["total"], len(gold["expected"]["content_anchors"]))
        self.assertEqual(scored["status"], "REVIEW")

    def test_i_missing_gold_acquisition_method_is_skipped(self) -> None:
        gold = _stage_gold("S1-01")
        scored = score_stage1(gold, _stage_actual(gold))
        check = next(item for item in scored["checks"] if item["check"] == "acquisition_method")
        self.assertEqual(check["status"], "SKIPPED")
        self.assertEqual(scored["metrics"]["acquisition_method_match"]["total"], 0)

    def test_j_natural_language_structure_assertion_requires_manual_review(self) -> None:
        gold = _stage_gold("S1-02")
        scored = score_stage1(gold, _stage_actual(gold))
        items = [item for item in scored["review_queue"] if item["reason"] == "manual_structure_review"]
        self.assertEqual(len(items), len(gold["expected"]["structure_assertions"]) + len(gold["expected"]["interaction_assertions"]))
        self.assertIn("block_count", items[0]["actual"])

    def test_m_review_is_not_automatically_attributed_to_live_drift(self) -> None:
        gold = _stage_gold("S1-02")
        scored = score_stage1(gold, _stage_actual(gold, content="changed"))
        reasons = {item["reason"] for item in scored["review_queue"]}
        self.assertIn("anchor_missing", reasons)
        self.assertNotIn("live_web_drift", reasons)
        anchor_item = next(
            item for item in scored["review_queue"] if item["reason"] == "anchor_missing"
        )
        self.assertEqual(
            anchor_item["possible_causes"],
            ["live_web_drift", "parser_regression", "rendering_change"],
        )

    def test_n_summary_denominators_exclude_blocked_content_checks(self) -> None:
        cases = [
            {
                "target": "stage1", "status": "REVIEW",
                "metrics": {
                    "content_anchor_recall": {"matched": 2, "total": 4, "rate": 0.5},
                    "title_match": True,
                    "quality_status_match": False,
                    "acquisition_method_match": {"matched": 0, "total": 0, "rate": None},
                },
            },
            {"target": "stage1", "status": "BLOCKED", "metrics": {}},
        ]
        summary = summarize_cases(cases)["stage1"]
        self.assertEqual(summary["page_execution_success_rate"], {"matched": 1, "total": 2, "rate": 0.5})
        self.assertEqual(summary["content_anchor_recall"], {"matched": 2, "total": 4, "rate": 0.5})
        self.assertEqual(summary["acquisition_method_match_count"]["total"], 0)

    def test_dry_run_plan_and_output_contain_all_legacy_cases(self) -> None:
        plan = load_plan("all", None)
        self.assertEqual(len(plan["source_discovery"]), 4)
        self.assertEqual(len(plan["stage1"]), 8)
        result = build_result(
            run_id="controlled",
            target="all",
            case_id=None,
            dry_run=True,
            plan=plan,
            cases=[],
            started_at="2026-09-24T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as directory:
            json_path, report_path = write_outputs(result, Path(directory) / "run")
            self.assertTrue(json_path.is_file())
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("Source Discovery Cases: 4", report)
            self.assertIn("Stage 1 Cases: 8", report)


class LegacyRunnerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_k_network_unavailable_is_blocked(self) -> None:
        class NetworkUnavailable(Exception):
            error_type = "network_error"

        def reader(_url: str) -> dict[str, Any]:
            raise NetworkUnavailable("sensitive provider detail")

        result = run_stage1_case(_stage_gold("S1-01"), reader=reader)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertNotIn("sensitive provider detail", str(result))

    async def test_l_unexpected_contract_failure_is_error(self) -> None:
        result = run_stage1_case(_stage_gold("S1-01"), reader=lambda _url: {"bad": True})
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error"]["type"], "TypeError")

    async def test_o_result_has_no_secret_fields_or_exception_message(self) -> None:
        gold = _source_gold("B01")

        async def discover(_name: str, _url: str) -> dict[str, Any]:
            return _source_actual(gold)

        result = await run_source_case(gold, discover=discover)

        def keys(value: Any) -> list[str]:
            if isinstance(value, dict):
                return [str(key).casefold() for key in value] + [item for child in value.values() for item in keys(child)]
            if isinstance(value, list):
                return [item for child in value for item in keys(child)]
            return []

        forbidden = {"api_key", "token", "authorization", "secret", ".env", "trace"}
        self.assertTrue(forbidden.isdisjoint(keys(result)))
        self.assertEqual(result["evaluation_type"], "legacy_regression")
        self.assertEqual(result["evaluation_use"], "regression_only")


if __name__ == "__main__":
    unittest.main()
