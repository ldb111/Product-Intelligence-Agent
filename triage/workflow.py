"""先由程序检索，再做一次 Reasoning 判断，模型没有任何工具。"""

from llm.client import get_llm
from research.evidence import EvidenceError, no_generated_urls
from research.tools import ResearchTools

from .prompts import TRIAGE_PROMPT
from .schemas import TriageDecision, TriageResult


class TriageWorkflow:
    def __init__(self, *, knowledge, memory, product_id: str, competitor_id: str,
                 memory_scope: str, memory_scope_id: str, client=None):
        self.knowledge, self.memory = knowledge, memory
        self.scope = dict(product_id=product_id, competitor_id=competitor_id,
                          memory_scope=memory_scope, memory_scope_id=memory_scope_id)
        self.client = client if client is not None else get_llm(role="reasoning")
        self.last_attempt = None

    def run(self, event, *, current_focus: str = "", product_profile: str | None = None) -> dict:
        """接收已确认事件，固定检索两域与记忆，再输出绑定事件的分诊及审计。

        查询由事件字段确定；三个上下文独立命名。客户端仅执行一次 decide，
        不提供工具定义或联网能力。引用校验失败拒绝结果，不改写事件或自动重试。
        """
        if event.decision != "event":
            raise ValueError("Triage 只接受已确认事件。")
        tools = ResearchTools(knowledge=self.knowledge, memory=self.memory, search=None, **self.scope)
        event_context = tools.seed_event(event)
        event_context["evidence"] = [i for i in event_context["evidence"] if i["source"] == "event"]
        query = " ".join([event.competitor, event.event_subject or "", event.change_action or "",
                          *(f"{i.item} {i.current_state or ''}" for i in event.change_items)])[:1500]
        own = tools.retrieve_knowledge(query, "own_product")
        competitor = tools.retrieve_knowledge(query, "competitor")
        memory = tools.retrieve_memory(query)
        payload = {"event": event_context, "product_profile": product_profile, "current_focus": current_focus,
                   "knowledge_context": {"own_product": own, "competitor": competitor},
                   "memory_context": memory}
        decision, runtime = self.client.decide(TRIAGE_PROMPT, payload, TriageDecision)
        self.last_attempt = {"draft": decision.model_dump(), "context": payload,
                             "model_runtime": [runtime.model_dump()], "retrieval_runtime": tools.runtime}
        decision = TriageDecision.model_validate(decision.model_dump())
        no_generated_urls(decision.model_dump())
        registry = tools.registry
        citations = [registry.get(i, factual=True) for i in decision.evidence_refs]
        for i in decision.memory_context_refs:
            if registry.get(i).source != "memory":
                raise EvidenceError("关注上下文引用必须属于 Memory。")
        for claim in decision.own_product_relevance.claims:
            sources = registry.check_support(claim.support)
            if (not any(i.source == "knowledge" and i.domain == "own_product" for i in sources)
                    or not any(i.domain == "competitor" for i in sources)
                    or not {i.result_id for i in sources} <= set(decision.evidence_refs)):
                raise EvidenceError("自身关联缺少双域直接证据引用。")
        if decision.own_product_relevance.status == "insufficient_own_product_context":
            if "insufficient_own_product_context" not in decision.missing_context:
                raise EvidenceError("自身资料不足必须显式记录缺口。")
        result = TriageResult(**decision.model_dump(), event_id=event.candidate_id)
        return {"result": result, "context": payload, "citations": [i.model_dump() for i in citations],
                "audit": {"query": query, "model_runtime": [runtime.model_dump()],
                          "retrieval_runtime": tools.runtime, "model_calls": 1, "web_tool_calls": 0}}
