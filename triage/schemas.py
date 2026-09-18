"""分诊决策与关注上下文、产品事实的边界。"""

from typing import Literal

from pydantic import Field

from research.schemas import Contract, OwnComparison


class TriageDecision(Contract):
    decision: Literal["research", "monitor", "no_action"]
    reason: str = Field(min_length=1, max_length=2000)
    focus_relevance: str = Field(min_length=1, max_length=2000, description="与当前关注的关系，不陈述新的产品事实")
    own_product_relevance: OwnComparison
    evidence_refs: list[str] = Field(description="仅限事实证据 ID，不含 Memory")
    memory_context_refs: list[str] = Field(default_factory=list, description="仅用于解释关注优先级的 Memory ID")
    uncertainty: list[str]
    missing_context: list[str]


class TriageResult(TriageDecision):
    """event_id 由程序加回，不让模型改变事件归属。"""
    event_id: str
