"""模型只提交语义内容和来源 ID；所有来源信息由程序登记和补齐。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Support(Contract):
    result_id: str
    excerpt: str = Field(min_length=1, max_length=2000, description="对应证据正文的连续逐字片段")
    evidence_summary: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="给产品经理直接阅读的简洁证据说明；必须严格基于同一 result_id 和 excerpt",
    )


class Claim(Contract):
    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["fact", "inference", "recommendation"]
    support: list[Support] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def factual_support(self):
        if self.kind == "fact" and not self.support:
            raise ValueError("事实必须引用原文。")
        # Research Claim 的产品级证据说明由模型与引用同时输出。Support 也被核验流程复用，
        # 因此字段本身保持可选，但凡进入正式 Claim 的引用都必须包含非空说明。
        if any(not item.evidence_summary or not item.evidence_summary.strip() for item in self.support):
            raise ValueError("研究声明的每条引用都必须包含证据说明。")
        return self


class OwnComparison(Contract):
    status: Literal["supported", "insufficient_own_product_context"]
    claims: list[Claim] = Field(default_factory=list, max_length=10,
                              description="每项是一条双边比较，其 support 必须同时含 own_product 知识和 competitor 证据；不要拆成单方事实条目")

    @model_validator(mode="after")
    def missing_means_unknown(self):
        if (self.status == "supported") != bool(self.claims):
            raise ValueError("只有有直接证据的比较可以含结论；资料不足时保留空列表。")
        return self


class ResearchReport(Contract):
    event_summary: list[Claim] = Field(min_length=1, max_length=10)
    background: list[Claim] = Field(max_length=10)
    competitor_context: list[Claim] = Field(max_length=10)
    own_product_comparison: OwnComparison
    implications: list[Claim] = Field(max_length=10)
    watch_next: list[Claim] = Field(max_length=10)
    uncertainties: list[str] = Field(max_length=20)
    evidence_refs: list[str] = Field(description="全部 support 中实际使用的事实证据 ID，去重")


class FollowUpAnswer(Contract):
    """单个事件追问的最小结构化答案；沿用 Claim 与 Support 证据体系。"""

    answer: list[Claim] = Field(min_length=1, max_length=20)
    uncertainties: list[str] = Field(default_factory=list, max_length=10)
    evidence_refs: list[str] = Field(description="全部 answer.support 实际使用的事实证据 ID，去重")


class VerificationResult(Contract):
    decision: Literal["event", "no_event", "still_pending"]
    reason: str = Field(min_length=1, max_length=2000)
    new_evidence_refs: list[str]
    support: list[Support] = Field(default_factory=list)
    unresolved_questions: list[str]


class Citation(Contract):
    """仅由程序构造；不作为模型最终输出 Schema 的一部分。"""
    result_id: str
    source: Literal["event", "page", "knowledge", "memory", "search"]
    domain: Literal["competitor", "own_product", "memory"]
    source_id: str
    source_group: str
    url: str | None = None
    title: str
    content: str
    source_reference: str | None = None
    credibility: str
    status: str
    observed_at: str | None = None
