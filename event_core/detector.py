"""两条事件检测路径的固定工作流，不包含自主搜索或规划循环。"""

from __future__ import annotations

from llm.client import get_llm

from .evidence import EvidenceValidationError, build_evidence, content_fingerprint
from .prompts import EVENT_DETECTION_PROMPT
from .schemas import DetectionDecisions, DetectionInput, DetectionResult, EventCandidate, EventDecision


def _deterministic_decision(data: DetectionInput) -> EventDecision | None:
    """先处理无需语义判断的边界，返回安全结论或允许调用 LLM 的 None。"""
    if data.evidence_status != "confirmed":
        reason = "输入证据尚未确认或存在冲突，需要后续核验。"
    elif data.detection_path == "explicit_statement":
        if data.source_credibility == "official":
            return None
        reason = "明确变化声明尚未确认来自官方来源。"
    else:
        status = data.state_context.comparison_status
        if status in {"baseline", "unchanged"}:
            return EventDecision(
                decision="no_event",
                decision_reason="首次普通扫描只建立基线。" if status == "baseline" else "前后观察未发生变化。",
            )
        if status == "comparable" and data.previous_state and data.current_state and data.contextual_diff:
            return None
        reason = "缺少可比较的两侧局部状态或差异；未观察到不能推断为不存在。"
    return EventDecision(decision="uncertain", decision_reason=reason, uncertainty_reason=reason)


def detect_event(data: DetectionInput) -> DetectionResult:
    """把有限 Stage 1 证据转成 event/no_event/uncertain 候选。

    输入由采集适配器或调用方构造。程序先检查基线和可比较资格，再用 Reasoning Role
    做一次语义判断，最后验证事实引用并确定性构造候选身份、来源、时间和证据状态。
    输出可包含多个独立业务动作；不自动核验 pending，也不把模型异常伪装为成功。
    """
    data = DetectionInput.model_validate(data.model_dump())
    decision = _deterministic_decision(data)
    runtime = None
    if decision is not None:
        decisions = DetectionDecisions(candidates=[decision])
    else:
        client = get_llm(role="reasoning")
        decisions, runtime = client.decide(EVENT_DETECTION_PROMPT, data.model_dump(mode="json"), DetectionDecisions)
        decisions = DetectionDecisions.model_validate(decisions.model_dump())
    candidates = []
    for decision in decisions.candidates:
        try:
            evidence = build_evidence(data, decision)
        except EvidenceValidationError as exc:
            # 只附加已经按业务 Schema 解析的内容与非敏感审计信息，便于复盘真实 Bad Case。
            # 不附加底层响应、请求、认证头或隐藏推理；调用方仍然收到明确失败。
            exc.failed_decision = decision.model_dump(mode="json")
            exc.runtime = runtime.model_dump(mode="json") if runtime else None
            raise
        candidate_id = content_fingerprint({"input": data.model_dump(mode="json"), "decision": decision.model_dump()})
        candidates.append(EventCandidate(
            **decision.model_dump(), candidate_id=candidate_id, competitor=data.competitor,
            detection_path=data.detection_path, evidence=evidence,
        ))
    return DetectionResult(candidates=candidates, runtime=runtime)
