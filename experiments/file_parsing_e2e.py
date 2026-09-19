"""五种模拟文件 → 本地解析 → 现有 Knowledge 入库 → 真实 Embedding/Rerank → Retrieval。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from file_parsing import parse_and_prepare
from knowledge import KnowledgeIngestor, KnowledgeRetriever, KnowledgeStore
from llm.config import LLMConfigurationError
from llm.retrieval_models import get_embedding, get_rerank


FORMATS = ("md", "txt", "docx", "pdf", "xlsx")
QUERY = "产品是否支持竞争事件的证据追踪？"


def run(report: dict, fixture_dir: Path, database: Path) -> None:
    """逐格式真实解析、入库和检索，并按格式核对来源位置。

    五个文件只含模拟 PIA 资料。每类查询通过 source_type 限定自己的 Fixture，
    这样验收的是解析 provenance 与真实 RAG 接线，不把跨格式排名偶然性当质量结论。
    """
    prepared, documents = {}, []
    for kind in FORMATS:
        path = fixture_dir / f"demo.{kind}"
        value = parse_and_prepare(path, document_id=f"file-parsing-e2e-{kind}",
                                  product_id="file-parsing-e2e", source_type=f"file_parsing_{kind}")
        prepared[kind] = value
        if value.status != "parsed" or not value.ingestion_input:
            raise AssertionError(f"{kind} Fixture 未产生可入库正文。")
        documents.extend(value.ingestion_input)
    report["parsing"] = {
        kind: {"status": value.status, "metadata": value.document_metadata,
               "unit_count": len(value.ingestion_input), "warnings": value.warnings}
        for kind, value in prepared.items()
    }

    embedding, rerank = get_embedding(), get_rerank()
    report["configured_runtime"] = [
        {"role": item.config.role, "provider": item.config.provider, "model": item.config.model}
        for item in (embedding, rerank)
    ]
    store = KnowledgeStore(database)
    if store.counts()["documents"]:
        raise ValueError("真实验收需要新的数据库路径。")
    ingestor = KnowledgeIngestor(store, embedding)
    report["ingestion"] = []
    for document in documents:
        started = perf_counter()
        result = ingestor.ingest(document)
        report["ingestion"].append({"result": result.model_dump(mode="json"),
                                    "latency_seconds": round(perf_counter() - started, 4),
                                    "runtime": [asdict(item) for item in ingestor.last_runtime]})

    retriever = KnowledgeRetriever(KnowledgeStore(database), embedding, rerank)
    report["retrieval"] = []
    for kind in FORMATS:
        started = perf_counter()
        output = retriever.retrieve(QUERY, knowledge_domain="own_product", top_n=10, top_k=1,
                                    filters={"product_id": "file-parsing-e2e",
                                             "source_type": f"file_parsing_{kind}"})
        result = output.results[0] if output.results else None
        metadata = result.metadata if result else {}
        location_ok = (metadata.get("page_number") is not None if kind == "pdf" else
                       metadata.get("sheet_name") == "产品能力" if kind == "xlsx" else
                       "竞争事件证据追踪" in metadata.get("heading_path", []) if kind in {"docx", "md"} else True)
        relevant = result is not None and any(word in result.content for word in ("证据", "来源", "回溯"))
        passed = bool(result and Path(result.source_reference).name == f"demo.{kind}"
                      and result.source_type == f"file_parsing_{kind}" and location_ok and relevant)
        report["retrieval"].append({"file_type": kind, "status": "PASS" if passed else "FAIL",
                                    "latency_seconds": round(perf_counter() - started, 4),
                                    "response": output.model_dump(mode="json"),
                                    "runtime": [asdict(item) for item in retriever.last_runtime]})

    records = [item for entry in report["ingestion"] + report["retrieval"] for item in entry["runtime"]]
    report["store_counts"] = store.counts()
    report["usage_summary"] = {
        "model_calls": len(records),
        "reported_total_tokens": sum((item["usage"] or {}).get("total_tokens", 0) for item in records),
        "calls_without_usage": sum(item["usage"] is None for item in records),
    }
    report["status"] = "PASS" if all(item["status"] == "PASS" for item in report["retrieval"]) else "FAIL"


def main() -> int:
    """写出独立技术报告；失败只记录异常类型，不输出服务响应或凭据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path,
                        default=PROJECT_ROOT / "experiments/fixtures/file_parsing")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    parser.add_argument("--output", type=Path,
                        default=PROJECT_ROOT / f"experiments/output/file_parsing_e2e_{stamp}.json")
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    database = args.database or args.output.with_suffix(".sqlite3")
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "query": QUERY}
    started = perf_counter()
    try:
        run(report, args.fixtures, database)
    except LLMConfigurationError as exc:
        report.update(status="BLOCKED", error=str(exc))
    except Exception as exc:
        report.update(status="FAIL", error_type=type(exc).__name__)
    report["latency_seconds"] = round(perf_counter() - started, 4)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(args.output),
                      "error_type": report.get("error_type"), "usage": report.get("usage_summary")},
                     ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
