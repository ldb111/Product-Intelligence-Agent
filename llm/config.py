"""把静态 Role 解析为环境配置；不参与业务判断或动态路由。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Literal, Mapping
from urllib.parse import urlsplit


ModelRole = Literal["default", "fast", "reasoning"]


class LLMConfigurationError(ValueError):
    """只报告错误的配置名称，绝不包含配置值。"""


@dataclass(frozen=True)
class ModelConfig:
    """运行所需配置；密钥不参与 repr，不能被调试输出意外展开。"""

    role: ModelRole
    provider: str
    model: str
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)


def load_model_config(
    role: ModelRole, environ: Mapping[str, str] | None = None,
) -> ModelConfig:
    """从环境解析指定角色，返回可创建客户端的配置。

    Default/Fast 沿用既有 LLM_* 命名。Reasoning 读取独立的 LLM_REASONING_*，
    密钥优先使用 LLM_REASONING_API_KEY，否则读取供应商名称对应的 *_API_KEY。
    任一必需项缺失都失败，禁止回退到其他角色，也不修改环境。
    """
    if role not in {"default", "fast", "reasoning"}:
        raise LLMConfigurationError("未知模型角色。")
    source = os.environ if environ is None else environ
    prefix = "LLM_REASONING" if role == "reasoning" else "LLM"
    names = [f"{prefix}_{suffix}" for suffix in ("PROVIDER", "BASE_URL", "MODEL")]
    values = {name: source.get(name, "").strip() for name in names}
    missing = [name for name in names if not values[name]]
    if missing:
        raise LLMConfigurationError("缺少模型配置：" + ", ".join(missing))
    provider = values[names[0]]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", provider):
        raise LLMConfigurationError(f"{names[0]} 必须是有效的供应商标识。")
    key_name = f"{prefix}_API_KEY"
    if role == "reasoning" and not source.get(key_name, "").strip():
        key_name = f"{provider.upper()}_API_KEY"
    api_key = source.get(key_name, "").strip()
    if not api_key:
        raise LLMConfigurationError("缺少模型配置：" + key_name)
    try:
        parsed = urlsplit(values[names[1]])
        valid_url = (
            parsed.scheme in {"http", "https"} and parsed.hostname
            and not parsed.username and not parsed.password and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        raise LLMConfigurationError(f"{names[1]} 必须是不含认证信息的 HTTP/HTTPS 地址。")
    return ModelConfig(role, provider, values[names[2]], values[names[1]], api_key)
