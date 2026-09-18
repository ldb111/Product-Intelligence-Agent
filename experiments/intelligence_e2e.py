"""三例真实验收：历史真实 Event + 明确模拟的自身资料/关注；不自动沉淀研究结果。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_core.schemas import EventCandidate
from knowledge import KnowledgeIngestor, KnowledgeRetriever, KnowledgeStore
from knowledge.inputs import event_documents, own_product_file
from knowledge.schemas import content_hash
from llm.retrieval_models import get_embedding, get_rerank
from memory import MemoryRetriever, MemoryStore, MemoryWriter
from research.agent import ResearchAgent
from research.evidence import EvidenceError
from research.tools import ResearchTools
from source_discovery.search.tencent_wsa import TencentWsaSearchProvider
from triage.workflow import TriageWorkflow


FOCUS = "模拟研究关注：优先分析免费套餐的周期积分补充与获客策略，本次需要比较双方免费计划。"


def save(report: dict, output: Path) -> None:
    """每个完成阶段写检查点；不记录请求头、配置凭据、原始异常或隐藏推理。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def run(report: dict, output: Path, event_report: Path) -> None:
    """建独立 Fixture 库并执行 A/B/C；只在准备阶段显式写入原始资料和模拟关注。

    真实事件沿用已有官方页面验收的观察时间，不声称本轮重新检测了历史变化。
    验收仅检查真实调用链、引用和边界，不预设 verification 的结论。
    """
    raw = event_report.read_text(encoding="utf-8")
    archived = json.loads(raw)
    case = next(c for c in archived["cases"] if c["case"] == "B")
    if archived["status"] != "PASS" or case["status"] != "PASS" or case["kind"] != "live_official_page_with_live_reasoning":
        raise ValueError("需要真实官方事件验收记录。")
    event = EventCandidate.model_validate(case["detection"]["candidates"][0])
    report["provenance"] = {"event_report": str(event_report), "sha256": content_hash(raw),
                            "event": event.model_dump(mode="json"),
                            "own_product": "explicit_test_fixture", "memory": "explicit_test_fixture"}
    report["stage"] = "fixture_preparation"
    save(report, output)
    embedding, rerank = get_embedding(), get_rerank()
    ks, ms = KnowledgeStore(output.with_suffix(".knowledge.sqlite3")), MemoryStore(output.with_suffix(".memory.sqlite3"))
    if ks.counts()["documents"] or ms.counts()["total"]:
        raise ValueError("必须使用新的验收库。")
    own = own_product_file(ROOT / "experiments/fixtures/knowledge_own_product.md", document_id="intelligence-own-fixture",
                           product_id="fixture-product", title="Own Product 测试 Fixture", source_type="product_document",
                           metadata={"fixture": True})
    docs = [own, *event_documents(event, competitor_id="octoreport", source_reference=str(event_report))]
    report["setup_runtime"] = []
    ingestor = KnowledgeIngestor(ks, embedding)
    for doc in docs:
        ingestor.ingest(doc)
        report["setup_runtime"].extend(asdict(r) for r in ingestor.last_runtime)
    writer = MemoryWriter(ms, embedding)
    writer.remember_explicit(scope="workspace", scope_id="intelligence-e2e-fixture", memory_type="focus", content=FOCUS,
                             source_reference="fixture://intelligence/focus", source_text=FOCUS, metadata={"fixture": True})
    report["setup_runtime"].extend(asdict(r) for r in writer.last_runtime)
    before = {"knowledge": ks.counts(), "memory": ms.counts()}
    scope = dict(product_id="fixture-product", competitor_id="octoreport", memory_scope="workspace",
                 memory_scope_id="intelligence-e2e-fixture")
    knowledge, memory = KnowledgeRetriever(ks, embedding, rerank), MemoryRetriever(ms, embedding)
    report["stage"] = "A_triage"
    save(report, output)
    start = perf_counter()
    workflow = TriageWorkflow(knowledge=knowledge, memory=memory, **scope)
    try:
        a = workflow.run(event, current_focus=FOCUS,
                         product_profile="自身资料为明确标注的模拟产品；本次验证免费档积分比较链路。")
    except Exception:
        report["cases"] = {"A": {"status": "FAIL", "attempt": workflow.last_attempt}}
        raise
    triage = a["result"]
    a["result"] = triage.model_dump()
    a["latency_seconds"] = round(perf_counter() - start, 3)
    a["status"] = "PASS" if (a["context"]["knowledge_context"]["own_product"]["results"]
                              and a["context"]["knowledge_context"]["competitor"]["results"]
                              and a["context"]["memory_context"]["results"]) else "FAIL"
    report["cases"] = {"A": a}
    save(report, output)

    pending_evidence = event.evidence[0].model_copy(update={
        "evidence_status": "pending", "relevant_excerpt": "待核验线索：免费档积分补充方式可能发生变化。",
        "evidence_context": {"fixture": True},
    })
    uncertain = EventCandidate(candidate_id="intelligence-uncertain-fixture", competitor=event.competitor,
                               detection_path="explicit_statement", decision="uncertain", decision_reason="需要原文核验",
                               uncertainty_reason="官方变更日志是否明确说明免费档从一次性起始积分改为每月补充？",
                               evidence=[pending_evidence])
    for label, mode, candidate, focus in (
        ("B", "verification", uncertain, "请实际搜索该候选的公开来源并读取相关页面，再判断是否能确认；不预设结果。"),
        ("C", "deep_research", event, FOCUS),
    ):
        report["stage"] = label + "_" + mode
        save(report, output)
        t = ResearchTools(knowledge=knowledge, memory=memory, search=TencentWsaSearchProvider(), **scope,
                          official_hosts=frozenset(urlsplit(e.source_url).hostname for e in event.evidence
                                                   if e.source_credibility == "official"))
        agent = ResearchAgent(t)
        try:
            outcome = agent.run(candidate, mode=mode, triage_result=triage if label == "C" else None, current_focus=focus)
        except Exception:
            report["cases"][label] = {"status": "FAIL", "trace": agent.trace, "states": agent.states,
                                     "model_runtime": [r.model_dump() for r in agent.client.runtime],
                                     "retrieval_runtime": t.runtime,
                                     "evidence_registry": [i.model_dump() for i in t.registry.items.values()],
                                     "draft": agent.last_result.model_dump() if agent.last_result else None}
            raise
        names = {r["tool"] for r in outcome["audit"]["trace"] if r["status"] == "ok"}
        required = {"web_search", "read_page"} if label == "B" else {
            "retrieve_competitor_knowledge", "retrieve_own_product_knowledge", "retrieve_memory"}
        outcome["status"] = "PASS" if required <= names else "FAIL"
        report["cases"][label] = outcome
        save(report, output)
    after = {"knowledge": ks.counts(), "memory": ms.counts()}
    report["read_only_check"] = {"before": before, "after": after, "passed": before == after}
    runtimes = list(report["setup_runtime"])
    for case in report["cases"].values():
        runtimes += case["audit"]["model_runtime"] + case["audit"]["retrieval_runtime"]
    report["metrics"] = {"model_calls_including_setup_and_retrieval": len(runtimes),
                         "reported_total_tokens": sum((r.get("token_usage") or r.get("usage") or {}).get("total_tokens", 0) for r in runtimes),
                         "calls_without_usage": sum(not (r.get("token_usage") or r.get("usage")) for r in runtimes)}
    report["status"] = "PASS" if before == after and all(c["status"] == "PASS" for c in report["cases"].values()) else "FAIL"


def main() -> int:
    """命令行只打印状态和文件位置；异常类型单独留痕，不打印可能含认证内容的异常正文。"""
    parser = argparse.ArgumentParser(description=__doc__)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    parser.add_argument("--output", type=Path, default=ROOT / f"experiments/output/intelligence_e2e_{stamp}.json")
    parser.add_argument("--event-report", type=Path, default=ROOT / "experiments/output/event_core_v01_live_02.json")
    args = parser.parse_args()
    report = {"status": "RUNNING", "started_at": datetime.now(timezone.utc).isoformat()}
    start = perf_counter()
    try:
        run(report, args.output, args.event_report)
    except Exception as exc:
        report.update(status="FAIL", error_type=type(exc).__name__)
        if isinstance(exc, EvidenceError):
            report["boundary_error"] = str(exc)
    report["latency_seconds"] = round(perf_counter() - start, 3)
    save(report, args.output)
    print(json.dumps({"status": report["status"], "stage": report.get("stage"), "output": str(args.output),
                      "error_type": report.get("error_type"), "metrics": report.get("metrics")}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
