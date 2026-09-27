"""Event Detection Fresh Gold 的轻量 JSONL 校验。

本模块只保护独立留出集的数据边界：候选来源必须可追溯，待标注记录不得夹带自动答案，
人工完成的答案必须符合 Event Core 的三类决策语义。它不调用 Event Detector、LLM 或生产
Pipeline，也不会补全、推断或改写任何人工标签。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PATHS = {"state_comparison", "explicit_statement"}
DECISIONS = {"event", "no_event", "uncertain"}
EVENT_TYPES = {
    "capability",
    "availability",
    "pricing",
    "plan_packaging",
    "access_permission",
    "commercialization",
    "deprecation",
    "positioning",
}
ANNOTATION_STATUSES = {"pending_human_review", "ready"}
ANNOTATION_METHODS = {"ai_assisted_human_review"}
SUPPLEMENT_COVERAGE = {
    "EDS-001": "pricing",
    "EDS-002": "plan_packaging",
    "EDS-003": "access_permission",
    "EDS-004": "commercialization",
    "EDS-005": "positioning",
}
GOLD_FIELDS = {
    "decision",
    "event_type",
    "event_subject",
    "change_action",
    "change_items",
    "event_time",
    "effective_time",
    "affected_scope",
    "annotation_notes",
}
EVENT_FACT_FIELDS = {
    "event_type",
    "event_subject",
    "change_action",
    "event_time",
    "effective_time",
    "affected_scope",
}
SOURCE_FIELDS = {"url", "title", "published_at", "excerpt", "retrieved_at"}


class EventDetectionGoldValidationError(ValueError):
    """表示 Fresh Event Detection 候选或人工 Gold 不满足数据契约。"""


def _require_object(value: Any, location: str) -> dict[str, Any]:
    """确认输入是 JSON 对象，并返回便于后续校验的字典。"""
    if not isinstance(value, dict):
        raise EventDetectionGoldValidationError(f"{location} must be an object")
    return value


def _require_non_empty_string(value: Any, location: str) -> str:
    """确认必填文本存在，避免空来源、空身份进入人工队列。"""
    if not isinstance(value, str) or not value.strip():
        raise EventDetectionGoldValidationError(
            f"{location} must be a non-empty string"
        )
    return value


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """逐行读取 JSONL，拒绝空行、非法 JSON 和非对象记录。

    输入是数据文件路径；每个物理行必须恰好是一条 JSON 对象。输出保持原有顺序，便于
    人工队列、后续评测和版本审计引用稳定的 Case 顺序。
    """
    source = Path(path)
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise EventDetectionGoldValidationError(
                f"{source}:{line_number} must not be empty"
            )
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EventDetectionGoldValidationError(
                f"{source}:{line_number} is not valid JSON"
            ) from exc
        records.append(_require_object(value, f"{source}:{line_number}"))
    return records


def _validate_source(value: Any, location: str) -> None:
    """校验一条短来源证据，确保人工标注可以回到官方原文复核。"""
    source = _require_object(value, location)
    missing = sorted(SOURCE_FIELDS - source.keys())
    if missing:
        raise EventDetectionGoldValidationError(
            f"{location} missing: {', '.join(missing)}"
        )
    url = _require_non_empty_string(source["url"], f"{location}.url")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise EventDetectionGoldValidationError(
            f"{location}.url must be an absolute HTTPS URL"
        )
    _require_non_empty_string(source["title"], f"{location}.title")
    _require_non_empty_string(source["published_at"], f"{location}.published_at")
    excerpt = _require_non_empty_string(source["excerpt"], f"{location}.excerpt")
    if len(excerpt) > 500:
        raise EventDetectionGoldValidationError(
            f"{location}.excerpt must be at most 500 characters"
        )
    _require_non_empty_string(source["retrieved_at"], f"{location}.retrieved_at")


def _validate_pending_gold(gold: dict[str, Any], index: int) -> None:
    """保证待人工标注 Case 没有被采集脚本或模型偷偷写入答案。"""
    for field in GOLD_FIELDS - {"change_items"}:
        if gold[field] is not None:
            raise EventDetectionGoldValidationError(
                f"record {index}.gold.{field} must be null while pending"
            )
    if gold["change_items"] != []:
        raise EventDetectionGoldValidationError(
            f"record {index}.gold.change_items must be empty while pending"
        )


def _validate_ready_gold(gold: dict[str, Any], index: int) -> None:
    """检查人工完成答案的三类决策边界，但不判断答案本身是否正确。"""
    decision = gold["decision"]
    if decision not in DECISIONS:
        raise EventDetectionGoldValidationError(
            f"record {index}.gold.decision is invalid"
        )
    event_type = gold["event_type"]
    if event_type is not None and event_type not in EVENT_TYPES:
        raise EventDetectionGoldValidationError(
            f"record {index}.gold.event_type is invalid"
        )
    if not isinstance(gold["change_items"], list):
        raise EventDetectionGoldValidationError(
            f"record {index}.gold.change_items must be a list"
        )
    for item_index, item_value in enumerate(gold["change_items"]):
        item = _require_object(
            item_value, f"record {index}.gold.change_items[{item_index}]"
        )
        _require_non_empty_string(
            item.get("item"), f"record {index}.gold.change_items[{item_index}].item"
        )
        for state_field in ("previous_state", "current_state"):
            if item.get(state_field) is not None:
                _require_non_empty_string(
                    item[state_field],
                    f"record {index}.gold.change_items[{item_index}].{state_field}",
                )
        if item.get("current_state") is None:
            raise EventDetectionGoldValidationError(
                f"record {index}.gold.change_items[{item_index}].current_state is required"
            )

    if decision == "event":
        if event_type is None:
            raise EventDetectionGoldValidationError(
                f"record {index}.gold.event_type is required for event"
            )
        for field in ("event_subject", "change_action"):
            _require_non_empty_string(gold[field], f"record {index}.gold.{field}")
        if not gold["change_items"]:
            raise EventDetectionGoldValidationError(
                f"record {index}.gold.change_items is required for event"
            )
        # 官方页面可能只说明变化而没有事件时间，因此 event_time 允许为 null。
        for field in ("event_time", "effective_time", "affected_scope"):
            if gold[field] is not None:
                _require_non_empty_string(gold[field], f"record {index}.gold.{field}")
    else:
        for field in EVENT_FACT_FIELDS:
            if gold[field] is not None:
                raise EventDetectionGoldValidationError(
                    f"record {index}.gold.{field} must be null for {decision}"
                )
        if gold["change_items"]:
            raise EventDetectionGoldValidationError(
                f"record {index}.gold.change_items must be empty for {decision}"
            )
        if decision == "uncertain":
            _require_non_empty_string(
                gold["annotation_notes"],
                f"record {index}.gold.annotation_notes",
            )


def _validate_path_input(
    path: str,
    input_data: dict[str, Any],
    provenance: dict[str, Any],
    index: int,
) -> None:
    """校验 Path A 与 Path B 的输入形态，防止用声明冒充历史状态。

    Path A 必须同时提供真实前后状态及各自来源；Path B 只提供官方明确声明，不允许为了
    补齐结构而虚构 previous/current state。这一约束直接保护本轮 Fresh Holdout 的真实性。
    """
    prefix = f"record {index}"
    _require_non_empty_string(input_data.get("competitor"), f"{prefix}.input.competitor")
    _require_non_empty_string(input_data.get("product_name"), f"{prefix}.input.product_name")
    if path == "state_comparison":
        previous = _require_object(input_data.get("previous_state"), f"{prefix}.input.previous_state")
        current = _require_object(input_data.get("current_state"), f"{prefix}.input.current_state")
        diff = _require_object(input_data.get("contextual_diff"), f"{prefix}.input.contextual_diff")
        for state_name, state in (("previous_state", previous), ("current_state", current)):
            _require_non_empty_string(state.get("version"), f"{prefix}.input.{state_name}.version")
            _require_non_empty_string(state.get("observed_at"), f"{prefix}.input.{state_name}.observed_at")
            _require_non_empty_string(state.get("content"), f"{prefix}.input.{state_name}.content")
        _require_non_empty_string(diff.get("comparison_scope"), f"{prefix}.input.contextual_diff.comparison_scope")
        previous_excerpt = _require_non_empty_string(
            diff.get("previous_excerpt"),
            f"{prefix}.input.contextual_diff.previous_excerpt",
        )
        current_excerpt = _require_non_empty_string(
            diff.get("current_excerpt"),
            f"{prefix}.input.contextual_diff.current_excerpt",
        )
        if previous_excerpt != previous["content"] or current_excerpt != current["content"]:
            raise EventDetectionGoldValidationError(
                f"{prefix}.input.contextual_diff excerpts must match the two source states"
            )
        if input_data.get("explicit_statement") is not None:
            raise EventDetectionGoldValidationError(
                f"{prefix}.input.explicit_statement must be null for state_comparison"
            )
        _validate_source(provenance.get("previous_source"), f"{prefix}.provenance.previous_source")
        _validate_source(provenance.get("current_source"), f"{prefix}.provenance.current_source")
    else:
        for field in ("previous_state", "current_state", "contextual_diff"):
            if input_data.get(field) is not None:
                raise EventDetectionGoldValidationError(
                    f"{prefix}.input.{field} must be null for explicit_statement"
                )
        statement = _require_object(input_data.get("explicit_statement"), f"{prefix}.input.explicit_statement")
        _require_non_empty_string(statement.get("observed_at"), f"{prefix}.input.explicit_statement.observed_at")
        _require_non_empty_string(statement.get("content"), f"{prefix}.input.explicit_statement.content")
        if provenance.get("previous_source") is not None:
            raise EventDetectionGoldValidationError(
                f"{prefix}.provenance.previous_source must be null for explicit_statement"
            )
        _validate_source(provenance.get("current_source"), f"{prefix}.provenance.current_source")


def validate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验整组 Fresh Event Detection 记录并保证 case_id 唯一。

    输入是已解析记录；处理身份、污染、路径、来源和 Gold 状态后返回原记录列表。函数只做
    确定性数据完整性检查，不给 Case 分配 event、no_event 或 uncertain。
    """
    seen: set[str] = set()
    for index, record_value in enumerate(records, 1):
        record = _require_object(record_value, f"record {index}")
        metadata = _require_object(record.get("metadata"), f"record {index}.metadata")
        case_id = _require_non_empty_string(metadata.get("case_id"), f"record {index}.metadata.case_id")
        if case_id in seen:
            raise EventDetectionGoldValidationError(f"duplicate case_id: {case_id}")
        seen.add(case_id)
        path = metadata.get("path")
        if path not in PATHS:
            raise EventDetectionGoldValidationError(f"record {index}.metadata.path is invalid")
        if metadata.get("evaluation_use") != "independent_holdout":
            raise EventDetectionGoldValidationError(
                f"record {index}.metadata.evaluation_use must be independent_holdout"
            )
        if metadata.get("contaminated") is not False:
            raise EventDetectionGoldValidationError(
                f"record {index}.metadata.contaminated must be false"
            )
        _require_non_empty_string(metadata.get("market"), f"record {index}.metadata.market")
        _require_non_empty_string(metadata.get("collected_at"), f"record {index}.metadata.collected_at")
        _require_non_empty_string(metadata.get("source_date"), f"record {index}.metadata.source_date")
        status = metadata.get("annotation_status")
        if status not in ANNOTATION_STATUSES:
            raise EventDetectionGoldValidationError(
                f"record {index}.metadata.annotation_status is invalid"
            )
        # 这是标注流程的来源记录，不是被测模型生成的答案或生产事件字段。
        method = metadata.get("annotation_method")
        if method is not None and method not in ANNOTATION_METHODS:
            raise EventDetectionGoldValidationError(
                f"record {index}.metadata.annotation_method is invalid"
            )

        input_data = _require_object(record.get("input"), f"record {index}.input")
        provenance = _require_object(record.get("provenance"), f"record {index}.provenance")
        _validate_path_input(path, input_data, provenance, index)

        gold = _require_object(record.get("gold"), f"record {index}.gold")
        missing_gold = sorted(GOLD_FIELDS - gold.keys())
        if missing_gold:
            raise EventDetectionGoldValidationError(
                f"record {index}.gold missing: {', '.join(missing_gold)}"
            )
        if status == "pending_human_review":
            if method is not None:
                raise EventDetectionGoldValidationError(
                    f"record {index}.metadata.annotation_method must be null while pending"
                )
            if metadata.get("annotated_by") is not None:
                raise EventDetectionGoldValidationError(
                    f"record {index}.metadata.annotated_by must be null while pending"
                )
            _validate_pending_gold(gold, index)
        else:
            _require_non_empty_string(metadata.get("annotated_by"), f"record {index}.metadata.annotated_by")
            _validate_ready_gold(gold, index)
    return records


def validate_supplement_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验本轮 5 条补充记录的数量、选材目标和评测前冻结边界。

    输入为补充 JSONL 记录；先复用来源和 Gold 生命周期校验，再检查固定 Case ID、选材目标
    与模型评测前创建标记。输出原记录。目标类型只解释选材原因，不会推断 Gold event_type；
    待标注与人工完成状态均由通用校验规则处理。
    """
    validate_records(records)
    if {record["metadata"]["case_id"] for record in records} != set(SUPPLEMENT_COVERAGE):
        raise EventDetectionGoldValidationError("supplement must contain exactly EDS-001 through EDS-005")
    for record in records:
        metadata = record["metadata"]
        if metadata.get("target_coverage_type") != SUPPLEMENT_COVERAGE[metadata["case_id"]]:
            raise EventDetectionGoldValidationError("supplement target_coverage_type is invalid")
        if metadata.get("created_before_model_evaluation") is not True:
            raise EventDetectionGoldValidationError("supplement must be created before model evaluation")
    return records


def load_and_validate(path: str | Path) -> list[dict[str, Any]]:
    """读取并校验一个 Fresh Event Detection JSONL 文件。"""
    return validate_records(read_jsonl(path))
