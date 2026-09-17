"""最小三 Case 验收：真实模型 + Stage 1；明确区分本地 Fixture 与官方网页。"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.page_reader import acquire_page_with_browser_fallback
from backend.snapshot import create_snapshot
from event_core import deduplicate_event, detect_event
from event_core.inputs import build_explicit_statement_input, build_state_comparison_input
from event_core.evidence import EvidenceValidationError
from llm.client import get_llm
from llm.config import LLMConfigurationError


OFFICIAL_URL = "https://z-report.cn/changelog"
CHANGE_EXCERPT = "免费档新增每月 2,000 积分自动补充（原为一次性起始积分）。"
ORDINARY_EXCERPT = "每次发版登记一条。"


class FixtureHandler(BaseHTTPRequestHandler):
    """提供两个可控版本，不将模拟产品变化冒充真实互联网事件。"""

    export_text = "The export feature supports CSV only. PDF export is not supported."

    def do_GET(self) -> None:
        body = f"""<!doctype html><html><head><title>Fixture Product</title></head>
        <body><h1>Fixture Product</h1><h2>Export</h2><p>{type(self).export_text}</p>
        <p>This controlled acceptance fixture describes export capabilities for the
        same product and account scope. Product documentation, account management,
        storage and subscription settings are unchanged between observations.</p></body></html>""".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def read_snapshot(url: str) -> dict[str, Any]:
    """真实执行冻结采集和质量门槛，在内存中创建可审计快照。"""
    acquisition = acquire_page_with_browser_fallback(url)
    if not acquisition["quality_gate"]["downstream_allowed"]:
        raise ValueError("E2E 页面没有通过 Stage 1 Quality Gate。")
    return create_snapshot(acquisition["page_data"])


def run_case(label: str, kind: str, input_data, expected: set[str]) -> dict:
    """检测、证据校验和去重组成固定验收路径，记录真实运行数据。

    event 先进入空集合得到 NEW_EVENT，再把同一事件重放到去重层，要求 SAME_EVENT。
    no_event 不创建伪事件；uncertain 仅进入待核验去重分支。结果含输入与审计，方便复盘。
    """
    result = detect_event(input_data)
    decisions = {candidate.decision for candidate in result.candidates}
    records = []
    passed = decisions <= expected and result.runtime is not None
    for candidate in result.candidates:
        if candidate.decision == "event":
            first = deduplicate_event(candidate, [])
            repeated = deduplicate_event(candidate, [first.event])
            records.append({"initial": first.model_dump(mode="json"), "replay": repeated.model_dump(mode="json")})
            passed = passed and first.decision == "NEW_EVENT" and repeated.decision == "SAME_EVENT"
        elif candidate.decision == "uncertain":
            records.append(deduplicate_event(candidate, []).model_dump(mode="json"))
    return {
        "case": label, "kind": kind, "status": "PASS" if passed else "FAIL",
        "input": input_data.model_dump(mode="json"),
        "detection": result.model_dump(mode="json"), "dedup": records,
    }


def run_acceptance(results: list[dict], official_snapshot: Path | None = None) -> None:
    """准备三个有明确来源的输入，依次运行；任一步失败都停止并报告。

    A 是真实本地 HTTP 采集的受控 Fixture；B/C 默认共享一次官方页面采集，避免重复
    联网。若显式传入保存的快照，则 B/C 标为历史真实页面回放，不称为实时互联网采集。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/product"
        previous = read_snapshot(url)
        FixtureHandler.export_text = "The export feature supports both CSV and PDF. PDF export is now supported."
        current = read_snapshot(url)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        FixtureHandler.export_text = "The export feature supports CSV only. PDF export is not supported."
    path_a = build_state_comparison_input(
        dict(is_first_scan=False, previous_snapshot=previous, current_snapshot=current),
        competitor="Acceptance Fixture Product", source_type="product_page", source_credibility="official",
    )
    results.append(run_case("A", "semi_real_fixture_with_live_reasoning", path_a, {"event"}))
    if official_snapshot:
        official = json.loads(official_snapshot.read_text(encoding="utf-8"))
        kind = "archived_official_evidence_with_live_reasoning"
    else:
        official = read_snapshot(OFFICIAL_URL)
        kind = "live_official_page_with_live_reasoning"
    for label, excerpt, expected in (("B", CHANGE_EXCERPT, {"event"}), ("C", ORDINARY_EXCERPT, {"no_event", "uncertain"})):
        input_data = build_explicit_statement_input(
            official, excerpt, competitor="OctoReport 智报", source_type="changelog",
            source_credibility="official",
        )
        results.append(run_case(label, kind, input_data, expected))


def main() -> int:
    """加载配置、运行最小验收并写入单次报告；失败返回非零退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-snapshot", type=Path)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "experiments/output/event_core_e2e.json")
    args = parser.parse_args()
    report: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat(), "cases": []}
    try:
        config = get_llm(role="reasoning").config
        report["configured_runtime"] = {"role": config.role, "provider": config.provider, "model": config.model}
        run_acceptance(report["cases"], args.official_snapshot)
        report["status"] = "PASS" if all(case["status"] == "PASS" for case in report["cases"]) else "FAIL"
    except LLMConfigurationError as exc:
        report.update(status="BLOCKED", error=str(exc))
    except EvidenceValidationError as exc:
        report.update(status="FAIL", error_type=type(exc).__name__, error=str(exc),
                      failed_decision=getattr(exc, "failed_decision", None),
                      failed_runtime=getattr(exc, "runtime", None))
    except Exception as exc:
        # 底层网络异常可能包含敏感请求细节，验收报告只记录异常类型，不保存异常正文。
        report.update(status="FAIL", error_type=type(exc).__name__)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(args.output), "error": report.get("error"), "error_type": report.get("error_type")}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
