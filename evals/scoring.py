"""Legacy Regression 的确定性比较和汇总函数。

这里不访问网络、不调用模型，也不判断差异究竟来自网页漂移还是代码回归。所有旧 Gold
不一致都进入 REVIEW，留给人工复核。
"""

from __future__ import annotations

import re
import hashlib
from collections import Counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit


REVIEW_REASONS = {
    "live_content_mismatch",
    "url_changed",
    "title_changed",
    "new_source_detected",
    "expected_source_missing",
    "source_type_changed",
    "quality_status_changed",
    "acquisition_method_changed",
    "anchor_missing",
    "manual_structure_review",
    "unknown_mismatch",
}


def ratio(matched: int, total: int) -> dict[str, int | float | None]:
    """返回带明确分子、分母的指标；没有分母时不制造零分。"""
    return {
        "matched": matched,
        "total": total,
        "rate": matched / total if total else None,
    }


def _url_without_trailing_slash(url: str, host_aliases: set[str]) -> str:
    """只做旧 Gold 允许的最小 URL 比较，不删除查询参数或改变路径。"""
    parsed = urlsplit(url.strip())
    hostname = (parsed.hostname or "").casefold()
    if hostname in host_aliases:
        hostname = sorted(host_aliases)[0]
    netloc = hostname
    if parsed.port:
        netloc += f":{parsed.port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, parsed.query, parsed.fragment))


def _normalize_host(host: str, host_aliases: set[str]) -> str:
    """按 Gold 明示的 host aliases 归一化域名，不自行扩展等价规则。"""
    normalized = host.strip().casefold()
    if normalized in host_aliases:
        return sorted(host_aliases)[0]
    return normalized


def match_url(
    expected_url: str,
    actual_url: str,
    equivalence: dict[str, Any] | None = None,
) -> str:
    """返回 exact、equivalent 或 unmatched，保留人工审计所需的匹配级别。"""
    if expected_url.strip() == actual_url.strip():
        return "exact"
    aliases = {
        str(host).casefold()
        for host in (equivalence or {}).get("host_aliases", [])
        if isinstance(host, str) and host.strip()
    }
    if _url_without_trailing_slash(expected_url, aliases) == _url_without_trailing_slash(
        actual_url, aliases
    ):
        return "equivalent"
    return "unmatched"


def _review(
    case_id: str,
    check: str,
    expected: Any,
    actual: Any,
    reason: str,
) -> dict[str, Any]:
    """创建受限原因枚举的人工复核项。"""
    if reason not in REVIEW_REASONS:
        raise ValueError(f"unsupported review reason: {reason}")
    return {
        "case_id": case_id,
        "check": check,
        "legacy_expected": expected,
        "actual": actual,
        "reason": reason,
    }


