"""Research 专用公共入口；保留工具会话，不改变冻结的单次语义判断接口。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import load_dotenv
from openai import OpenAI

from .client import LLMCallError, LLMStructuredOutputError, RuntimeRecord
from .config import LLMConfigurationError


@dataclass(frozen=True)
class ResearchConfig:
    provider: str
    model: str
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    role: str = "research"


def load_research_config(environ: Mapping[str, str] | None = None) -> ResearchConfig:
    """读取独立 Research 配置并校验地址，返回隐藏凭据的配置对象。

    仅复用指定供应商的 Key；不读取 Fast/Reasoning 模型配置，不提供默认模型。
    测试传入独立映射时不读取本地文件，也不会修改进程环境。
    """
    if environ is None:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    source = os.environ if environ is None else environ
    values = [source.get("LLM_RESEARCH_" + name, "").strip() for name in ("PROVIDER", "MODEL", "BASE_URL")]
    if not all(values):
        raise LLMConfigurationError("缺少 LLM_RESEARCH_PROVIDER / MODEL / BASE_URL 配置。")
    provider, model, base = values
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", provider):
        raise LLMConfigurationError("Research 供应商标识无效。")
    try:
        url = urlsplit(base)
        valid = (url.scheme in {"http", "https"} and url.hostname and not url.username
                 and not url.password and not url.query and not url.fragment)
        url.port
    except ValueError:
        valid = False
    if not valid:
        raise LLMConfigurationError("Research 服务地址无效。")
    key_name = provider.upper() + "_API_KEY"
    key = source.get(key_name, "").strip()
    if not key:
        raise LLMConfigurationError("缺少 Research 供应商凭据：" + key_name)
    return ResearchConfig(provider, model, base, key)


class ResearchClient:
    """传输层负责多轮消息兼容；业务层只接触工具请求、结构化结果和脱敏用量。"""

    def __init__(self, config: ResearchConfig):
        self.config = config
        self._client = OpenAI(api_key=config.api_key, base_url=config.base_url, timeout=120, max_retries=0)
        self._messages: list[dict] = []
        self.runtime: list[RuntimeRecord] = []

    def start(self, prompt: str, payload: dict) -> None:
        """用当前任务重置会话；运行记录与原始模型消息分离，隐藏推理不写审计文件。"""
        self.runtime = []
        self._messages = [{"role": "system", "content": prompt},
                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _request(self, messages: list[dict], **options):
        """发送一次无自动重试的请求，返回消息并记录实际模型、用量和耗时。

        调用失败不泄漏原始响应或认证信息，也不切换角色；不完整输出明确失败。
        """
        started = perf_counter()
        try:
            response = self._client.chat.completions.create(model=self.config.model, messages=messages, **options)
        except Exception:
            raise LLMCallError("Research 调用失败；未重试或切换模型。") from None
        usage = response.usage
        self.runtime.append(RuntimeRecord(
            role="research", provider=self.config.provider, model=self.config.model,
            response_model=response.model, latency_seconds=round(perf_counter() - started, 3),
            token_usage={"input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens,
                         "total_tokens": usage.total_tokens} if usage else None,
        ))
        if not response.choices or response.choices[0].finish_reason not in {"stop", "tool_calls"}:
            raise LLMStructuredOutputError("Research 输出不完整。")
        return response.choices[0].message

    def choose_tools(self, definitions: list[dict]) -> list[dict]:
        """让模型根据会话选择下一批工具，返回名称、参数和调用 ID。

        使用真实 function calling。必要的 reasoning_content 只在传输会话内原样回传，
        不交给业务决策或日志；普通 ChatOpenAI 会丢弃这个兼容字段，因此单独适配。
        """
        message = self._request(self._messages, tools=definitions, tool_choice="auto")
        raw = message.model_dump(exclude_none=True)
        kept = {k: raw[k] for k in ("role", "content", "tool_calls", "reasoning_content") if k in raw}
        self._messages.append(kept)
        return [{"id": call.id, "name": call.function.name, "arguments": call.function.arguments}
                for call in (message.tool_calls or [])]

    def accept_result(self, call_id: str, result: dict) -> None:
        """把已验证的工具结果交还本轮调用；模型无法自行写入工具结果。"""
        self._messages.append({"role": "tool", "tool_call_id": call_id,
                               "content": json.dumps(result, ensure_ascii=False)})

    def finalize(self, prompt: str, payload: dict, schema):
        """关闭搜索后只用登记证据生成一次 JSON；新请求不带任何工具或旧规划文本。

        结构校验失败直接返回错误，不追加修复循环，保证 Finalization 不能重新联网。
        """
        messages = [{"role": "system", "content": prompt + "\n只返回以下 Schema 的 JSON：\n"
                     + json.dumps(schema.model_json_schema(), ensure_ascii=False)},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        message = self._request(messages, response_format={"type": "json_object"})
        if message.tool_calls:
            raise LLMStructuredOutputError("最终输出不得请求工具。")
        try:
            return schema.model_validate_json(message.content or "")
        except ValueError:
            raise LLMStructuredOutputError("Research 最终输出未通过结构校验。") from None


def get_research_llm(environ: Mapping[str, str] | None = None) -> ResearchClient:
    """等价于 research 角色的公共工厂；业务代码不选择供应商或模型。"""
    return ResearchClient(load_research_config(environ))
