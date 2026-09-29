"""Event Detection Independent Holdout 的执行、评分与动态报告入口。

Runner 只把模型首次运行前冻结的两份 Gold 输入适配到正式 ``detect_event`` 接口。
它不实现事件判断、不联网刷新证据、不修改 Prompt，也不把 Gold 或选材元数据放入模型上下文。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from dotenv import load_dotenv
from pydantic import ValidationError

from evals.event_detection_schema import (
    DECISIONS,
    EVENT_TYPES,
    load_and_validate,
    read_jsonl,
    validate_supplement_records,
)
from event_core import detect_event
from event_core.evidence import EvidenceValidationError
from event_core.prompts import EVENT_DETECTION_PROMPT
from event_core.schemas import DetectionInput, DetectionResult, StateContext
from llm.client import LLMCallError, LLMStructuredOutputError
from llm.config import LLMConfigurationError, load_model_config


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ORIGINAL_GOLD = REPOSITORY_ROOT / "evals" / "gold" / "event_detection_fresh_v0.1.jsonl"
SUPPLEMENT_GOLD = REPOSITORY_ROOT / "evals" / "gold" / "event_detection_fresh_supplement_v0.1.jsonl"
DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT / "tmp" / "evals" / "event_detection"
GOLD_FREEZE_COMMIT = "ab0a64ced811f7cd686c66c7e2943e0aa6f8e49c"
PROMPT_IDENTIFIER = "event_core.prompts.EVENT_DETECTION_PROMPT"
LABELS = ("event", "no_event", "uncertain")
FORBIDDEN_MODEL_KEYS = {
    "gold", "decision", "event_type", "annotation_notes", "annotated_by",
    "annotation_method", "target_coverage_type", "selection_notes",
    "contamination_check", "freshness_check", "expected", "metrics",
}
SECRET_KEYS = {
    "api_key", "authorization", "secret", "password", "credential",
    "llm_reasoning_api_key", "deepseek_api_key",
}


def _utc_now() -> str:
    """返回带时区的 UTC 时间，供运行元数据与目录身份使用。"""
    return datetime.now(timezone.utc).isoformat()


def _run_id() -> str:
    """生成不会覆盖既有动态评测结果的运行目录名。"""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _git_text_at_commit(commit: str, path: Path) -> str:
    """读取冻结 Commit 中的 Gold 文本，用于阻止评测前后的工作区标签漂移。"""
    relative = path.relative_to(REPOSITORY_ROOT).as_posix()
    completed = subprocess.run(
        ["git", "show", f"{commit}:{relative}"], cwd=REPOSITORY_ROOT,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"无法读取冻结 Gold：{relative}")
    return completed.stdout


def _jsonl_objects(text: str) -> list[dict[str, Any]]:
    """把 Git Blob 中的 JSONL 解析为对象，避免换行风格造成错误漂移判断。"""
    try:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise ValueError("冻结 Commit 中的 Gold 不是合法 JSONL。") from exc


def verify_gold_freeze() -> None:
    """确认两份当前 Gold 与首次模型运行前的冻结 Commit 完全一致。"""
    for path in (ORIGINAL_GOLD, SUPPLEMENT_GOLD):
        current = read_jsonl(path)
        frozen = _jsonl_objects(_git_text_at_commit(GOLD_FREEZE_COMMIT, path))
        if current != frozen:
            raise ValueError(f"Gold 已偏离冻结 Commit：{path.name}")


def load_dataset(*, verify_freeze: bool = True) -> list[dict[str, Any]]:
    """联合读取两份 Gold，并检查最终 35 条独立留出集的全局身份与状态。

    输入只决定是否与 Git 冻结版本交叉核对；处理过程复用 eval-only Schema，拒绝重复 ID、
    非 ready、污染或非独立留出记录；输出仍保留 Original 后 Supplement 的稳定顺序。
    """
    if verify_freeze:
        verify_gold_freeze()
    original = load_and_validate(ORIGINAL_GOLD)
    supplement = validate_supplement_records(read_jsonl(SUPPLEMENT_GOLD))
    records = [*original, *supplement]
    case_ids = [record["metadata"]["case_id"] for record in records]
    if len(original) != 30 or len(supplement) != 5 or len(records) != 35:
        raise ValueError("Independent Holdout 必须是 30 Original + 5 Supplement。")
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("两份 Gold 的 case_id 必须全局唯一。")
    for record in records:
        metadata = record["metadata"]
        if metadata.get("annotation_status") != "ready":
            raise ValueError("Independent Holdout 仍有未完成标注的 Case。")
        if metadata.get("evaluation_use") != "independent_holdout" or metadata.get("contaminated") is not False:
            raise ValueError("Independent Holdout 的用途或污染边界无效。")
    return records


def filter_cases(
    records: list[dict[str, Any]], *, case_id: str | None = None,
    path: str | None = None,
) -> list[dict[str, Any]]:
    """按 CLI 的 Case 或 Path 过滤执行计划，未知或冲突筛选明确失败。"""
    selected = [
        record for record in records
        if (case_id is None or record["metadata"]["case_id"] == case_id)
        and (path is None or record["metadata"]["path"] == path)
    ]
    if not selected:
        raise ValueError("Case / Path 筛选没有匹配 Independent Holdout。")
    return selected


def dataset_statistics(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """统计数据集真实分布；只用于报告，不参与模型输入或推理。"""
    values = list(records)
    decisions = Counter(record["gold"]["decision"] for record in values)
    event_types = Counter(
        record["gold"]["event_type"] for record in values
        if record["gold"]["decision"] == "event"
    )
    paths = Counter(record["metadata"]["path"] for record in values)
    return {
        "total_cases": len(values),
        "decision_distribution": {label: decisions[label] for label in LABELS},
        "path_distribution": dict(paths),
        "event_type_distribution": {name: event_types[name] for name in sorted(EVENT_TYPES)},
        "event_type_coverage": len({name for name, count in event_types.items() if count}),
    }


def _normalize_observed_at(value: str) -> str:
    """把 Gold 的观察日期适配成生产 Schema 要求的带时区时间。

    冻结 Case 只记录“哪一天观察到页面”，没有更细的时分秒。这里使用当天 UTC 零点作为
    技术边界值，仅满足 ``DetectionInput`` 的时区约束；它仍是观察时间，不能被模型当作
    事件发生时间。已有带时区时间保持原值，含时间但缺少时区的值则拒绝猜测。
    """
    date_value = value
    if value.startswith("undated; retrieved "):
        # 页面没有发布日期时，Gold 明确给出的 retrieved 日期就是“观察到”的日期；
        # 这里只适配 DetectionInput，不把 retrieved 日期升级成事件发生时间。
        date_value = value.removeprefix("undated; retrieved ")
    if len(date_value) == 10:
        datetime.fromisoformat(date_value)
        return f"{date_value}T00:00:00+00:00"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Gold 观察时间包含时分秒时必须明确时区。")
    return value


def adapt_case_to_detection_input(record: dict[str, Any]) -> DetectionInput:
    """把冻结 Case 转成生产 ``DetectionInput``，不携带 Gold 或选材元数据。

    Path A 保留前后原文、观察时间与冻结 contextual diff；Path B 仅传官方明确声明，
    没有历史状态时继续为空。来源字段来自 provenance，不重新联网或猜测新事实。
    """
    metadata = record["metadata"]
    source = record["provenance"]["current_source"]
    data = record["input"]
    common = {
        "competitor": data["competitor"],
        "source_url": source["url"],
        "source_title": source["title"],
        "source_type": metadata["source_kind"],
        "source_credibility": "official",
        "evidence_status": "confirmed",
        "detection_path": metadata["path"],
    }
    if metadata["path"] == "state_comparison":
        previous = data["previous_state"]
        current = data["current_state"]
        frozen_diff = data["contextual_diff"]
        contextual_diff = [{
            "comparison_scope": frozen_diff["comparison_scope"],
            "diff_status": frozen_diff["diff_status"],
            "previous": frozen_diff["previous_excerpt"],
            "current": frozen_diff["current_excerpt"],
        }]
        return DetectionInput(
            **common,
            state_context=StateContext(comparison_status="comparable"),
            previous_state=previous["content"], current_state=current["content"],
            contextual_diff=contextual_diff,
            previous_observed_at=_normalize_observed_at(previous["observed_at"]),
            current_observed_at=_normalize_observed_at(current["observed_at"]),
        )
    statement = data["explicit_statement"]
    return DetectionInput(
        **common,
        state_context=StateContext(comparison_status="current_only"),
        previous_state=None, current_state=statement["content"],
        contextual_diff=[], previous_observed_at=None,
        current_observed_at=_normalize_observed_at(statement["observed_at"]),
    )


def _walk_keys(value: Any) -> set[str]:
    """递归收集结构化键名；只审查字段身份，不误伤证据正文中的普通单词。"""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            found.add(str(key).casefold())
            found.update(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_walk_keys(child))
    return found


def leakage_guard(data: DetectionInput) -> dict[str, Any]:
    """证明实际序列化模型输入不含 Gold、标注信息或 Supplement 选材答案。

    输入是生产 Schema 已验证的 DetectionInput；输出仅保留 SHA256 和顶层字段，供 Dry Run
    审计。任何禁止键命中都立即失败，不能在正式模型调用后再补救。
    """
    payload = data.model_dump(mode="json")
    leaked = sorted(_walk_keys(payload) & FORBIDDEN_MODEL_KEYS)
    if leaked:
        raise ValueError("Gold Leakage Guard 命中禁止字段：" + ", ".join(leaked))
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "status": "PASS",
        "model_input_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "top_level_fields": sorted(payload),
    }


def build_execution_plan(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """在任何模型调用前完成全部输入构造和 Leakage Guard。"""
    plan = []
    for record in records:
        model_input = adapt_case_to_detection_input(record)
        plan.append({
            "record": record,
            "model_input": model_input,
            "leakage": leakage_guard(model_input),
        })
    return plan


def _decision_view(candidate: Any) -> dict[str, Any]:
    """从生产 EventCandidate 提取评测所需字段，保留引用但不复制派生 Evidence。"""
    return {
        key: getattr(candidate, key)
        for key in (
            "decision", "event_type", "event_subject", "change_action", "change_items",
            "event_time", "effective_time", "affected_scope", "decision_reason",
            "uncertainty_reason", "citations",
        )
    } | {
        "change_items": [item.model_dump(mode="json") for item in candidate.change_items],
        "citations": {
            key: [quote.model_dump(mode="json") for quote in quotes]
            for key, quotes in candidate.citations.items()
        },
    }


def normalize_detection(result: DetectionResult) -> dict[str, Any]:
    """把生产多候选输出归一成 Case 级决策，同时完整保留候选供人工复核。

    event 优先于 uncertain，uncertain 优先于 no_event。只有一个 event 候选时才把其类型和
    事实作为 Case 级字段；多个 event 候选保持 ``event_type=null``，避免任意挑选提高得分。
    """
    views = [_decision_view(candidate) for candidate in result.candidates]
    events = [candidate for candidate in views if candidate["decision"] == "event"]
    uncertain = [candidate for candidate in views if candidate["decision"] == "uncertain"]
    if events:
        decision = "event"
        selected = events[0] if len(events) == 1 else None
    elif uncertain:
        decision = "uncertain"
        selected = uncertain[0]
    else:
        decision = "no_event"
        selected = views[0]
    facts = (
        {key: selected[key] for key in (
            "event_type", "event_subject", "change_action", "change_items",
            "event_time", "effective_time", "affected_scope",
        )}
        if selected is not None else
        {"event_type": None, "event_subject": None, "change_action": None,
         "change_items": [], "event_time": None, "effective_time": None,
         "affected_scope": None}
    )
    return {
        "decision": decision,
        **facts,
        "candidate_count": len(views),
        "event_candidate_count": len(events),
        "candidates": views,
        "runtime": result.runtime.model_dump(mode="json") if result.runtime else None,
    }


def _has_event_facts(candidate: dict[str, Any]) -> bool:
    """检查非事件候选是否携带生产 Schema 禁止的确认事实。"""
    return any(candidate.get(key) is not None for key in (
        "event_type", "event_subject", "change_action", "event_time",
        "effective_time", "affected_scope",
    )) or bool(candidate.get("change_items")) or bool(candidate.get("citations"))


def classify_bad_case(gold_decision: str, actual_decision: str, type_correct: bool | None) -> str | None:
    """按冻结规则为 Decision mismatch 或 Event Type mismatch 生成稳定分类。"""
    mapping = {
        ("event", "no_event"): "event_missed_as_no_event",
        ("event", "uncertain"): "event_missed_as_uncertain",
        ("no_event", "event"): "no_event_false_positive_event",
        ("no_event", "uncertain"): "no_event_predicted_uncertain",
        ("uncertain", "event"): "uncertain_overclaimed_as_event",
        ("uncertain", "no_event"): "uncertain_collapsed_to_no_event",
    }
    if gold_decision != actual_decision:
        return mapping[(gold_decision, actual_decision)]
    if gold_decision == "event" and type_correct is False:
        return "event_type_mismatch"
    return None


def score_success_case(record: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    """比较一次合法生产输出，生成自动指标、硬约束检查和人工字段复核材料。"""
    gold = record["gold"]
    decision_correct = actual["decision"] == gold["decision"]
    strict_type_correct = None
    conditional_type_correct = None
    if gold["decision"] == "event":
        strict_type_correct = (
            actual["decision"] == "event" and actual["event_type"] == gold["event_type"]
        )
        if actual["decision"] == "event":
            conditional_type_correct = actual["event_type"] == gold["event_type"]

    event_candidates = [
        candidate for candidate in actual["candidates"] if candidate["decision"] == "event"
    ]
    unsupported_event_time = (
        gold["event_time"] is None
        and any(candidate.get("event_time") is not None for candidate in event_candidates)
    )
    supports_previous = (
        record["input"].get("previous_state") is not None
        or any(item.get("previous_state") is not None for item in gold["change_items"])
    )
    unsupported_previous = (
        not supports_previous
        and any(
            item.get("previous_state") is not None
            for candidate in event_candidates for item in candidate["change_items"]
        )
    )
    no_event_leakage = any(
        candidate["decision"] == "no_event" and _has_event_facts(candidate)
        for candidate in actual["candidates"]
    )
    uncertain_violation = any(
        candidate["decision"] == "uncertain" and _has_event_facts(candidate)
        for candidate in actual["candidates"]
    )
    missing_current_state = any(
        item.get("current_state") is None
        for candidate in event_candidates for item in candidate["change_items"]
    )
    unsupported_scope_review = (
        gold["affected_scope"] is None
        and any(candidate.get("affected_scope") is not None for candidate in event_candidates)
    )
    violations = [
        name for name, failed in (
            ("unsupported_event_time", unsupported_event_time),
            ("unsupported_previous_state", unsupported_previous),
            ("no_event_field_leakage", no_event_leakage),
            ("uncertain_field_discipline", uncertain_violation),
            ("missing_current_state", missing_current_state),
        ) if failed
    ]
    checks = {
        "unsupported_event_time": unsupported_event_time,
        "unsupported_previous_state": unsupported_previous,
        "no_event_field_leakage": no_event_leakage,
        "uncertain_field_discipline_violation": uncertain_violation,
        "missing_current_state": missing_current_state,
        "unsupported_scope_review": unsupported_scope_review,
        "event_time_exact_match": (
            actual["event_time"] == gold["event_time"]
            if gold["event_time"] is not None else None
        ),
        "supported_event_time_abstention": (
            not unsupported_event_time if gold["event_time"] is None else None
        ),
        "hard_constraint_violations": violations,
    }
    bad_category = classify_bad_case(gold["decision"], actual["decision"], strict_type_correct)
    review_reasons = []
    if gold["decision"] == "event" or actual["decision"] == "event":
        review_reasons.append("semantic_field_review")
    if actual["event_candidate_count"] != 1 and actual["decision"] == "event":
        review_reasons.append("multiple_event_candidates")
    if bad_category == "event_type_mismatch":
        review_reasons.append("event_type_mismatch")
    review_reasons.extend(violations)
    if unsupported_scope_review:
        review_reasons.append("unsupported_scope_review")
    return {
        "case_id": record["metadata"]["case_id"],
        "path": record["metadata"]["path"],
        "execution_status": "SUCCESS",
        "gold": gold,
        "actual": actual,
        "checks": checks,
        "decision_correct": decision_correct,
        "event_type_correct": strict_type_correct,
        "conditional_event_type_correct": conditional_type_correct,
        "bad_case_category": bad_category,
        "field_review_reasons": list(dict.fromkeys(review_reasons)),
    }


def _non_success_case(
    record: dict[str, Any], status: str, error_type: str,
    leakage: dict[str, Any], *, model_calls: int,
    failed_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造不进入分数分母的 BLOCKED / ERROR；不保存底层异常正文。"""
    value = {
        "case_id": record["metadata"]["case_id"],
        "path": record["metadata"]["path"],
        "execution_status": status,
        "gold": record["gold"],
        "actual": None,
        "checks": {"leakage_guard": leakage},
        "decision_correct": None,
        "event_type_correct": None,
        "conditional_event_type_correct": None,
        "bad_case_category": None,
        "field_review_reasons": [],
        "error_type": error_type,
        "model_calls": model_calls,
    }
    if failed_decision is not None:
        value["failed_decision"] = failed_decision
    return value


