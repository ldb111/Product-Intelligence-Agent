"""关注与偏好产品操作测试；复用正式 Memory Writer/Store/Retriever。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from llm.retrieval_models import EmbeddingOutput
from memory import MemoryCandidate, MemoryRetriever, MemoryStore, PreferenceService


class FixtureEmbedding:
    """提供固定向量，只隔离外部模型，不替代 Memory 写入规则。"""

    space_id = "fixture/preferences"

    def embed(self, texts):
        return EmbeddingOutput([[1.0, 0.0] for _ in texts], [])


class PreferenceProductTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.embedding = FixtureEmbedding()
        self.candidates = []
        self.service = PreferenceService(
            self.store, embedding_factory=lambda: self.embedding,
            extractor=lambda text, source_kind: SimpleNamespace(candidates=list(self.candidates)),
        )

    def test_explicit_add_edit_and_remove_follow_memory_lifecycle(self):
        added = self.service.add("workspace-a", "focus", "持续关注企业权限治理。")
        self.assertEqual(self.service.list("workspace-a")["confirmed_items"], [added])
        retrieved = MemoryRetriever(self.store, self.embedding).retrieve_memory(
            "企业权限", scope="workspace", scope_id="workspace-a"
        )
        self.assertEqual([item.content for item in retrieved.results], ["持续关注企业权限治理。"])
        edited = self.service.edit(
            "workspace-a", added["item_id"], "focus", "持续关注企业权限治理与审计。"
        )
        active = self.service.list("workspace-a")["confirmed_items"]
        self.assertEqual([item["content"] for item in active], ["持续关注企业权限治理与审计。"])
        self.assertNotEqual(edited["item_id"], added["item_id"])
        self.assertFalse(self.store.get(added["item_id"]).is_active)
        self.service.remove("workspace-a", edited["item_id"])
        self.assertEqual(self.service.list("workspace-a")["confirmed_items"], [])
        removed = MemoryRetriever(self.store, self.embedding).retrieve_memory(
            "企业权限", scope="workspace", scope_id="workspace-a"
        )
        self.assertEqual(removed.status, "no_results")

    def test_extracted_candidate_is_not_retrievable_until_user_confirms(self):
        text = "我长期关注企业权限能力。"
        self.candidates = [MemoryCandidate(memory_type="focus", content=text)]
        pending = self.service.extract_from_user_message(
            "workspace-a", text, event_id="evt-1", turn_id="turn-1"
        )[0]
        before = MemoryRetriever(self.store, self.embedding).retrieve_memory(
            "企业权限", scope="workspace", scope_id="workspace-a"
        )
        self.assertEqual(before.status, "no_results")
        confirmed = self.service.confirm("workspace-a", pending["item_id"])
        self.assertEqual(confirmed["source"], "从研究交流中确认")
        after = MemoryRetriever(self.store, self.embedding).retrieve_memory(
            "企业权限", scope="workspace", scope_id="workspace-a"
        )
        self.assertEqual([item.content for item in after.results], [text])
        self.assertEqual(self.service.list("workspace-a")["awaiting_confirmation_items"], [])

    def test_ignored_candidate_and_other_workspace_are_excluded(self):
        text = "后续优先研究中国市场。"
        self.candidates = [MemoryCandidate(memory_type="focus", content=text)]
        pending = self.service.extract_from_user_message(
            "workspace-a", text, event_id="evt-1", turn_id="turn-1"
        )[0]
        self.assertEqual(self.service.list("workspace-b")["awaiting_confirmation_items"], [])
        self.service.ignore("workspace-a", pending["item_id"])
        self.assertEqual(self.service.list("workspace-a")["awaiting_confirmation_items"], [])

    def test_only_user_message_is_sent_to_candidate_extractor(self):
        seen = []
        service = PreferenceService(
            self.store, embedding_factory=lambda: self.embedding,
            extractor=lambda text, source_kind: (
                seen.append((text, source_kind)) or SimpleNamespace(candidates=[])
            ),
        )
        service.extract_from_user_message(
            "workspace-a", "我偏好中文结论。", event_id="evt-1", turn_id="turn-1"
        )
        self.assertEqual(seen, [("我偏好中文结论。", "research_context")])


if __name__ == "__main__":
    unittest.main()
