"""复用正式Stage 1采集与Snapshot构造的Legacy Regression入口。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from evals.runners.source_discovery_regression import blocked_exception
from evals.scoring import score_stage1


StageReader = Callable[[str], dict[str, Any]]


def _time() -> str:
    return datetime.now(timezone.utc).isoformat()


def _production_stage1(url: str) -> dict[str, Any]:
    """调用正式静态优先/浏览器回退入口，并在内存中构造Snapshot，不写生产历史。"""
    from backend.page_reader import acquire_page_with_browser_fallback
    from backend.snapshot import create_snapshot

    result = acquire_page_with_browser_fallback(url)
    snapshot = None
    if result["quality_gate"].get("downstream_allowed"):
        snapshot = create_snapshot(
            result["page_data"], traversal_result=result.get("traversal_result")
        )
    return {**result, "snapshot": snapshot}


def run_stage1_case(
    gold: dict[str, Any], *,
    reader: StageReader | None = None,
    url_equivalence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """运行一个页面Case；外部阻塞与程序错误使用不同状态，且不自动重试。"""
    started = _time()
    case_id = gold["metadata"]["legacy_case_id"]
    base = {
        "case_id": case_id,
        "target": "stage1",
        "evaluation_type": "legacy_regression",
        "evaluation_use": "regression_only",
        "gold_version": gold["metadata"]["legacy_version"],
        "started_at": started,
    }
    try:
        actual = (reader or _production_stage1)(gold["input"]["url"])
        scored = score_stage1(gold, actual, url_equivalence=url_equivalence)
        page = actual["page_data"]
        quality = actual["quality_gate"]
        return {
            **base, **scored,
            "actual": {
                "requested_url": page.get("requested_url"),
                "final_url": page.get("final_url"),
                "status_code": page.get("status_code"),
                "title": page.get("title"),
                "acquisition_method": page.get("acquisition_method"),
                "quality_status": quality.get("status"),
                "content_chars": len(page.get("content", "")),
            },
            "error": None,
            "finished_at": _time(),
        }
    except Exception as exc:
        status = "BLOCKED" if blocked_exception(exc) else "ERROR"
        return {
            **base,
            "status": status,
            "checks": [], "metrics": {}, "review_queue": [],
            "actual": None,
            "error": {"type": type(exc).__name__, "category": status.casefold()},
            "finished_at": _time(),
        }
