"""把原文引用变成可追踪证据，并保护事实字段的证据边界。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .schemas import DetectionInput, EventDecision, Evidence


class EvidenceValidationError(ValueError):
    """输出引用不属于输入，或事实超出了证据可确定的范围。"""


def content_fingerprint(value: Any) -> str:
    """给精确内容生成稳定标识，只用于内容去重，不推断事件同一性。"""
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def fact_fields(decision: EventDecision) -> dict[str, str]:
    """展开非空事实字段，供引用逐项校验；不生成或补齐业务值。"""
    facts = {
        name: getattr(decision, name)
        for name in ("event_type", "event_subject", "change_action", "event_time", "effective_time", "affected_scope")
        if getattr(decision, name) is not None
    }
    for index, item in enumerate(decision.change_items):
        for name, value in item.model_dump().items():
            if value is not None:
                facts[f"change_items.{index}.{name}"] = value
    return facts


def validate_claims(data: DetectionInput, decision: EventDecision) -> None:
    """校验模型事实与引用的对应关系，成功时无返回值。

    输入为局部证据和模型决策。逐条检查引文真实存在、两侧状态引用正确，以及状态、
    时间、范围值确为逐字摘取。语义归纳是否被原文蕴含仍是 LLM 的判断边界；本函数
    不把字符匹配宣称为完整事实核验，也不允许观察时间作为正文证据。
    """
    facts = fact_fields(decision)
    if set(decision.citations) != set(facts):
        raise EvidenceValidationError("每个非空事实都必须且只能引用对应字段的证据。")
    for field, value in facts.items():
        quotes = decision.citations[field]
        if not quotes:
            raise EvidenceValidationError("事实字段缺少原文引用。")
        for quote in quotes:
            text = getattr(data, quote.input_field) or ""
            if not quote.excerpt.strip() or quote.excerpt not in text:
                raise EvidenceValidationError("原文引用不属于本次输入。")
            if field.endswith(".previous_state"):
                # Path B 可引用官方正文中明确叙述的历史，但仍禁止从当前事实反推历史。
                allowed = {"previous_state"} if data.detection_path == "state_comparison" else {"previous_state", "current_state"}
                if quote.input_field not in allowed:
                    raise EvidenceValidationError("历史状态必须引用历史观察或明确历史声明。")
            if field.endswith(".current_state") and quote.input_field != "current_state":
                raise EvidenceValidationError("当前状态必须引用当前观察。")
        verbatim = field in {"event_time", "effective_time", "affected_scope"} or field.endswith((".previous_state", ".current_state"))
        if verbatim and not any(value in quote.excerpt for quote in quotes):
            raise EvidenceValidationError("状态、时间或范围字段必须为所引原文的直接摘录。")
    if decision.decision != "event":
        return
    if data.evidence_status != "confirmed":
        raise EvidenceValidationError("未确认或冲突输入不允许确认事件。")
    if data.detection_path == "state_comparison":
        for item in decision.change_items:
            if item.previous_state is None or item.current_state is None:
                raise EvidenceValidationError("状态比较事件必须有两侧明确状态，单侧观察不足以证明新增或消失。")
            if item.previous_state == item.current_state:
                raise EvidenceValidationError("相同状态不能被声明为状态迁移。")
    elif data.source_credibility != "official":
        raise EvidenceValidationError("明确变化声明路径需要已确认的官方来源。")


def build_evidence(data: DetectionInput, decision: EventDecision) -> list[Evidence]:
    """从通过校验的输入构造证据，不接受模型提供的来源或观察时间。

    按原文所在字段收集引文、能力范围和对应观察时间。uncertain 进入 pending；输入
    conflict 则保留 conflict。confirmed 仅代表这些有限引用通过检查，不代表来源全知。
    """
    validate_claims(data, decision)
    evidence: list[Evidence] = []
    for input_field in ("previous_state", "current_state", "relevant_source_context"):
        source_text = getattr(data, input_field)
        if not source_text:
            continue
        citations = {
            field: [q.excerpt for q in quotes if q.input_field == input_field]
            for field, quotes in decision.citations.items()
            if any(q.input_field == input_field for q in quotes)
        }
        observed_at = data.previous_observed_at if input_field == "previous_state" else data.current_observed_at
        if observed_at is None:
            raise EvidenceValidationError("历史证据缺少真实观察时间。")
        status = data.evidence_status
        if decision.decision == "uncertain" and status != "conflict":
            status = "pending"
        excerpts = list(dict.fromkeys(text for texts in citations.values() for text in texts))
        values = dict(
            source_url=data.source_url, source_title=data.source_title, source_type=data.source_type,
            source_credibility=data.source_credibility, evidence_status=status,
            evidence_capability=list(citations), observed_at=observed_at,
            relevant_excerpt="\n".join(excerpts) if excerpts else source_text,
            evidence_context={
                "input_field": input_field, "source_text": source_text,
                "citations": citations, "claim_snapshot": fact_fields(decision),
                "state_context": data.state_context.model_dump(),
            },
        )
        evidence.append(Evidence(evidence_id=content_fingerprint(values), **values))
    if not evidence:
        raise EvidenceValidationError("没有可追踪的局部原文证据。")
    return evidence


def merge_evidence(existing: list[Evidence], incoming: list[Evidence]) -> list[Evidence]:
    """在已判定 SAME_EVENT 后添加证据；不覆盖原状态，不自动消解 conflict。

    输入是两个事件的证据列表，精确相同证据按程序指纹去重，输出独立的新列表。
    不同来源、不同观察时间仍保留，因为它们是不同的证据记录。
    """
    result: list[Evidence] = []
    seen: set[str] = set()
    for evidence in [*existing, *incoming]:
        key = content_fingerprint(evidence.model_dump(exclude={"evidence_id"}))
        if key not in seen:
            result.append(evidence.model_copy(deep=True))
            seen.add(key)
    return result
