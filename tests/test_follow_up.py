"""事件追问的 SQLite 回合状态、隔离与幂等控制测试。"""

from __future__ import annotations

import tempfile
import unittest

from pipeline.store import PipelineStore


class FollowUpStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PipelineStore(directory.name)
        self.scope = dict(workspace_id="workspace-a", product_id="product-a", event_id="event-a")

    def test_user_message_is_durable_before_answer_and_duplicate_request_is_idempotent(self):
        first = self.store.claim_follow_up(
            **self.scope, request_id="request-123", question="这件事有什么影响？", process_token="p1"
        )
        second = self.store.claim_follow_up(
            **self.scope, request_id="request-123", question="这段文本不会覆盖原问题", process_token="p2"
        )
        self.assertTrue(first["claimed"])
        self.assertFalse(second["claimed"])
        self.assertEqual(second["turn"]["user_message"], "这件事有什么影响？")
        self.assertEqual(len(self.store.follow_up_turns(**self.scope)), 1)

    def test_completed_turn_persists_answer_and_cannot_be_retried(self):
        claimed = self.store.claim_follow_up(
            **self.scope, request_id="request-123", question="下一步关注什么？", process_token="p1"
        )["turn"]
        artifact = {"result": {"answer": []}, "citations": []}
        completed = self.store.finish_follow_up(
            **self.scope, turn_id=claimed["turn_id"], process_token="p1",
            artifact=artifact, candidate_count=1,
        )
        self.assertEqual(completed["status"], "completed")
        retry = self.store.retry_follow_up(
            **self.scope, turn_id=claimed["turn_id"], process_token="p2"
        )
        self.assertFalse(retry["claimed"])
        reference = completed["artifact"]
        self.assertEqual(self.store.get(reference["namespace"], reference["key"]), artifact)

    def test_failed_turn_keeps_user_message_and_explicit_retry_reuses_same_turn(self):
        claimed = self.store.claim_follow_up(
            **self.scope, request_id="request-123", question="请继续验证。", process_token="p1"
        )["turn"]
        failed = self.store.fail_follow_up(
            **self.scope, turn_id=claimed["turn_id"], process_token="p1"
        )
        self.assertEqual(failed["status"], "failed")
        retry = self.store.retry_follow_up(
            **self.scope, turn_id=claimed["turn_id"], process_token="p2"
        )
        self.assertTrue(retry["claimed"])
        self.assertEqual(retry["turn"]["user_message"], "请继续验证。")
        self.assertEqual(retry["turn"]["attempts"], 2)

    def test_workspace_product_and_event_scopes_do_not_cross_read(self):
        self.store.claim_follow_up(
            **self.scope, request_id="request-123", question="范围测试", process_token="p1"
        )
        self.assertEqual(self.store.follow_up_turns(**self.scope)[0]["user_message"], "范围测试")
        self.assertEqual(self.store.follow_up_turns(
            workspace_id="workspace-b", product_id="product-a", event_id="event-a"
        ), [])
        self.assertEqual(self.store.follow_up_turns(
            workspace_id="workspace-a", product_id="product-b", event_id="event-a"
        ), [])
        self.assertEqual(self.store.follow_up_turns(
            workspace_id="workspace-a", product_id="product-a", event_id="event-b"
        ), [])


if __name__ == "__main__":
    unittest.main()