def score_source_discovery(
    gold: dict[str, Any], actual: dict[str, Any]
) -> dict[str, Any]:
    """比较一次 Source Discovery 输出，不对新增来源计算传统 Precision。"""
    case_id = gold["metadata"]["legacy_case_id"]
    expected = gold["expected"]
    equivalence = expected.get("url_equivalence")
    expected_sources = expected["sources"]
    actual_sources = actual.get("sources")
    if not isinstance(actual_sources, list):
        raise TypeError("Source Discovery actual.sources must be a list")

    used_actual: set[int] = set()
    matches: list[dict[str, Any]] = []
    review_queue: list[dict[str, Any]] = []
    matched_core = matched_supplementary = type_matches = 0

    for expected_source in expected_sources:
        selected_index = None
        selected_kind = "unmatched"
        for index, actual_source in enumerate(actual_sources):
            if index in used_actual or not isinstance(actual_source, dict):
                continue
            kind = match_url(expected_source["url"], str(actual_source.get("url", "")), equivalence)
            if kind == "exact" or (kind == "equivalent" and selected_index is None):
                selected_index, selected_kind = index, kind
                if kind == "exact":
                    break
        if selected_index is None:
            matches.append({
                "expected_url": expected_source["url"], "actual_url": None,
                "url_match": "unmatched", "legacy_importance": expected_source["legacy_importance"],
            })
            review_queue.append(_review(
                case_id, "expected_source", expected_source["url"], None,
                "expected_source_missing",
            ))
            continue

        used_actual.add(selected_index)
        actual_source = actual_sources[selected_index]
        importance = expected_source["legacy_importance"]
        if importance == "core":
            matched_core += 1
        else:
            matched_supplementary += 1
        source_type_match = actual_source.get("source_type") == expected_source["source_type"]
        title_match = actual_source.get("title") == expected_source["title"]
        type_matches += int(source_type_match)
        matches.append({
            "expected_url": expected_source["url"],
            "actual_url": actual_source.get("url"),
            "url_match": selected_kind,
            "legacy_importance": importance,
            "source_type_match": source_type_match,
            "title_match": "exact_match" if title_match else "changed",
            "monitoring_reason": {
                "legacy_expected": expected_source["monitoring_reason"],
                "actual": actual_source.get("monitoring_reason"),
                "automatic_score": None,
            },
        })
        if not source_type_match:
            review_queue.append(_review(
                case_id, "source_type", expected_source["source_type"],
                actual_source.get("source_type"), "source_type_changed",
            ))
        if not title_match:
            review_queue.append(_review(
                case_id, "source_title", expected_source["title"],
                actual_source.get("title"), "title_changed",
            ))

    unexpected = [
        source for index, source in enumerate(actual_sources)
        if index not in used_actual and isinstance(source, dict)
    ]
    for source in unexpected:
        review_queue.append(_review(
            case_id, "unexpected_selected_source", None,
            {key: source.get(key) for key in ("url", "title", "source_type")},
            "new_source_detected",
        ))

    host_aliases = {
        str(host).casefold()
        for host in (equivalence or {}).get("host_aliases", [])
        if isinstance(host, str) and host.strip()
    }
    expected_domains = {
        _normalize_host(str(item), host_aliases) for item in expected["official_domains"]
    }
    actual_domains = {
        _normalize_host(str(item), host_aliases)
        for item in actual.get("official_domains", [])
        if isinstance(item, str)
    }
    domain_match = expected_domains == actual_domains
    if not domain_match:
        review_queue.append(_review(
            case_id, "official_domains", sorted(expected_domains),
            sorted(actual_domains), "unknown_mismatch",
        ))

    complete_match = actual.get("discovery_complete") == expected["discovery_complete"]
    if not complete_match:
        review_queue.append(_review(
            case_id, "discovery_complete", expected["discovery_complete"],
            actual.get("discovery_complete"), "unknown_mismatch",
        ))

    expected_gaps = {" ".join(str(item).split()).casefold() for item in expected["remaining_gaps"]}
    actual_gaps = {
        " ".join(str(item).split()).casefold()
        for item in actual.get("remaining_gaps", [])
    }
    gaps_match = expected_gaps == actual_gaps
    if not gaps_match:
        review_queue.append(_review(
            case_id, "remaining_gaps", expected["remaining_gaps"],
            actual.get("remaining_gaps", []), "unknown_mismatch",
        ))

    core_total = sum(s["legacy_importance"] == "core" for s in expected_sources)
    supplementary_total = len(expected_sources) - core_total
    metrics = {
        "expected_source_recall": ratio(len(used_actual), len(expected_sources)),
        "core_source_recall": ratio(matched_core, core_total),
        "supplementary_source_recall": ratio(matched_supplementary, supplementary_total),
        "source_type_match_rate": ratio(type_matches, len(used_actual)),
        "official_domain_match": domain_match,
        "discovery_complete_match": complete_match,
        "remaining_gaps_match": gaps_match,
        "unexpected_selected_sources": len(unexpected),
    }
    return {
        "checks": matches,
        "metrics": metrics,
        "review_queue": review_queue,
        "status": "REVIEW" if review_queue else "PASS",
    }


