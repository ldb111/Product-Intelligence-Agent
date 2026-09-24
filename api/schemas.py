"""HTTP 层只接受产品输入，并只返回普通用户需要的业务字段。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ApiContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompetitorCreate(ApiContract):
    name: str = Field(min_length=1, max_length=200)
    official_url: str = Field(min_length=1, max_length=2000)
    additional_urls: list[str] = Field(default_factory=list, max_length=20)
    own_product_id: str | None = Field(default=None, min_length=1, max_length=200)
    product_profile: str | None = Field(default=None, max_length=4000)
    current_focus: str = Field(default="", max_length=2000)


class CompetitorUpdate(ApiContract):
    official_url: str | None = Field(default=None, max_length=2000)
    additional_urls: list[str] | None = Field(default=None, max_length=20)
    own_product_id: str | None = Field(default=None, min_length=1, max_length=200)
    product_profile: str | None = Field(default=None, max_length=4000)
    current_focus: str | None = Field(default=None, max_length=2000)


class ResearchStart(ApiContract):
    retry: bool = False


class FollowUpCreate(ApiContract):
    request_id: str = Field(min_length=8, max_length=200)
    question: str = Field(min_length=1, max_length=4000)


class PreferenceWrite(ApiContract):
    type: str = Field(pattern="^(focus|preference|constraint|goal|decision|hypothesis|open_question)$")
    content: str = Field(min_length=1, max_length=2000)


class ProductConfigurationUpdate(ApiContract):
    product_name: str | None = Field(default=None, max_length=200)
    current_focus: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def has_changes(self):
        """至少要求一个显式配置字段，避免空请求产生无意义更新时间。"""
        if "product_name" not in self.model_fields_set and "current_focus" not in self.model_fields_set:
            raise ValueError("至少提供一项产品配置。")
        if self.product_name is None and self.current_focus is None:
            raise ValueError("产品配置不能为 null。")
        return self


class ProductError(ApiContract):
    code: str
    message: str


class ApiEnvelope(ApiContract):
    data: Any
