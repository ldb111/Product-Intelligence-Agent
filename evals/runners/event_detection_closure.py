"""开发回归、历史离线重算及冻结 RC 的一次性最终独立评测。

旧 35 条从收尾开始属于 development_regression。独立性来自 Git 冻结顺序和未用于
开发的新材料，不来自更换一个输出标题。所有动态结果仅存 tmp。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from evals.event_detection_schema import read_jsonl, validate_records
from evals.event_metrics import summarize
from evals.runners import event_detection_independent as base
from event_core.evidence import EvidenceValidationError, validate_claims
from event_core.schemas import DetectionResult, EventDecision
from llm.client import LLMCallError
from llm.config import LLMConfigurationError


def canonical_commit(value: str) -> str:
    """将短 SHA、分支或完整 SHA 统一为 Commit 身份，防止别名绕过一次性凭条。"""
    result = subprocess.run(["git", "rev-parse", "--verify", value + "^{commit}"],
                            cwd=base.REPOSITORY_ROOT, capture_output=True, text=True, encoding="utf-8")
    if result.returncode or len(result.stdout.strip()) != 40:
        raise ValueError("无法解析冻结 Commit。")
    return result.stdout.strip()


def execute(item: dict, detector=None) -> dict:
    """执行单个生产检测，并保存可重现的错误定位与完整已解析决策；绝不重试。"""
    detector = detector or base.detect_event
    try:
        output = detector(item["model_input"])
        output = DetectionResult.model_validate(output.model_dump())
        case = base.score_success_case(item["record"], base.normalize_detection(output))
        case.update(model_calls=1 if output.runtime else 0)
        case["checks"]["leakage_guard"] = item["leakage"]
        return case
    except (LLMCallError, LLMConfigurationError) as exc:
        return base._non_success_case(item["record"], "BLOCKED", type(exc).__name__,
                                      item["leakage"], model_calls=1)
    except Exception as exc:
        case = base._non_success_case(item["record"], "ERROR", type(exc).__name__,
                                      item["leakage"], model_calls=1)
        if isinstance(exc, EvidenceValidationError):
            case["validation_diagnostic"] = exc.diagnostic
            case["raw_decisions"] = getattr(exc, "raw_decisions", {})
            case["failed_decision"] = getattr(exc, "failed_decision", None)
        return case


def verify_freeze_order(path: Path, records: list[dict], rc: str, candidate: str, gold: str) -> None:
    """检查 RC→空候选→Gold 的 Git 因果顺序，拒绝评测前偷改输入或生产实现。

    三个 Commit 必须不同且依次为祖先；标注只能更新 Gold 和标注元数据。生产目录及
    评测执行器必须与 RC 一致，工作区也必须干净，因此最终答案不能反向影响系统。
    """
    def git(*args):
        return subprocess.run(["git", *args], cwd=base.REPOSITORY_ROOT,
                              capture_output=True, text=True, encoding="utf-8")
    commits = [git("rev-parse", value + "^{commit}").stdout.strip() for value in (rc, candidate, gold)]
    if len(set(commits)) != 3 or not all(commits):
        raise ValueError("必须提供三个不同的真实冻结 Commit。")
    for earlier, later in zip(commits, commits[1:]):
        if git("merge-base", "--is-ancestor", earlier, later).returncode:
            raise ValueError("冻结顺序不是 RC → Candidate → Gold。")
    if git("merge-base", "--is-ancestor", commits[-1], "HEAD").returncode:
        raise ValueError("当前版本不包含冻结 Gold。")
    candidate_records = validate_records(base._jsonl_objects(base._git_text_at_commit(candidate, path)))
    if len(candidate_records) != len(records):
        raise ValueError("标注改变了候选数量。")
    annotation_fields = {"annotation_status", "annotation_method", "annotated_by"}
    for before, after in zip(candidate_records, records):
        if before["metadata"]["annotation_status"] != "pending_expert_review":
            raise ValueError("候选必须在答案为空时冻结。")
        for key in ("input", "provenance"):
            if before[key] != after[key]:
                raise ValueError("标注期间不得修改模型输入或来源。")
        if {k: v for k, v in before["metadata"].items() if k not in annotation_fields} != {
                k: v for k, v in after["metadata"].items() if k not in annotation_fields}:
            raise ValueError("标注期间改变了非标注元数据。")
    protected = ["api", "backend", "event_core", "file_parsing", "frontend", "knowledge",
                 "llm", "memory", "pipeline", "research", "source_discovery", "triage",
                 "evals/runners", "evals/event_metrics.py", "evals/event_detection_schema.py", "requirements.txt"]
    if (git("diff", "--name-only", rc, "--", *protected).stdout.strip()
            or git("diff", "--cached", "--name-only").stdout.strip()
            or git("ls-files", "--others", "--exclude-standard", "--", *protected).stdout.strip()):
        raise ValueError("RC 后生产/评测实现改变或仍有暂存修改。")


def load_final(path: Path, freeze: str, rc: str, candidate: str) -> list[dict]:
    """验证最终数据在指定 Commit 冻结、答案已完成且与工作区一致。"""
    records = validate_records(read_jsonl(path))
    frozen = base._jsonl_objects(base._git_text_at_commit(freeze, path))
    if records != frozen or not records:
        raise ValueError("最终 Gold 不匹配冻结 Commit。")
    if any(r["metadata"]["annotation_status"] != "ready" or
           r["metadata"].get("annotation_method") != "independent_expert_model_review" for r in records):
        raise ValueError("最终 Gold 必须如实记录独立专家模型标注。")
    old_ids = {r["metadata"]["case_id"] for r in base.load_dataset()}
    if any(r["metadata"]["case_id"] in old_ids for r in records):
        raise ValueError("不得把开发集作为最终留出集。")
    verify_freeze_order(path, records, rc, candidate, freeze)
    return records


def rescore(path: Path, records: list[dict]) -> tuple[dict, list[dict]]:
    """离线重算历史输出，重新触发原有校验以定位错误；不运行 Detector。"""
    saved = json.loads(path.read_text(encoding="utf-8"))
    by_id = {r["metadata"]["case_id"]: r for r in records}
    cases = saved["cases"]
    for case in cases:
        if case.get("failed_decision"):
            try:
                validate_claims(base.adapt_case_to_detection_input(by_id[case["case_id"]]),
                                EventDecision.model_validate(case["failed_decision"]))
            except EvidenceValidationError as exc:
                case["validation_diagnostic"] = exc.diagnostic
    return saved, cases


def main(argv=None) -> int:
    """先全量校验输入，再执行受控评测并逐条落盘，保留中断与失败事实。

    最终运行预先创建以 Gold Commit 为键的 receipt，第二次对同批 Gold 调用会直接拒绝。
    日志只显示 Case ID 和状态。异常正文、认证信息、Gold 不进入模型调用。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rescore", type=Path)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--freeze-commit")
    parser.add_argument("--rc-commit")
    parser.add_argument("--candidate-commit")
    parser.add_argument("--case", action="append", dest="case_ids")
    args = parser.parse_args(argv)
    if bool(args.dataset) != bool(args.freeze_commit):
        parser.error("--dataset 和 --freeze-commit 必须一起提供")
    if args.dataset and (args.rescore or args.case_ids):
        parser.error("最终评测必须一次完整运行")
    if args.dataset and not (args.rc_commit and args.candidate_commit):
        parser.error("最终评测必须提供 RC 和 Candidate 冻结 Commit")
    if args.dataset:
        args.freeze_commit = canonical_commit(args.freeze_commit)
        args.rc_commit = canonical_commit(args.rc_commit)
        args.candidate_commit = canonical_commit(args.candidate_commit)
    records = load_final(args.dataset.resolve(), args.freeze_commit, args.rc_commit, args.candidate_commit) if args.dataset else base.load_dataset()
    if args.case_ids:
        selected = [r for r in records if r["metadata"]["case_id"] in args.case_ids]
        if len(selected) != len(set(args.case_ids)):
            parser.error("包含未知 Case ID")
        records = selected
    plan = base.build_execution_plan(records)
    evaluation_type = "final_independent_holdout" if args.dataset else "development_regression"
    run_id, started = base._run_id(), base._utc_now()
    output = base.REPOSITORY_ROOT / "tmp" / "evals" / "closure" / run_id
    output.mkdir(parents=True, exist_ok=False)
    cases = []
    historical = None
    runtime = None
    if args.rescore:
        historical, cases = rescore(args.rescore, records)
        evaluation_type = "historical_baseline_rescore"
    elif not args.dry_run:
        runtime = base.runtime_metadata()
        if runtime["provider"] != "deepseek" or runtime["model"] != "deepseek-flash":
            raise ValueError("当前收尾只授权既定 DeepSeek / deepseek-flash。")
        if args.dataset:
            receipts = output.parent / "receipts"
            receipts.mkdir(exist_ok=True)
            receipt = receipts / (args.freeze_commit + ".json")
            with receipt.open("x", encoding="utf-8") as handle:
                json.dump({"started": started, "run_id": run_id}, handle)
        blocked = False
        for item in plan:
            case = (base._non_success_case(item["record"], "BLOCKED", "UpstreamProviderUnavailable",
                                           item["leakage"], model_calls=0) if blocked else execute(item))
            blocked = blocked or case["execution_status"] == "BLOCKED"
            base.assert_no_secret_fields(case)
            cases.append(case)
            with (output / "case_results.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(case, ensure_ascii=False) + "\n")
            print(f"{case['case_id']}: {case['execution_status']}", flush=True)
    result = base.build_result(run_id=run_id, started_at=started, dry_run=args.dry_run,
                               all_records=records, plan=plan, cases=cases, runtime=runtime,
                               config_status="NOT_CALLED" if args.dry_run or args.rescore else "READY")
    result.update(evaluation_type=evaluation_type, total_cases=len(records),
                  gold_freeze_commit=args.freeze_commit or base.GOLD_FREEZE_COMMIT,
                  runner_commit=base._code_head(), new_model_calls=0 if args.rescore else
                  sum(c.get("model_calls", 0) for c in cases))
    if historical:
        result["historical_source_sha256"] = hashlib.sha256(args.rescore.read_bytes()).hexdigest()
        for field in ("provider", "model", "prompt_sha256", "production_detector_identifier", "code_head_at_run"):
            result[field] = historical[field]
    if args.dataset:
        result.update(release_candidate_commit=args.rc_commit, candidate_freeze_commit=args.candidate_commit)
        result["limitations"] = ["新留出集仅提供小样本独立证据；标注者为专家模型，未经真人复核。",
                                 "不能代表 PIA 全部 AI 能力或生产级泛化准确率。"]
    if not args.dry_run:
        result["execution_summary"] = summarize(cases, records)
    base.assert_no_secret_fields(result)
    (output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = base.render_report(result).replace("# PIA Event Detection Independent Evaluation",
                                                "# PIA Event Detection " + evaluation_type)
    report = report.replace("这 35 条冻结 Independent Holdout 上的 Baseline #1", "本次标明用途的数据集")
    report += "\n## Execution / End-to-End / Time Eligibility\n\n```json\n" + json.dumps(
        result["execution_summary"], ensure_ascii=False, indent=2) + "\n```\n"
    (output / "report.md").write_text(report, encoding="utf-8")
    base._write_jsonl(output / "bad_case_candidates.jsonl", result["bad_case_candidates"])
    base._write_jsonl(output / "field_review_queue.jsonl", result["field_review_queue"])
    print(f"Output: {output}", flush=True)
    print(f"Leakage Guard PASS; selected={len(plan)}; new_model_calls={result['new_model_calls']}", flush=True)
    return int(any(c["execution_status"] != "SUCCESS" for c in cases))


if __name__ == "__main__":
    raise SystemExit(main())
