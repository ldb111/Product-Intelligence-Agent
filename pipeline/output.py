"""用户结果只投影业务字段；内部数据库路径、运行记录和工具轨迹另存审计。"""

import re

from memory.policy import contains_secret

from .schemas import ProductResult
from .store import product_event_id, product_pending_id


def public_text(value):
    """递归清理业务文本中的明显敏感信息和本机路径，不把技术异常正文展示给用户。"""
    if isinstance(value, dict):
        return {k: public_text(v) for k, v in value.items()}
    if isinstance(value, list):
        return [public_text(v) for v in value]
    if isinstance(value, str):
        if contains_secret(value):
            return "[敏感内容已隐藏]"
        if re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|file://|(?:^|\s)/(?:home|Users|tmp|var)/", value):
            return "[内部路径已隐藏]"
    return value


def present_artifact(artifact: dict, group: str) -> dict:
    """为一个分诊/研究结果限定引用命名空间，再从真实来源投影用户可见字段。

    不同 Agent 任务都可能有 knowledge_001；加上程序产物键后，聚合结果不会把
    两个任务的相同局部 ID 错连到同一来源。不会改写声明、路由或证据原文。
    """
    citations = artifact.get("citations", [])
    mapping = {c["result_id"]: group + ":" + c["result_id"] for c in citations}

    def qualify(value):
        if isinstance(value, dict):
            return {k: qualify(v) for k, v in value.items()}
        if isinstance(value, list):
            return [qualify(v) for v in value]
        return mapping.get(value, value) if isinstance(value, str) else value

    result = dict(artifact["result"])
    result.pop("memory_context_refs", None)
    sources = [{k: c[k] for k in ("result_id", "url", "title", "content", "domain", "source") if k in c}
               for c in citations]
    return {"result": qualify(result), "citations": qualify(sources)}


def present_event(
    event: dict, *, pending: bool = False, discovery_origin: str | None = None
) -> tuple[dict, list[dict]]:
    """保留事件事实与真实来源；排除内部 evidence_context、存储引用和模型用量。"""
    facts = {k: event[k] for k in ("competitor", "decision", "event_type", "event_subject",
             "change_action", "change_items", "event_time", "effective_time", "affected_scope", "decision_reason",
             "uncertainty_reason") if k in event}
    facts["pending_id" if pending else "event_id"] = (
        product_pending_id(event) if pending else product_event_id(event)
    )
    if discovery_origin:
        facts["discovery_origin"] = discovery_origin
    facts["evidence_refs"] = ["event:" + e["evidence_id"] for e in event["evidence"]]
    citations = [{"result_id": "event:" + e["evidence_id"], "url": e["source_url"], "title": e["source_title"],
                  "content": e["relevant_excerpt"], "source": "event", "domain": "competitor"}
                 for e in event["evidence"]]
    return facts, citations


def present_research_action(store, workspace: str, state: dict) -> dict:
    """把独立 Research 生命周期投影为产品状态，隐藏内部 Event/Artifact/进程键。"""
    action = {"event_id": state["event_id"], "recommended": True, "status": state["status"]}
    if state["status"] == "completed":
        reference = state["artifact"]
        artifact = store.get(reference["namespace"], reference["key"])
        if artifact is not None:
            view = present_artifact(artifact, "research:" + state["event_id"])
            action["result"] = view["result"]
            action["citations"] = view["citations"]
    elif state["status"] == "failed":
        action["error"] = {"type": state.get("error_type", "research_error"),
                           "message": "研究任务未完成，可显式重试。"}
    return public_text(action)


def build_product_result(store, run_id: str, product, workspace: str, error: str | None) -> ProductResult:
    """从已持久化的本次检查点和产物组装产品结果；失败也保留已经完成的步骤。

    不把原始业务对象任意展开到用户界面：只有明确允许的字段进入返回契约。
    当前运行没有检测事件时，不把历史事件混称为本次新增事件。
    """
    sources = store.get("sources", workspace, {"sources": [], "remaining_gaps": []})
    result = ProductResult(competitor=product.competitor_name, current_focus=product.current_focus,
                           monitoring_sources=[{k: s[k] for k in ("url", "title", "source_type", "monitoring_reason")}
                                               for s in sources["sources"]])
    result.uncertainties.extend(sources["remaining_gaps"])
    steps = store.items("steps:" + run_id)
    scans = [s["data"] for k, s in steps.items() if k.startswith("scan:") and s["status"] == "complete"]
    if scans:
        result.baseline_created = any(s["is_first_scan"] for s in scans)
        result.scan_status = "baseline" if all(s["is_first_scan"] for s in scans) else "scanned"
    workspace_events = store.items("events:" + workspace)
    event_origins = store.items("run_event_origins:" + run_id)
    for internal_id, event in store.items("run_events:" + run_id).items():
        origin = event_origins.get(internal_id, {}).get("discovery_origin", "normal_monitoring")
        facts, citations = present_event(event, discovery_origin=origin)
        if origin == "historical_backfill":
            result.historical_events.append(facts)
        else:
            result.detected_events.append(facts)
        result.citations.extend(citations)
    for pending in store.items("pending:" + workspace).values():
        if pending["status"] == "pending":
            facts, citations = present_event(
                pending["candidate"], pending=True,
                discovery_origin=pending.get("discovery_origin", "normal_monitoring"),
            )
            result.pending_candidates.append({"candidate": facts, "reason": pending["reason"]})
            result.citations.extend(citations)
    for index, (_, reference) in enumerate(store.items("run_artifacts:" + run_id).items(), start=1):
        artifact = store.get(reference["namespace"], reference["key"])
        if reference["kind"] == "triage":
            internal_event_id = artifact["result"].get("event_id")
            event = workspace_events.get(internal_event_id)
            public_event_id = product_event_id(event) if event else "event_unavailable"
            view = present_artifact(artifact, "triage:" + public_event_id)
            view["result"]["event_id"] = public_event_id
        else:
            view = present_artifact(artifact, reference["kind"] + ":" + str(index))
        getattr(result, reference["kind"] + "_results").append(view)
        result.citations.extend(view["citations"])
        result.uncertainties.extend(view["result"].get("uncertainties", view["result"].get("uncertainty", [])))
        result.uncertainties.extend(view["result"].get("missing_context", []))
    for state in store.items("research_status:" + workspace).values():
        action = present_research_action(store, workspace, state)
        result.research_actions.append(action)
        if action["status"] == "completed" and "result" in action:
            result.research_results.append({"result": action["result"], "citations": action.get("citations", [])})
            result.citations.extend(action.get("citations", []))
    result.citations = list({c["result_id"]: c for c in result.citations}.values())
    result.uncertainties = list(dict.fromkeys(result.uncertainties))
    result.status = "failed" if error else "pending" if result.pending_candidates else "complete"
    if error:
        result.error = {"type": error, "message": "该步骤未完成，已保存先前结果，可恢复后继续。"}
        if error == "scan_error":
            result.scan_status = "partial" if scans else "failed"
    return ProductResult.model_validate(public_text(result.model_dump()))
