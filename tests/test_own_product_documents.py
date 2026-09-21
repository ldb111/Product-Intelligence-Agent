"""Own Product Document Service 的产品接线、原子替换与隔离测试。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from docx import Document
from openpyxl import Workbook
from pypdf import PdfWriter

from knowledge import KnowledgeRetriever, KnowledgeStore
from llm.retrieval_models import EmbeddingOutput, RerankItem, RerankOutput
from pipeline.documents import (
    OwnProductDocumentService, product_scope_id, workspace_knowledge_path,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "experiments" / "fixtures" / "file_parsing"


class ControlledEmbedding:
    """固定二维向量，测试只验证接线、过滤和存储，不冒充语义质量评测。"""

    space_id = "controlled/own-product-documents-v1"

    def __init__(self):
        self.calls = 0
        self.fail = False

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("controlled embedding failure")
        return EmbeddingOutput([[1.0, 0.0] for _ in texts], [])


class ControlledRerank:
    def rerank(self, query, documents, top_k):
        return RerankOutput(
            [RerankItem(index=index, score=1.0 - index / 1000)
             for index in range(min(top_k, len(documents)))], []
        )


class OwnProductDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.embedding = ControlledEmbedding()
        self.rerank = ControlledRerank()
        self.service = OwnProductDocumentService(
            self.state, embedding_factory=lambda: self.embedding
        )
        self.workspace = "workspace-a"
        self.product = "product-a"
        self.scope = product_scope_id(self.workspace, self.product)

    def store(self, workspace=None):
        return KnowledgeStore(workspace_knowledge_path(self.state, workspace or self.workspace))

    def retrieve(self, query="产品资料", *, workspace=None, product_scope=None,
                 domain="own_product"):
        store = self.store(workspace)
        return KnowledgeRetriever(store, self.embedding, self.rerank).retrieve(
            query, knowledge_domain=domain, top_n=100, top_k=100,
            filters={"product_id": product_scope or self.scope},
        )

    def write(self, relative, content, *, binary=False):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def markdown(self, relative, sections):
        return self.write(relative, "\n\n".join(
            f"# {heading}\n\n{body}" for heading, body in sections
        ))

    def workbook(self, relative, sheets):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        workbook = Workbook()
        workbook.remove(workbook.active)
        for name, value in sheets:
            sheet = workbook.create_sheet(name)
            sheet.append(["项目", "内容"])
            sheet.append([name, value])
        workbook.save(path)
        return path

    def test_five_formats_use_product_entry_and_are_retrievable_with_provenance(self):
        for kind in ("txt", "md", "docx", "pdf", "xlsx"):
            with self.subTest(kind=kind):
                product = "five-format-" + kind
                scope = product_scope_id(self.workspace, product)
                result = self.service.add_own_product_document(
                    workspace_id=self.workspace, product_id=product,
                    file=FIXTURES / f"demo.{kind}",
                )
                self.assertEqual((result.parsing_status, result.ingestion_status),
                                 ("parsed", "completed"))
                response = self.retrieve(product_scope=scope)
                matched = [item for item in response.results
                           if item.document_id.startswith(result.document_id + ":unit:")]
                self.assertTrue(matched)
                self.assertTrue(all(item.rerank_score is not None for item in matched))
                self.assertTrue(all(item.knowledge_domain == "own_product" for item in matched))
                metadata = matched[0].metadata
                self.assertRegex(metadata["source_file_sha256"], r"^[0-9a-f]{64}$")
                if kind == "pdf":
                    self.assertIsNotNone(metadata["page_number"])
                elif kind == "xlsx":
                    self.assertIsNotNone(metadata["sheet_name"])
                    self.assertIsNotNone(metadata["cell_range"])
                elif kind in {"docx", "md"}:
                    self.assertTrue(metadata["heading_path"])

    def test_add_exact_duplicate_across_paths_and_same_content_replace_are_free(self):
        first_path = self.write("one/product.txt", "完全相同的产品资料。")
        second_path = self.root / "two" / "renamed.txt"
        second_path.parent.mkdir()
        shutil.copyfile(first_path, second_path)
        first = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=first_path
        )
        counts = self.store().counts()
        calls = self.embedding.calls
        same_path = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=first_path
        )
        repeated = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=second_path
        )
        same_replace = self.service.replace_own_product_document(
            workspace_id=self.workspace, product_id=self.product,
            document_id=first.document_id, new_file=second_path,
        )
        self.assertEqual(same_path.document_id, first.document_id)
        self.assertEqual(same_path.content_status, "existing")
        self.assertEqual(repeated.document_id, first.document_id)
        self.assertEqual(repeated.content_status, "existing")
        self.assertEqual(same_replace.content_status, "unchanged")
        self.assertEqual(self.store().counts(), counts)
        self.assertEqual(self.embedding.calls, calls)

    def test_same_name_with_different_content_requires_explicit_replace(self):
        first = self.write("a/manual.txt", "旧内容")
        second = self.write("b/manual.txt", "不同的新内容")
        created = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=first
        )
        calls = self.embedding.calls
        conflict = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=second
        )
        self.assertEqual(conflict.content_status, "conflict")
        self.assertEqual(conflict.document_id, created.document_id)
        self.assertEqual(self.embedding.calls, calls)
        self.assertIn("明确选择更新", conflict.failure_reason)

    def test_concurrent_same_bytes_add_has_one_logical_document_and_one_knowledge_set(self):
        """两个请求都通过事务外预检时，Store 仍原子收敛到同一逻辑文档。"""
        first = self.write("parallel/one.txt", "CONCURRENT_EXACT_DUPLICATE")
        second = self.root / "parallel-copy" / "renamed.txt"
        second.parent.mkdir()
        shutil.copyfile(first, second)
        barrier = threading.Barrier(2)
        original = KnowledgeStore.replace_document_set

        def enter_transaction_together(store, **kwargs):
            barrier.wait(timeout=5)
            return original(store, **kwargs)

        with patch.object(KnowledgeStore, "replace_document_set", enter_transaction_together):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(
                    self.service.add_own_product_document,
                    workspace_id=self.workspace, product_id=self.product, file=path,
                ) for path in (first, second)]
                results = [future.result(timeout=10) for future in futures]

        records = self.store().document_sets("own_product", self.scope)
        self.assertEqual(len(records), 1)
        self.assertEqual({result.document_id for result in results}, {records[0]["document_id"]})
        self.assertEqual({result.content_status for result in results}, {"current", "existing"})
        self.assertEqual(self.store().counts(), {"documents": 1, "chunks": 1})
        self.assertEqual(len(self.retrieve().candidates), 1)
        # 两次预计算允许重复调用 Embedding，但事务内唯一约束禁止重复持久化。
        self.assertEqual(self.embedding.calls, 3)

    def test_concurrent_different_content_add_preserves_name_rules(self):
        """不同名称分别创建；同名不同内容即使同时进入事务也返回受控冲突。"""
        original = KnowledgeStore.replace_document_set

        different_product = "parallel-different-names"
        different_scope = product_scope_id(self.workspace, different_product)
        different_paths = (
            self.write("different/one.txt", "DIFFERENT_CONTENT_ONE"),
            self.write("different/two.txt", "DIFFERENT_CONTENT_TWO"),
        )
        barrier = threading.Barrier(2)

        def enter_different_transaction_together(store, **kwargs):
            barrier.wait(timeout=5)
            return original(store, **kwargs)

        with patch.object(KnowledgeStore, "replace_document_set",
                          enter_different_transaction_together):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = [future.result(timeout=10) for future in [
                    pool.submit(self.service.add_own_product_document,
                                workspace_id=self.workspace, product_id=different_product,
                                file=path)
                    for path in different_paths
                ]]
        self.assertEqual({result.content_status for result in results}, {"current"})
        self.assertEqual(len({result.document_id for result in results}), 2)
        self.assertEqual(len(self.store().document_sets("own_product", different_scope)), 2)

        same_name_product = "parallel-same-name"
        same_name_scope = product_scope_id(self.workspace, same_name_product)
        same_name_paths = (
            self.write("same-name/a/manual.txt", "SAME_NAME_CONTENT_A"),
            self.write("same-name/b/manual.txt", "SAME_NAME_CONTENT_B"),
        )
        barrier = threading.Barrier(2)

        def enter_same_name_transaction_together(store, **kwargs):
            barrier.wait(timeout=5)
            return original(store, **kwargs)

        with patch.object(KnowledgeStore, "replace_document_set",
                          enter_same_name_transaction_together):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = [future.result(timeout=10) for future in [
                    pool.submit(self.service.add_own_product_document,
                                workspace_id=self.workspace, product_id=same_name_product,
                                file=path)
                    for path in same_name_paths
                ]]
        records = self.store().document_sets("own_product", same_name_scope)
        self.assertEqual(len(records), 1)
        self.assertEqual({result.content_status for result in results}, {"current", "conflict"})
        self.assertEqual({result.document_id for result in results}, {records[0]["document_id"]})

    def test_concurrent_replace_rejects_stale_write_and_retry_uses_latest_state(self):
        """两个 Replace 都基于 A 预计算时，B 提交后 C 必须冲突且不能留下部分数据。"""
        initial = self.write("replace-race/a.txt", "REPLACE_STATE_A")
        version_b = self.write("replace-race/b.txt", "REPLACE_STATE_B")
        version_c = self.write("replace-race/c.txt", "REPLACE_STATE_C")
        created = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=initial,
            display_name="race.txt",
        )
        hash_b = hashlib.sha256(version_b.read_bytes()).hexdigest()
        barrier = threading.Barrier(2)
        b_committed = threading.Event()
        original = KnowledgeStore.replace_document_set

        def commit_b_before_c(store, **kwargs):
            barrier.wait(timeout=5)
            if kwargs["registry"]["current_content_hash"] == hash_b:
                try:
                    return original(store, **kwargs)
                finally:
                    b_committed.set()
            if not b_committed.wait(timeout=5):
                raise TimeoutError("controlled B commit did not finish")
            return original(store, **kwargs)

        with patch.object(KnowledgeStore, "replace_document_set", commit_b_before_c):
            with ThreadPoolExecutor(max_workers=2) as pool:
                future_b = pool.submit(
                    self.service.replace_own_product_document,
                    workspace_id=self.workspace, product_id=self.product,
                    document_id=created.document_id, new_file=version_b,
                )
                future_c = pool.submit(
                    self.service.replace_own_product_document,
                    workspace_id=self.workspace, product_id=self.product,
                    document_id=created.document_id, new_file=version_c,
                )
                result_b = future_b.result(timeout=10)
                result_c = future_c.result(timeout=10)

        current = self.store().document_set_record(
            "own_product", self.scope, created.document_id
        )
        self.assertEqual(result_b.content_status, "current")
        self.assertEqual(result_c.content_status, "conflict")
        self.assertEqual(current["current_content_hash"], hash_b)
        content = "\n".join(item.content for item in self.retrieve().candidates)
        self.assertIn("REPLACE_STATE_B", content)
        self.assertNotIn("REPLACE_STATE_A", content)
        self.assertNotIn("REPLACE_STATE_C", content)
        self.assertEqual(self.store().counts(), {"documents": 1, "chunks": 1})

        retried = self.service.replace_own_product_document(
            workspace_id=self.workspace, product_id=self.product,
            document_id=created.document_id, new_file=version_c,
        )
        self.assertEqual(retried.content_status, "current")
        content = "\n".join(item.content for item in self.retrieve().candidates)
        self.assertIn("REPLACE_STATE_C", content)
        self.assertNotIn("REPLACE_STATE_A", content)
        self.assertNotIn("REPLACE_STATE_B", content)

    def test_concurrent_same_target_replace_is_idempotent(self):
        """两个 A→B 请求都完成预计算后，后提交者应得到 unchanged 等价结果。"""
        initial = self.write("same-target/a.txt", "SAME_TARGET_A")
        target = self.write("same-target/b.txt", "SAME_TARGET_B")
        created = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=initial,
            display_name="same-target.txt",
        )
        barrier = threading.Barrier(2)
        original = KnowledgeStore.replace_document_set

        def enter_transaction_together(store, **kwargs):
            barrier.wait(timeout=5)
            return original(store, **kwargs)

        with patch.object(KnowledgeStore, "replace_document_set", enter_transaction_together):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = [future.result(timeout=10) for future in [
                    pool.submit(self.service.replace_own_product_document,
                                workspace_id=self.workspace, product_id=self.product,
                                document_id=created.document_id, new_file=target)
                    for _ in range(2)
                ]]
        self.assertEqual({result.content_status for result in results}, {"current", "unchanged"})
        self.assertEqual({result.document_id for result in results}, {created.document_id})
        self.assertEqual(len(self.store().document_sets("own_product", self.scope)), 1)
        self.assertEqual(self.store().counts(), {"documents": 1, "chunks": 1})
        content = "\n".join(item.content for item in self.retrieve().candidates)
        self.assertIn("SAME_TARGET_B", content)
        self.assertNotIn("SAME_TARGET_A", content)

    def test_partial_unit_match_does_not_merge_distinct_source_files(self):
        first = self.markdown("one/first.md", [("共同", "SHARED_UNIT"), ("独有", "FIRST_ONLY")])
        second = self.markdown("two/second.md", [("共同", "SHARED_UNIT"), ("独有", "SECOND_ONLY")])
        a = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=first
        )
        b = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=second
        )
        self.assertNotEqual(a.document_id, b.document_id)
        self.assertEqual(a.unit_count, 2)
        self.assertEqual(b.unit_count, 2)
        self.assertEqual(self.store().counts()["documents"], 4)

    def test_replace_three_units_with_two_removes_stale_unit_and_old_content(self):
        old = self.markdown("old/manual.md", [
            ("一", "旧内容一"), ("二", "旧内容二"), ("三", "STALE_ONLY_CONTENT")
        ])
        new = self.markdown("new/manual-new.md", [
            ("一", "新内容一"), ("二", "FRESH_ONLY_CONTENT")
        ])
        created = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=old,
            display_name="manual.md",
        )
        self.assertEqual(created.unit_count, 3)
        replaced = self.service.replace_own_product_document(
            workspace_id=self.workspace, product_id=self.product,
            document_id=created.document_id, new_file=new,
        )
        self.assertEqual(replaced.document_id, created.document_id)
        self.assertEqual(replaced.unit_count, 2)
        self.assertIsNone(self.store().document_record(
            "own_product", created.document_id + ":unit:3"
        ))
        content = "\n".join(item.content for item in self.retrieve().candidates)
        self.assertIn("FRESH_ONLY_CONTENT", content)
        self.assertNotIn("STALE_ONLY_CONTENT", content)

    def test_xlsx_removed_sheet_does_not_remain_retrievable(self):
        old = self.workbook("old/book.xlsx", [
            ("保留一", "旧一"), ("保留二", "旧二"), ("删除页", "REMOVED_SHEET_CONTENT")
        ])
        new = self.workbook("new/book-new.xlsx", [
            ("保留一", "新一"), ("保留二", "NEW_SHEET_CONTENT")
        ])
        created = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=old,
            display_name="book.xlsx",
        )
        replaced = self.service.replace_own_product_document(
            workspace_id=self.workspace, product_id=self.product,
            document_id=created.document_id, new_file=new,
        )
        self.assertEqual(replaced.unit_count, 2)
        content = "\n".join(item.content for item in self.retrieve().candidates)
        self.assertIn("NEW_SHEET_CONTENT", content)
        self.assertNotIn("REMOVED_SHEET_CONTENT", content)

    def test_transaction_failure_rolls_back_whole_set_and_retry_succeeds(self):
        old = self.markdown("old/atomic.md", [
            ("一", "OLD_ONE"), ("二", "OLD_TWO"), ("三", "OLD_THREE")
        ])
        new = self.markdown("new/atomic-new.md", [("一", "NEW_ONE"), ("二", "NEW_TWO")])
        created = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=old,
            display_name="atomic.md",
        )
        before = self.store().document_set_record("own_product", self.scope, created.document_id)
        database = workspace_knowledge_path(self.state, self.workspace)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER controlled_document_set_failure BEFORE INSERT ON documents "
                "BEGIN SELECT RAISE(ABORT, 'controlled'); END"
            )
        failed = self.service.replace_own_product_document(
            workspace_id=self.workspace, product_id=self.product,
            document_id=created.document_id, new_file=new,
        )
        after = self.store().document_set_record("own_product", self.scope, created.document_id)
        self.assertEqual(failed.content_status, "current_preserved")
        self.assertEqual(after["current_content_hash"], before["current_content_hash"])
        self.assertIsNotNone(self.store().document_record(
            "own_product", created.document_id + ":unit:3"
        ))
        content = "\n".join(item.content for item in self.retrieve().candidates)
        self.assertIn("OLD_THREE", content)
        self.assertNotIn("NEW_ONE", content)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP TRIGGER controlled_document_set_failure")
        retried = self.service.replace_own_product_document(
            workspace_id=self.workspace, product_id=self.product,
            document_id=created.document_id, new_file=new,
        )
        self.assertEqual(retried.content_status, "current")
        self.assertIsNone(self.store().document_record(
            "own_product", created.document_id + ":unit:3"
        ))

    def test_embedding_failure_preserves_old_version_and_retry_is_safe(self):
        old = self.markdown("old/embed.md", [("旧", "OLD_EMBED_CONTENT")])
        new = self.markdown("new/embed-new.md", [("新", "NEW_EMBED_CONTENT")])
        created = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=old,
            display_name="embed.md",
        )
        before = self.store().document_set_record("own_product", self.scope, created.document_id)
        self.embedding.fail = True
        failed = self.service.replace_own_product_document(
            workspace_id=self.workspace, product_id=self.product,
            document_id=created.document_id, new_file=new,
        )
        current = self.store().document_set_record("own_product", self.scope, created.document_id)
        self.assertEqual((failed.parsing_status, failed.ingestion_status), ("parsed", "failed"))
        self.assertEqual(current["current_content_hash"], before["current_content_hash"])
        self.embedding.fail = False
        retried = self.service.replace_own_product_document(
            workspace_id=self.workspace, product_id=self.product,
            document_id=created.document_id, new_file=new,
        )
        self.assertEqual(retried.ingestion_status, "completed")
        content = "\n".join(item.content for item in self.retrieve().candidates)
        self.assertIn("NEW_EMBED_CONTENT", content)
        self.assertNotIn("OLD_EMBED_CONTENT", content)

    def test_initial_add_embedding_failure_has_zero_pollution_and_can_retry(self):
        path = self.markdown("initial/failure.md", [("产品", "INITIAL_RETRY_CONTENT")])
        self.embedding.fail = True
        failed = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=path
        )
        self.assertEqual((failed.parsing_status, failed.ingestion_status), ("parsed", "failed"))
        self.assertEqual(self.store().counts(), {"documents": 0, "chunks": 0})
        self.assertEqual(self.store().document_sets("own_product", self.scope), [])
        self.embedding.fail = False
        retried = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=path
        )
        self.assertEqual(retried.ingestion_status, "completed")
        self.assertIn("INITIAL_RETRY_CONTENT",
                      "\n".join(item.content for item in self.retrieve().candidates))

    def test_replace_parse_failure_preserves_current_registry_and_knowledge(self):
        old = self.markdown("replace/old.md", [("产品", "CURRENT_VALID_CONTENT")])
        unsupported = self.write("replace/new.doc", "unsupported replacement")
        created = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=old,
            display_name="replace.md",
        )
        before = self.store().document_set_record("own_product", self.scope, created.document_id)
        failed = self.service.replace_own_product_document(
            workspace_id=self.workspace, product_id=self.product,
            document_id=created.document_id, new_file=unsupported,
        )
        after = self.store().document_set_record("own_product", self.scope, created.document_id)
        self.assertEqual(failed.parsing_status, "unsupported")
        self.assertEqual(failed.content_status, "current_preserved")
        self.assertEqual(after, before)
        self.assertIn("CURRENT_VALID_CONTENT",
                      "\n".join(item.content for item in self.retrieve().candidates))

    def test_unsupported_and_scanned_pdf_do_not_pollute_knowledge(self):
        for suffix in ("doc", "xls", "ppt", "pptx", "bin"):
            result = self.service.add_own_product_document(
                workspace_id=self.workspace, product_id=self.product,
                file=self.write(f"unsupported/sample.{suffix}", "unsupported"),
            )
            self.assertEqual(result.parsing_status, "unsupported")
            self.assertEqual(result.knowledge_status, "unavailable")
        blank = self.root / "blank.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with blank.open("wb") as stream:
            writer.write(stream)
        scanned = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=blank
        )
        self.assertEqual(scanned.parsing_status, "scanned_or_no_text")
        self.assertIn("可能是扫描型 PDF", scanned.failure_reason)
        self.assertEqual(self.store().counts(), {"documents": 0, "chunks": 0})

    def test_reopen_identity_and_domain_workspace_product_isolation(self):
        path = self.write("scope/product.txt", "WORKSPACE_A_PRIVATE_CONTENT")
        created = self.service.add_own_product_document(
            workspace_id=self.workspace, product_id=self.product, file=path
        )
        reopened = OwnProductDocumentService(
            self.state, embedding_factory=lambda: self.embedding
        )
        status = reopened.get_own_product_document(
            workspace_id=self.workspace, product_id=self.product,
            document_id=created.document_id,
        )
        self.assertEqual(status.document_id, created.document_id)
        self.assertEqual(status.content_status, "current")
        self.assertEqual(self.retrieve(domain="competitor").status, "no_results")
        other_product = product_scope_id(self.workspace, "product-b")
        self.assertEqual(self.retrieve(product_scope=other_product).status, "no_results")
        other_workspace_scope = product_scope_id("workspace-b", self.product)
        self.assertEqual(self.retrieve(
            workspace="workspace-b", product_scope=other_workspace_scope
        ).status, "no_results")
        public = json.dumps(status.model_dump(), ensure_ascii=False)
        self.assertNotIn(str(path.resolve()), public)
        self.assertNotIn(":unit:", public)


if __name__ == "__main__":
    unittest.main()
