"""产品输入不要求技术 ID；用户结果与开发审计使用不同契约。"""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from memory.policy import contains_secret
from file_parsing.schemas import ParsingStatus


class ProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: str = Field(min_length=1, max_length=200)
    own_product_id: str | None = Field(default=None, min_length=1, max_length=200)
    competitor_name: str = Field(min_length=1, max_length=200)
    official_url: str | None = None
    additional_urls: list[str] = Field(default_factory=list, max_length=20)
    product_profile: str | None = Field(default=None, max_length=4000)
    current_focus: str = Field(default="", max_length=2000)
    own_product_documents: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("workspace_id", "own_product_id", "competitor_name", "product_profile", "current_focus")
    @classmethod
    def safe_text(cls, value):
        if value is not None and contains_secret(value):
            raise ValueError("输入包含认证信息，不能进入产品流程。")
        if isinstance(value, str) and value and not value.strip():
            raise ValueError("输入不能仅为空白。")
        return value

    @field_validator("official_url", "additional_urls")
    @classmethod
    def valid_urls(cls, value):
        for url in value if isinstance(value, list) else ([value] if value else []):
            parsed = urlsplit(url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                    or parsed.password or contains_secret(url)):
                raise ValueError("来源必须是无认证信息的网页地址。")
            parsed.port
        return value


class ProductResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    competitor: str
    monitoring_sources: list[dict] = Field(default_factory=list)
    scan_status: str = "not_scanned"
    baseline_created: bool = False
    detected_events: list[dict] = Field(default_factory=list)
    historical_events: list[dict] = Field(default_factory=list)
    pending_candidates: list[dict] = Field(default_factory=list)
    triage_results: list[dict] = Field(default_factory=list)
    research_actions: list[dict] = Field(default_factory=list)
    research_results: list[dict] = Field(default_factory=list)
    verification_results: list[dict] = Field(default_factory=list)
    citations: list[dict] = Field(default_factory=list)
    current_focus: str
    uncertainties: list[str] = Field(default_factory=list)
    status: Literal["complete", "pending", "failed"] = "complete"
    error: dict | None = None


class OwnProductDocumentResult(BaseModel):
    """自身产品资料配置动作的用户结果，不暴露路径、unit ID、向量或数据库字段。"""

    model_config = ConfigDict(extra="forbid")
    document_id: str | None = None
    display_name: str
    file_format: str
    parsing_status: ParsingStatus
    ingestion_status: Literal["not_started", "ingesting", "completed", "failed"]
    content_status: Literal[
        "current", "existing", "unchanged", "conflict", "unavailable", "current_preserved"
    ]
    knowledge_status: Literal["available", "unavailable"]
    failure_reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    unit_count: int = 0


class PipelineFailure(RuntimeError):
    """仅携带业务错误类别；底层异常正文不能混入产品结果。"""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category