def execute_case(
    plan_item: dict[str, Any],
    detector: Callable[[DetectionInput], DetectionResult] = detect_event,
) -> dict[str, Any]:
    """调用一次真实 Detector；预测错误正常评分，外部失败与 Runner 错误分开记录。"""
    record = plan_item["record"]
    leakage = plan_item["leakage"]
    try:
        result = detector(plan_item["model_input"])
        result = DetectionResult.model_validate(result.model_dump())
        scored = score_success_case(record, normalize_detection(result))
        scored["checks"]["leakage_guard"] = leakage
        scored["model_calls"] = 1
        return scored
    except (LLMConfigurationError, LLMCallError) as exc:
        return _non_success_case(record, "BLOCKED", type(exc).__name__, leakage, model_calls=1)
    except EvidenceValidationError as exc:
        failed = getattr(exc, "failed_decision", None)
        return _non_success_case(
            record, "ERROR", type(exc).__name__, leakage, model_calls=1,
            failed_decision=failed if isinstance(failed, dict) else None,
        )
    except (LLMStructuredOutputError, ValidationError, ValueError, TypeError) as exc:
        return _non_success_case(record, "ERROR", type(exc).__name__, leakage, model_calls=1)
    except Exception as exc:  # pragma: no cover - 防止供应商异常正文进入输出的最后边界
        return _non_success_case(record, "ERROR", type(exc).__name__, leakage, model_calls=1)


