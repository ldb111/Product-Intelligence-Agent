"""Final Integration 最小验收：一条真实受控 Baseline + 一条历史真实 Event 的后半链路。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_core.schemas import EventCandidate
from experiments.event_core_e2e import FixtureHandler
from knowledge.schemas import content_hash
from pipeline.orchestrator import ProductPipeline
from pipeline.output import build_product_result
from pipeline.schemas import ProductInput
from pipeline.store import identity
from source_discovery.schemas import SourceDiscoveryResult


FOCUS = "后续竞品研究优先关注免费套餐的周期积分补充与获客策略，并比较双方免费计划。"


def save_json(path: Path, content) -> None:
    """保存可阅读 UTF-8 JSON；用户产品数据和开发审计使用两个明确不同的文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def runtime_metrics(audit: dict) -> dict:
    """从各步骤和独立产物累计已实际报告的模型用量，不把引用重复算成多次调用。"""
    records = []
    for stage in audit.get("steps", {}).values():
        data = stage.get("data", {})
        records.extend(data.get("runtime", []))
    for artifact in audit.get("artifacts", {}).values():
        data = artifact.get("audit", {})
        records.extend(data.get("model_runtime", []))
        records.extend(data.get("retrieval_runtime", []))
    return {"model_calls": len(records),
            "reported_tokens": sum((r.get("token_usage") or r.get("usage") or {}).get("total_tokens", 0) for r in records),
            "calls_without_usage": sum(not (r.get("token_usage") or r.get("usage")) for r in records),
            "roles": sorted({r["role"] for r in records})}


async def run(args, public: dict, audit: dict) -> None:
    """只执行规定的两个案例；不把历史回放冒充完整重新发现/扫描产生的新事件。

    Baseline 页面通过已有 FixtureHandler 提供，真实走冻结采集器和质量门槛。
    Case B 复用已 PASS 的官方 Event；先完成分诊 recommendation，再模拟用户显式研究。
    关注记忆的写入是明确的模拟用户操作，研究结果不会触发写入。
    """
    pipeline = ProductPipeline(args.state_dir)
    if pipeline.store.items("runs"):
        raise ValueError("首次验收需要新状态目录；失败恢复请使用 --resume-run。")
    public["status"], audit["stage"] = "RUNNING", "baseline"
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/product"
        config = ProductInput(workspace_id="pia-baseline-fixture", competitor_name="受控 Baseline Fixture", official_url=url)
        # 明确确认受控来源，不伪造 Source Discovery 的真实模型调用。
        pipeline.confirm_monitoring_sources(config, SourceDiscoveryResult(
            competitor_name=config.competitor_name, official_domains=["127.0.0.1"], discovery_complete=True,
            remaining_gaps=[], sources=[{"url": url, "title": "Controlled Fixture", "source_type": "product_page",
                                        "monitoring_reason": "受控演示来源，用于首次扫描验收"}]))
        first = await pipeline.run(config)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    public["baseline"] = first["product"]
    audit["baseline"] = {"run_id": first["run_id"], **first["audit"]}
    if (first["product"]["status"] != "complete" or not first["product"]["baseline_created"]
            or first["product"]["detected_events"] or first["product"]["triage_results"]
            or first["product"]["research_results"]):
        raise AssertionError("Baseline 未通过。")
    save_json(args.output, public)
    save_json(args.audit, audit)

    await run_main(args, pipeline, public, audit)


async def run_main(args, pipeline, public, audit):
    """从已通过的 Baseline 后继续准备和运行主链路，网络中断不会重复采集首次页面。"""
    raw = args.event_report.read_text(encoding="utf-8")
    archived = json.loads(raw)
    case = next(c for c in archived["cases"] if c["case"] == "B")
    if archived["status"] != "PASS" or case["status"] != "PASS" or case["kind"] != "live_official_page_with_live_reasoning":
        raise ValueError("需已有真实官方来源验收记录。")
    event = EventCandidate.model_validate(case["detection"]["candidates"][0])
    audit["event_provenance"] = {"path": str(args.event_report), "sha256": content_hash(raw),
                                 "kind": "archived_real_official_event"}
    config = ProductInput(workspace_id="pia-integration-fixture", own_product_id="pia-own-fixture",
                           competitor_name=event.competitor,
                           official_url="https://z-report.cn", current_focus=FOCUS,
                           product_profile="自身产品资料为明确的模拟 Fixture，本轮研究免费套餐的周期积分机制。")
    # 自身资料先通过独立 Workspace Configuration 入口入库，Monitoring/回放只消费持久化知识。
    if "own_product_ingestion" not in audit:
        audit["own_product_ingestion"] = pipeline.add_own_product_document(
            workspace_id=config.workspace_id, product_id=config.own_product_id,
            file=ROOT / "experiments/fixtures/knowledge_own_product.md",
        ).model_dump(mode="json")
    # 用户明确要求的测试记忆通过正式写入工作流准备；不是 current_focus 的自动副作用。
    audit["stage"] = "explicit_memory_preparation"
    if "memory_preparation" not in audit:
        audit["memory_preparation"] = pipeline.remember_explicit(config, FOCUS)
    audit["stage"] = "confirmed_event_to_product_result"
    save_json(args.audit, audit)
    output = await pipeline.run_events(config, [event], provenance="archived-event:" + content_hash(raw))
    finish_main(output, public, audit, pipeline, config)


