"""知识层控制逻辑测试；所有模拟向量/分数均为测试替身，不证明真实 RAG 质量。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import Mock

from pydantic import ValidationError

from knowledge import KnowledgeDocument, KnowledgeIngestor, KnowledgeRetriever, KnowledgeStore
from knowledge.chunking import chunk_document
from knowledge.inputs import event_documents, own_product_file
from knowledge.schemas import (
    EmptyChunkError, InvalidDomainError, KnowledgeAdmissionError, StoreWriteError, VectorSpaceError,
)
from llm.retrieval_models import EmbeddingAPIError, EmbeddingOutput, RerankAPIError, RerankItem, RerankOutput


def fixture(domain="own_product", document_id="fixture-document", **changes):
    """明确标注的测试资料，绝不作为正式产品事实入库。"""
    values = dict(
        knowledge_domain=domain, document_id=document_id, title="测试 Fixture 产品",
        content="# 套餐\n\n测试 Fixture：免费档每月提供积分。\n\n## 权限\n\n测试 Fixture：支持文档权限。",
        product_id="fixture-product" if domain == "own_product" else None,
        competitor_id="fixture-competitor" if domain == "competitor" else None,
        source_reference="fixture://knowledge-test", source_type="prd" if domain == "own_product" else "verified_official_content",
        source_credibility="user_provided" if domain == "own_product" else "official",
        source_url=None if domain == "own_product" else "https://example.invalid/docs?version=1#pricing",
        verification_reference=None if domain == "own_product" else "fixture://verification",
        evidence_status=None if domain == "own_product" else "confirmed",
        observed_at="2026-09-18T10:00:00+08:00", metadata={"fixture": True},
    )
    return KnowledgeDocument(**{**values, **changes})


def embedding():
    client = Mock(space_id="fixture/test-embedding")
    client.embed.side_effect = lambda texts: EmbeddingOutput([[1.0, 0.0] for _ in texts], [])
    return client


def rerank():
    client = Mock()
    client.rerank.side_effect = lambda query, documents, k: RerankOutput(
        [RerankItem(i, 0.9 - rank / 100) for rank, i in enumerate(reversed(range(len(documents))))][:k], [])
    return client


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "knowledge.sqlite3"
        self.store = KnowledgeStore(self.path)
        self.embedding = embedding()
        self.rerank = rerank()
        self.ingestor = KnowledgeIngestor(self.store, self.embedding)
        self.retriever = KnowledgeRetriever(self.store, self.embedding, self.rerank)

    def test_own_product_ingestion_and_persistence(self):
        result = self.ingestor.ingest(fixture())
        self.assertEqual(result.status, "inserted")
        reopened = KnowledgeStore(self.path)
        self.assertEqual(reopened.counts(), {"documents": 1, "chunks": 2})
        self.assertEqual(reopened.document_record("own_product", result.document_id)[1].content, fixture().content)

    def test_competitor_ingestion_preserves_source_metadata(self):
        document = fixture("competitor")
        self.ingestor.ingest(document)
        result = self.retriever.retrieve("积分", knowledge_domain="competitor").results[0]
        for name in ("source_url", "source_reference", "evidence_status", "competitor_id", "observed_at", "metadata"):
            self.assertEqual(getattr(result, name), getattr(document, name))

    def test_domains_are_isolated_even_for_same_id_content_and_vector(self):
        own = self.ingestor.ingest(fixture())
        rival = self.ingestor.ingest(fixture("competitor"))
        self.assertTrue(set(own.chunk_ids).isdisjoint(rival.chunk_ids))
        for domain in ("own_product", "competitor"):
            output = self.retriever.retrieve("套餐", knowledge_domain=domain)
            self.assertEqual({r.knowledge_domain for r in output.candidates + output.results}, {domain})

    def test_both_is_explicit_and_returns_both_domains(self):
        self.ingestor.ingest(fixture())
        self.ingestor.ingest(fixture("competitor"))
        output = self.retriever.retrieve("套餐", knowledge_domain="both")
        self.assertEqual({r.knowledge_domain for r in output.results}, {"own_product", "competitor"})
        with self.assertRaises(TypeError):
            self.retriever.retrieve("套餐")

    def test_invalid_domain_fails_before_embedding(self):
        with self.assertRaises(InvalidDomainError):
            self.retriever.retrieve("套餐", knowledge_domain="all")
        self.embedding.embed.assert_not_called()
        with self.assertRaises(ValidationError):
            fixture("both")

    def test_structure_boundaries_preserve_heading_context_and_lists(self):
        doc = fixture(content="# 产品\n\n## 权限\n\n- 管理员\n- 编辑者\n\n## 定价\n\n免费套餐。")
        chunks = chunk_document(doc)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].heading_path, ["产品", "权限"])
        self.assertIn("- 管理员\n- 编辑者", chunks[0].content)
        self.assertEqual(chunks[1].heading_path, ["产品", "定价"])
        self.assertIn(doc.title, chunks[0].model_text())

    def test_long_section_fallback_loses_no_body_and_preserves_context(self):
        body = "测试产品支持文档权限和免费积分。" * 70
        chunks = chunk_document(fixture(content="# 产品权限\n\n" + body), max_chars=100)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(c.content for c in chunks), body)
        self.assertTrue(all(len(c.content) <= 100 and c.heading_path == ["产品权限"] for c in chunks))

    def test_oversize_table_repeats_header_as_context(self):
        header = "| 套餐 | 积分 |\n| --- | --- |"
        chunks = chunk_document(fixture(content="# Pricing\n\n" + header + "\n" + "| 免费 | 100 |\n" * 40), 100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.table_context == header for c in chunks))

    def test_fenced_code_hash_is_not_heading(self):
        chunks = chunk_document(fixture(content="# 示例\n\n```python\n# 注释\nx = 1\n```\n\n正文"))
        self.assertEqual(chunks[0].heading_path, ["示例"])
        self.assertIn("# 注释", chunks[0].content)

    def test_duplicate_normalized_content_skips_model_and_data_growth(self):
        first = self.ingestor.ingest(fixture())
        self.embedding.embed.reset_mock()
        for _ in range(3):
            repeated = self.ingestor.ingest(fixture(content="\n" + fixture().content.replace("\n", "\r\n") + "\n"))
            self.assertEqual(repeated.status, "unchanged")
            self.assertEqual(first.chunk_ids, repeated.chunk_ids)
            self.assertEqual(first.content_hash, repeated.content_hash)
        self.embedding.embed.assert_not_called()
        self.assertEqual(self.store.counts()["chunks"], 2)

    def test_upsert_removes_old_chunks(self):
        first = self.ingestor.ingest(fixture())
        update = self.ingestor.ingest(fixture(content="测试 Fixture：只保留新定价。"))
        self.assertEqual(update.status, "updated")
        self.assertEqual(self.store.counts(), {"documents": 1, "chunks": 1})
        output = self.retriever.retrieve("定价", knowledge_domain="own_product")
        self.assertTrue(set(first.chunk_ids).isdisjoint({r.chunk_id for r in output.results}))

    def test_metadata_only_update_refreshes_citation(self):
        first = self.ingestor.ingest(fixture())
        second = self.ingestor.ingest(fixture(source_reference="fixture://new-location", metadata={"fixture": True, "revision": 2}))
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(second.status, "updated")
        output = self.retriever.retrieve("权限", knowledge_domain="own_product")
        self.assertEqual(output.results[0].source_reference, "fixture://new-location")

    def test_top_n_then_rerank_top_k_uses_original_candidate_indexes(self):
        for i in range(5):
            self.ingestor.ingest(fixture(document_id=f"fixture-{i}", content=f"Fixture 第 {i} 个套餐"))
        output = self.retriever.retrieve("套餐", knowledge_domain="own_product", top_n=3, top_k=2)
        self.assertEqual(len(output.candidates), 3)
        self.assertEqual(len(self.rerank.rerank.call_args.args[1]), 3)
        self.assertEqual([r.chunk_id for r in output.results], [output.candidates[2].chunk_id, output.candidates[1].chunk_id])
        self.assertTrue(all(r.rerank_score is not None for r in output.results))

    def test_real_cosine_order_not_lexical_search(self):
        self.embedding.embed.side_effect = lambda texts: EmbeddingOutput([[1, 0] if "甲" in t else [0, 1] for t in texts], [])
        self.ingestor.ingest(fixture(document_id="a", content="甲"))
        self.ingestor.ingest(fixture(document_id="b", content="乙"))
        output = KnowledgeRetriever(self.store, self.embedding, None).retrieve("甲", knowledge_domain="own_product", top_n=2, top_k=1)
        self.assertEqual(output.results[0].document_id, "a")
        self.assertEqual(output.results[0].retrieval_score, 1)
        self.assertIsNone(output.results[0].rerank_score)

    def test_metadata_filter_applies_before_top_n_and_rerank(self):
        self.ingestor.ingest(fixture(product_id="allowed"))
        self.ingestor.ingest(fixture(document_id="other", product_id="excluded"))
        output = self.retriever.retrieve("权限", knowledge_domain="own_product", top_n=1, top_k=1, filters={"product_id": "allowed"})
        self.assertEqual(output.results[0].product_id, "allowed")

    def test_every_result_points_to_exact_persisted_source_interval(self):
        self.ingestor.ingest(fixture())
        output = self.retriever.retrieve("权限", knowledge_domain="own_product")
        for result in output.results:
            doc = self.store.document_record(result.knowledge_domain, result.document_id)[1]
            self.assertEqual(doc.content[result.start_offset:result.end_offset], result.content)
            self.assertTrue(result.chunk_id and result.content_hash and result.source_reference)

    def test_pending_conflicting_and_unverified_candidates_rejected(self):
        for changes in ({"evidence_status": "pending"}, {"evidence_status": "conflict"},
                        {"source_type": "search_result"}, {"verification_reference": None},
                        {"source_credibility": "unverified"}, {"source_type": "research_result"}):
            with self.subTest(changes=changes), self.assertRaises(KnowledgeAdmissionError):
                self.ingestor.ingest(fixture("competitor", **changes))
        self.embedding.embed.assert_not_called()

    def test_own_document_requires_explicit_user_material(self):
        with self.assertRaises(KnowledgeAdmissionError):
            self.ingestor.ingest(fixture(source_credibility="unverified"))

    def test_empty_document_and_empty_chunk_are_distinct_failures(self):
        with self.assertRaisesRegex(ValidationError, "文档正文为空"):
            fixture(content=" \n")
        with self.assertRaises(EmptyChunkError):
            self.ingestor.ingest(fixture(content="# 只有标题"))
        self.embedding.embed.assert_not_called()

    def test_api_failure_leaves_old_document_intact(self):
        self.ingestor.ingest(fixture())
        self.embedding.embed.side_effect = EmbeddingAPIError("测试失败")
        with self.assertRaises(EmbeddingAPIError):
            self.ingestor.ingest(fixture(content="新版本"))
        self.assertEqual(self.store.document_record("own_product", "fixture-document")[1].content, fixture().content)

    def test_store_failure_rolls_back_document_and_chunks(self):
        self.ingestor.ingest(fixture())
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TRIGGER fail_chunk BEFORE INSERT ON chunks BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        with self.assertRaises(StoreWriteError):
            self.ingestor.ingest(fixture(content="新版本"))
        self.assertEqual(self.store.counts()["chunks"], 2)
        self.assertEqual(self.store.document_record("own_product", "fixture-document")[1].content, fixture().content)

    def test_rerank_failure_does_not_return_fake_or_unranked_results(self):
        self.ingestor.ingest(fixture())
        self.rerank.rerank.side_effect = RerankAPIError("测试失败")
        with self.assertRaises(RerankAPIError):
            self.retriever.retrieve("权限", knowledge_domain="own_product")

    def test_no_results_is_explicit_and_does_not_call_rerank(self):
        output = self.retriever.retrieve("权限", knowledge_domain="own_product")
        self.assertEqual(output.status, "no_results")
        self.assertEqual(output.results, [])
        self.rerank.rerank.assert_not_called()

    def test_space_change_fails_before_paid_query_or_duplicate_ingestion(self):
        self.ingestor.ingest(fixture())
        self.embedding.space_id = "fixture/other-model"
        self.embedding.embed.reset_mock()
        with self.assertRaises(VectorSpaceError):
            self.ingestor.ingest(fixture())
        with self.assertRaises(VectorSpaceError):
            self.retriever.retrieve("权限", knowledge_domain="own_product")
        self.embedding.embed.assert_not_called()

    def test_dimension_mismatch_does_not_silently_zip_shorter_vectors(self):
        self.ingestor.ingest(fixture())
        self.embedding.embed.side_effect = lambda texts: EmbeddingOutput([[1, 0, 0]], [])
        with self.assertRaises(VectorSpaceError):
            self.retriever.retrieve("权限", knowledge_domain="own_product")

    def test_invalid_top_counts_and_filters_fail_before_models(self):
        for kwargs in ({"top_n": 1, "top_k": 2}, {"top_n": 0}, {"filters": {"unknown": True}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.retriever.retrieve("权限", knowledge_domain="own_product", **kwargs)
        self.embedding.embed.assert_not_called()

    def test_user_file_adapter_keeps_original_reference_and_null_time(self):
        path = Path(self.directory.name) / "fixture.md"
        path.write_text("# 测试 Fixture\n\n套餐说明", encoding="utf-8")
        doc = own_product_file(path, document_id="fixture-file", product_id="fixture-product", title="Fixture", source_type="pricing")
        self.assertEqual(doc.source_reference, str(path.resolve()))
        self.assertIsNone(doc.observed_at)
        self.assertEqual(self.ingestor.ingest(doc).status, "inserted")

    def test_event_adapter_keeps_individual_evidence_provenance(self):
        from tests.test_event_core import data, detect_with_mock
        event = detect_with_mock(data())[0].candidates[0]
        docs = event_documents(event, competitor_id="fixture-rival", source_reference="fixture://event")
        self.assertEqual(len(docs), len(event.evidence))
        for doc, evidence in zip(docs, event.evidence):
            self.assertEqual(doc.source_url, evidence.source_url)
            self.assertEqual(doc.observed_at, evidence.observed_at)
            self.assertEqual(doc.content, evidence.relevant_excerpt)
            self.assertEqual(doc.metadata["evidence_id"], evidence.evidence_id)
            self.assertEqual(doc.evidence_status, "confirmed")
            self.ingestor.ingest(doc)

    def test_model_visible_results_do_not_include_runtime_configuration(self):
        self.ingestor.ingest(fixture())
        output = self.retriever.retrieve("权限", knowledge_domain="own_product")
        serialized = output.model_dump_json()
        for internal in ("api_key", "endpoint", "provider", "response_model", "token_usage"):
            self.assertNotIn(internal, serialized)


if __name__ == "__main__":
    unittest.main()
