"""只读适配已存在的 Event / Evidence 与用户文本文件，不改变冻结模块。"""

from pathlib import Path

from event_core.schemas import EventCandidate, Evidence

from .schemas import KnowledgeAdmissionError, KnowledgeDocument


def own_product_file(path: str | Path, *, document_id: str, product_id: str | None,
                     title: str, source_type: str, metadata: dict | None = None) -> KnowledgeDocument:
    """读取用户指定 UTF-8 文本/Markdown 并保留真实文件引用，返回自身知识文档。

    只接收明确文件，不扫描文件夹，不推算文件修改时间为产品事实时间。
    PDF/DOCX 需要调用方先提取正文并保留原引用，不能把二进制误作产品文本。
    """
    path = Path(path).resolve()
    if path.suffix.lower() not in {".txt", ".md"}:
        raise KnowledgeAdmissionError("当前文件入口仅支持 UTF-8 .txt 和 .md。")
    return KnowledgeDocument(
        knowledge_domain="own_product", document_id=document_id, product_id=product_id,
        source_type=source_type, title=title, content=path.read_text(encoding="utf-8-sig"),
        source_reference=str(path), source_credibility="user_provided", metadata=metadata or {},
    )


def evidence_document(evidence: Evidence, *, competitor_id: str, source_reference: str,
                      event_id: str | None = None) -> KnowledgeDocument:
    """把上游已校验 Evidence 转为知识，逐条保留来源和有限证据语义。

    输入必须是可信调用方提供的正式 Evidence。再次检查引用存在于 source_text；
    不从摘要扩写事实、不重新生成 URL，不用 observed_at 推断 event_time。
    此字符串校验不证明官方身份，来源可信度仍由上游负责。
    """
    evidence = Evidence.model_validate(evidence.model_dump())
    context = evidence.evidence_context
    source_text = context.get("source_text")
    if (evidence.evidence_status != "confirmed" or not isinstance(source_text, str)
            or not source_text.strip() or not evidence.relevant_excerpt.strip()
            or any(line not in source_text for line in evidence.relevant_excerpt.splitlines() if line.strip())):
        raise KnowledgeAdmissionError("Evidence 未确认或缺少可核对的原文引用。")
    return KnowledgeDocument(
        knowledge_domain="competitor", document_id=f"evidence:{evidence.evidence_id}",
        competitor_id=competitor_id, source_type="competition_event" if event_id else "evidence",
        source_url=evidence.source_url, source_reference=source_reference, title=evidence.source_title,
        content=evidence.relevant_excerpt, observed_at=evidence.observed_at,
        evidence_status=evidence.evidence_status, source_credibility=evidence.source_credibility,
        verification_reference=f"evidence:{evidence.evidence_id}",
        metadata={"evidence_id": evidence.evidence_id, "event_id": event_id,
                  "original_source_type": evidence.source_type,
                  "evidence_capability": evidence.evidence_capability, "evidence_context": context},
    )


def event_documents(event: EventCandidate, *, competitor_id: str, source_reference: str) -> list[KnowledgeDocument]:
    """将已确认事件按 Evidence 转为文档，输出每条来源独立的知识列表。

    多源事件不能把来源 A 的声明归到来源 B。仅检索每份证据自己的原文，事件归纳
    和字段引用仍保留在上游证据上下文中。no_event / uncertain 不入事实知识库。
    """
    event = EventCandidate.model_validate(event.model_dump())
    if event.decision != "event":
        raise KnowledgeAdmissionError("只有已确认 Event 可以进入竞品事实知识库。")
    return [evidence_document(e, competitor_id=competitor_id,
                              source_reference=source_reference, event_id=event.candidate_id)
            for e in event.evidence]
