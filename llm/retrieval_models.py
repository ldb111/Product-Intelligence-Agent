"""公共 Embedding / Rerank 专用角色；业务层不接触供应商协议或认证。

HTTP 契约核对：阿里云 text-embedding-synchronous-api / text-rerank-api。
两个接口的请求结构不同；这里使用已安装 requests，不依赖未经验证的 SDK 方法。
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Literal, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import load_dotenv

from .config import LLMConfigurationError

logger = logging.getLogger(__name__)


class EmbeddingAPIError(RuntimeError):
    """向量服务失败或响应非法；不能用伪造向量继续。"""


class RerankAPIError(RuntimeError):
    """排序服务失败或响应非法；不能偷偷返回未排序结果。"""


@dataclass(frozen=True)
class RetrievalModelConfig:
    role: Literal["embedding", "rerank"]
    provider: str
    model: str
    endpoint: str = field(repr=False)
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class ModelCallRecord:
    """仅供开发审计；不属于交给后续业务模型的 Retrieval Result。"""

    role: str
    provider: str
    model: str
    response_model: str | None
    latency_seconds: float
    usage: dict[str, int] | None
    request_id: str | None
    input_count: int


@dataclass(frozen=True)
class EmbeddingOutput:
    vectors: list[list[float]]
    runtime: list[ModelCallRecord]


@dataclass(frozen=True)
class RerankItem:
    index: int
    score: float


@dataclass(frozen=True)
class RerankOutput:
    items: list[RerankItem]
    runtime: list[ModelCallRecord]


class EmbeddingRole(Protocol):
    """只约定向量化和向量空间身份，便于业务与传输解耦。"""

    @property
    def space_id(self) -> str: ...

    def embed(self, texts: list[str]) -> EmbeddingOutput: ...


class RerankRole(Protocol):
    def rerank(self, query: str, documents: list[str], top_k: int) -> RerankOutput: ...


def load_retrieval_config(
    role: Literal["embedding", "rerank"], environ: Mapping[str, str] | None = None,
) -> RetrievalModelConfig:
    """读取专用角色配置，复用匹配供应商的凭据，返回不含默认模型的配置。

    不改动冻结的 Chat 配置解析器。现有 LLM_PROVIDER 与角色供应商一致时，复用
    LLM_API_KEY / LLM_BASE_URL；否则需要供应商自己的配置，绝不借用其他供应商 Key。
    *_BASE_URL 为可选覆盖项，通常只需任务规定的四个非 Secret 配置。
    """
    if role not in {"embedding", "rerank"}:
        raise LLMConfigurationError("未知检索模型角色。")
    if environ is None:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    source = os.environ if environ is None else environ
    prefix = role.upper()
    provider = source.get(prefix + "_PROVIDER", "").strip()
    model = source.get(prefix + "_MODEL", "").strip()
    for name, value in ((prefix + "_PROVIDER", provider), (prefix + "_MODEL", model)):
        if not value:
            raise LLMConfigurationError("缺少模型配置：" + name)
    if provider != "aliyun_bailian":
        raise LLMConfigurationError("检索角色尚未支持此供应商协议。")
    matches = source.get("LLM_PROVIDER", "").strip() == provider
    key = source.get(provider.upper() + "_API_KEY", "").strip()
    if not key and matches:
        key = source.get("LLM_API_KEY", "").strip()
    if not key:
        raise LLMConfigurationError("缺少匹配供应商的凭据配置。")
    explicit_base = source.get(prefix + "_BASE_URL", "").strip()
    base = explicit_base or source.get(provider.upper() + "_BASE_URL", "").strip()
    if not base and matches:
        base = source.get("LLM_BASE_URL", "").strip()
    try:
        url = urlsplit(base)
        valid = (url.scheme == "https" and url.hostname and not url.username
                 and not url.password and not url.query and not url.fragment)
        url.port  # 同时拒绝畸形端口，不把原 URL 放进异常。
    except ValueError:
        valid = False
    if not valid:
        raise LLMConfigurationError("检索模型 BASE_URL 缺失或不是无认证信息的 HTTPS 地址。")
    path = url.path.rstrip("/")
    if role == "rerank" and not explicit_base:
        if path != "/compatible-mode/v1":
            raise LLMConfigurationError("无法推导排序接口，请配置 RERANK_BASE_URL。")
        # 官方 qwen3-rerank 采用独立兼容协议路径，不能把 Chat 的路径直接照搬。
        path = "/compatible-api/v1"
    endpoint = urlunsplit((url.scheme, url.netloc, path, "", ""))
    endpoint += "/embeddings" if role == "embedding" else "/reranks"
    return RetrievalModelConfig(role, provider, model, endpoint, key)


def unit_vector(vector: list[float]) -> list[float]:
    """校验非空、有限且非零的向量并归一化；输出用于余弦计算的单位向量。"""
    if not isinstance(vector, list) or not vector or any(
        type(v) not in {int, float} or not math.isfinite(v) for v in vector
    ):
        raise ValueError("向量必须包含有限数值。")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("向量长度必须有限且非零。")
    return [v / norm for v in vector]


def _post(config: RetrievalModelConfig, body: dict, count: int) -> tuple[dict, ModelCallRecord]:
    """执行一次 HTTPS 请求并返回 JSON 与白名单审计字段，无隐藏重试或重定向。

    HTTP 非 200、业务错误体、非法 JSON 均失败。开发日志保留阶段、状态码与异常类型；
    不写原始异常/响应，因为它们可能包含认证头或服务回显的输入。业务错误不暴露实现。
    """
    error = EmbeddingAPIError if config.role == "embedding" else RerankAPIError
    started = perf_counter()
    status = None
    try:
        with requests.post(
            config.endpoint, headers={"Authorization": "Bearer " + config.api_key},
            json=body, timeout=(10, 60), allow_redirects=False,
        ) as response:
            status = response.status_code
            if status != 200:
                raise ValueError("http_status")
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("error") or payload.get("code"):
            raise ValueError("response_body")
        response_model = payload.get("model")
        if response_model is not None and not isinstance(response_model, str):
            raise ValueError("response_model")
        raw_usage = payload.get("usage")
        usage = {k: v for k, v in raw_usage.items()
                 if k in {"prompt_tokens", "total_tokens", "input_tokens", "output_tokens"}
                 and type(v) is int and v >= 0} if isinstance(raw_usage, dict) else None
        request_id = payload.get("id") or payload.get("request_id")
        record = ModelCallRecord(
            config.role, config.provider, config.model, response_model,
            round(perf_counter() - started, 4), usage or None,
            request_id if isinstance(request_id, str) else None, count,
        )
        return payload, record
    except Exception as exc:
        logger.warning("model_request_failed role=%s status=%s type=%s",
                       config.role, status, type(exc).__name__)
        raise error("向量化失败，请稍后重试。" if config.role == "embedding"
                    else "重排序失败，请稍后重试。") from None


class EmbeddingClient:
    def __init__(self, config: RetrievalModelConfig):
        if config.role != "embedding":
            raise LLMConfigurationError("向量客户端需要 embedding 角色。")
        self.config = config

    @property
    def space_id(self) -> str:
        return f"{self.config.provider}/{self.config.model}"

    def embed(self, texts: list[str]) -> EmbeddingOutput:
        """把非空文本分批向量化，按 index 还原顺序，返回真实向量和每批审计。

        text-embedding-v4 的批量上限为 10；不显式传 dimensions，使用模型默认维度。
        缺向量、重复 index、非有限数值或批次间维度不一致都失败，不返回部分结果。
        """
        if not texts or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError("向量化输入不能为空。")
        vectors, records = [], []
        for start in range(0, len(texts), 10):
            batch = texts[start:start + 10]
            payload, record = _post(self.config, {
                "model": self.config.model, "input": batch, "encoding_format": "float",
            }, len(batch))
            records.append(record)
            try:
                data = payload["data"]
                if not isinstance(data, list) or len(data) != len(batch):
                    raise ValueError("count")
                indexes = [item["index"] for item in data]
                if any(type(i) is not int for i in indexes) or sorted(indexes) != list(range(len(batch))):
                    raise ValueError("indexes")
                for item in sorted(data, key=lambda item: item["index"]):
                    vector = item["embedding"]
                    unit_vector(vector)
                    if vectors and len(vector) != len(vectors[0]):
                        raise ValueError("dimensions")
                    vectors.append(vector)
            except (KeyError, ValueError, TypeError, OverflowError):
                logger.warning("embedding_response_invalid")
                raise EmbeddingAPIError("向量化结果无效；未生成知识。") from None
        return EmbeddingOutput(vectors, records)


class RerankClient:
    def __init__(self, config: RetrievalModelConfig):
        if config.role != "rerank":
            raise LLMConfigurationError("排序客户端需要 rerank 角色。")
        self.config = config

    def rerank(self, query: str, documents: list[str], top_k: int) -> RerankOutput:
        """只发送召回候选，返回候选下标和真实分数；来源始终由调用方按下标映射。

        输入是查询、候选正文和结果数，输出不使用响应中的文档或 URL。非法下标、重复
        下标、缺结果及非有限分数全部拒绝，防止错误归因到另一条证据。
        """
        if (not query.strip() or not documents or len(documents) > 500
                or any(not t.strip() for t in documents)
                or type(top_k) is not int or not 1 <= top_k <= len(documents)):
            raise ValueError("排序查询、候选或结果数量无效。")
        payload, record = _post(self.config, {
            "model": self.config.model, "query": query, "documents": documents, "top_n": top_k,
        }, len(documents))
        try:
            rows = payload["results"]
            if not isinstance(rows, list) or len(rows) != top_k:
                raise ValueError("count")
            items = [RerankItem(row["index"], row["relevance_score"]) for row in rows]
            if len({i.index for i in items}) != len(items) or any(
                type(i.index) is not int or not 0 <= i.index < len(documents)
                or type(i.score) not in {int, float} or not math.isfinite(i.score)
                or not 0 <= i.score <= 1 for i in items
            ):
                raise ValueError("rank")
        except (KeyError, ValueError, TypeError):
            logger.warning("rerank_response_invalid")
            raise RerankAPIError("重排序结果无效；未返回检索结果。") from None
        return RerankOutput(sorted(items, key=lambda i: (-i.score, i.index)), [record])


def get_embedding(environ: Mapping[str, str] | None = None) -> EmbeddingClient:
    """公共角色入口：读取环境并创建向量客户端，不接受业务方指定 Chat 模型。"""
    return EmbeddingClient(load_retrieval_config("embedding", environ))


def get_rerank(environ: Mapping[str, str] | None = None) -> RerankClient:
    """公共角色入口：读取环境并创建专用排序客户端。"""
    return RerankClient(load_retrieval_config("rerank", environ))