def finish_main(output, public, audit, pipeline, config):
    """分三阶段验收 recommendation、显式 Research 和 completed 幂等复用。"""
    product = output["product"]
    event_id = product["detected_events"][0]["event_id"] if product["detected_events"] else None
    phase_a = (product["status"] == "complete" and event_id is not None
               and any(t["result"]["decision"] == "research" for t in product["triage_results"])
               and product["research_actions"] and product["research_actions"][0]["status"] == "not_started"
               and not product["research_results"])
    explicit = pipeline.start_research(config, event_id) if event_id else {"product": {"accepted": False}, "audit": {}}
    phase_b = (explicit["product"].get("accepted") is True
               and explicit["product"]["research"]["status"] == "completed"
               and bool(explicit["product"]["research"].get("result")))
    repeated = pipeline.start_research(config, event_id) if event_id else {"product": {"accepted": False}, "audit": {}}
    phase_c = (repeated["product"].get("accepted") is True
               and repeated["product"]["research"]["status"] == "completed"
               and repeated["audit"].get("reused") is True)
    public.update({"competitor": product["competitor"], "event": product["detected_events"],
                   "triage": product["triage_results"], "research_recommendation": product["research_actions"],
                   "research": explicit["product"].get("research"),
                   "citations": product["citations"], "uncertainties": product["uncertainties"],
                   "phases": {"recommendation": phase_a, "explicit_start": phase_b,
                              "completed_reuse": phase_c}, "product": product})
    audit["main"] = {"run_id": output["run_id"], **output["audit"]}
    if explicit["audit"].get("artifact") or "explicit_research" not in audit:
        audit["explicit_research"] = explicit["audit"]
    metric_audit = {"steps": output["audit"]["steps"], "artifacts": dict(output["audit"]["artifacts"])}
    if audit["explicit_research"].get("artifact"):
        metric_audit["artifacts"]["explicit_research"] = audit["explicit_research"]["artifact"]
    audit["metrics"] = runtime_metrics(metric_audit)
    artifacts = output["audit"]["artifacts"]
    triage = next((v for k, v in artifacts.items() if k.startswith("triage:")), None)
    context = triage.get("context", {}) if triage else {}
    knowledge = context.get("knowledge_context", {})
    passed = (phase_a and phase_b and phase_c
              and knowledge.get("own_product", {}).get("results") and knowledge.get("competitor", {}).get("results")
              and context.get("memory_context", {}).get("results")
              and {"reasoning", "research", "embedding", "rerank"} <= set(audit["metrics"]["roles"]))
    public["status"] = "PASS" if passed else "FAIL"


async def main() -> int:
    """CLI 只打印状态和产物位置；仅失败步骤可显式恢复，不重跑已完成的真实模型链路。"""
    parser = argparse.ArgumentParser(description=__doc__)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    parser.add_argument("--output", type=Path, default=ROOT / f"experiments/output/pia_e2e_{stamp}.json")
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--event-report", type=Path, default=ROOT / "experiments/output/event_core_v01_live_02.json")
    parser.add_argument("--resume-run")
    parser.add_argument("--resume-setup", action="store_true")
    args = parser.parse_args()
    args.audit = args.audit or args.output.with_suffix(".audit.json")
    args.state_dir = args.state_dir or args.output.with_suffix(".state")
    public, audit = {"status": "RUNNING"}, {"started_at": datetime.now(timezone.utc).isoformat()}
    started = perf_counter()
    try:
        if args.resume_run or args.resume_setup:
            public = json.loads(args.output.read_text(encoding="utf-8"))
            audit = json.loads(args.audit.read_text(encoding="utf-8"))
            if args.resume_run and args.resume_setup:
                raise ValueError("两种恢复入口不能同时使用。")
            if audit.get("error_type"):
                audit.setdefault("interrupted_attempts", []).append(
                    {"stage": audit.get("stage"), "error_type": audit.pop("error_type"),
                     "latency_seconds": audit.get("latency_seconds")})
            pipeline = ProductPipeline(args.state_dir)
            # 只从已保存的原始状态重新投影显示字段，绝不重新执行已通过的 Baseline。
            baseline_id = audit["baseline"]["run_id"]
            baseline_run = pipeline.store.get("runs", baseline_id)
            baseline_config = ProductInput.model_validate(baseline_run["product"])
            public["baseline"] = build_product_result(
                pipeline.store, baseline_id, baseline_config,
                identity([baseline_config.workspace_id, baseline_config.competitor_name]), None).model_dump()
            if args.resume_run:
                result = await pipeline.resume(args.resume_run)
                run_config = ProductInput.model_validate(pipeline.store.get("runs", args.resume_run)["product"])
                finish_main(result, public, audit, pipeline, run_config)
            else:
                baseline = public.get("baseline", {})
                if baseline.get("status") != "complete" or not baseline.get("baseline_created"):
                    raise ValueError("准备阶段恢复要求已有成功 Baseline。")
                if any(r.get("mode") == "event_replay" for r in pipeline.store.items("runs").values()):
                    raise ValueError("主链路已经开始，请按保存的 run_id 恢复。")
                await run_main(args, pipeline, public, audit)
        else:
            await run(args, public, audit)
    except Exception as exc:
        public["status"] = "FAIL"
        audit["error_type"] = type(exc).__name__
    audit["latency_seconds"] = round(perf_counter() - started, 3)
    save_json(args.output, public)
    save_json(args.audit, audit)
    print(json.dumps({"status": public["status"], "output": str(args.output), "audit": str(args.audit),
                      "stage": audit.get("stage"), "error_type": audit.get("error_type"),
                      "run_id": audit.get("main", {}).get("run_id")}, ensure_ascii=False))
    return 0 if public["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