def _content_contains(content: str, anchor: str) -> bool:
    """只做空白归一化后的逐字包含，不假装理解自然语言结构。"""
    normalize = lambda value: " ".join(value.split()).casefold()
    return normalize(anchor) in normalize(content)


def summarize_blocks(blocks: Any) -> dict[str, Any]:
    """为人工结构复核输出小型摘要，不把整页正文复制到评测报告。"""
    if not isinstance(blocks, list):
        raise TypeError("Stage 1 page_data.blocks must be a list")
    types = Counter(
        block.get("type", "unknown") for block in blocks if isinstance(block, dict)
    )
    headings = [
        block.get("text") for block in blocks
        if isinstance(block, dict) and block.get("type") == "heading"
        and isinstance(block.get("text"), str)
    ]
    return {"block_count": len(blocks), "block_types": dict(types), "headings": headings[:20]}


def score_stage1(
    gold: dict[str, Any],
    actual: dict[str, Any],
    *,
    url_equivalence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """比较 Stage 1 首次采集能力；历史状态和 Diff 明确不在 V0.1 评测范围。"""
    case_id = gold["metadata"]["legacy_case_id"]
    expected = gold["expected"]
    page = actual.get("page_data")
    quality = actual.get("quality_gate")
    snapshot = actual.get("snapshot")
    if not isinstance(page, dict) or not isinstance(quality, dict):
        raise TypeError("Stage 1 actual output violates the production contract")
    if snapshot is not None and not isinstance(snapshot, dict):
        raise TypeError("Stage 1 snapshot must be an object")

    review_queue: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    final_url_match = match_url(gold["input"]["url"], str(page.get("final_url", "")), url_equivalence)
    checks.append({"check": "final_url", "status": final_url_match})
    if final_url_match == "unmatched":
        review_queue.append(_review(
            case_id, "final_url", gold["input"]["url"], page.get("final_url"),
            "url_changed",
        ))

    title_match = page.get("title") == expected["title"]
    checks.append({"check": "title", "status": "PASS" if title_match else "REVIEW"})
    if not title_match:
        review_queue.append(_review(
            case_id, "title", expected["title"], page.get("title"), "title_changed"
        ))

    acquisition_total = 0
    acquisition_matched = 0
    if "acquisition_method" in expected:
        acquisition_total = 1
        acquisition_matched = int(page.get("acquisition_method") == expected["acquisition_method"])
        checks.append({"check": "acquisition_method", "status": "PASS" if acquisition_matched else "REVIEW"})
        if not acquisition_matched:
            review_queue.append(_review(
                case_id, "acquisition_method", expected["acquisition_method"],
                page.get("acquisition_method"), "acquisition_method_changed",
            ))
    else:
        checks.append({"check": "acquisition_method", "status": "SKIPPED"})

    quality_match = quality.get("status") == expected["quality_status"]
    checks.append({"check": "quality_status", "status": "PASS" if quality_match else "REVIEW"})
    if not quality_match:
        review_queue.append(_review(
            case_id, "quality_status", expected["quality_status"],
            quality.get("status"), "quality_status_changed",
        ))

    content = page.get("content")
    if not isinstance(content, str):
        raise TypeError("Stage 1 page_data.content must be a string")
    anchor_results = []
    for anchor in expected.get("content_anchors", []):
        matched = _content_contains(content, anchor)
        anchor_results.append({"anchor": anchor, "matched": matched})
        if not matched:
            review_item = _review(
                case_id, "content_anchor", anchor, None, "anchor_missing"
            )
            review_item["possible_causes"] = [
                "live_web_drift",
                "parser_regression",
                "rendering_change",
            ]
            review_queue.append(review_item)

    block_summary = summarize_blocks(page.get("blocks"))
    manual_assertions = [
        *expected.get("structure_assertions", []),
        *expected.get("interaction_assertions", []),
    ]
    for assertion in manual_assertions:
        review_queue.append(_review(
            case_id, "structure_assertion", assertion, block_summary,
            "manual_structure_review",
        ))

    snapshot_checks = []
    for key in expected.get("snapshot_assertions", {}):
        passed = False
        if key == "requested_url":
            passed = (snapshot or {}).get("requested_url") == gold["input"]["url"]
        elif key == "final_url":
            passed = (
                snapshot is not None
                and match_url(
                    gold["input"]["url"],
                    str(snapshot.get("final_url", "")),
                    url_equivalence,
                )
                != "unmatched"
            )
        elif key == "status_code":
            snapshot_status = (snapshot or {}).get("status_code")
            passed = isinstance(snapshot_status, int) and 200 <= snapshot_status < 300
        elif key == "captured_at":
            passed = isinstance((snapshot or {}).get("captured_at"), str) and bool(snapshot["captured_at"])
        elif key == "content_hash":
            value = (snapshot or {}).get("content_hash")
            computed = hashlib.sha256(content.encode("utf-8")).hexdigest()
            passed = (
                isinstance(value, str)
                and bool(re.fullmatch(r"[0-9a-f]{64}", value))
                and value == computed
            )
        else:
            continue
        snapshot_checks.append({"check": key, "matched": passed})
        if not passed:
            review_queue.append(_review(
                case_id, f"snapshot.{key}", expected["snapshot_assertions"][key],
                (snapshot or {}).get(key), "unknown_mismatch",
            ))

    matched_anchors = sum(item["matched"] for item in anchor_results)
    return {
        "checks": checks,
        "metrics": {
            "page_execution_success": True,
            "content_anchor_recall": ratio(matched_anchors, len(anchor_results)),
            "title_match": title_match,
            "quality_status_match": quality_match,
            "acquisition_method_match": ratio(acquisition_matched, acquisition_total),
            "snapshot_assertions": ratio(
                sum(item["matched"] for item in snapshot_checks), len(snapshot_checks)
            ),
        },
        "anchor_results": anchor_results,
        "manual_review_assertions": manual_assertions,
        "structured_blocks_summary": block_summary,
        "review_queue": review_queue,
        "status": "REVIEW" if review_queue else "PASS",
    }


def summarize_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """按责任分开汇总 Source Discovery 与 Stage 1，绝不生成混合总分。"""
    status_counts = Counter(case.get("status") for case in cases)
    summary: dict[str, Any] = {
        "total_cases": len(cases),
        "pass_cases": status_counts["PASS"],
        "review_cases": status_counts["REVIEW"],
        "blocked_cases": status_counts["BLOCKED"],
        "error_cases": status_counts["ERROR"],
    }
    source_cases = [c for c in cases if c.get("target") == "source_discovery" and c.get("status") in {"PASS", "REVIEW"}]
    stage_cases = [c for c in cases if c.get("target") == "stage1" and c.get("status") in {"PASS", "REVIEW"}]

    def combine(items: list[dict[str, Any]], name: str) -> dict[str, Any]:
        values = [item["metrics"][name] for item in items if name in item.get("metrics", {})]
        return ratio(
            sum(int(value["matched"]) for value in values),
            sum(int(value["total"]) for value in values),
        )

    summary["source_discovery"] = {
        "evaluated_cases": len(source_cases),
        "expected_source_recall": combine(source_cases, "expected_source_recall"),
        "core_source_recall": combine(source_cases, "core_source_recall"),
        "supplementary_source_recall": combine(source_cases, "supplementary_source_recall"),
        "source_type_match_rate": combine(source_cases, "source_type_match_rate"),
    }
    summary["stage1"] = {
        "evaluated_cases": len(stage_cases),
        "page_execution_success_rate": ratio(len(stage_cases), len([c for c in cases if c.get("target") == "stage1"])),
        "content_anchor_recall": combine(stage_cases, "content_anchor_recall"),
        "title_match_count": ratio(sum(bool(c["metrics"]["title_match"]) for c in stage_cases), len(stage_cases)),
        "quality_status_match_count": ratio(sum(bool(c["metrics"]["quality_status_match"]) for c in stage_cases), len(stage_cases)),
        "acquisition_method_match_count": combine(stage_cases, "acquisition_method_match"),
    }
    return summary
