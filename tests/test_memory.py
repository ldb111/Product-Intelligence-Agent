"""Long-term Memory 控制测试；Mock 模型/向量只证明流程，不代表真实模型质量。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from llm.client import LLMCallError, RuntimeRecord
from llm.retrieval_models import EmbeddingAPIError, EmbeddingOutput
from memory import MemoryCandidate, MemoryRetriever, MemoryStore, MemoryWriter, extract_candidates
from memory.policy import validate_candidate
from memory.schemas import (
    MemoryCandidateBatch, MemoryPolicyError, MemoryReferences,
    MemoryStoreWriteError, MemoryVectorSpaceError,
)


def runtime():
    return RuntimeRecord(role="fast", provider="fixture", model="fixture-fast",
                         response_model="fixture-fast", latency_seconds=0, token_usage=None)


def vector_for(text: str):
    if any(word in text for word in ("权限", "中国", "企业版", "竞争事件")):
        return [1.0, 0.0]
    return [0.0, 1.0]


def embedding():
    client = Mock(space_id="fixture/text-embedding")
    client.embed.side_effect = lambda texts: EmbeddingOutput([vector_for(text) for text in texts], [])
    return client


FOCUS = "后续竞品研究优先关注中国市场和企业权限能力。"
PREFERENCE = "研究输出默认使用中文表格。"


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "memory.sqlite3"
        self.store = MemoryStore(self.path)
        self.embedding = embedding()
        self.writer = MemoryWriter(self.store, self.embedding)
        self.retriever = MemoryRetriever(self.store, self.embedding)

    def remember(self, content=FOCUS, **changes):
        values = dict(
            scope="workspace", scope_id="pia-fixture", memory_type="focus", content=content,
            source_reference="fixture://session/1#message-1", source_text=content,
            memory_key="competitor_research_focus", metadata={"fixture": True},
        )
        return self.writer.remember_explicit(**{**values, **changes})

    def test_explicit_focus_is_confirmed_active_and_traceable(self):
        result = self.remember()
        self.assertEqual(result.action, "inserted")
        self.assertEqual(result.memory.status, "confirmed")
        self.assertTrue(result.memory.is_active)
        self.assertEqual(result.memory.source_reference, "fixture://session/1#message-1")
        self.assertEqual(result.memory.source_text, FOCUS)

    def test_explicit_preference_is_written(self):
        result = self.remember(
            PREFERENCE, memory_type="preference", memory_key="output_language_format",
            source_text=PREFERENCE,
        )
        self.assertEqual(result.memory.memory_type, "preference")
        self.assertEqual(result.memory.content, PREFERENCE)

    def test_all_minimal_memory_types_have_finite_contract(self):
        examples = {
            "focus": FOCUS,
            "preference": PREFERENCE,
            "constraint": "企业研究必须仅限中国市场。",
            "goal": "持续跟踪企业权限商业化是长期目标。",
            "decision": "已经决定后续采用月度竞品复盘。",
            "hypothesis": "当前假设企业权限可能决定大客户转化。",
            "open_question": "尚未确认企业版权限是否覆盖外部协作。",
        }
        for kind, text in examples.items():
            with self.subTest(kind=kind):
                candidate = MemoryCandidate(memory_type=kind, content=text)
                validate_candidate(candidate, source_text=text, source_kind="direct_user")

    def test_fast_llm_extracts_only_source_supported_candidate(self):
        client = Mock()
        client.decide.return_value = MemoryCandidateBatch(
            candidates=[MemoryCandidate(memory_type="focus", content="中国市场和企业权限能力")]
        ), runtime()
        output = extract_candidates(FOCUS, source_kind="direct_user", client=client)
        self.assertEqual(output.candidates[0].content, "中国市场和企业权限能力")
        self.assertEqual(output.rejected_count, 0)
        prompt, payload, schema = client.decide.call_args.args
        self.assertEqual(payload, {"source_text": FOCUS})
        self.assertIs(schema, MemoryCandidateBatch)
        self.assertNotIn("provider", prompt.casefold())

    def test_focus_word_can_support_nearest_preference_type_without_becoming_fact(self):
        candidate = MemoryCandidate(memory_type="preference", content=FOCUS)
        validate_candidate(candidate, source_text=FOCUS, source_kind="direct_user")

    def test_default_extraction_uses_fast_role(self):
        client = Mock()
        client.decide.return_value = MemoryCandidateBatch(candidates=[]), runtime()
        with patch("memory.extraction.get_llm", return_value=client) as get:
            output = extract_candidates("没有值得长期记录的普通句子。", source_kind="direct_user")
        get.assert_called_once_with(role="fast")
        self.assertEqual(output.candidates, [])

    def test_no_long_term_value_produces_no_write(self):
        client = Mock()
        client.decide.return_value = MemoryCandidateBatch(candidates=[]), runtime()
        output = extract_candidates("请打开这份文件。", source_kind="direct_user", client=client)
        self.assertEqual(output.candidates, [])
        self.assertEqual(self.store.counts()["total"], 0)

    def test_temporary_operation_is_rejected(self):
        text = "本次仅计划运行测试。"
        candidate = MemoryCandidate(memory_type="goal", content=text)
        with self.assertRaisesRegex(MemoryPolicyError, "一次性"):
            self.writer.remember_extracted(
                candidate, scope="research", scope_id="fixture", source_reference="fixture://temp",
                source_text=text,
            )

    def test_event_evidence_snapshot_and_knowledge_facts_are_rejected(self):
        fact = "产品支持文档级权限。"
        candidate = MemoryCandidate(memory_type="focus", content=fact)
        for kind in ("event", "evidence", "snapshot", "knowledge", "pricing", "web_page"):
            with self.subTest(kind=kind), self.assertRaises(MemoryPolicyError):
                self.writer.remember_extracted(
                    candidate, scope="workspace", scope_id="fixture", source_reference="fixture://fact",
                    source_text=fact, source_kind=kind,
                )
        self.embedding.embed.assert_not_called()

    def test_fact_cannot_be_rewritten_as_inferred_focus(self):
        client = Mock()
        client.decide.return_value = MemoryCandidateBatch(
            candidates=[MemoryCandidate(memory_type="focus", content="产品支持文档级权限。")]
        ), runtime()
        output = extract_candidates("产品支持文档级权限。", source_kind="direct_user", client=client)
        self.assertEqual(output.candidates, [])
        self.assertEqual(output.rejected_count, 1)

    def test_secret_is_rejected_before_llm_embedding_or_storage(self):
        secret_text = "后续优先关注权限，API_KEY=fixture-secret-value。"
        client = Mock()
        with self.assertRaisesRegex(MemoryPolicyError, "秘密"):
            extract_candidates(secret_text, source_kind="direct_user", client=client)
        with self.assertRaises(MemoryPolicyError):
            self.writer.remember_explicit(
                scope="user", scope_id="fixture", memory_type="focus", content=secret_text,
                source_reference="fixture://secret", source_text=secret_text,
            )
        client.decide.assert_not_called()
        self.embedding.embed.assert_not_called()
        self.assertEqual(self.store.counts()["total"], 0)

    def test_unsupported_or_inferred_content_is_rejected(self):
        candidate = MemoryCandidate(memory_type="preference", content="用户偏好低价产品。")
        with self.assertRaisesRegex(MemoryPolicyError, "逐字"):
            self.writer.remember_extracted(
                candidate, scope="user", scope_id="fixture", source_reference="fixture://inference",
                source_text="用户浏览了低价产品页面。",
            )

    def test_confirmed_is_semantically_retrievable_with_memory_context_marker(self):
        self.remember()
        self.remember(
            PREFERENCE, memory_type="preference", memory_key="output_language_format",
            source_text=PREFERENCE,
        )
        output = self.retriever.retrieve_memory(
            "分析新的企业版竞争事件时应该重点考虑什么？", scope="workspace", scope_id="pia-fixture", top_k=1,
        )
        self.assertEqual(output.status, "ok")
        self.assertEqual(output.results[0].content, FOCUS)
        self.assertEqual(output.results[0].context_type, "memory")
        self.assertEqual(output.results[0].retrieval_score, 1)

    def test_pending_is_not_embedded_or_retrieved(self):
        candidate = MemoryCandidate(memory_type="focus", content=FOCUS)
        result = self.writer.remember_extracted(
            candidate, scope="workspace", scope_id="pia-fixture",
            source_reference="fixture://extracted", source_text=FOCUS,
        )
        self.assertEqual(result.action, "pending")
        self.embedding.embed.assert_not_called()
        output = self.retriever.retrieve_memory("企业权限", scope="workspace", scope_id="pia-fixture")
        self.assertEqual(output.status, "no_results")
        self.embedding.embed.assert_not_called()

    def test_explicit_same_key_supersedes_and_keeps_history(self):
        old = self.remember().memory
        new_text = "后续竞品研究改为优先关注日韩市场和商业化能力。"
        result = self.remember(new_text, source_text=new_text, source_reference="fixture://session/2#message-1")
        self.assertEqual(result.action, "superseded")
        self.assertEqual(result.superseded_memory_id, old.memory_id)
        self.assertEqual(result.memory.supersedes_memory_id, old.memory_id)
        previous = self.store.get(old.memory_id)
        self.assertEqual(previous.status, "superseded")
        self.assertFalse(previous.is_active)
        self.assertEqual(self.store.counts(), {"total": 2, "pending": 0, "confirmed": 1, "superseded": 1})

    def test_superseded_is_not_retrieved(self):
        old = self.remember().memory
        new_text = "后续竞品研究改为优先关注日韩市场和商业化能力。"
        new = self.remember(new_text, source_text=new_text).memory
        output = self.retriever.retrieve_memory("竞品研究重点", scope="workspace", scope_id="pia-fixture", top_k=5)
        self.assertEqual([item.memory_id for item in output.results], [new.memory_id])
        self.assertNotIn(old.memory_id, [item.memory_id for item in output.results])

    def test_ambiguous_extracted_conflict_stays_pending_without_overwrite(self):
        old = self.remember().memory
        text = "后续研究可能重点关注日韩市场。"
        result = self.writer.remember_extracted(
            MemoryCandidate(memory_type="focus", content=text), scope="workspace", scope_id="pia-fixture",
            source_reference="fixture://uncertain", source_text=text, memory_key="competitor_research_focus",
        )
        self.assertEqual(result.memory.status, "pending")
        self.assertEqual(self.store.get(old.memory_id).status, "confirmed")
        self.assertTrue(self.store.get(old.memory_id).is_active)

    def test_exact_duplicate_is_unchanged_without_embedding(self):
        first = self.remember()
        self.embedding.embed.reset_mock()
        second = self.remember()
        self.assertEqual(second.action, "unchanged")
        self.assertEqual(second.memory.memory_id, first.memory.memory_id)
        self.embedding.embed.assert_not_called()

    def test_scope_and_scope_id_are_both_isolated(self):
        self.remember(scope="user", scope_id="alice", memory_key="focus_a")
        self.remember(scope="workspace", scope_id="pia-fixture", memory_key="focus_b")
        self.remember(scope="workspace", scope_id="other", memory_key="focus_c")
        user = self.retriever.retrieve_memory("企业权限", scope="user", scope_id="alice")
        workspace = self.retriever.retrieve_memory("企业权限", scope="workspace", scope_id="pia-fixture")
        other = self.retriever.retrieve_memory("企业权限", scope="workspace", scope_id="missing")
        self.assertEqual(len(user.results), 1)
        self.assertEqual(len(workspace.results), 1)
        self.assertEqual(other.results, [])

    def test_type_filter_is_programmatic_before_embedding(self):
        self.remember()
        self.remember(PREFERENCE, memory_type="preference", memory_key="format", source_text=PREFERENCE)
        output = self.retriever.retrieve_memory(
            "研究偏好", scope="workspace", scope_id="pia-fixture", memory_types=("preference",),
        )
        self.assertEqual({r.memory_type for r in output.results}, {"preference"})

    def test_process_restart_keeps_memory_and_vector(self):
        original = self.remember().memory
        reopened = MemoryStore(self.path)
        output = MemoryRetriever(reopened, self.embedding).retrieve_memory(
            "企业版权限", scope="workspace", scope_id="pia-fixture",
        )
        self.assertEqual(output.results[0].memory_id, original.memory_id)

    def test_embedding_failure_does_not_supersede_old_memory(self):
        old = self.remember().memory
        self.embedding.embed.side_effect = EmbeddingAPIError("fixture failure")
        update = "后续竞品研究改为优先关注日韩市场和商业化能力。"
        with self.assertRaises(EmbeddingAPIError):
            self.remember(update, source_text=update)
        current = self.store.get(old.memory_id)
        self.assertEqual(current.status, "confirmed")
        self.assertTrue(current.is_active)
        self.assertEqual(self.store.counts()["total"], 1)

    def test_transaction_failure_rolls_back_supersession(self):
        old = self.remember().memory
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TRIGGER fail_confirmed BEFORE INSERT ON memories
                WHEN NEW.status='confirmed' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END""")
        update = "后续竞品研究改为优先关注日韩市场和商业化能力。"
        with self.assertRaises(MemoryStoreWriteError):
            self.remember(update, source_text=update)
        current = self.store.get(old.memory_id)
        self.assertEqual(current.status, "confirmed")
        self.assertTrue(current.is_active)
        self.assertEqual(self.store.counts()["total"], 1)

    def test_vector_space_change_fails_before_query_embedding(self):
        self.remember()
        self.embedding.space_id = "fixture/changed-model"
        self.embedding.embed.reset_mock()
        with self.assertRaises(MemoryVectorSpaceError):
            self.retriever.retrieve_memory("企业权限", scope="workspace", scope_id="pia-fixture")
        self.embedding.embed.assert_not_called()

    def test_reference_ids_do_not_copy_fact_objects(self):
        refs = MemoryReferences(event_id="event-1", document_id="doc-1", competitor_id="rival-1", research_id="research-1")
        record = self.remember(references=refs).memory
        self.assertEqual(record.references, refs)
        serialized = record.model_dump_json()
        self.assertNotIn("evidence_status", serialized)
        self.assertNotIn("change_items", serialized)

    def test_soft_deactivate_and_physical_delete_are_available(self):
        record = self.remember().memory
        self.assertTrue(self.store.deactivate(record.memory_id, datetime.now(timezone.utc)))
        output = self.retriever.retrieve_memory("权限", scope="workspace", scope_id="pia-fixture")
        self.assertEqual(output.results, [])
        self.assertTrue(self.store.delete(record.memory_id))
        self.assertIsNone(self.store.get(record.memory_id))

    def test_llm_failure_does_not_create_memory(self):
        client = Mock()
        client.decide.side_effect = LLMCallError("fixture failure")
        with self.assertRaises(LLMCallError):
            extract_candidates(FOCUS, source_kind="direct_user", client=client)
        self.assertEqual(self.store.counts()["total"], 0)

    def test_invalid_query_and_type_filter_fail_before_embedding(self):
        self.remember()
        self.embedding.embed.reset_mock()
        for kwargs in ({"query": ""}, {"query": "权限", "top_k": 0},
                       {"query": "权限", "memory_types": ("unknown",)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.retriever.retrieve_memory(scope="workspace", scope_id="pia-fixture", **kwargs)
        self.embedding.embed.assert_not_called()

    def test_source_reference_with_secret_is_rejected(self):
        with self.assertRaises(MemoryPolicyError):
            self.remember(source_reference="fixture://session?token=secret-value-123")
        self.embedding.embed.assert_not_called()

    def test_secret_query_is_rejected_before_embedding(self):
        self.remember()
        self.embedding.embed.reset_mock()
        with self.assertRaisesRegex(ValueError, "秘密"):
            self.retriever.retrieve_memory(
                "查找 API_KEY=fixture-secret-value", scope="workspace", scope_id="pia-fixture",
            )
        self.embedding.embed.assert_not_called()

    def test_business_result_does_not_expose_model_configuration(self):
        self.remember()
        output = self.retriever.retrieve_memory("权限", scope="workspace", scope_id="pia-fixture")
        serialized = output.model_dump_json()
        for internal in ("api_key", "endpoint", "provider", "response_model", "token_usage"):
            self.assertNotIn(internal, serialized)


if __name__ == "__main__":
    unittest.main()
