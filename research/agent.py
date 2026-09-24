"""一个有界工具循环服务核验、初始研究与事件追问；搜索和最终生成严格分离。"""

import json
from dataclasses import dataclass
from time import perf_counter

from llm.research import get_research_llm

from .prompts import FINAL_PROMPT, RESEARCH_PROMPT
from .schemas import FollowUpAnswer, ResearchReport, VerificationResult
from .tools import TOOL_DEFINITIONS


@dataclass(frozen=True)
class ResearchLimits:
    max_tool_calls: int = 10
    max_search_calls: int = 2
    max_page_reads: int = 3
    max_model_turns: int = 7

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in vars(self).values()):
            raise ValueError("研究预算必须为正整数。")


class ResearchAgent:
    """不使用图递归或第二个 Agent；client 与 tools 可注入以验证真实边界。"""

    def __init__(self, tools, *, client=None, limits: ResearchLimits | None = None):
        self.tools = tools
        self.client = client if client is not None else get_research_llm()
        self.limits = limits or ResearchLimits()
        self.used = False
        self.trace: list[dict] = []
        self.states: list[str] = []
        self.last_result = None

    def run(self, event, *, mode: str, triage_result=None, current_focus: str = "",
            initial_report: dict | None = None, initial_citations: list[dict] | None = None,
            conversation_history: list[dict] | None = None, question: str | None = None) -> dict:
        """核对输入资格，执行有界工具决策，关闭后一次生成并校验引用。

        verification 只输出结果和新证据，不替换上游候选。deep_research 必须有该事件
        的 research 路由。任何预算阻断或工具失败都会退出搜索，不把拒绝反馈变成循环。
        最终证据与运行审计分开，原始模型消息/隐藏推理不进入报告。
        """
        if self.used or self.tools.registry.items:
            raise ValueError("每次研究必须使用新 Agent 和新工具上下文。")
        if mode not in {"verification", "deep_research", "follow_up"}:
            raise ValueError("未知研究模式。")
        if mode == "verification" and event.decision != "uncertain":
            raise ValueError("核验只接受 uncertain 候选。")
        if mode == "deep_research":
            if (event.decision != "event" or triage_result is None
                    or triage_result.event_id != event.candidate_id or triage_result.decision != "research"):
                raise ValueError("深度研究需要同一已确认事件的 research 分诊结果。")
        if mode == "follow_up":
            if (event.decision != "event" or not isinstance(initial_report, dict)
                    or not isinstance(initial_citations, list)
                    or not isinstance(conversation_history, list)
                    or not isinstance(question, str) or not question.strip()):
                raise ValueError("继续追问需要已确认事件、初始研究、既有引用、历史消息和用户问题。")
        self.used = True
        started = perf_counter()
        payload = {"mode": mode, "event": self.tools.seed_event(event), "current_focus": current_focus}
        if mode == "follow_up":
            payload.update({
                "initial_research": initial_report,
                "initial_research_evidence": self.tools.seed_citations(initial_citations or []),
                "conversation_history": conversation_history,
                "question": question.strip(),
            })
        if triage_result is not None:
            # 分诊引用属于上一个登记表，不能冒充本轮引用；此处仅提供路由上下文。
            payload["triage"] = {"decision": triage_result.decision, "reason": triage_result.reason}
        initial_ids = set(self.tools.registry.items)
        self.client.start(RESEARCH_PROMPT, payload)
        self.states.append("SEARCH_OPEN")
        counts = {"tool": 0, "search": 0, "read": 0}
        stop = "model_turn_limit"
        closed = False
        for _ in range(self.limits.max_model_turns):
            calls = self.client.choose_tools(TOOL_DEFINITIONS)
            if not calls:
                stop = "model_finished"
                break
            for call in calls:
                name = call["name"]
                reason = None
                if counts["tool"] >= self.limits.max_tool_calls:
                    reason = "tool_limit"
                elif name == "web_search" and counts["search"] >= self.limits.max_search_calls:
                    reason = "search_limit"
                elif name == "read_page" and counts["read"] >= self.limits.max_page_reads:
                    reason = "read_limit"
                if reason:
                    self.trace.append({"tool": name, "status": "blocked", "reason": reason})
                    stop, closed = reason, True
                    break
                counts["tool"] += 1
                counts["search"] += name == "web_search"
                counts["read"] += name == "read_page"
                before = set(self.tools.registry.items)
                call_started = perf_counter()
                try:
                    arguments = json.loads(call["arguments"])
                    result = self.tools.execute(name, arguments)
                except Exception as exc:
                    self.trace.append({"tool": name, "status": "error", "error_type": type(exc).__name__,
                                       "latency_seconds": round(perf_counter() - call_started, 3)})
                    stop, closed = "tool_error", True
                    break
                self.trace.append({"tool": name, "status": "ok", "arguments": arguments,
                                   "new_result_ids": sorted(set(self.tools.registry.items) - before),
                                   "latency_seconds": round(perf_counter() - call_started, 3)})
                self.client.accept_result(call["id"], result)
                if counts["tool"] == self.limits.max_tool_calls:
                    stop, closed = "tool_limit", True
                    break
            if closed:
                break
        self.states.extend(["SEARCH_CLOSED", "FINALIZATION"])
        # 最终模型已不能读取网页，搜索摘要此时没有决策价值，移除以免混入事实。
        final_event = {**payload["event"], "evidence": [i for i in payload["event"]["evidence"] if i["source"] == "event"]}
        final_items = [i for i in self.tools.registry.model_items() if i["source"] != "search"]
        final_payload = {**payload, "event": final_event, "stop_reason": stop, "evidence": final_items,
                         "initial_evidence_ids": sorted(initial_ids)}
        schema = (VerificationResult if mode == "verification" else
                  FollowUpAnswer if mode == "follow_up" else ResearchReport)
        result = self.client.finalize(FINAL_PROMPT, final_payload, schema)
        self.last_result = result
        registry = self.tools.registry
        citations = (registry.validate_verification(result, initial_ids) if mode == "verification"
                     else registry.validate_follow_up(result) if mode == "follow_up"
                     else registry.validate_report(result))
        self.states.append("COMPLETE")
        return {"mode": mode, "event_id": event.candidate_id, "result": result.model_dump(),
                "citations": [item.model_dump() for item in citations],
                "evidence_registry": [item.model_dump() for item in registry.items.values()],
                "audit": {"states": self.states, "stop_reason": stop, "tool_calls": counts,
                          "trace": self.trace, "model_runtime": [r.model_dump() for r in self.client.runtime],
                          "retrieval_runtime": self.tools.runtime,
                          "latency_seconds": round(perf_counter() - started, 3)}}
