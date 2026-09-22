"""自身产品配置契约：按工作区和产品持久化产品名称与当前研究重点。"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from memory.policy import contains_secret

from .schemas import ProductInput
from .store import PipelineStore, identity


class ProductConfiguration(BaseModel):
    """描述一个工作区内的自身产品配置。

    输入是 workspace_id、product_id、产品名称和当前研究重点；校验会拒绝认证信息，
    但允许名称或重点暂时为空。输出是可安全保存并在下一次 Monitoring/Triage 使用的配置。
    这些字段属于产品配置，不写入 Long-term Memory。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str = Field(min_length=1, max_length=200)
    product_id: str = Field(min_length=1, max_length=200)
    product_name: str = Field(default="", max_length=200)
    current_focus: str = Field(default="", max_length=2000)
    updated_at: str | None = None

    @field_validator("workspace_id", "product_id", "product_name", "current_focus")
    @classmethod
    def safe_text(cls, value: str) -> str:
        normalized = value.strip()
        if contains_secret(normalized):
            raise ValueError("产品配置包含认证信息，不能保存。")
        return normalized


class ProductConfigurationService:
    """使用 PipelineStore 保存并读取最小自身产品配置。

    输入是明确的 workspace + product 作用域；处理时只读写独立
    ``product_configurations`` 命名空间；输出为 ProductConfiguration，或把最新
    current_focus 合并到一次 Pipeline ProductInput。内部哈希键不会进入 API 响应。
    """

    namespace = "product_configurations"

    def __init__(self, store: PipelineStore):
        self.store = store

    @staticmethod
    def _key(workspace_id: str, product_id: str) -> str:
        return identity([workspace_id, product_id])

    def get(self, workspace_id: str, product_id: str) -> ProductConfiguration:
        key = self._key(workspace_id, product_id)
        saved = self.store.get(self.namespace, key)
        if saved is None:
            return ProductConfiguration(workspace_id=workspace_id, product_id=product_id)
        return ProductConfiguration.model_validate(saved)

    def update(
        self,
        workspace_id: str,
        product_id: str,
        changes: dict[str, str],
    ) -> ProductConfiguration:
        current = self.get(workspace_id, product_id)
        values = current.model_dump()
        values.update(changes)
        values["workspace_id"] = workspace_id
        values["product_id"] = product_id
        values["updated_at"] = datetime.now(timezone.utc).isoformat()
        updated = ProductConfiguration.model_validate(values)
        self.store.put(self.namespace, self._key(workspace_id, product_id), updated.model_dump())
        return updated

    def apply(self, product: ProductInput) -> ProductInput:
        """把已保存的最新研究重点合并到一次执行输入。

        没有 own_product_id 或尚未建立配置时沿用原输入，兼容已有 CLI/Fixture；一旦用户
        保存过配置，即使重点被清空也以该显式配置为准，避免继续使用旧竞品记录中的值。
        """
        if product.own_product_id is None:
            return product
        key = self._key(product.workspace_id, product.own_product_id)
        saved = self.store.get(self.namespace, key)
        if saved is None:
            return product
        configuration = ProductConfiguration.model_validate(saved)
        return product.model_copy(update={"current_focus": configuration.current_focus})
