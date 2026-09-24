"""复用正式 Source Discovery Agent 的 Legacy Regression 入口。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from evals.scoring import score_source_discovery


AsyncDiscovery = Callable[[str, str], Awaitable[Any]]
BLOCKED_CLASS_NAMES = {
    "AgentConfigurationError",
    "AgentToolConfigurationError",
    "LLMConfigurationError",
    "SearchProviderError",
    "PageReadError",
    "BrowserReadError",
    "Stage1AdapterError",
    "LLMCallError",
    "APIConnectionError",
    "APITimeoutError",
    "AuthenticationError",
    "PermissionDeniedError",
    "RateLimitError",
    "APIStatusError",
}
BLOCKED_ERROR_TYPES = {
    "missing_api_key", "authentication_failed", "quota_exceeded", "quota_unavailable",
    "service_not_enabled", "provider_error", "provider_http_error", "request_failed",
    "timeout", "network_error", "connection_error", "http_error", "browser_error",
    "quality_rejected", "access_denied", "captcha",
}


def _time() -> str:
    return datetime.now(timezone.utc).isoformat()


def bootstrap_official_url(gold: dict[str, Any]) -> str:
    """只从单个核心产品页取得 Regression bootstrap，不向 Agent 泄露其他Gold来源。"""
    candidates = [
        source["url"] for source in gold["expected"]["sources"]
        if source["legacy_importance"] == "core" and source["source_type"] == "product_page"
    ]
    if not candidates:
        raise ValueError("Legacy case has no core product_page bootstrap URL")
    return candidates[0]


def blocked_exception(exc: Exception) -> bool:
    """只按稳定异常类型分类外部环境阻塞，不保存可能含敏感信息的异常正文。"""
    error_type = getattr(exc, "error_type", None)
    name = type(exc).__name__
    return (
        name in BLOCKED_CLASS_NAMES
        or error_type in BLOCKED_ERROR_TYPES
        or any(
            token in name.casefold()
            for token in ("timeout", "connection", "authentication", "ratelimit")
        )
    )


async def _production_discovery(competitor_name: str, official_url: str):
    from source_discovery.agent import run_source_discovery

    return await run_source_discovery(competitor_name, official_url)


async def run_source_case(
    gold: dict[str, Any], *, discover: AsyncDiscovery | None = None
) -> dict[str, Any]:
    """运行一个来源发现Case；每次只调用一次正式Agent，不自动重试。"""
    started = _time()
    case_id = gold["metadata"]["legacy_case_id"]
    bootstrap = bootstrap_official_url(gold)
    base = {
        "case_id": case_id,
        "target": "source_discovery",
        "evaluation_type": "legacy_regression",
        "evaluation_use": "regression_only",
        "gold_version": gold["metadata"]["legacy_version"],
        "started_at": started,
        "bootstrap_source": "legacy_gold",
        "bootstrap_official_url": bootstrap,
    }
    try:
        value = await (discover or _production_discovery)(
            gold["input"]["competitor_name"], bootstrap
        )
        actual = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        if not isinstance(actual, dict):
            raise TypeError("Source Discovery result must be an object")
        scored = score_source_discovery(gold, actual)
        return {
            **base, **scored,
            "actual": {
                "competitor_name": actual.get("competitor_name"),
                "official_domains": actual.get("official_domains"),
                "sources": actual.get("sources"),
                "discovery_complete": actual.get("discovery_complete"),
                "remaining_gaps": actual.get("remaining_gaps"),
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
