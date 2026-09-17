"""复用已安装的 OpenAI-compatible 客户端执行单次结构化语义判断。"""

from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping, TypeVar

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict

from .config import ModelConfig, ModelRole, load_model_config


T = TypeVar("T", bound=BaseModel)


class LLMCallError(RuntimeError):
    """调用失败，错误消息不透传底层请求或认证信息。"""


class LLMStructuredOutputError(ValueError):
    """模型没有给出可验证的结构化结果。"""


class RuntimeRecord(BaseModel):
    """开发审计信息，程序填充，不进入业务模型上下文。"""

    model_config = ConfigDict(extra="forbid")
    role: str
    provider: str
    model: str
    response_model: str | None
    latency_seconds: float
    token_usage: dict[str, int] | None


class SemanticClient:
    """一个角色对应一个客户端；无工具、规划循环或失败换模型行为。"""

    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        try:
            self._model = ChatOpenAI(
                api_key=config.api_key, base_url=config.base_url, model=config.model,
                max_retries=0, timeout=120,
            )
        except Exception:
            raise LLMCallError("指定角色的模型客户端初始化失败。") from None

    def decide(self, prompt: str, payload: dict[str, Any], schema: type[T]) -> tuple[T, RuntimeRecord]:
        """发送一次 JSON 判断请求，校验输出并返回结果与非敏感运行记录。

        输入是业务 Prompt、有限证据和 Pydantic Schema。使用当前安装版本支持的
        json_mode，并把 JSON Schema 放入指令；无需工具调用能力或 Agent。
        输出解析错误与请求错误分别失败，不记录原始响应、异常正文或隐藏推理内容。
        """
        started = perf_counter()
        messages = [
            ("system", prompt + "\n仅返回符合以下 Schema 的 JSON：\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False)),
            ("human", json.dumps(payload, ensure_ascii=False)),
        ]
        try:
            runnable = self._model.with_structured_output(schema, method="json_mode", include_raw=True)
            response = runnable.invoke(messages, config={"callbacks": []})
        except Exception:
            # 请求异常可能携带认证头或响应正文，不能直接拼接 str(exc) 或保留异常链。
            raise LLMCallError("指定角色的模型调用失败；未回退到其他模型。") from None
        if response.get("parsing_error") is not None or response.get("parsed") is None:
            raise LLMStructuredOutputError("模型结构化输出无效；未生成业务结果。")
        try:
            parsed = schema.model_validate(response["parsed"])
        except ValueError:
            raise LLMStructuredOutputError("模型结构化输出未通过 Schema 校验。") from None
        raw = response["raw"]
        usage = getattr(raw, "usage_metadata", None)
        metadata = getattr(raw, "response_metadata", {})
        record = RuntimeRecord(
            role=self.config.role, provider=self.config.provider, model=self.config.model,
            response_model=metadata.get("model_name") or metadata.get("model"),
            latency_seconds=round(perf_counter() - started, 3),
            token_usage=(
                {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens") if isinstance(usage.get(key), int)}
                if isinstance(usage, dict) else None
            ),
        )
        return parsed, record


def get_llm(role: ModelRole, environ: Mapping[str, str] | None = None) -> SemanticClient:
    """加载本地环境后创建静态角色客户端；测试可传独立配置映射。

    只在此入口读取项目配置文件，保留进程环境优先级，不改写文件或密钥。
    业务层只提交 role，无权覆盖供应商、地址或模型名。
    """
    if environ is None:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    return SemanticClient(load_model_config(role, environ))
