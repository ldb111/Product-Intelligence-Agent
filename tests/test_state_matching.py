"""Stage 1 V0.3-4A Interactive State Matching 自动化测试。"""

from __future__ import annotations

import unittest

from backend.change_detection import EXTRACTION_VERSION_CHANGED_REASON
from backend.snapshot import EXTRACTION_VERSION, INTERACTIVE_STATE_SCHEMA_VERSION
from backend.state_matching import (
    INTERACTIVE_STATE_SCHEMA_UNAVAILABLE_REASON,
    INTERACTIVE_STATE_SCHEMA_VERSION_CHANGED_REASON,
    match_interactive_states,
)


class InteractiveStateMatchingTests(unittest.TestCase):
    """验证状态匹配语义，不调用网页、浏览器或重新计算 Hash。"""

    @staticmethod
    def _state(
        state_key: str,
        content_hash: str | None,
        *,
        comparison_eligible: bool | None = None,
    ) -> dict[str, object]:
        state: dict[str, object] = {
            "state_key": state_key,
            "scope_path": ["套餐"],
            "state_path": [state_key],
            "is_default": state_key == "monthly",
            "blocks": [],
            "content_hash": content_hash,
            "captured_at": "2026-09-07T10:00:00+08:00",
        }
        if comparison_eligible is not None:
            state["comparison_eligible"] = comparison_eligible
        return state

    @staticmethod
    def _snapshot(
        states: list[dict[str, object]],
        *,
        status: str = "complete",
        page_restored: bool = True,
        schema_version: str | None = INTERACTIVE_STATE_SCHEMA_VERSION,
        extraction_version: str | None = EXTRACTION_VERSION,
    ) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "interactive_states": states,
            "interactive_state_traversal": {
                "status": status,
                "page_restored": page_restored,
            },
        }
        if schema_version is not None:
            snapshot["interactive_state_schema_version"] = schema_version
        if extraction_version is not None:
            snapshot["extraction_version"] = extraction_version
        return snapshot

    @staticmethod
    def _history(
        current: dict[str, object],
        previous: dict[str, object] | None,
        *,
        first_scan: bool = False,
    ) -> dict[str, object]:
        return {
            "is_first_scan": first_scan,
            "current_snapshot": current,
            "previous_snapshot": previous,
        }

    def test_first_scan_only_builds_baseline(self) -> None:
        current = self._snapshot([self._state("monthly", "hash-current")])

        result = match_interactive_states(
            self._history(current, None, first_scan=True)
        )

        self.assertTrue(result["is_first_scan"])
        self.assertIsNone(result["changed"])
        self.assertEqual(result["comparison_status"], "baseline")
        self.assertEqual(result["state_changes"], [])

    def test_same_key_and_hash_is_unchanged(self) -> None:
        previous = self._snapshot([self._state("monthly", "same-hash")])
        current = self._snapshot([self._state("monthly", "same-hash")])

        result = match_interactive_states(self._history(current, previous))

        self.assertFalse(result["changed"])
        self.assertEqual(result["comparison_status"], "complete")
        self.assertEqual(result["state_changes"][0]["change_type"], "unchanged")

    def test_same_key_and_different_hash_is_modified(self) -> None:
        previous = self._snapshot([self._state("monthly", "old-hash")])
        current = self._snapshot([self._state("monthly", "new-hash")])

        result = match_interactive_states(self._history(current, previous))

        self.assertTrue(result["changed"])
        self.assertEqual(result["state_changes"][0]["change_type"], "modified")
        self.assertEqual(
            result["state_changes"][0]["previous_content_hash"], "old-hash"
        )
        self.assertEqual(
            result["state_changes"][0]["current_content_hash"], "new-hash"
        )

    def test_complete_coverage_classifies_current_and_previous_only(self) -> None:
        previous = self._snapshot(
            [
                self._state("shared", "same"),
                self._state("previous-state", "previous-hash"),
            ]
        )
        current = self._snapshot(
            [
                self._state("shared", "same"),
                self._state("current-state", "current-hash"),
            ]
        )

        result = match_interactive_states(self._history(current, previous))
        changes = {
            change["state_key"]: change["change_type"]
            for change in result["state_changes"]
        }

        self.assertTrue(result["changed"])
        self.assertEqual(changes["current-state"], "current_only")
        self.assertEqual(changes["previous-state"], "previous_only")

    def test_partial_coverage_keeps_one_sided_observations_uncertain(self) -> None:
        previous = self._snapshot(
            [
                self._state("shared", "same"),
                self._state("previous-state", "previous-hash"),
            ]
        )
        current = self._snapshot(
            [
                self._state("shared", "same"),
                self._state("current-state", "current-hash"),
            ],
            status="partial",
        )

        result = match_interactive_states(self._history(current, previous))
        changes = {
            change["state_key"]: change for change in result["state_changes"]
        }

        self.assertIsNone(result["changed"])
        self.assertEqual(result["comparison_status"], "incomplete")
        self.assertEqual(changes["current-state"]["change_type"], "unresolved")
        self.assertEqual(changes["current-state"]["observation"], "current_only")
        self.assertEqual(
            changes["previous-state"]["change_type"], "missing_unconfirmed"
        )
        self.assertEqual(
            changes["previous-state"]["observation"], "previous_only"
        )

    def test_aborted_coverage_does_not_turn_missing_state_into_fact(self) -> None:
        previous = self._snapshot([self._state("yearly", "old")])
        current = self._snapshot([], status="aborted", page_restored=False)

        result = match_interactive_states(self._history(current, previous))

        self.assertIsNone(result["changed"])
        self.assertEqual(result["current_traversal_status"], "aborted")
        self.assertEqual(
            result["state_changes"][0]["change_type"], "missing_unconfirmed"
        )
        # 状态匹配只描述观察结果，不能越权生成产品发布、下线或弃用事实。
        serialized_types = str(result["state_changes"]).lower()
        for forbidden_term in ("newly_launched", "removed", "deprecated"):
            self.assertNotIn(forbidden_term, serialized_types)

    def test_definite_modified_state_remains_changed_during_partial_traversal(self) -> None:
        previous = self._snapshot([self._state("monthly", "old")])
        current = self._snapshot(
            [self._state("monthly", "new")], status="partial"
        )

        result = match_interactive_states(self._history(current, previous))

        self.assertTrue(result["changed"])
        self.assertEqual(result["comparison_status"], "incomplete")
        self.assertEqual(result["state_changes"][0]["change_type"], "modified")

    def test_comparison_ineligible_is_excluded_and_missing_flag_defaults_true(self) -> None:
        previous = self._snapshot(
            [
                self._state("monthly", "same"),
            ]
        )
        current = self._snapshot(
            [
                self._state("navigation", None, comparison_eligible=False),
                # 不写 comparison_eligible，验证兼容字段缺省按 true 处理。
                self._state("monthly", "same"),
            ]
        )

        result = match_interactive_states(self._history(current, previous))

        self.assertFalse(result["changed"])
        # current-only 的不可比较导航节点也必须完全排除，不能制造状态变化。
        self.assertEqual(result["excluded_state_count"], 1)
        self.assertEqual(
            [change["state_key"] for change in result["state_changes"]],
            ["monthly"],
        )

    def test_legacy_snapshot_without_state_schema_is_not_comparable(self) -> None:
        previous = {"extraction_version": EXTRACTION_VERSION}
        current = self._snapshot([self._state("monthly", "hash")])

        result = match_interactive_states(self._history(current, previous))

        self.assertIsNone(result["changed"])
        self.assertEqual(result["comparison_status"], "not_comparable")
        self.assertEqual(
            result["comparison_skipped_reason"],
            INTERACTIVE_STATE_SCHEMA_UNAVAILABLE_REASON,
        )
        self.assertEqual(result["state_changes"], [])

    def test_state_schema_version_mismatch_is_not_comparable(self) -> None:
        previous = self._snapshot(
            [self._state("monthly", "old")],
            schema_version="interactive_states_legacy",
        )
        current = self._snapshot([self._state("monthly", "new")])

        result = match_interactive_states(self._history(current, previous))

        self.assertIsNone(result["changed"])
        self.assertEqual(
            result["comparison_skipped_reason"],
            INTERACTIVE_STATE_SCHEMA_VERSION_CHANGED_REASON,
        )

    def test_extraction_version_mismatch_uses_existing_protection(self) -> None:
        previous = self._snapshot(
            [self._state("monthly", "old")], extraction_version="old-extractor"
        )
        current = self._snapshot([self._state("monthly", "new")])

        result = match_interactive_states(self._history(current, previous))

        self.assertIsNone(result["changed"])
        self.assertEqual(
            result["comparison_skipped_reason"], EXTRACTION_VERSION_CHANGED_REASON
        )


if __name__ == "__main__":
    unittest.main()
