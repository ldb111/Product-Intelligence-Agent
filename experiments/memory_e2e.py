"""Long-term Memory V0.1 最小真实验收，只使用明确标注的模拟会话文本。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from llm.client import LLMCallError, LLMStructuredOutputError
from llm.config import LLMConfigurationError
from llm.retrieval_models import EmbeddingAPIError, get_embedding
from memory import MemoryRetriever, MemoryStore, MemoryWriter, extract_candidates
from memory.schemas import MemoryReferences


SESSION_1 = "后续竞品研究优先关注中国市场和企业权限能力。"
SESSION_2_QUERY = "分析新的企业版竞争事件时应该重点考虑什么？"
SESSION_3_UPDATE = "后续竞品研究改为优先关注日韩市场和商业化能力。"


def run(report: dict, database: Path) -> None:
    """真实执行提取、写入、重开、召回和替代，结果写入脱敏开发报告。

    Session 1 是公开在本脚本中的模拟文本。Fast LLM 只提出逐字 Candidate；程序再以
    显式用户来源写入 confirmed。Session 2 在重开数据库后语义召回。最后以同一稳定
    key 写入明确新值，验证旧记录保留为 superseded。全程没有 Pricing/Event 事实。
    """
    store = MemoryStore(database)
    if store.counts()["total"]:
        raise ValueError("真实验收需要空白 Memory 数据库，避免复用历史结果。")
    embedding = get_embedding()
    writer = MemoryWriter(store, embedding)
    report["configured_runtime"] = {
        "fast": None,
        "embedding": {"provider": embedding.config.provider, "model": embedding.config.model},
    }

    started = perf_counter()
    extraction = extract_candidates(SESSION_1, source_kind="direct_user")
    report["configured_runtime"]["fast"] = {
        "provider": extraction.runtime.provider, "model": extraction.runtime.model,
        "response_model": extraction.runtime.response_model,
    }
    if not extraction.candidates:
        raise AssertionError("Fast Role 没有提取到模拟文本中的明确长期关注。")
    if any(candidate.content not in SESSION_1 for candidate in extraction.candidates):
        raise AssertionError("提取结果不是模拟来源的逐字片段。")
    report["extraction"] = {
        "latency_seconds": round(perf_counter() - started, 4),
        "candidates": [candidate.model_dump() for candidate in extraction.candidates],
        "rejected_count": extraction.rejected_count,
        "runtime": extraction.runtime.model_dump(),
    }

    started = perf_counter()
    initial = writer.remember_explicit(
        scope="workspace", scope_id="pia-memory-e2e-fixture", memory_type="focus",
        memory_key="competitor_research_focus", content=SESSION_1,
        source_reference="fixture://memory-e2e/session-1#message-1", source_text=SESSION_1,
        references=MemoryReferences(research_id="memory-e2e-fixture"),
        metadata={"fixture": True, "contains_real_user_data": False},
    )
    report["initial_write"] = {
        "latency_seconds": round(perf_counter() - started, 4),
        "result": initial.model_dump(mode="json"),
        "runtime": [asdict(item) for item in writer.last_runtime],
    }
    if initial.action != "inserted" or initial.memory.status != "confirmed":
        raise AssertionError("显式 Memory 未以 confirmed 状态写入。")

    code = "import json,sys; from memory import MemoryStore; print(json.dumps(MemoryStore(sys.argv[1]).counts()))"
    child = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code, str(database)], cwd=PROJECT_ROOT,
        capture_output=True, text=True, timeout=30, check=True,
    )
    persisted = json.loads(child.stdout)
    if persisted != {"total": 1, "pending": 0, "confirmed": 1, "superseded": 0}:
        raise AssertionError("独立进程未读取到已确认 Memory。")
    report["persistence"] = {"status": "PASS", "fresh_process_counts": persisted}

    reopened = MemoryStore(database)
    retriever = MemoryRetriever(reopened, embedding)
    started = perf_counter()
    retrieval = retriever.retrieve_memory(
        SESSION_2_QUERY, scope="workspace", scope_id="pia-memory-e2e-fixture", top_k=3,
    )
    report["retrieval"] = {
        "latency_seconds": round(perf_counter() - started, 4),
        "result": retrieval.model_dump(mode="json"),
        "runtime": [asdict(item) for item in retriever.last_runtime],
    }
    if (retrieval.status != "ok" or len(retrieval.results) != 1
            or retrieval.results[0].content != SESSION_1
            or retrieval.results[0].context_type != "memory"
            or any(term in retrieval.results[0].content for term in ("价格", "定价", "上线", "发布"))):
        raise AssertionError("重启后的 Memory 召回结果不符合域边界。")

    started = perf_counter()
    update = writer.remember_explicit(
        scope="workspace", scope_id="pia-memory-e2e-fixture", memory_type="focus",
        memory_key="competitor_research_focus", content=SESSION_3_UPDATE,
        source_reference="fixture://memory-e2e/session-3#message-1", source_text=SESSION_3_UPDATE,
        references=MemoryReferences(research_id="memory-e2e-fixture"),
        metadata={"fixture": True, "contains_real_user_data": False},
    )
    old = reopened.get(initial.memory.memory_id)
    report["supersession"] = {
        "latency_seconds": round(perf_counter() - started, 4),
        "result": update.model_dump(mode="json"),
        "old_memory": old.model_dump(mode="json") if old else None,
        "runtime": [asdict(item) for item in writer.last_runtime],
        "counts": reopened.counts(),
    }
    if (update.action != "superseded" or update.memory.supersedes_memory_id != initial.memory.memory_id
            or old is None or old.status != "superseded" or old.is_active
            or reopened.counts() != {"total": 2, "pending": 0, "confirmed": 1, "superseded": 1}):
        raise AssertionError("显式更新没有正确保留 superseded 历史。")

    runtime_records = [extraction.runtime.model_dump()]
    runtime_records.extend(report["initial_write"]["runtime"])
    runtime_records.extend(report["retrieval"]["runtime"])
    runtime_records.extend(report["supersession"]["runtime"])
    total_tokens = 0
    missing_usage = 0
    for item in runtime_records:
        usage = item.get("token_usage") if "token_usage" in item else item.get("usage")
        if usage is None:
            missing_usage += 1
        else:
            total_tokens += usage.get("total_tokens", 0)
    report["usage_summary"] = {
        "model_calls": len(runtime_records), "reported_total_tokens": total_tokens,
        "calls_without_usage": missing_usage,
    }
    report["status"] = "PASS"


def main() -> int:
    """运行真实验收并始终写出无 Secret 报告；失败不保存底层服务异常正文。"""
    parser = argparse.ArgumentParser(description=__doc__)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / f"experiments/output/memory_e2e_{stamp}.json")
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    database = args.database or args.output.with_suffix(".sqlite3")
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(), "database": str(database),
        "fixture": {"contains_real_user_data": False, "session_1": SESSION_1,
                    "session_2_query": SESSION_2_QUERY, "session_3_update": SESSION_3_UPDATE},
    }
    started = perf_counter()
    try:
        run(report, database)
    except LLMConfigurationError as exc:
        report.update(status="BLOCKED", error=str(exc))
    except (LLMCallError, LLMStructuredOutputError, EmbeddingAPIError) as exc:
        report.update(status="FAIL", error_type=type(exc).__name__)
    except Exception as exc:
        report.update(status="FAIL", error_type=type(exc).__name__)
    report["latency_seconds"] = round(perf_counter() - started, 4)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": report["status"], "output": str(args.output),
        "error_type": report.get("error_type"), "usage": report.get("usage_summary"),
    }, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
