"""Fast LLM 只做 Memory Candidate 提取，程序随后独立执行 Write Policy。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from llm.client import RuntimeRecord, get_llm

from .policy import validate_candidate, validate_source_before_model
from .prompts import MEMORY_EXTRACTION_PROMPT
from .schemas import MemoryCandidate, MemoryCandidateBatch, MemoryPolicyError, MemorySourceKind


class SemanticRole(Protocol):
    def decide(self, prompt: str, payload: dict, schema: type): ...


@dataclass(frozen=True)
class ExtractionOutcome:
    """开发调用结果；runtime 不应作为后续 Agent 的 Memory Context。"""

    candidates: list[MemoryCandidate]
    rejected_count: int
    runtime: RuntimeRecord


def extract_candidates(source_text: str, *, source_kind: MemorySourceKind,
                       client: SemanticRole | None = None) -> ExtractionOutcome:
    """从有限来源文本提取逐字 Candidate，返回通过程序校验的候选与运行审计。

    调用模型前先拦截 Secret 和过大输入。Fast Role 仅看到 source_text，不看到 scope、
    用户身份、Provider 或写库信息。模型结果逐项验证；无长期价值可返回空列表，非法
    候选被拒绝且不写库。事实型来源直接拒绝，避免把 RAG/Event 当成抽取语料。
    """
    validate_source_before_model(source_text)
    if source_kind not in {"direct_user", "research_context"}:
        raise MemoryPolicyError("事实型来源不得用于长期记忆提取。")
    model = client or get_llm(role="fast")
    batch, runtime = model.decide(
        MEMORY_EXTRACTION_PROMPT, {"source_text": source_text}, MemoryCandidateBatch,
    )
    accepted, rejected = [], 0
    for candidate in batch.candidates:
        try:
            validate_candidate(candidate, source_text=source_text, source_kind=source_kind)
        except MemoryPolicyError:
            rejected += 1
        else:
            accepted.append(candidate)
    return ExtractionOutcome(accepted, rejected, runtime)
