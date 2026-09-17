"""事件语义去重与证据合并；与网页内容哈希去重独立。"""

from __future__ import annotations

from llm.client import get_llm

from .evidence import merge_evidence
from .prompts import EVENT_DEDUP_PROMPT
from .schemas import DedupDecision, DedupResult, EventCandidate


def _semantic_view(candidate: EventCandidate) -> dict:
    """只提供事件事实和有限引文；观察时间留作审计，不作为事件时间参与去重。"""
    view = candidate.model_dump(exclude={"candidate_id", "evidence", "citations"})
    view["evidence"] = [
        {
            "source_url": e.source_url, "source_credibility": e.source_credibility,
            "evidence_status": e.evidence_status, "evidence_capability": e.evidence_capability,
            "relevant_excerpt": e.relevant_excerpt,
            "claim_snapshot": e.evidence_context.get("claim_snapshot", {}),
        }
        for e in candidate.evidence
    ]
    return view


def deduplicate_event(candidate: EventCandidate, existing_events: list[EventCandidate]) -> DedupResult:
    """把一个候选与调用方提供的事件集合比较，返回三态去重结果。

    程序排除不同竞品；同竞品逐对调用 Reasoning Role 判定实际状态迁移。只有唯一
    SAME_EVENT 且无模糊匹配才合并 Evidence；否则返回新事件或待核验候选。
    合并不覆盖已有事实，补充的时间或范围留在各自 Evidence 中，避免无依据选边。
    """
    candidate = EventCandidate.model_validate(candidate.model_dump())
    existing_events = [EventCandidate.model_validate(item.model_dump()) for item in existing_events]
    if candidate.decision == "no_event":
        raise ValueError("no_event 不进入事件去重。")
    if candidate.decision == "uncertain":
        return DedupResult(decision="UNCERTAIN", reason="候选证据尚待核验。", pending_candidate=candidate)
    if any(event.decision != "event" for event in existing_events):
        raise ValueError("已有事件集合只能包含确认的事件；Pending 单独保留。")
    if len({e.candidate_id for e in existing_events}) != len(existing_events):
        raise ValueError("已有事件集合包含重复身份。")
    peers = [e for e in existing_events if e.competitor == candidate.competitor]
    if not peers:
        return DedupResult(decision="NEW_EVENT", reason="没有同竞品的已有事件。", event=candidate)
    client = get_llm(role="reasoning")
    same, ambiguous, runtime = [], [], []
    for existing in peers:
        verdict, record = client.decide(
            EVENT_DEDUP_PROMPT,
            {"existing_event": _semantic_view(existing), "candidate": _semantic_view(candidate)},
            DedupDecision,
        )
        verdict = DedupDecision.model_validate(verdict.model_dump())
        runtime.append(record)
        if verdict.decision == "SAME_EVENT":
            same.append((existing, verdict.reason))
        elif verdict.decision == "UNCERTAIN":
            ambiguous.append(verdict.reason)
    if ambiguous or len(same) > 1:
        return DedupResult(
            decision="UNCERTAIN", reason="存在模糊匹配或多个匹配，需要核验后再合并。",
            pending_candidate=candidate, runtime=runtime,
        )
    if same:
        existing, reason = same[0]
        merged = existing.model_dump()
        merged["evidence"] = merge_evidence(existing.evidence, candidate.evidence)
        return DedupResult(
            decision="SAME_EVENT", reason=reason, matched_event_id=existing.candidate_id,
            event=EventCandidate.model_validate(merged), runtime=runtime,
        )
    return DedupResult(decision="NEW_EVENT", reason="与已有事件属于不同产品状态变化。", event=candidate, runtime=runtime)
