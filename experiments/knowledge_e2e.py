"""最小真实 RAG 验收：Own Fixture + 已有真实官方 Event/Evidence 回放。

不执行 Source Discovery 或 Event Detection，不生成最终答案。每次默认使用新的本地库，
便于证明实际向量化，避免把先前缓存误称为本轮真实调用。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from event_core.schemas import EventCandidate
from knowledge import KnowledgeIngestor, KnowledgeRetriever, KnowledgeStore
from knowledge.inputs import event_documents, own_product_file
from knowledge.schemas import content_hash
from llm.config import LLMConfigurationError
from llm.retrieval_models import get_embedding, get_rerank


def verify_provenance(store: KnowledgeStore, results, inputs: dict) -> bool:
    """把召回结果与本次原始文档逐项比较；来源、正文区间和指纹均由程序验证。"""
    for result in results:
        original = inputs[(result.knowledge_domain, result.document_id)]
        stored = store.document_record(result.knowledge_domain, result.document_id)[1]
        if (result.source_url != original.source_url or result.source_reference != original.source_reference
                or result.evidence_status != original.evidence_status or result.metadata != original.metadata
                or result.content != stored.content[result.start_offset:result.end_offset]
                or result.content_hash != content_hash(result.content)
                or result.document_content_hash != content_hash(stored.content)):
            return False
    return True


def run(report: dict, event_report: Path, database: Path) -> None:
    """读取明确来源，实际入库/重开/查询，记录模型、候选、分数和用量。

    B 只取历史验收中已 PASS 的官方 Case；保留原观察日期，不能称为本轮实时采集。
    三个查询分别检查自身域、竞品域和 both，both 的前两条应覆盖两域的相关积分事实。
    """
    raw = event_report.read_text(encoding="utf-8")
    archived = json.loads(raw)
    case = next(c for c in archived["cases"] if c["case"] == "B")
    if (archived["status"] != "PASS" or case["status"] != "PASS"
            or case["kind"] != "live_official_page_with_live_reasoning"):
        raise ValueError("需要已有真实官方来源验收记录。")
    event = EventCandidate.model_validate(case["detection"]["candidates"][0])
    if any(e.source_credibility != "official" for e in event.evidence):
        raise ValueError("验收竞品证据不是官方来源。")
    own = own_product_file(
        PROJECT_ROOT / "experiments/fixtures/knowledge_own_product.md",
        document_id="knowledge-e2e-own-fixture", product_id="fixture-product",
        title="Own Product 测试 Fixture", source_type="product_document",
        metadata={"fixture": True, "not_real_product_data": True},
    )
    competitor = event_documents(event, competitor_id="octoreport",
                                 source_reference=str(event_report.resolve()) + "#/cases/1/detection/candidates/0")
    documents = [own, *competitor]
    report["data"] = {
        "own_kind": "explicit_test_fixture", "competitor_kind": "archived_real_official_event_evidence",
        "event_report": str(event_report.resolve()), "event_report_hash": content_hash(raw),
        "documents": [d.model_dump(mode="json") for d in documents],
    }
    embedding, rerank = get_embedding(), get_rerank()
    report["configured_runtime"] = [
        {"role": c.config.role, "provider": c.config.provider, "model": c.config.model} for c in (embedding, rerank)
    ]
    store = KnowledgeStore(database)
    if store.counts()["documents"]:
        raise ValueError("真实验收需要新数据库路径，以避免复用旧向量。")
    ingestor = KnowledgeIngestor(store, embedding)
    report["ingestion"] = []
    for document in documents:
        start = perf_counter()
        result = ingestor.ingest(document)
        report["ingestion"].append({"result": result.model_dump(mode="json"),
                                    "latency_seconds": round(perf_counter() - start, 4),
                                    "runtime": [asdict(r) for r in ingestor.last_runtime]})
    before = store.counts()
    repeated = [ingestor.ingest(d).status for d in documents]
    if repeated != ["unchanged"] * len(documents) or before != store.counts() or ingestor.last_runtime:
        raise AssertionError("重复入库未通过。")
    report["dedup"] = {"statuses": repeated, "counts_before": before, "counts_after": store.counts()}
    # 在全新 Python 进程中重开库并检查持久化数量；子进程不携带凭据或模型调用。
    code = "import json,sys; from knowledge import KnowledgeStore; print(json.dumps(KnowledgeStore(sys.argv[1]).counts()))"
    child = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(database)],
                           cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30, check=True)
    persisted = json.loads(child.stdout)
    if persisted != before:
        raise AssertionError("跨进程持久化未通过。")
    report["persistence"] = {"fresh_process_counts": persisted, "status": "PASS"}
    with closing(sqlite3.connect(database)) as connection:
        profile = connection.execute("SELECT space_id, dimension FROM vector_space").fetchone()
    report["vector_space"] = {"identity": profile[0], "dimension": profile[1]}
    reopened = KnowledgeStore(database)
    retriever = KnowledgeRetriever(reopened, embedding, rerank)
    inputs = {(d.knowledge_domain, d.document_id): d for d in documents}
    report["queries"] = []
    for domain, query, k in (
        ("own_product", "测试 Fixture 产品的免费套餐每月如何补充积分？", 1),
        ("competitor", "智报 OctoReport 免费档每月积分如何补充？", 1),
        ("both", "自身测试产品和智报 OctoReport 的免费套餐每月分别补充多少积分？", 2),
    ):
        start = perf_counter()
        output = retriever.retrieve(query, knowledge_domain=domain, top_n=5, top_k=k)
        expected = {"own_product", "competitor"} if domain == "both" else {domain}
        passed = (len(output.results) == k and {r.knowledge_domain for r in output.results} == expected
                  and all("积分" in r.content for r in output.results)
                  and all(r.rerank_score is not None for r in output.results)
                  and verify_provenance(reopened, output.candidates + output.results, inputs)
                  and {r.knowledge_domain for r in output.candidates} <= expected)
        report["queries"].append({"status": "PASS" if passed else "FAIL",
                                  "latency_seconds": round(perf_counter() - start, 4),
                                  "retrieval": output.model_dump(mode="json"),
                                  "runtime": [asdict(r) for r in retriever.last_runtime]})
    records = [r for entry in report["ingestion"] + report["queries"] for r in entry["runtime"]]
    report["usage_summary"] = {"model_calls": len(records),
                             "reported_total_tokens": sum((r["usage"] or {}).get("total_tokens", 0) for r in records),
                             "calls_without_usage": sum(r["usage"] is None for r in records)}
    report["status"] = "PASS" if all(q["status"] == "PASS" for q in report["queries"]) else "FAIL"


def main() -> int:
    """运行验收并始终写出脱敏报告；失败只记异常类型，不输出服务响应或密钥。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-report", type=Path, default=PROJECT_ROOT / "experiments/output/event_core_v01_live_02.json")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / f"experiments/output/knowledge_e2e_{stamp}.json")
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    database = args.database or args.output.with_suffix(".sqlite3")
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "database": str(database)}
    started = perf_counter()
    try:
        run(report, args.event_report, database)
    except LLMConfigurationError as exc:
        report.update(status="BLOCKED", error=str(exc))
    except Exception as exc:
        report.update(status="FAIL", error_type=type(exc).__name__)
    report["latency_seconds"] = round(perf_counter() - started, 4)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(args.output),
                      "error_type": report.get("error_type"), "usage": report.get("usage_summary")}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
