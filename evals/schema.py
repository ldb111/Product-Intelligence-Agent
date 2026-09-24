"""Legacy Regression Gold 的最小 JSONL 校验。

本模块只检查数据文件能否安全、稳定地被后续 Regression Runner 读取。它不调用模型、
不访问网页、不计算评分，也不把旧 Gold Set 声称为独立质量评测。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal


EvaluationKind = Literal["source_discovery", "stage1"]
REQUIRED_METADATA_FIELDS = {
    "legacy_case_id",
    "legacy_version",
    "verified_at",
    "evaluation_use",
}
SOURCE_FIELDS = {
    "url",
    "title",
    "source_type",
    "legacy_importance",
    "monitoring_reason",
}
SOURCE_IMPORTANCE = {"core", "supplementary"}
FORBIDDEN_STAGE_EXPECTED_FIELDS = {
    "state_matching_expected",
    "contextual_diff_expected",
    "contextual_state_diff_expected",
}


class LegacyGoldValidationError(ValueError):
    """表示 Legacy Gold 数据无法满足最小、确定性的读取契约。"""


def _require_object(value: Any, location: str) -> dict[str, Any]:
    """确认一个 JSON 值是对象，返回便于后续字段检查的字典。"""
    if not isinstance(value, dict):
        raise LegacyGoldValidationError(f"{location} must be an object")
    return value


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """逐行读取 JSONL，并拒绝空行、非法 JSON 和非对象记录。

    输入是一个 JSONL 文件路径；每个非空物理行必须恰好是一条 JSON 对象记录。输出按
    文件顺序返回记录列表，方便测试和未来 Runner 保留 Legacy Case 的稳定顺序。
    """
    source = Path(path)
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise LegacyGoldValidationError(
                f"{source}:{line_number} must not be empty"
            )
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LegacyGoldValidationError(
                f"{source}:{line_number} is not valid JSON"
            ) from exc
        records.append(_require_object(value, f"{source}:{line_number}"))
    return records


def _validate_metadata(record: dict[str, Any], index: int) -> str:
    """校验所有 Legacy 记录共同的版本、身份和用途边界。"""
    metadata = _require_object(record.get("metadata"), f"record {index}.metadata")
    missing = sorted(REQUIRED_METADATA_FIELDS - metadata.keys())
    if missing:
        raise LegacyGoldValidationError(
            f"record {index}.metadata missing: {', '.join(missing)}"
        )
    case_id = metadata["legacy_case_id"]
    if not isinstance(case_id, str) or not case_id.strip():
        raise LegacyGoldValidationError(
            f"record {index}.metadata.legacy_case_id must be a non-empty string"
        )
    if metadata["evaluation_use"] != "regression_only":
        raise LegacyGoldValidationError(
            f"record {index}.metadata.evaluation_use must be regression_only"
        )
    return case_id


def _validate_source_discovery(record: dict[str, Any], index: int) -> None:
    """检查来源发现金标的输入和人工来源列表结构。"""
    input_data = _require_object(record.get("input"), f"record {index}.input")
    if not isinstance(input_data.get("competitor_name"), str) or not input_data[
        "competitor_name"
    ].strip():
        raise LegacyGoldValidationError(
            f"record {index}.input.competitor_name must be a non-empty string"
        )
    expected = _require_object(record.get("expected"), f"record {index}.expected")
    sources = expected.get("sources")
    if not isinstance(sources, list):
        raise LegacyGoldValidationError(
            f"record {index}.expected.sources must be a list"
        )
    for source_index, source_value in enumerate(sources):
        source = _require_object(
            source_value, f"record {index}.expected.sources[{source_index}]"
        )
        missing = sorted(SOURCE_FIELDS - source.keys())
        if missing:
            raise LegacyGoldValidationError(
                f"record {index}.expected.sources[{source_index}] missing: "
                + ", ".join(missing)
            )
        if source["legacy_importance"] not in SOURCE_IMPORTANCE:
            raise LegacyGoldValidationError(
                f"record {index}.expected.sources[{source_index}] has invalid "
                "legacy_importance"
            )
        for field in SOURCE_FIELDS - {"legacy_importance"}:
            if not isinstance(source[field], str) or not source[field].strip():
                raise LegacyGoldValidationError(
                    f"record {index}.expected.sources[{source_index}].{field} "
                    "must be a non-empty string"
                )


def _contains_forbidden_stage_field(value: Any) -> str | None:
    """递归寻找旧 Gold 明确没有提供的历史比较答案字段。"""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_STAGE_EXPECTED_FIELDS:
                return key
            found = _contains_forbidden_stage_field(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _contains_forbidden_stage_field(child)
            if found:
                return found
    return None


def _validate_stage1(record: dict[str, Any], index: int) -> None:
    """检查 Stage 1 页面输入，并禁止迁移中补造历史变化答案。"""
    input_data = _require_object(record.get("input"), f"record {index}.input")
    if not isinstance(input_data.get("url"), str) or not input_data["url"].strip():
        raise LegacyGoldValidationError(
            f"record {index}.input.url must be a non-empty string"
        )
    expected = _require_object(record.get("expected"), f"record {index}.expected")
    forbidden = _contains_forbidden_stage_field(expected)
    if forbidden:
        raise LegacyGoldValidationError(
            f"record {index}.expected contains forbidden field: {forbidden}"
        )


def validate_records(
    records: list[dict[str, Any]], kind: EvaluationKind
) -> list[dict[str, Any]]:
    """校验一组记录并保证 legacy_case_id 在文件内唯一。

    输入是已解析记录与资产类型；处理共同 metadata 后调用对应的小型结构检查；输出原
    记录列表，便于调用方在校验通过后继续读取，不额外转换或修正人工标准答案。
    """
    seen: set[str] = set()
    for index, record in enumerate(records, 1):
        _require_object(record, f"record {index}")
        case_id = _validate_metadata(record, index)
        if case_id in seen:
            raise LegacyGoldValidationError(f"duplicate legacy_case_id: {case_id}")
        seen.add(case_id)
        if kind == "source_discovery":
            _validate_source_discovery(record, index)
        elif kind == "stage1":
            _validate_stage1(record, index)
        else:
            raise LegacyGoldValidationError(f"unsupported evaluation kind: {kind}")
    return records


def load_and_validate(
    path: str | Path, kind: EvaluationKind
) -> list[dict[str, Any]]:
    """读取并校验一个 Legacy Regression JSONL 文件。"""
    return validate_records(read_jsonl(path), kind)

