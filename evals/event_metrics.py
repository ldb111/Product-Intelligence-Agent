"""闭环评测的确定性计分：执行、校验后质量、端到端结果及时间证据资格。

本模块只读取保存的输入/输出，不调用模型、不修改标签。历史独立输出可离线重算，
并与之后的开发回归和新独立留出结果明确区分。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from evals.runners import event_detection_independent as base


MONTHS = {name.casefold(): index for index, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1)}


def dates_in_text(text: str | None) -> set[str]:
    """从文字提取完整年月日，校验真实日历后返回 ISO 日期集合。

    输入可为中文、ISO 或英文月份日期。仅转换明确完整日期，不猜年份、地区日/月顺序，
    不把版本号、观察日期或月份粒度补成事件日期。多日期文本保留全部候选。
    """
    if not text:
        return set()
    parts = [tuple(map(int, match)) for match in re.findall(
        r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?(?!\d)", text)]
    for month, day, year in re.findall(
        r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
        text, flags=re.IGNORECASE,
    ):
        parts.append((int(year), MONTHS[month.casefold()], int(day)))
    normalized = set()
    for year, month, day in parts:
        try:
            normalized.add(date(year, month, day).isoformat())
        except ValueError:
            continue
    return normalized


def event_time_check(record: dict, actual: dict | None) -> dict:
    """只把实际模型正文含 Gold 日期的事件纳入抽取指标。

    Gold 与实际正文日期的交集只是确定性资格门槛；日期是否代表事件发生时间仍依赖
    冻结标注语义。source metadata / observed_at 不作为可引用正文。输出保留原始值，
    标准化只用于评测，绝不改写生产事实。
    """
    data = base.adapt_case_to_detection_input(record)
    body = "\n".join(filter(None, (data.previous_state, data.current_state, data.relevant_source_context)))
    expected = dates_in_text(record["gold"]["event_time"])
    present = dates_in_text(body)
    eligible = record["gold"]["decision"] == "event" and len(expected) == 1 and expected <= present
    predicted = dates_in_text(actual.get("event_time")) if actual else set()
    return {
        "eligible": eligible,
        "reason": "date_supported_in_model_evidence" if eligible else
                  "gold_time_unknown" if not record["gold"]["event_time"] else
                  "gold_date_absent_or_ambiguous_in_model_evidence",
        "gold_normalized": sorted(expected), "actual_normalized": sorted(predicted),
        "normalized_match": eligible and actual is not None and predicted == expected,
        "raw_exact_match": eligible and actual is not None and actual.get("event_time") == record["gold"]["event_time"],
    }


def summarize(cases: list[dict], records: list[dict]) -> dict[str, Any]:
    """补足全链路分母与诊断覆盖率，避免剔除运行失败后显得质量过高。

    SUCCESS 才进入正式预测质量；端到端分母包含所有选中样本；错误前保存的完整语义
    决策只进入 diagnostic 指标，永不成为正式 Event。旧 Baseline 的单个失败候选不是
    完整候选集，因此不能假装可恢复其整组 Raw Decision。
    """
    summary = base.summarize_results(cases)
    by_id = {r["metadata"]["case_id"]: r for r in records}
    success = [c for c in cases if c["execution_status"] == "SUCCESS"]
    summary["execution_success_rate"] = base._ratio(len(success), len(cases))
    summary["validated_decision_accuracy"] = summary["decision"]["accuracy"]
    summary["end_to_end_correct_rate"] = base._ratio(
        sum(bool(c.get("decision_correct")) for c in success), len(cases))
    raw = []
    for c in cases:
        if c["actual"] is not None:
            raw.append((c, c["actual"]["decision"]))
        elif c.get("raw_decisions", {}).get("candidates"):
            decisions = {candidate["decision"] for candidate in c["raw_decisions"]["candidates"]}
            prediction = "event" if "event" in decisions else "uncertain" if "uncertain" in decisions else "no_event"
            raw.append((c, prediction))
    summary["raw_semantic_diagnostic"] = {
        "agreement": base._ratio(sum(c["gold"]["decision"] == p for c, p in raw), len(raw)),
        "coverage": base._ratio(len(raw), len(cases)), "diagnostic_only": True,
    }
    times = [(c, event_time_check(by_id[c["case_id"]], c["actual"])) for c in cases]
    eligible = [(c, t) for c, t in times if t["eligible"] and c["execution_status"] == "SUCCESS"]
    summary["event_time_eligible"] = {
        "accuracy": base._ratio(sum(t["normalized_match"] for _, t in eligible), len(eligible)),
        "eligible_case_ids": [c["case_id"] for c, t in times if t["eligible"]],
        "not_eligible_case_ids": [c["case_id"] for c, t in times if not t["eligible"]],
        "checks": {c["case_id"]: t for c, t in times},
    }
    # 同时公开完整数据的类型支持数，避免执行失败的类型从报告中悄然消失。
    summary["all_selected_gold_event_types"] = base.dataset_statistics(records)["event_type_distribution"]
    return summary
