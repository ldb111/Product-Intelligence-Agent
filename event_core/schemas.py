"""Event Core 的输入、语义决策、证据与去重契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from llm.client import RuntimeRecord


Text = Annotated[str, Field(min_length=1, max_length=2000)]
Decision = Literal["event", "no_event", "uncertain"]
DetectionPath = Literal["state_comparison", "explicit_statement"]
EventType = Literal[
    "capability", "availability", "pricing", "plan_packaging",
    "access_permission", "commercialization", "deprecation", "positioning",
]
EvidenceStatus = Literal["confirmed", "pending", "conflict"]
SourceCredibility = Literal["official", "unverified"]
InputField = Literal["previous_state", "current_state", "relevant_source_context"]


class Contract(BaseModel):
    """拒绝未定义字段，防止置信度或模型自造来源混入契约。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class StateContext(Contract):
    """程序提供的比较资格与局部位置，不能由模型反推。"""

    comparison_status: Literal[
        "baseline", "comparable", "unchanged", "not_comparable",
        "current_only", "previous_only", "unresolved", "missing_unconfirmed",
    ] = "not_comparable"
    state_key: str | None = None
    scope_path: list[Any] = Field(default_factory=list)
    state_path: list[Any] = Field(default_factory=list)
    heading_path: list[Any] = Field(default_factory=list)


class DetectionInput(Contract):
    """由采集方提供的一组局部证据；正文有大小上限，不接收整页 HTML。"""

    competitor: Text
    source_url: Text
    source_title: str
    source_type: Text
    source_credibility: SourceCredibility = "unverified"
    evidence_status: EvidenceStatus = "confirmed"
    detection_path: DetectionPath
    state_context: StateContext = Field(default_factory=StateContext)
    previous_state: str | None = Field(default=None, max_length=12000)
    current_state: str | None = Field(default=None, max_length=12000)
    contextual_diff: list[dict[str, Any]] = Field(default_factory=list)
    previous_observed_at: datetime | None = None
    current_observed_at: datetime
    relevant_source_context: str | None = Field(default=None, max_length=6000)

    @field_validator("source_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("来源必须是有效的公开 HTTP/HTTPS URL。")
        return value

    @model_validator(mode="after")
    def validate_observations(self) -> "DetectionInput":
        for timestamp in (self.previous_observed_at, self.current_observed_at):
            if timestamp is not None and timestamp.utcoffset() is None:
                raise ValueError("观察时间必须包含时区。")
        if self.previous_observed_at and self.previous_observed_at >= self.current_observed_at:
            raise ValueError("Previous 观察时间必须早于 Current。")
        if self.previous_state and self.previous_observed_at is None:
            raise ValueError("历史观察原文必须附带其真实观察时间。")
        if len(self.model_dump_json()) > 40000:
            raise ValueError("局部证据过大；请先按业务上下文选择有限的差异范围。")
        return self


class Quote(Contract):
    """指向输入原文的逐字引用；不接受观察时间或模型生成的 URL。"""

    input_field: InputField
    excerpt: Text


class ChangeItem(Contract):
    """同一业务动作下的一项变化；状态值保持原文，不补缺失的历史。"""

    item: Text
    previous_state: Text | None = None
    current_state: Text | None = None


class EventDecision(Contract):
    """模型仅提交语义判断和引用，不负责来源身份、观察时间或证据状态。"""

    decision: Decision
    event_type: EventType | None = None
    event_subject: Text | None = None
    change_action: Text | None = None
    change_items: list[ChangeItem] = Field(default_factory=list, max_length=20)
    event_time: Text | None = None
    effective_time: Text | None = None
    affected_scope: Text | None = None
    decision_reason: Text
    uncertainty_reason: Text | None = None
    citations: dict[str, list[Quote]] = Field(
        default_factory=dict,
        description="事实字段到原文引用的映射。键如 event_subject、change_action、event_type、change_items.0.item、change_items.0.previous_state、change_items.0.current_state、event_time、effective_time、affected_scope。",
    )

    @model_validator(mode="after")
    def validate_decision(self) -> "EventDecision":
        facts = (self.event_type, self.event_subject, self.change_action, self.event_time,
                 self.effective_time, self.affected_scope)
        if self.decision == "event":
            if not all((self.event_type, self.event_subject, self.change_action, self.change_items)):
                raise ValueError("event 必须描述一个有证据的独立业务动作。")
            if self.uncertainty_reason is not None:
                raise ValueError("event 不能同时标记为未确认。")
        elif any(value is not None for value in facts) or self.change_items or self.citations:
            raise ValueError("no_event/uncertain 不得携带已确认事件事实。")
        if (self.decision == "uncertain") != (self.uncertainty_reason is not None):
            raise ValueError("只有 uncertain 必须提供 uncertainty_reason。")
        return self


class DetectionDecisions(Contract):
    """按独立业务动作返回候选；相关子能力可属于同一个候选。"""

    candidates: list[EventDecision] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_no_event(self) -> "DetectionDecisions":
        if len(self.candidates) > 1 and any(c.decision == "no_event" for c in self.candidates):
            raise ValueError("no_event 是本组证据的否定结论，不能与其他候选混用。")
        return self


class Evidence(Contract):
    """程序构造的证据；confirmed 指引用通过校验，不等于所有事实均被证明。"""

    evidence_id: str
    source_url: str
    source_title: str
    source_type: str
    source_credibility: SourceCredibility
    evidence_status: EvidenceStatus
    evidence_capability: list[str]
    observed_at: datetime
    relevant_excerpt: str
    evidence_context: dict[str, Any]


class EventCandidate(EventDecision):
    """可供后续核验或去重的候选；身份和来源来自程序。"""

    candidate_id: str
    competitor: str
    detection_path: DetectionPath
    evidence: list[Evidence] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence_status(self) -> "EventCandidate":
        if self.decision == "event" and any(e.evidence_status != "confirmed" for e in self.evidence):
            raise ValueError("待确认或冲突证据不能生成 confirmed Event。")
        if self.decision == "uncertain" and all(e.evidence_status == "confirmed" for e in self.evidence):
            raise ValueError("uncertain 必须保留 pending 或 conflict 边界。")
        return self


class DetectionResult(Contract):
    candidates: list[EventCandidate]
    runtime: RuntimeRecord | None = None


class DedupDecision(Contract):
    decision: Literal["SAME_EVENT", "NEW_EVENT", "UNCERTAIN"]
    reason: Text


class DedupResult(DedupDecision):
    matched_event_id: str | None = None
    event: EventCandidate | None = None
    pending_candidate: EventCandidate | None = None
    runtime: list[RuntimeRecord] = Field(default_factory=list)