def execute_plan(
    plan: list[dict[str, Any]],
    detector: Callable[[DetectionInput], DetectionResult] = detect_event,
) -> list[dict[str, Any]]:
    """按固定顺序每 Case 最多调用一次；Provider 阻断后停止继续请求并标记余下 Case。"""
    results: list[dict[str, Any]] = []
    provider_block: str | None = None
    for item in plan:
        if provider_block is not None:
            results.append(_non_success_case(
                item["record"], "BLOCKED", "UpstreamProviderUnavailable",
                item["leakage"], model_calls=0,
            ))
            continue
        result = execute_case(item, detector)
        results.append(result)
        if result["execution_status"] == "BLOCKED":
            provider_block = result["case_id"]
    return results


def _ratio(matched: int, total: int) -> dict[str, Any]:
    return {"matched": matched, "total": total, "rate": matched / total if total else None}


def _per_class(cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """由三分类混淆结果计算每类 Precision / Recall / F1。"""
    output = {}
    for label in LABELS:
        tp = sum(c["gold"]["decision"] == label and c["actual"]["decision"] == label for c in cases)
        predicted = sum(c["actual"]["decision"] == label for c in cases)
        support = sum(c["gold"]["decision"] == label for c in cases)
        precision = tp / predicted if predicted else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        output[label] = {
            "support": support, "predicted": predicted, "true_positive": tp,
            "precision": precision, "recall": recall, "f1": f1,
        }
    return output


def _decision_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    correct = sum(bool(case["decision_correct"]) for case in cases)
    per_class = _per_class(cases)
    return {
        "accuracy": _ratio(correct, len(cases)),
        "per_class": per_class,
        "macro_precision": sum(value["precision"] for value in per_class.values()) / len(LABELS),
        "macro_recall": sum(value["recall"] for value in per_class.values()) / len(LABELS),
        "macro_f1": sum(value["f1"] for value in per_class.values()) / len(LABELS),
    }


def _confusion_matrix(cases: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    matrix = {gold: {actual: 0 for actual in LABELS} for gold in LABELS}
    for case in cases:
        matrix[case["gold"]["decision"]][case["actual"]["decision"]] += 1
    return matrix


def _event_binary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(c["gold"]["decision"] == "event" and c["actual"]["decision"] == "event" for c in cases)
    fp = sum(c["gold"]["decision"] != "event" and c["actual"]["decision"] == "event" for c in cases)
    fn = sum(c["gold"]["decision"] == "event" and c["actual"]["decision"] != "event" for c in cases)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"true_positive": tp, "false_positive": fp, "false_negative": fn,
            "precision": precision, "recall": recall, "f1": f1}


def _path_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    output = {}
    for path in ("state_comparison", "explicit_statement"):
        selected = [case for case in cases if case["path"] == path]
        output[path] = {
            "evaluated_cases": len(selected),
            **_decision_metrics(selected),
        }
    return output


def _event_type_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    gold_events = [case for case in cases if case["gold"]["decision"] == "event"]
    predicted_events = [case for case in gold_events if case["actual"]["decision"] == "event"]
    strict_correct = sum(bool(case["event_type_correct"]) for case in gold_events)
    conditional_correct = sum(bool(case["conditional_event_type_correct"]) for case in predicted_events)
    per_class = {}
    for event_type in sorted(EVENT_TYPES):
        selected = [case for case in gold_events if case["gold"]["event_type"] == event_type]
        per_class[event_type] = {
            "gold_count": len(selected),
            "correct_count": sum(bool(case["event_type_correct"]) for case in selected),
        }
    return {
        "strict_accuracy": _ratio(strict_correct, len(gold_events)),
        "conditional_accuracy": _ratio(conditional_correct, len(predicted_events)),
        "per_class": per_class,
        "single_sample_warning": (
            "单样本类型只能用于发现问题，不能证明稳定泛化能力。"
        ),
    }


def _event_time_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    supported = [case for case in cases if case["gold"]["event_time"] is not None]
    unsupported = [case for case in cases if case["gold"]["event_time"] is None]
    return {
        "exact_match": _ratio(
            sum(case["checks"]["event_time_exact_match"] is True for case in supported),
            len(supported),
        ),
        "supported_abstention": _ratio(
            sum(case["checks"]["supported_event_time_abstention"] is True for case in unsupported),
            len(unsupported),
        ),
        "unsupported_event_time": sum(case["checks"]["unsupported_event_time"] for case in unsupported),
    }


def build_field_review_queue(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """输出自然语言字段的人工语义复核队列，不使用脆弱字符串完全匹配。"""
    queue = []
    for case in cases:
        if case["execution_status"] != "SUCCESS" or not case["field_review_reasons"]:
            continue
        gold, actual = case["gold"], case["actual"]
        queue.append({
            "case_id": case["case_id"],
            "gold_decision": gold["decision"], "actual_decision": actual["decision"],
            "gold_event_type": gold["event_type"], "actual_event_type": actual["event_type"],
            "gold_event_subject": gold["event_subject"], "actual_event_subject": actual["event_subject"],
            "gold_change_action": gold["change_action"], "actual_change_action": actual["change_action"],
            "gold_change_items": gold["change_items"], "actual_change_items": actual["change_items"],
            "gold_event_time": gold["event_time"], "actual_event_time": actual["event_time"],
            "gold_affected_scope": gold["affected_scope"], "actual_affected_scope": actual["affected_scope"],
            "review_reason": case["field_review_reasons"],
        })
    return queue


def summarize_results(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """只用 SUCCESS Case 计算质量指标，BLOCKED / ERROR 独立计数且不进入分母。"""
    success = [case for case in cases if case["execution_status"] == "SUCCESS"]
    blocked = [case for case in cases if case["execution_status"] == "BLOCKED"]
    errors = [case for case in cases if case["execution_status"] == "ERROR"]
    violations = [
        {"case_id": case["case_id"], "violation": violation}
        for case in success for violation in case["checks"]["hard_constraint_violations"]
    ]
    return {
        "total_cases": len(cases), "evaluated_cases": len(success),
        "blocked_cases": len(blocked), "error_cases": len(errors),
        "model_calls": sum(case.get("model_calls", 0) for case in cases),
        "decision": _decision_metrics(success),
        "confusion_matrix": _confusion_matrix(success),
        "event_binary": _event_binary(success),
        "path_metrics": _path_metrics(success),
        "event_type": _event_type_metrics(success),
        "event_time": _event_time_metrics(success),
        "hard_constraints": {
            "violation_count": len(violations), "violations": violations,
            "unsupported_scope_review_count": sum(
                case["checks"]["unsupported_scope_review"] for case in success
            ),
        },
    }


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _implementation_identifier() -> str:
    """对生产 Detector、Schema、Evidence 与 Prompt 文件哈希，定位本次真实实现版本。"""
    paths = [
        REPOSITORY_ROOT / "event_core" / name
        for name in ("detector.py", "schemas.py", "evidence.py", "prompts.py")
    ]
    value = "\n".join(f"{path.name}:{_hash_file(path)}" for path in paths)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _code_head() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def runtime_metadata() -> dict[str, Any]:
    """读取 Reasoning Role 的非敏感配置；不输出 Key、地址或 .env 内容。"""
    load_dotenv(REPOSITORY_ROOT / ".env", override=False)
    config = load_model_config("reasoning")
    return {"role": config.role, "provider": config.provider, "model": config.model}


def assert_no_secret_fields(value: Any) -> None:
    """写盘前拒绝常见认证字段，允许无敏感值的 token_usage 运行统计。"""
    leaked = sorted(_walk_keys(value) & SECRET_KEYS)
    if leaked:
        raise ValueError("Evaluation output contains secret fields: " + ", ".join(leaked))


def build_result(
    *, run_id: str, started_at: str, dry_run: bool,
    all_records: list[dict[str, Any]], plan: list[dict[str, Any]],
    cases: list[dict[str, Any]], runtime: dict[str, Any] | None,
    config_status: str,
) -> dict[str, Any]:
    """构造机器可读结果，Gold 只在模型执行结束后的评测输出中出现。"""
    summary = summarize_results(cases) if not dry_run else {
        "total_cases": len(plan), "evaluated_cases": 0, "blocked_cases": 0,
        "error_cases": 0, "model_calls": 0,
    }
    bad_cases = [
        {"case_id": case["case_id"], "path": case["path"],
         "category": case["bad_case_category"], "gold": case["gold"],
         "actual": case["actual"]}
        for case in cases if case.get("bad_case_category")
    ]
    field_queue = build_field_review_queue(cases)
    result = {
        "evaluation_type": "development_regression",
        "gold_freeze_commit": GOLD_FREEZE_COMMIT,
        "runner_commit": None,
        "code_head_at_run": _code_head(),
        "run_id": run_id,
        "run_started_at": started_at,
        "run_finished_at": _utc_now(),
        "dry_run": dry_run,
        "provider": runtime.get("provider") if runtime else None,
        "model": runtime.get("model") if runtime else None,
        "model_role": runtime.get("role") if runtime else "reasoning",
        "model_configuration_status": config_status,
        "prompt_identifier": PROMPT_IDENTIFIER,
        "prompt_sha256": hashlib.sha256(EVENT_DETECTION_PROMPT.encode("utf-8")).hexdigest(),
        "production_detector_identifier": _implementation_identifier(),
        "total_cases": 35,
        "dataset": dataset_statistics(all_records),
        "selected_plan": {
            "total_cases": len(plan),
            "case_ids": [item["record"]["metadata"]["case_id"] for item in plan],
            "gold_leakage_check": "PASS",
            "model_input_hashes": {
                item["record"]["metadata"]["case_id"]: item["leakage"]["model_input_sha256"]
                for item in plan
            },
        },
        "execution_summary": summary,
        "cases": cases,
        "bad_case_candidates": bad_cases,
        "field_review_queue": field_queue,
        "blocked_case_ids": [case["case_id"] for case in cases if case["execution_status"] == "BLOCKED"],
        "error_case_ids": [case["case_id"] for case in cases if case["execution_status"] == "ERROR"],
        "limitations": [
            "旧 35 条已用于开发；新运行仅为开发回归，不能解释为独立质量或 PIA 总体 AI 准确率。",
            "单样本事件类型只用于发现问题，不能证明稳定泛化能力。",
            "event_subject、change_action、change_items 与 affected_scope 的语义等价性需要人工复核。",
        ],
    }
    assert_no_secret_fields(result)
    return result


def _rate_text(value: dict[str, Any] | None) -> str:
    if not value:
        return "N/A"
    rate = value.get("rate")
    return f"{value['matched']}/{value['total']} ({rate:.2%})" if rate is not None else f"{value['matched']}/{value['total']} (N/A)"


def render_report(result: dict[str, Any]) -> str:
    """生成固定结构的审计报告，不夸大 35 条 MVP Holdout 的证据强度。"""
    summary = result["execution_summary"]
    decision = summary.get("decision", {})
    binary = summary.get("event_binary", {})
    event_type = summary.get("event_type", {})
    matrix = summary.get("confusion_matrix", {g: {a: 0 for a in LABELS} for g in LABELS})
    lines = [
        "# PIA Event Detection Independent Evaluation", "",
        "## Run Information", "",
        f"- Run ID: {result['run_id']}",
        f"- Gold Freeze Commit: {result['gold_freeze_commit']}",
        f"- Code HEAD: {result['code_head_at_run']}",
        f"- Dry Run: {result['dry_run']}",
        f"- Provider / Model: {result['provider']} / {result['model']}",
        f"- Prompt SHA256: {result['prompt_sha256']}", "",
        "## Dataset", "",
        f"- Total: {result['dataset']['total_cases']}",
        f"- Decision Distribution: {result['dataset']['decision_distribution']}",
        f"- Event Type Coverage: {result['dataset']['event_type_coverage']}/8", "",
        "## Execution Summary", "",
        f"- Selected: {result['selected_plan']['total_cases']}",
        f"- SUCCESS: {summary.get('evaluated_cases', 0)}",
        f"- BLOCKED: {summary.get('blocked_cases', 0)}",
        f"- ERROR: {summary.get('error_cases', 0)}",
        f"- Model Calls: {summary.get('model_calls', 0)}", "",
        "## Decision Metrics", "",
        f"- Accuracy: {_rate_text(decision.get('accuracy'))}",
        f"- Macro Precision: {decision.get('macro_precision', 'N/A')}",
        f"- Macro Recall: {decision.get('macro_recall', 'N/A')}",
        f"- Macro F1: {decision.get('macro_f1', 'N/A')}", "",
        "## Confusion Matrix", "",
        "| Gold \\ Actual | event | no_event | uncertain |",
        "|---|---:|---:|---:|",
        *[f"| {gold} | {matrix[gold]['event']} | {matrix[gold]['no_event']} | {matrix[gold]['uncertain']} |" for gold in LABELS],
        "", "## Per-Class Metrics", "",
    ]
    for label, values in decision.get("per_class", {}).items():
        lines.append(
            f"- {label}: P={values['precision']:.4f}, R={values['recall']:.4f}, F1={values['f1']:.4f}, support={values['support']}"
        )
    lines.extend(["", "## Path A / Path B", ""])
    for path, values in summary.get("path_metrics", {}).items():
        lines.append(f"- {path}: accuracy={_rate_text(values['accuracy'])}, macro_f1={values['macro_f1']:.4f}")
    lines.extend([
        "", "## Event Binary Metrics", "",
        f"- Precision: {binary.get('precision', 'N/A')}",
        f"- Recall: {binary.get('recall', 'N/A')}",
        f"- F1: {binary.get('f1', 'N/A')}", "",
        "## Event Type Metrics", "",
        f"- Strict Accuracy: {_rate_text(event_type.get('strict_accuracy'))}",
        f"- Conditional Accuracy: {_rate_text(event_type.get('conditional_accuracy'))}",
    ])
    for event_type_name, values in event_type.get("per_class", {}).items():
        lines.append(
            f"- {event_type_name}: {values['correct_count']}/{values['gold_count']}"
        )
    hard_constraints = summary.get("hard_constraints", {})
    hard_counts = Counter(
        item["violation"] for item in hard_constraints.get("violations", [])
    )
    lines.extend([
        f"- Limitation: {event_type.get('single_sample_warning', 'N/A')}", "",
        "## Hard Constraint Checks", "",
        f"- Violations: {hard_constraints.get('violation_count', 0)}",
        f"- unsupported_event_time: {hard_counts['unsupported_event_time']}",
        f"- unsupported_previous_state: {hard_counts['unsupported_previous_state']}",
        f"- no_event_field_leakage: {hard_counts['no_event_field_leakage']}",
        f"- uncertain_field_discipline: {hard_counts['uncertain_field_discipline']}",
        f"- missing_current_state: {hard_counts['missing_current_state']}",
        f"- unsupported_scope_review: {hard_constraints.get('unsupported_scope_review_count', 0)}",
        f"- Event Time: {summary.get('event_time', {})}", "",
        "## Bad Case Candidates", "",
        *([f"- {item['case_id']}: {item['category']}" for item in result["bad_case_candidates"]] or ["无。"]),
        "", "## Field Review Queue", "",
        *([f"- {item['case_id']}: {', '.join(item['review_reason'])}" for item in result["field_review_queue"]] or ["无。"]),
        "", "## Blocked Cases", "",
        *([f"- {case_id}" for case_id in result["blocked_case_ids"]] or ["无。"]),
        "", "## Errors", "",
        *([f"- {case_id}" for case_id in result["error_case_ids"]] or ["无。"]),
        "", "## Limitations", "",
        *[f"- {item}" for item in result["limitations"]],
        "", "## Interpretation", "",
        "结果用途由 evaluation_type 标识；旧 35 条首次运行后已用于开发，后续分数不再独立。",
        "不能据此声称 PIA 总体 AI 准确率、生产级准确率或充分证明模型泛化能力。", "",
    ])
    return "\n".join(lines)


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def write_outputs(result: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    """把动态结果写入 tmp；拒绝覆盖旧 Run，并在写盘前执行 Secret 字段检查。"""
    assert_no_secret_fields(result)
    output_dir.mkdir(parents=True, exist_ok=False)
    paths = {
        "results": output_dir / "results.json",
        "report": output_dir / "report.md",
        "bad_cases": output_dir / "bad_case_candidates.jsonl",
        "field_review": output_dir / "field_review_queue.jsonl",
    }
    paths["results"].write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    paths["report"].write_text(render_report(result), encoding="utf-8")
    _write_jsonl(paths["bad_cases"], result["bad_case_candidates"])
    _write_jsonl(paths["field_review"], result["field_review_queue"])
    return paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run PIA Event Detection Independent Evaluation")
    parser.add_argument("--case", dest="case_id")
    parser.add_argument("--path", choices=("state_comparison", "explicit_statement"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """先完成全量 Leakage Guard，再执行 Baseline #1 并生成动态报告。"""
    args = parse_args(argv)
    run_id, started_at = _run_id(), _utc_now()
    try:
        all_records = load_dataset(verify_freeze=True)
        selected = filter_cases(all_records, case_id=args.case_id, path=args.path)
        plan = build_execution_plan(selected)
        runtime = None
        config_status = "READY"
        try:
            runtime = runtime_metadata()
        except LLMConfigurationError:
            config_status = "BLOCKED"

        if args.dry_run:
            cases: list[dict[str, Any]] = []
        elif config_status == "BLOCKED":
            cases = [
                _non_success_case(
                    item["record"], "BLOCKED", "LLMConfigurationError",
                    item["leakage"], model_calls=0,
                )
                for item in plan
            ]
        else:
            cases = execute_plan(plan)
        result = build_result(
            run_id=run_id, started_at=started_at, dry_run=args.dry_run,
            all_records=all_records, plan=plan, cases=cases,
            runtime=runtime, config_status=config_status,
        )
        output_dir = args.output or (DEFAULT_OUTPUT_ROOT / run_id)
        paths = write_outputs(result, output_dir)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Event Detection Independent Eval ERROR: {type(exc).__name__}")
        return 2

    summary = result["execution_summary"]
    print(f"Event Detection Independent Eval output: {output_dir}")
    print(f"Results: {paths['results'].name}; report: {paths['report'].name}")
    print(
        f"Cases: selected={len(plan)} evaluated={summary.get('evaluated_cases', 0)} "
        f"blocked={summary.get('blocked_cases', 0)} error={summary.get('error_cases', 0)}"
    )
    print(
        f"Gold Leakage Check: PASS; Model Calls: {summary.get('model_calls', 0)}; "
        f"Event Type Coverage: {result['dataset']['event_type_coverage']}/8"
    )
    if args.dry_run:
        return 0
    return 1 if summary.get("blocked_cases", 0) or summary.get("error_cases", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
