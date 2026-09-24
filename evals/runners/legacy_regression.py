"""Legacy Regression Runner 的命令行入口与可审计报告生成。

Runner 只复用正式 Source Discovery 和 Stage 1 入口读取当前结果，再与已经被开发使用过
的旧 Gold 做回归比较。它不会把结果描述成独立质量评测，也不会写入生产快照历史。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.runners.source_discovery_regression import run_source_case
from evals.runners.stage1_regression import run_stage1_case
from evals.schema import load_and_validate
from evals.scoring import summarize_cases


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_GOLD = REPOSITORY_ROOT / "evals" / "gold" / "source_discovery_legacy_regression_v0.2.jsonl"
STAGE1_GOLD = REPOSITORY_ROOT / "evals" / "gold" / "stage1_legacy_regression_v0.2.jsonl"
DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT / "tmp" / "evals"
NOT_EVALUATED = [
    "previous_snapshot",
    "state_matching",
    "contextual_diff",
    "contextual_state_diff",
]


def _run_id() -> str:
    """创建本次运行的稳定目录名，避免覆盖以前的动态结果。"""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _filter_cases(records: list[dict[str, Any]], case_id: str | None) -> list[dict[str, Any]]:
    """按可选 Case ID 选择记录；未知 ID 由上层统一报错。"""
    if case_id is None:
        return records
    return [record for record in records if record["metadata"]["legacy_case_id"] == case_id]


def load_plan(target: str, case_id: str | None) -> dict[str, list[dict[str, Any]]]:
    """读取并校验 Gold，输出本次应该执行的 Source 与 Stage 1 Case。"""
    source_records = (
        load_and_validate(SOURCE_GOLD, "source_discovery")
        if target in {"all", "source_discovery"}
        else []
    )
    stage_records = (
        load_and_validate(STAGE1_GOLD, "stage1")
        if target in {"all", "stage1"}
        else []
    )
    plan = {
        "source_discovery": _filter_cases(source_records, case_id),
        "stage1": _filter_cases(stage_records, case_id),
    }
    if case_id and not any(plan.values()):
        raise ValueError(f"unknown legacy case id: {case_id}")
    return plan


def _source_equivalence_by_parent() -> dict[str, dict[str, Any]]:
    """让 Stage 1 仅沿用其 parent Source Case 已明示的 URL 等价规则。"""
    records = load_and_validate(SOURCE_GOLD, "source_discovery")
    return {
        record["metadata"]["legacy_case_id"]: record["expected"].get("url_equivalence", {})
        for record in records
    }


def build_result(
    *,
    run_id: str,
    target: str,
    case_id: str | None,
    dry_run: bool,
    plan: dict[str, list[dict[str, Any]]],
    cases: list[dict[str, Any]],
    started_at: str,
) -> dict[str, Any]:
    """构造机器可读结果；所有指标都保留分子和分母，不生成混合总分。"""
    review_queue = [item for case in cases for item in case.get("review_queue", [])]
    blocked = [case for case in cases if case.get("status") == "BLOCKED"]
    errors = [case for case in cases if case.get("status") == "ERROR"]
    return {
        "evaluation_type": "legacy_regression",
        "evaluation_use": "regression_only",
        "run_id": run_id,
        "target": target,
        "case_filter": case_id,
        "dry_run": dry_run,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "planned": {
            "source_discovery_cases": len(plan["source_discovery"]),
            "stage1_cases": len(plan["stage1"]),
            "total_cases": len(plan["source_discovery"]) + len(plan["stage1"]),
            "case_ids": {
                kind: [record["metadata"]["legacy_case_id"] for record in records]
                for kind, records in plan.items()
            },
            "production_capabilities": {
                "source_discovery": (
                    "SourceDiscoveryAgent + Candidate Registry + Search/Read Tools"
                ),
                "stage1": (
                    "Static First + Browser Fallback + Quality Gate + Structured Content + Snapshot"
                ),
            },
        },
        "summary": summarize_cases(cases),
        "cases": cases,
        "review_queue": review_queue,
        "blocked_cases": [case["case_id"] for case in blocked],
        "error_cases": [case["case_id"] for case in errors],
        "not_evaluated": NOT_EVALUATED,
        "interpretation": (
            "旧 Gold 已参与开发，只能用于回归检查；结果不能作为独立模型质量、准确率或"
            "泛化能力证明。真实网页变化与实现回归无法由本 Runner 自动区分。"
        ),
    }


async def execute_plan(plan: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """逐 Case 执行正式入口；一个 Case 只调用一次，不自动重试。"""
    cases: list[dict[str, Any]] = []
    for gold in plan["source_discovery"]:
        cases.append(await run_source_case(gold))

    equivalence_by_parent = _source_equivalence_by_parent()
    for gold in plan["stage1"]:
        parent = gold["metadata"].get("parent_case_id")
        cases.append(
            run_stage1_case(gold, url_equivalence=equivalence_by_parent.get(parent, {}))
        )
    return cases


def _metric_text(value: Any) -> str:
    if isinstance(value, dict) and {"matched", "total", "rate"} <= value.keys():
        rate = "N/A" if value["rate"] is None else f"{value['rate']:.2%}"
        return f"{value['matched']}/{value['total']} ({rate})"
    return str(value)


def _metrics_section(metrics: dict[str, Any]) -> list[str]:
    if not metrics:
        return ["无可汇总指标。"]
    return [f"- {key}: {_metric_text(value)}" for key, value in metrics.items()]


def _review_line(item: dict[str, Any]) -> str:
    """显示受限原因；只在 Runner 未作归因时列出允许的人工判断方向。"""
    line = f"- {item['case_id']} / {item['check']}: {item['reason']}"
    possible_causes = item.get("possible_causes")
    if isinstance(possible_causes, list) and possible_causes:
        line += " (possible: " + ", ".join(str(value) for value in possible_causes) + ")"
    return line


def render_report(result: dict[str, Any]) -> str:
    """生成面向人工审计的 Markdown，明确 Legacy 资产的证据边界。"""
    lines = [
        "# PIA Legacy Regression Report",
        "",
        "## Run Information",
        "",
        f"- Run ID: {result['run_id']}",
        f"- Evaluation Type: {result['evaluation_type']}",
        f"- Evaluation Use: {result['evaluation_use']}",
        f"- Dry Run: {result['dry_run']}",
        "",
        "## Evaluation Scope",
        "",
        f"- Target: {result['target']}",
        f"- Source Discovery Cases: {result['planned']['source_discovery_cases']}",
        f"- Stage 1 Cases: {result['planned']['stage1_cases']}",
        "- Source Discovery Case IDs: "
        + ", ".join(result["planned"]["case_ids"]["source_discovery"]),
        "- Stage 1 Case IDs: " + ", ".join(result["planned"]["case_ids"]["stage1"]),
        "- Source Discovery Production Capability: "
        + result["planned"]["production_capabilities"]["source_discovery"],
        "- Stage 1 Production Capability: "
        + result["planned"]["production_capabilities"]["stage1"],
        "",
        "## Source Discovery Metrics",
        "",
        *_metrics_section(result["summary"]["source_discovery"]),
        "",
        "## Stage 1 Metrics",
        "",
        *_metrics_section(result["summary"]["stage1"]),
        "",
        "## Case Results",
        "",
    ]
    if result["dry_run"]:
        lines.append("Dry Run 只校验资产与执行计划，没有调用网页或模型。")
    elif result["cases"]:
        lines.extend(
            f"- {case['case_id']} / {case['target']}: {case['status']}"
            for case in result["cases"]
        )
    else:
        lines.append("无 Case 结果。")

    lines.extend(["", "## Review Queue", ""])
    if result["review_queue"]:
        lines.extend(_review_line(item) for item in result["review_queue"])
    else:
        lines.append("无。")

    lines.extend(["", "## Blocked Cases", ""])
    lines.extend([f"- {case_id}" for case_id in result["blocked_cases"]] or ["无。"])
    lines.extend(["", "## Errors", ""])
    lines.extend([f"- {case_id}" for case_id in result["error_cases"]] or ["无。"])
    lines.extend(["", "## Not Evaluated", ""])
    lines.extend(f"- {item}" for item in result["not_evaluated"])
    lines.extend(["", "## Interpretation", "", result["interpretation"], ""])
    return "\n".join(lines)


def write_outputs(result: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    """把动态运行结果写到指定目录；默认目录位于被 Git 忽略的 tmp/evals。"""
    output_dir.mkdir(parents=True, exist_ok=False)
    json_path = output_dir / "results.json"
    report_path = output_dir / "report.md"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_path.write_text(render_report(result), encoding="utf-8")
    return json_path, report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run PIA Legacy Regression checks")
    parser.add_argument(
        "--target", choices=("all", "source_discovery", "stage1"), default="all"
    )
    parser.add_argument("--case", dest="case_id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """解析命令行、执行计划并写报告；ERROR 时返回非零退出码。"""
    args = parse_args(argv)
    run_id = _run_id()
    started_at = _utc_now()
    try:
        plan = load_plan(args.target, args.case_id)
        cases = [] if args.dry_run else asyncio.run(execute_plan(plan))
        result = build_result(
            run_id=run_id,
            target=args.target,
            case_id=args.case_id,
            dry_run=args.dry_run,
            plan=plan,
            cases=cases,
            started_at=started_at,
        )
        output_dir = args.output or (DEFAULT_OUTPUT_ROOT / run_id)
        json_path, report_path = write_outputs(result, output_dir)
    except (OSError, ValueError, TypeError) as exc:
        print(f"Legacy Regression Runner ERROR: {type(exc).__name__}")
        return 2

    print(f"Legacy Regression output: {output_dir}")
    print(f"Results: {json_path.name}; report: {report_path.name}")
    print(
        "Cases: "
        f"planned={result['planned']['total_cases']} "
        f"pass={result['summary']['pass_cases']} "
        f"review={result['summary']['review_cases']} "
        f"blocked={result['summary']['blocked_cases']} "
        f"error={result['summary']['error_cases']}"
    )
    return 1 if result["summary"]["error_cases"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
