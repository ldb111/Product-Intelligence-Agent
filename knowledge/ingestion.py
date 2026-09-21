"""固定入库工作流：资格检查 → 规范化 → 切片 → 向量化 → 原子持久化。"""

import json

from llm.retrieval_models import EmbeddingRole, ModelCallRecord

from .chunking import CHUNKING_VERSION, chunk_document, normalize
from .schemas import (
    DocumentSetIngestionResult, IngestionResult, KnowledgeAdmissionError,
    KnowledgeDocument, content_hash,
)
from .store import KnowledgeStore


def validate_admission(document: KnowledgeDocument) -> None:
    """检查调用方提供的知识资格；成功无返回值，不自行核验互联网身份。

    自身资料必须由用户提供。竞品只接收已核验官方内容、Event/Evidence 和确认状态，
    必须附核验引用；confirmed 沿用上游有限证据语义，不代表全量事实均已证实。
    Research Result 可在以后通过此契约扩展，目前明确拒绝，避免假装已实现 Research。
    """
    if document.knowledge_domain == "own_product":
        if document.source_credibility != "user_provided":
            raise KnowledgeAdmissionError("自身知识必须是用户明确提供的产品资料。")
        return
    if (document.source_type not in {"verified_official_content", "competition_event", "evidence", "confirmed_product_state"}
            or document.evidence_status != "confirmed" or not document.verification_reference):
        raise KnowledgeAdmissionError("竞品知识需要已确认状态、核验引用和受支持的事实类型。")
    if document.source_type in {"verified_official_content", "confirmed_product_state"} and document.source_credibility != "official":
        raise KnowledgeAdmissionError("官方内容和确认产品状态必须来自已核验官方来源。")


class KnowledgeIngestor:
    def __init__(self, store: KnowledgeStore, embedding: EmbeddingRole, max_chars: int = 1200):
        self.store = store
        self.embedding = embedding
        self.max_chars = max_chars
        self.last_runtime: list[ModelCallRecord] = []

    def ingest(self, document: KnowledgeDocument) -> IngestionResult:
        """接收单份显式文档，返回写入状态与切片 ID；重复文档不再调用模型。

        完整入库指纹覆盖来源和切片策略，避免正文未变时悄悄沿用旧 provenance。
        内容变更后先完成全部模型调用，成功才替换旧文档。审计独立于业务返回值。
        """
        self.last_runtime = []
        # 重建一次契约，防止调用方通过可变 metadata 或 model_copy 绕过构造校验。
        document = KnowledgeDocument.model_validate(document.model_dump())
        validate_admission(document)
        document = KnowledgeDocument(**{**document.model_dump(), "content": normalize(document.content)})
        chunks = chunk_document(document, self.max_chars)
        self.store.assert_space(self.embedding.space_id)
        fingerprint = content_hash(json.dumps({
            "document": document.model_dump(mode="json"), "chunking": CHUNKING_VERSION,
            "max_chars": self.max_chars, "space_id": self.embedding.space_id,
        }, ensure_ascii=False, sort_keys=True, allow_nan=False))
        old = self.store.document_record(document.knowledge_domain, document.document_id)
        if old and old[0] == fingerprint:
            status = "unchanged"
        else:
            output = self.embedding.embed([chunk.model_text() for chunk in chunks])
            self.last_runtime = output.runtime
            status = self.store.upsert(document, fingerprint, chunks, output.vectors, self.embedding.space_id)
        return IngestionResult(
            document_id=document.document_id, knowledge_domain=document.knowledge_domain,
            content_hash=content_hash(document.content), chunk_ids=[c.chunk_id for c in chunks], status=status,
        )

    def replace_document_set(
        self, *, logical_document_id: str, documents: list[KnowledgeDocument], registry: dict,
        expected_content_hash: str | None = None, create_only: bool = False,
    ) -> DocumentSetIngestionResult:
        """预计算一个文件的全部 unit，再通过 Store 单事务切换当前版本。

        输入是同一 own_product 逻辑文档的完整 KnowledgeDocument 集合和当前 Registry。
        本函数先完成契约校验、Chunking 与全部 Embedding；只有所有外部调用成功后才进入
        SQLite 事务。输出只说明整个集合已插入或更新，不提供逐 unit 的部分成功语义。
        """
        self.last_runtime = []
        if not logical_document_id or not documents:
            raise KnowledgeAdmissionError("文件级知识集合不能为空。")
        prepared = []
        all_chunks = []
        for index, value in enumerate(documents, 1):
            document = KnowledgeDocument.model_validate(value.model_dump())
            validate_admission(document)
            document = KnowledgeDocument(**{
                **document.model_dump(), "content": normalize(document.content)
            })
            if (document.knowledge_domain != "own_product"
                    or document.document_id != f"{logical_document_id}:unit:{index}"):
                raise KnowledgeAdmissionError("文件级知识集合的 unit 身份无效。")
            chunks = chunk_document(document, self.max_chars)
            fingerprint = content_hash(json.dumps({
                "document": document.model_dump(mode="json"), "chunking": CHUNKING_VERSION,
                "max_chars": self.max_chars, "space_id": self.embedding.space_id,
            }, ensure_ascii=False, sort_keys=True, allow_nan=False))
            prepared.append([document, fingerprint, chunks, []])
            all_chunks.extend(chunks)
        self.store.assert_space(self.embedding.space_id)
        output = self.embedding.embed([chunk.model_text() for chunk in all_chunks])
        self.last_runtime = output.runtime
        offset = 0
        for item in prepared:
            count = len(item[2])
            item[3] = output.vectors[offset:offset + count]
            offset += count
        if offset != len(output.vectors):
            raise ValueError("向量返回数量与文件级知识集合不一致。")
        status = self.store.replace_document_set(
            registry=registry, documents=[tuple(item) for item in prepared],
            space_id=self.embedding.space_id, expected_content_hash=expected_content_hash,
            create_only=create_only,
        )
        return DocumentSetIngestionResult(
            document_id=logical_document_id, knowledge_domain="own_product",
            unit_document_ids=[item[0].document_id for item in prepared], status=status,
        )
