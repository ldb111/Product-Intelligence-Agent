"""Source Discovery Agent 的最小运行链路。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call, wrap_tool_call
from langchain.agents.structured_output import ToolStrategy
from langchain.mcp import MCPAdapter
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator

from source_discovery.mcp_server import mcp
from source_discovery.prompts import SOURCE_DISCOVERY_SYSTEM_PROMPT
from source_discovery.schemas import (
    MonitoringSource,
    PageLink,
    ReadPageResult,
    SourceDiscoveryResult,
    SourceType,
)


MAX_SEARCH_COUNT = 6
MAX_READ_COUNT = 12
MAX_TOOL_CALL_COUNT = 20
MAX_CANDIDATE_URLS = 40
# 多个中间件会让一次工具调用经过多个 LangGraph step；该值只保证业务 Hard Stop
# 有机会生效，不会放宽 6/12/20 的实际工具调用上限。
AGENT_RECURSION_LIMIT = 4 * MAX_TOOL_CALL_COUNT + 20
REQUIRED_TOOL_NAMES = {"web_search", "read_page"}
REQUIRED_MODEL_CONFIG = (
    "LLM_PROVIDER",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
)
MODEL_VISIBLE_TOOL_NAMES = {"web_search", "read_candidate"}
USER_INPUT_PRIORITY = 300
WEB_SEARCH_PRIORITY = 200
PAGE_LINK_PARENT_PRIORITY = {
    "user_input": 30,
    "web_search": 20,
    "page_link": 10,
}
PAGE_REGION_PRIORITY = {"nav": 10, "main": 10, "other": 5, "footer": 0}
SOCIAL_HOST_SUFFIXES = (
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "weibo.com",
    "youtube.com",
)
STATIC_RESOURCE_SUFFIXES = (
    ".7z",
    ".avi",
    ".css",
    ".dmg",
    ".eot",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".map",
    ".mov",
    ".mp3",
    ".mp4",
    ".msi",
    ".png",
    ".rar",
    ".svg",
    ".tar",
    ".tgz",
    ".ttf",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
)
ACTION_PATH_TOKENS = {
    "account",
    "console",
    "contact",
    "cookie",
    "dashboard",
    "legal",
    "login",
    "logout",
    "privacy",
    "register",
    "signin",
    "signup",
    "terms",
}
ACTION_TEXT_MARKERS = (
    "contact us",
    "cookie policy",
    "legal notice",
    "privacy policy",
    "sign in",
    "sign up",
    "terms of service",
    "联系我们",
    "法律声明",
    "注册",
    "控制台",
    "登录",
    "登出",
    "退出登录",
    "隐私政策",
)


class AgentConfigurationError(Exception):
    """表示 Agent 启动所需的模型配置不完整。"""


class AgentToolConfigurationError(Exception):
    """表示 MCP 没有提供 Agent 所需的准确工具集合。"""


class EvidenceValidationError(Exception):
    """表示最终来源缺少真实搜索或页面读取证据。"""


class StructuredOutputRepairLimitError(Exception):
    """表示模型在一次修正后仍未生成合法结构化结果。"""


CandidateProvenance = Literal["user_input", "web_search", "page_link"]


@dataclass
class Candidate:
    """保存一个由真实输入、搜索结果或页面链接发现的候选网址。"""

    candidate_id: str
    url: str
    provenance: CandidateProvenance
    title: str | None = None
    anchor_text: str | None = None
    snippet: str | None = None
    discovered_from: str | None = None
    final_url: str | None = None
    aria_label: str | None = None
    page_region: str | None = None
    parent_provenance: CandidateProvenance | None = None
    admission_priority: int = 0
    read_successful: bool = False
    active: bool = True

    def model_view(self) -> dict[str, str]:
        """返回模型选择候选所需的最小字段，不暴露 Registry 内部状态。"""
        payload = {
            "candidate_id": self.candidate_id,
            "url": self.url,
            "provenance": self.provenance,
        }
        for name in (
            "title",
            "anchor_text",
            "aria_label",
            "snippet",
            "discovered_from",
            "page_region",
        ):
            value = getattr(self, name)
            if value:
                payload[name] = value
        return payload


def _link_description(link: PageLink) -> str:
    """合并链接的可见文本与无障碍描述，供确定性噪声判断使用。"""
    return " ".join(
        str(link.get(name, "")).strip()
        for name in ("anchor_text", "aria_label", "title")
        if str(link.get(name, "")).strip()
    )


def _is_obvious_link_noise(url: str, link: PageLink) -> bool:
    """过滤明确不是持续产品状态入口的操作、法律、社交和静态资源链接。

    规则不会判断来源类型，也不会过滤 docs、support、blog 等可能有监控价值的页面。
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    path = unquote(parsed.path).casefold()
    description = _link_description(link).casefold()
    if not description:
        return True
    if link.get("is_download"):
        return True
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in SOCIAL_HOST_SUFFIXES):
        return True
    if path.endswith(STATIC_RESOURCE_SUFFIXES):
        return True
    if "/cdn-cgi/l/email-protection" in path:
        return True
    # sign-in / sign_up 等常见拼写先压成 signin / signup，再按完整路径段判断；不会因为
    # 普通正文里偶然出现 login 等单词就误删其他页面。
    path_tokens = {
        token.replace("-", "")
        for token in re.split(r"[/_.]+", path)
        if token
    }
    if path_tokens & ACTION_PATH_TOKENS:
        return True
    return any(marker in description for marker in ACTION_TEXT_MARKERS)


def _page_link_priority(
    url: str,
    link: PageLink,
    *,
    final_url: str,
    parent_provenance: CandidateProvenance,
    evidence_hosts: set[str],
) -> int:
    """生成小范围确定性优先级，只决定有限候选槽位的占用顺序。"""
    parent = urlsplit(final_url)
    target = urlsplit(url)
    parent_host = (parent.hostname or "").casefold()
    target_host = (target.hostname or "").casefold()
    priority = PAGE_LINK_PARENT_PRIORITY[parent_provenance]
    if target_host == parent_host:
        priority += 30
    elif _registrable_domain(target_host) == _registrable_domain(parent_host):
        priority += 20
    elif target_host in evidence_hosts:
        priority += 15
    priority += PAGE_REGION_PRIORITY.get(str(link.get("page_region", "other")), 5)
    priority += 5  # Admission 已确保链接具有真实可读描述。
    depth = len([part for part in target.path.split("/") if part])
    if depth <= 2:
        priority += 5
    elif depth >= 6:
        priority -= 5
    if len(target.query.split("&")) >= 3:
        priority -= 5
    return priority


class ReadCandidateInput(BaseModel):
    """read_candidate 的模型可见输入，只允许提交候选编号。"""

    candidate_id: str = Field(
        min_length=1,
        description="已经由用户输入、网页搜索或页面链接发现的候选编号",
    )


class SelectedMonitoringSource(BaseModel):
    """模型对一个已读候选作出的业务选择，不包含程序已确定的 URL 和标题。"""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(
        min_length=1,
        description="已经成功读取、建议长期监控的候选编号",
    )
    source_type: SourceType = Field(description="该候选页面对应的监控来源类型")
    monitoring_reason: str = Field(
        min_length=1,
        description="该候选值得长期监控以及可能发现何种产品变化的简短理由",
    )


class SourceDiscoveryDecision(BaseModel):
    """模型内部决策结构；程序随后用真实读取证据生成对外结果。"""

    model_config = ConfigDict(extra="forbid")

    competitor_name: str = Field(min_length=1, description="本次研究的竞品名称")
    official_domains: list[str] = Field(description="已由真实证据确认的官方域名")
    selected_sources: list[SelectedMonitoringSource] = Field(
        description="模型从成功读取候选中选出的监控来源"
    )
    discovery_complete: bool = Field(description="主要监控来源覆盖是否已经足够")
    remaining_gaps: list[str] = Field(description="仍未确认的具体重要来源或信息范围")

    @model_validator(mode="after")
    def validate_completion_and_gaps(self) -> "SourceDiscoveryDecision":
        """内部决策与外部结果使用同一完成度约束，避免转换前后语义漂移。"""
        if self.discovery_complete and self.remaining_gaps:
            raise ValueError("remaining_gaps must be empty when discovery_complete is true")
        if not self.discovery_complete and not self.remaining_gaps:
            raise ValueError(
                "remaining_gaps must contain a concrete gap when discovery_complete is false"
            )
        return self


def _prepare_candidate_url(url: str, base_url: str | None = None) -> str | None:
    """把真实 URL 或 href 转成无 fragment 的 HTTP/HTTPS 绝对地址。

    本函数只做确定性语法处理：相对地址以成功页面的 final_url 为基准解析，过滤非网页
    协议并删除 fragment。它不会猜测路径、改写 hostname 或判断页面是否值得监控。
    """
    value = url.strip()
    if not value:
        return None
    absolute = urljoin(base_url, value) if base_url else value
    parsed = urlsplit(absolute)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.query,
            "",
        )
    )


def _candidate_url_key(url: str) -> str:
    """生成仅用于精确去重的 URL key，不改变实际请求所用的真实 URL。"""
    parsed = urlsplit(url)
    path = parsed.path or "/"
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            path,
            parsed.query,
            "",
        )
    )


def _registrable_domain(hostname: str) -> str:
    """在不增加依赖的前提下，对常见域名做保守的可注册主域近似判断。"""
    labels = hostname.casefold().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    common_second_level = {"ac", "co", "com", "edu", "gov", "net", "org"}
    if len(labels[-1]) == 2 and labels[-2] in common_second_level:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


@dataclass(frozen=True)
class ModelConfig:
    """构建聊天模型所需的供应商标识与 OpenAI-compatible 连接配置。"""

    provider: str
    api_key: str
    base_url: str
    model: str


@dataclass
class DiscoveryProgramState:
    """记录单个竞品一次运行中的确定性工具证据与调用计数。

    这些字段只供程序执行 Hard Stop（硬停止）、去重和最终证据校验，不会再次作为
    一整份状态复制进模型上下文。页面正文仍由原始 Tool Message 提供给模型。
    """

    input_urls: set[str] = field(default_factory=set)
    # Registry 保存任务生命周期内所有已分配编号的候选；candidates / candidate_urls
    # 只表示当前最多 40 个 Active Candidate，避免历史淘汰项重新进入模型上下文。
    candidate_registry: list[Candidate] = field(default_factory=list)
    candidate_urls: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    successful_pages: list[ReadPageResult] = field(default_factory=list)
    search_count: int = 0
    read_count: int = 0
    tool_call_count: int = 0
    search_closed: bool = False
    search_closed_reason: str | None = None
    finalization_mode: bool = False
    finalization_reason: str | None = None
    _candidate_url_set: set[str] = field(default_factory=set, repr=False)
    _candidate_keys: set[str] = field(default_factory=set, repr=False)
    _candidates_by_id: dict[str, Candidate] = field(default_factory=dict, repr=False)
    _pages_by_requested_url: dict[str, ReadPageResult] = field(
        default_factory=dict,
        repr=False,
    )
    _rejected_tool_calls: set[str] = field(default_factory=set, repr=False)
    _next_candidate_number: int = field(default=1, repr=False)

    def _deactivate_candidate(self, candidate: Candidate) -> None:
        """从 Active Pool 淘汰候选，但永久保留 Registry 编号与 URL 证据。"""
        candidate.active = False
        self.candidates.remove(candidate)
        self.candidate_urls.remove(candidate.url)

    def _replacement_page_link(
        self,
        provenance: CandidateProvenance,
        priority: int,
    ) -> Candidate | None:
        """为更高优先级候选寻找可安全淘汰的 Page Link。"""
        replaceable = [
            candidate
            for candidate in self.candidates
            if candidate.provenance == "page_link" and not candidate.read_successful
        ]
        if not replaceable:
            return None
        lowest = min(
            replaceable,
            key=lambda candidate: (
                candidate.admission_priority,
                -int(candidate.candidate_id.rsplit("_", 1)[-1]),
            ),
        )
        if provenance == "page_link" and priority <= lowest.admission_priority:
            return None
        return lowest

    def register_candidate(
        self,
        url: str,
        provenance: CandidateProvenance,
        *,
        title: str | None = None,
        anchor_text: str | None = None,
        snippet: str | None = None,
        discovered_from: str | None = None,
        base_url: str | None = None,
        aria_label: str | None = None,
        page_region: str | None = None,
        parent_provenance: CandidateProvenance | None = None,
        admission_priority: int | None = None,
    ) -> Candidate | None:
        """注册一个真实候选并分配稳定编号；重复 URL 返回已有记录。

        输入：用户、搜索或页面 href 提供的 URL 及其可选描述信息。
        处理：只做 HTTP/HTTPS、相对地址、fragment 和精确 URL 去重；达到 40 个候选后，
        Search 或更高优先级 Page Link 可以替换未读取的低优先级 Page Link，但不会淘汰
        user_input 或已成功读取页面。候选容量本身不再关闭搜索。
        输出：新建或已存在的 Candidate；非法地址或容量已满时返回 None。
        """
        prepared = _prepare_candidate_url(url, base_url)
        if prepared is None:
            return None
        key = _candidate_url_key(prepared)
        if key in self._candidate_keys:
            existing = next(
                candidate
                for candidate in self.candidate_registry
                if _candidate_url_key(candidate.url) == key
            )
            rank = {"page_link": 1, "web_search": 2, "user_input": 3}
            if rank[provenance] > rank[existing.provenance]:
                existing.provenance = provenance
                existing.admission_priority = (
                    USER_INPUT_PRIORITY
                    if provenance == "user_input"
                    else WEB_SEARCH_PRIORITY
                )
            if title and not existing.title:
                existing.title = title
            if snippet and not existing.snippet:
                existing.snippet = snippet
            return existing

        priority = admission_priority
        if priority is None:
            priority = (
                USER_INPUT_PRIORITY
                if provenance == "user_input"
                else WEB_SEARCH_PRIORITY
                if provenance == "web_search"
                else 0
            )
        if len(self.candidates) >= MAX_CANDIDATE_URLS:
            replacement = self._replacement_page_link(provenance, priority)
            if replacement is None:
                return None
            self._deactivate_candidate(replacement)

        candidate = Candidate(
            candidate_id=f"candidate_{self._next_candidate_number:03d}",
            url=prepared,
            provenance=provenance,
            title=title or None,
            anchor_text=anchor_text or None,
            snippet=snippet or None,
            discovered_from=discovered_from,
            aria_label=aria_label or None,
            page_region=page_region or None,
            parent_provenance=parent_provenance,
            admission_priority=priority,
        )
        self._next_candidate_number += 1
        self.candidate_registry.append(candidate)
        self.candidates.append(candidate)
        self.candidate_urls.append(prepared)
        self._candidate_url_set.add(prepared)
        self._candidate_keys.add(key)
        self._candidates_by_id[candidate.candidate_id] = candidate
        return candidate

    def register_user_url(self, url: str) -> Candidate | None:
        """把用户提供的官网或补充网址直接登记为可读取候选。"""
        candidate = self.register_candidate(url, "user_input")
        if candidate is not None:
            self.input_urls.add(candidate.url)
        return candidate

    def candidate_by_id(self, candidate_id: str) -> Candidate | None:
        """按模型提交的稳定编号查找候选，不接受 URL 作为替代输入。"""
        return self._candidates_by_id.get(candidate_id)

    def candidates_discovered_from(self, final_url: str) -> list[dict[str, str]]:
        """返回某个已读页面新发现的候选，用于精简的 read_candidate 结果。"""
        return [
            candidate.model_view()
            for candidate in self.candidates
            if candidate.discovered_from == final_url
        ]

    def record_tool_call(self) -> None:
        """记录模型实际发起的一次搜索或读取调用，无论之后是否获准执行。"""
        self.tool_call_count += 1

    def enter_finalization(self, reason: str) -> None:
        """切换到强制收敛模式；首次触发原因用于运行审计。"""
        if not self.finalization_mode:
            self.finalization_mode = True
            self.finalization_reason = reason

    def close_search(self, reason: str) -> None:
        """关闭后续搜索，但保留网页读取能力用于验证已经发现的候选网址。"""
        if not self.search_closed:
            self.search_closed = True
            self.search_closed_reason = reason

    def reject_tool_call(self, signature: str) -> None:
        """记录一次确定性拒绝，后续完全相同的调用可直接识别。"""
        self._rejected_tool_calls.add(signature)

    def was_rejected(self, signature: str) -> bool:
        """判断相同工具与完全相同参数是否已经被确定性规则拒绝。"""
        return signature in self._rejected_tool_calls

    def add_search_results(self, results: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """把真实搜索结果注册为候选，并为模型返回对应 candidate_id。"""
        accepted: list[dict[str, Any]] = []
        for item in results:
            url = item.get("url")
            if not isinstance(url, str):
                continue
            existed = _candidate_url_key(_prepare_candidate_url(url) or "") in self._candidate_keys
            candidate = self.register_candidate(
                url,
                "web_search",
                title=item.get("title") if isinstance(item.get("title"), str) else None,
                snippet=(
                    item.get("snippet")
                    if isinstance(item.get("snippet"), str)
                    else None
                ),
            )
            if candidate is None:
                continue
            if existed:
                continue
            accepted.append(
                {
                    **item,
                    "candidate_id": candidate.candidate_id,
                    "provenance": "web_search",
                }
            )
        return accepted

    def add_page_links(
        self,
        links: Sequence[PageLink],
        *,
        final_url: str,
        parent_candidate: Candidate | None = None,
    ) -> list[dict[str, str]]:
        """注册成功页面真实 href，并优先处理同站或同可注册主域链接。

        跨站链接不会被自动认定为官方来源，但仍可作为 page_link candidate；Agent 必须
        继续读取并通过现有 Evidence Validation 后才能推荐。候选上限对三种 provenance
        统一生效。
        """
        parent_provenance: CandidateProvenance = (
            parent_candidate.provenance if parent_candidate is not None else "page_link"
        )
        evidence_hosts = {
            (urlsplit(candidate.url).hostname or "").casefold()
            for candidate in self.candidates
            if candidate.provenance in {"user_input", "web_search"}
        }
        prepared_links: list[tuple[int, int, PageLink, str]] = []
        for index, link in enumerate(links):
            href = link.get("href")
            if not isinstance(href, str):
                continue
            prepared = _prepare_candidate_url(href, final_url)
            if prepared is None or _candidate_url_key(prepared) == _candidate_url_key(final_url):
                continue
            if _is_obvious_link_noise(prepared, link):
                continue
            priority = _page_link_priority(
                prepared,
                link,
                final_url=final_url,
                parent_provenance=parent_provenance,
                evidence_hosts=evidence_hosts,
            )
            prepared_links.append((-priority, index, link, prepared))

        accepted: list[dict[str, str]] = []
        for negative_priority, _, link, prepared in sorted(
            prepared_links, key=lambda item: item[:2]
        ):
            existed = _candidate_url_key(prepared) in self._candidate_keys
            candidate = self.register_candidate(
                prepared,
                "page_link",
                title=link.get("title"),
                anchor_text=link.get("anchor_text", ""),
                aria_label=link.get("aria_label"),
                discovered_from=final_url,
                page_region=link.get("page_region", "other"),
                parent_provenance=parent_provenance,
                admission_priority=-negative_priority,
            )
            if candidate is None:
                continue
            if not existed:
                accepted.append(candidate.model_view())
        return accepted

    def record_successful_page(
        self,
        page: ReadPageResult,
        candidate: Candidate | None = None,
    ) -> list[dict[str, str]]:
        """保存读取证据、记录重定向，并注册页面中真实出现的链接。"""
        requested_url = page["requested_url"]
        self._pages_by_requested_url[requested_url] = page
        self.successful_pages.append(page)
        if candidate is None:
            candidate = next(
                (
                    item
                    for item in self.candidate_registry
                    if item.url == requested_url
                ),
                None,
            )
        if candidate is not None:
            candidate.final_url = page["final_url"]
            candidate.read_successful = True
        return self.add_page_links(
            page.get("links", []),
            final_url=page["final_url"],
            parent_candidate=candidate,
        )

    def cached_page(self, requested_url: str) -> ReadPageResult | None:
        """返回同一运行中已经成功读取过的页面，避免重复访问网页。"""
        return self._pages_by_requested_url.get(requested_url)


class _StructuredOutputRepairLimiter:
    """只允许 ToolStrategy 对非法结构化输出反馈一次修正机会。"""

    def __init__(self) -> None:
        self.correction_count = 0

    def __call__(self, error: Exception) -> str:
        if self.correction_count >= 1:
            raise StructuredOutputRepairLimitError(
                "结构化结果经过一次修正后仍不符合要求。"
            ) from error
        self.correction_count += 1
        return "最终结果结构不符合要求。请仅根据已有真实证据修正字段，并重新提交一次。"


def load_model_config(environ: Mapping[str, str] | None = None) -> ModelConfig:
    """从环境变量读取模型配置，并在初始化模型前一次性检查缺失项。

    输入：可选的环境变量映射，测试可传入独立字典；正式运行默认使用 ``os.environ``。
    输出：不包含默认模型或默认接口地址的 ModelConfig。任何字段缺失都会明确失败，避免
    程序在运行中途才因模糊的认证或连接问题终止。
    """
    source = os.environ if environ is None else environ
    values = {name: str(source.get(name, "")).strip() for name in REQUIRED_MODEL_CONFIG}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise AgentConfigurationError(
            f"缺少 Source Discovery Agent 模型配置：{', '.join(missing)}"
        )
    return ModelConfig(
        provider=values["LLM_PROVIDER"],
        api_key=values["LLM_API_KEY"],
        base_url=values["LLM_BASE_URL"],
        model=values["LLM_MODEL"],
    )


def build_chat_model(config: ModelConfig) -> ChatOpenAI:
    """使用供应商无关配置创建 OpenAI-compatible ChatOpenAI 模型实例。"""
    return ChatOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        temperature=0,
    )


async def load_source_discovery_tools(adapter: MCPAdapter) -> list[BaseTool]:
    """通过 LangChain 第一方 MCP 适配器加载并核对两个已验收工具。"""
    tools = await adapter.list_tools()
    names = {tool.name for tool in tools}
    if names != REQUIRED_TOOL_NAMES:
        raise AgentToolConfigurationError(
            "Source Discovery Agent 需要且只能使用 web_search 和 read_page。"
        )
    return tools


def _coerce_read_page_result(result: Any) -> ReadPageResult:
    """把底层 MCP Tool 的直接调用结果还原为 ReadPageResult。

    LangChain 在 Agent Tool Call 和普通 ``ainvoke`` 场景下可能分别返回字典、JSON 文本
    或 ToolMessage。这里仅做传输形态适配，不改变网页读取字段或错误语义。
    """
    if isinstance(result, ToolMessage):
        payload = _structured_content(result)
    elif isinstance(result, dict):
        payload = result.get("structured_content", result)
    elif isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ValueError("网页读取工具没有返回结构化结果。") from exc
        payload = parsed
    elif isinstance(result, list):
        # LangChain MCP Tool 在普通 ainvoke 场景返回 MCP content blocks；read_page 的
        # 结构化 JSON 同时存在于 text block 中，因此无需绕过 MCP 或重复调用适配层。
        text_block = next(
            (
                block.get("text")
                for block in result
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ),
            None,
        )
        if text_block is None:
            payload = None
        else:
            try:
                payload = json.loads(text_block)
            except json.JSONDecodeError as exc:
                raise ValueError("网页读取工具没有返回结构化结果。") from exc
    else:
        payload = None
    if not isinstance(payload, dict):
        raise ValueError("网页读取工具没有返回结构化结果。")
    return payload  # type: ignore[return-value]


def build_model_visible_tools(
    mcp_tools: Sequence[BaseTool],
    program_state: DiscoveryProgramState,
) -> list[BaseTool]:
    """把底层 read_page 包装成只接受 candidate_id 的模型可见工具。

    输入：已通过 MCP 工具集合校验的 web_search/read_page，以及本次运行状态。
    处理：web_search 保持原工具；read_page 仅由闭包内部按 Registry 中的真实 URL 调用。
    输出：模型只能看到 web_search 与 read_candidate，无法提交任意 URL。
    """
    tools_by_name = {tool.name: tool for tool in mcp_tools}
    read_page_tool = tools_by_name["read_page"]

    async def read_candidate(candidate_id: str) -> tuple[str, ReadPageResult]:
        candidate = program_state.candidate_by_id(candidate_id)
        if candidate is None:
            # 正常 Agent 调用会先被中间件拒绝；这里仍保留稳定失败，避免包装工具被直接
            # 调用时绕过 Candidate Registry。
            raise ValueError("未知候选编号，无法读取页面。")
        result = await read_page_tool.ainvoke(
            {
                "type": "tool_call",
                "name": "read_page",
                "args": {"url": candidate.url},
                "id": f"internal-{candidate.candidate_id}",
            }
        )
        if isinstance(result, ToolMessage) and result.status == "error":
            raise ToolException("该候选页面未能获得可信正文。")
        page = _coerce_read_page_result(result)
        return json.dumps(page, ensure_ascii=False), page

    candidate_tool = StructuredTool.from_function(
        coroutine=read_candidate,
        name="read_candidate",
        description=(
            "读取一个已经发现的候选页面。输入 candidate_id；只能读取用户输入、网页搜索"
            "或已读页面链接中出现的候选。返回该页面的真实网址、标题、主要内容、质量状态"
            "以及新发现的候选链接。网页搜索请使用 web_search。"
        ),
        args_schema=ReadCandidateInput,
        response_format="content_and_artifact",
        handle_tool_error=True,
    )
    web_search_tool = tools_by_name["web_search"].model_copy(
        update={
            "description": (
                "搜索公开网页，发现与查询词相关的候选页面。输入搜索词和最大结果数；"
                "返回每个候选的 candidate_id、标题、网址和摘要。搜索不读取正文；需要"
                "验证候选页面时使用 read_candidate。"
            )
        }
    )
    model_tools = [web_search_tool, candidate_tool]
    assert {tool.name for tool in model_tools} == MODEL_VISIBLE_TOOL_NAMES
    return model_tools


def _structured_content(message: ToolMessage) -> dict[str, Any] | None:
    """从 MCP 转换后的 ToolMessage artifact 中读取结构化工具结果。"""
    artifact = message.artifact
    if not isinstance(artifact, dict):
        return None
    content = artifact.get("structured_content")
    if isinstance(content, dict):
        return content
    # StructuredTool 的 artifact 可以直接是业务字典；MCP Tool 则使用上面的嵌套字段。
    return artifact


def _replace_tool_payload(message: ToolMessage, payload: dict[str, Any]) -> ToolMessage:
    """同时更新模型可见文本与程序可读 artifact，避免两份结果不一致。"""
    artifact = dict(message.artifact) if isinstance(message.artifact, dict) else {}
    artifact["structured_content"] = payload
    return message.model_copy(
        update={
            "content": json.dumps(payload, ensure_ascii=False),
            "artifact": artifact,
        }
    )


def _read_candidate_payload(
    candidate: Candidate,
    page: ReadPageResult,
    discovered_candidates: Sequence[dict[str, str]],
) -> dict[str, Any]:
    """生成模型可见的读取结果，隐藏原始 href 列表并提供可直接选择的候选编号。"""
    return {
        "candidate_id": candidate.candidate_id,
        "requested_url": page["requested_url"],
        "final_url": page["final_url"],
        "title": page["title"],
        "main_content": page["main_content"],
        "headings": page["headings"],
        "acquisition_method": page["acquisition_method"],
        "quality_status": page["quality_status"],
        "discovered_candidates": list(discovered_candidates),
    }


def _tool_error_for_model(message: ToolMessage, tool_name: str) -> ToolMessage:
    """把底层失败转换成模型能够采取下一步行动的有限错误信息。"""
    if tool_name == "web_search":
        content = (
            "网页搜索暂时未获得结果。请调整搜索词后重试；如果无法继续，请基于已有证据"
            "收敛，并在 remaining_gaps 中说明具体未确认范围。"
        )
    else:
        content = (
            "该页面未能获得可信正文，请不要把它加入最终来源。可读取其他真实候选页面，"
            "或在 remaining_gaps 中说明具体未确认范围。"
        )
    return message.model_copy(update={"content": content, "artifact": None, "status": "error"})


def _tool_call_signature(tool_name: str, arguments: Any) -> str:
    """为确定性重复判断生成稳定签名，不进行语义相似度推断。"""
    return f"{tool_name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"


def _finalization_tool_message(request, reason: str) -> ToolMessage:
    """告知模型探索已经结束；下一次模型调用将看不到搜索和读取工具。"""
    return ToolMessage(
        content=(
            "探索已达到确定性边界，后续不再提供搜索或网页读取工具。请仅根据当前已有"
            "真实证据生成最终结构化结果；如果仍有重要未确认范围，必须返回 "
            "discovery_complete=false 并如实填写 remaining_gaps。"
        ),
        name=request.tool_call["name"],
        tool_call_id=request.tool_call["id"],
        status="error",
        artifact={"finalization_reason": reason},
    )


def _search_closed_tool_message(request, reason: str) -> ToolMessage:
    """拒绝搜索关闭后残留的搜索调用，同时提醒模型仍可读取已有候选页面。"""
    return ToolMessage(
        content=(
            "网页搜索已达到确定性边界，后续不再提供搜索工具。你仍可使用 "
            "read_candidate 读取和验证已经发现的候选，也可以直接基于已验证证据生成"
            "最终结果。"
        ),
        name=request.tool_call["name"],
        tool_call_id=request.tool_call["id"],
        status="error",
        artifact={"search_closed_reason": reason},
    )


def _build_finalization_evidence_summary(program_state: DiscoveryProgramState) -> str:
    """根据成功读取记录生成最终来源白名单，不复制网页正文或内部实现信息。"""
    verified_pages: list[dict[str, str]] = []
    seen_candidate_ids: set[str] = set()
    for candidate in program_state.candidate_registry:
        page = program_state.cached_page(candidate.url)
        if not candidate.read_successful or page is None:
            continue
        if candidate.candidate_id in seen_candidate_ids:
            continue
        seen_candidate_ids.add(candidate.candidate_id)
        verified_pages.append(
            {
                "candidate_id": candidate.candidate_id,
                "final_url": page["final_url"],
                "title": page["title"],
            }
        )
    return (
        "最终收敛证据白名单：selected_sources 只能使用以下已经成功读取并验证的 "
        "candidate_id。未出现在该列表中的候选不得进入最终选择。\n"
        + json.dumps({"verified_pages": verified_pages}, ensure_ascii=False)
    )


def _finalization_evidence_message(
    program_state: DiscoveryProgramState,
) -> HumanMessage:
    """创建一次性的收敛白名单消息，并保持原 System Prompt 不变。"""
    return HumanMessage(content=_build_finalization_evidence_summary(program_state))


def _runtime_tool_name(tool: BaseTool | dict[str, Any]) -> str | None:
    """读取模型调用中间件中的工具名称，用于只隐藏 web_search。"""
    if isinstance(tool, BaseTool):
        return tool.name
    name = tool.get("name")
    if isinstance(name, str):
        return name
    function = tool.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return None


def build_runtime_middleware(program_state: DiscoveryProgramState):
    """跟踪工具证据，并分别执行搜索关闭与完整强制收敛。"""

    @wrap_model_call(name="SourceDiscoveryFinalizationMiddleware")
    async def filter_tools_during_finalization(request, handler):
        if program_state.finalization_mode:
            request = request.override(
                tools=[],
                messages=[
                    *request.messages,
                    _finalization_evidence_message(program_state),
                ],
            )
        elif program_state.search_closed:
            request = request.override(
                tools=[
                    tool
                    for tool in request.tools
                    if _runtime_tool_name(tool) != "web_search"
                ]
            )
        return await handler(request)

    @wrap_tool_call(name="SourceDiscoveryRuntimeMiddleware")
    async def track_tool_call(request, handler):
        tool_name = request.tool_call["name"]
        args = request.tool_call.get("args", {})
        signature = _tool_call_signature(tool_name, args)
        program_state.record_tool_call()

        if program_state.finalization_mode:
            return _finalization_tool_message(
                request,
                program_state.finalization_reason or "finalization_mode",
            )

        if program_state.was_rejected(signature):
            program_state.enter_finalization("repeated_rejected_tool_call")
            return _finalization_tool_message(request, "repeated_rejected_tool_call")

        if program_state.tool_call_count >= MAX_TOOL_CALL_COUNT:
            program_state.reject_tool_call(signature)
            program_state.enter_finalization("total_tool_call_limit")
            return _finalization_tool_message(request, "total_tool_call_limit")

        if tool_name == "web_search":
            if program_state.search_closed or program_state.search_count >= MAX_SEARCH_COUNT:
                program_state.reject_tool_call(signature)
                reason = program_state.search_closed_reason or "search_limit"
                program_state.close_search(reason)
                return _search_closed_tool_message(request, reason)
            # 只有真正交给 MCP Provider 执行的调用才计入 search_count。
            program_state.search_count += 1

        if tool_name == "read_candidate":
            candidate_id = args.get("candidate_id")
            candidate = (
                program_state.candidate_by_id(candidate_id)
                if isinstance(candidate_id, str)
                else None
            )
            if candidate is None:
                program_state.reject_tool_call(signature)
                return ToolMessage(
                    content=(
                        "该 candidate_id 不存在。请从用户输入、搜索结果或页面链接返回的"
                        "真实候选编号中选择。"
                    ),
                    name=tool_name,
                    tool_call_id=request.tool_call["id"],
                    status="error",
                )
            cached = program_state.cached_page(candidate.url)
            if cached is not None:
                payload = _read_candidate_payload(
                    candidate,
                    cached,
                    program_state.candidates_discovered_from(cached["final_url"]),
                )
                return ToolMessage(
                    content=json.dumps(payload, ensure_ascii=False),
                    name=tool_name,
                    tool_call_id=request.tool_call["id"],
                    artifact={"structured_content": payload},
                )
            if program_state.read_count >= MAX_READ_COUNT:
                program_state.reject_tool_call(signature)
                program_state.enter_finalization("read_limit")
                return _finalization_tool_message(request, "read_limit")
            # 缓存命中和非法候选编号都没有进入真实网页读取，因此不计入 read_count。
            program_state.read_count += 1

        result = await handler(request)
        if tool_name == "web_search" and program_state.search_count >= MAX_SEARCH_COUNT:
            program_state.close_search("search_limit")
        if tool_name == "read_candidate" and program_state.read_count >= MAX_READ_COUNT:
            program_state.enter_finalization("read_limit")
        if not isinstance(result, ToolMessage):
            return result
        if result.status == "error":
            return _tool_error_for_model(result, tool_name)

        payload = _structured_content(result)
        if payload is None:
            return result

        if tool_name == "web_search":
            raw_results = payload.get("results", [])
            results = raw_results if isinstance(raw_results, list) else []
            filtered_payload = {
                **payload,
                "results": program_state.add_search_results(results),
            }
            return _replace_tool_payload(result, filtered_payload)

        if tool_name == "read_candidate":
            candidate_id = args.get("candidate_id")
            candidate = program_state.candidate_by_id(candidate_id)
            if candidate is None:
                return result
            page = payload  # type: ignore[assignment]
            discovered = program_state.record_successful_page(page, candidate)
            return _replace_tool_payload(
                result,
                _read_candidate_payload(candidate, page, discovered),
            )
        return result

    return [filter_tools_during_finalization, track_tool_call]


def create_source_discovery_agent(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    program_state: DiscoveryProgramState,
):
    """组合模型、MCP 工具、硬限制和 ToolStrategy，创建单次运行 Agent。"""
    middleware = build_runtime_middleware(program_state)
    model_visible_tools = build_model_visible_tools(tools, program_state)
    return create_agent(
        model=model,
        tools=model_visible_tools,
        system_prompt=SOURCE_DISCOVERY_SYSTEM_PROMPT,
        middleware=middleware,
        response_format=ToolStrategy(
            SourceDiscoveryDecision,
            handle_errors=_StructuredOutputRepairLimiter(),
        ),
    )


def materialize_source_discovery_result(
    decision: SourceDiscoveryDecision,
    program_state: DiscoveryProgramState,
) -> SourceDiscoveryResult:
    """用成功读取证据把模型业务决策转换为稳定的对外结果。

    输入：只包含 candidate_id、来源类型和理由的模型决策，以及本次 Candidate Registry。
    处理：逐项确认候选真实存在且成功读取，再复制完整 final_url，并按“采集标题、
    Candidate Registry 标题、final_url”顺序确定展示标题。
    输出：保持既有产品契约的 SourceDiscoveryResult；未读或未知候选会明确失败。
    """
    sources: list[MonitoringSource] = []
    for selected in decision.selected_sources:
        candidate = program_state.candidate_by_id(selected.candidate_id)
        if candidate is None:
            raise EvidenceValidationError(
                f"最终来源候选不存在：{selected.candidate_id}"
            )
        page = program_state.cached_page(candidate.url)
        if not candidate.read_successful or page is None:
            raise EvidenceValidationError(
                f"最终来源候选缺少成功页面读取证据：{selected.candidate_id}"
            )
        final_url = page.get("final_url")
        if not isinstance(final_url, str) or not final_url.strip():
            raise EvidenceValidationError(
                f"最终来源候选缺少真实最终网址：{selected.candidate_id}"
            )
        title = _resolve_materialized_title(page, candidate)
        sources.append(
            MonitoringSource(
                url=final_url,
                title=title,
                source_type=selected.source_type,
                monitoring_reason=selected.monitoring_reason,
            )
        )

    return SourceDiscoveryResult(
        competitor_name=decision.competitor_name,
        official_domains=decision.official_domains,
        sources=sources,
        discovery_complete=decision.discovery_complete,
        remaining_gaps=decision.remaining_gaps,
    )


def validate_result_evidence(
    result: SourceDiscoveryResult,
    program_state: DiscoveryProgramState,
) -> SourceDiscoveryResult:
    """确定性核验最终来源确实来自真实候选并完成过页面读取。

    校验不会替模型改写业务结论。来源必须匹配成功读取的 final_url，标题必须等于程序
    按同一 fallback 规则得到的展示标题；只有完全相同的最终 URL 会在校验后做基础去重。
    """
    verified_sources = []
    seen_urls: set[str] = set()
    for source in result.sources:
        matching_pages = [
            page
            for page in program_state.successful_pages
            if page["final_url"] == source.url
            and page["requested_url"]
            in (program_state.input_urls | program_state._candidate_url_set)
        ]
        if not matching_pages:
            raise EvidenceValidationError(
                f"最终来源缺少成功页面读取证据：{source.url}"
            )
        matching_titles = []
        for page in matching_pages:
            candidate = next(
                (
                    item
                    for item in program_state.candidate_registry
                    if item.url == page["requested_url"] and item.read_successful
                ),
                None,
            )
            matching_titles.append(_resolve_materialized_title(page, candidate))
        if source.title not in matching_titles:
            raise EvidenceValidationError(
                f"最终来源标题与确定性展示标题不一致：{source.url}"
            )
        if source.url in seen_urls:
            continue
        seen_urls.add(source.url)
        verified_sources.append(source)
    return result.model_copy(update={"sources": verified_sources})


def _resolve_materialized_title(
    page: ReadPageResult,
    candidate: Candidate | None,
) -> str:
    """为 MonitoringSource 生成不依赖模型的展示标题。

    输入：成功读取的页面记录，以及与该读取对应的 Candidate Registry 条目。
    处理：Stage 1 Adapter 已在 ``page.title`` 内按真实页面标题、HTML title、og:title、
    twitter:title 完成同次采集回退；本函数继续尝试 Registry 已有 title，最后直接使用
    真实 final_url。URL 兜底虽不如自然语言标题友好，但来源明确、稳定且不会引入猜测。
    输出：一定非空的展示标题；缺少 final_url 的异常由 Materialize 硬条件提前拒绝。
    """
    page_title = page.get("title")
    if isinstance(page_title, str) and page_title.strip():
        return " ".join(page_title.split())
    if candidate is not None and isinstance(candidate.title, str) and candidate.title.strip():
        return " ".join(candidate.title.split())
    final_url = page.get("final_url")
    return final_url.strip() if isinstance(final_url, str) else ""


def _build_user_message(
    competitor_name: str,
    official_url: str | None,
    additional_urls: Sequence[str],
    initial_candidates: Sequence[Candidate] = (),
) -> str:
    """把单个竞品输入整理为简洁、无底层实现信息的用户消息。"""
    official = official_url or "未提供"
    additional = "、".join(additional_urls) if additional_urls else "未提供"
    candidate_text = (
        json.dumps(
            [candidate.model_view() for candidate in initial_candidates],
            ensure_ascii=False,
        )
        if initial_candidates
        else "[]"
    )
    return (
        "请为以下单个竞品执行来源发现，并基于真实工具结果返回结构化结果。\n"
        f"竞品名称：{competitor_name}\n"
        f"官网网址：{official}\n"
        f"补充网址：{additional}\n"
        f"已注册候选：{candidate_text}"
    )


async def run_source_discovery(
    competitor_name: str,
    official_url: str | None = None,
    additional_urls: Sequence[str] | None = None,
    *,
    mcp_target: Any = None,
    program_state: DiscoveryProgramState | None = None,
) -> SourceDiscoveryResult:
    """运行一次单竞品 Source Discovery Agent，并返回通过证据校验的结果。

    输入：竞品名称、可选官网和补充网址；测试可注入 MCP target 与运行状态。
    处理：加载环境配置，通过第一方 MCPAdapter 获取工具，运行 Agent，最后执行确定性
    Schema、证据和 URL 去重校验。输出：SourceDiscoveryResult；任一证据不成立则明确失败。
    """
    cleaned_name = competitor_name.strip()
    if not cleaned_name:
        raise ValueError("competitor_name 不能为空")
    cleaned_official_url = official_url.strip() if official_url else None
    cleaned_additional_urls = [url.strip() for url in additional_urls or [] if url.strip()]

    # 正常运行只接受本地环境配置，避免调用方临时覆盖模型而造成配置与实际运行不一致。
    load_dotenv()
    model = build_chat_model(load_model_config())

    state = program_state or DiscoveryProgramState()
    initial_candidates: list[Candidate] = []
    if cleaned_official_url:
        candidate = state.register_user_url(cleaned_official_url)
        if candidate is not None:
            initial_candidates.append(candidate)
    for url in cleaned_additional_urls:
        candidate = state.register_user_url(url)
        if candidate is not None and candidate not in initial_candidates:
            initial_candidates.append(candidate)

    target = mcp if mcp_target is None else mcp_target
    async with MCPAdapter(target) as adapter:
        tools = await load_source_discovery_tools(adapter)
        agent = create_source_discovery_agent(model, tools, state)
        agent_result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": _build_user_message(
                            cleaned_name,
                            cleaned_official_url,
                            cleaned_additional_urls,
                            initial_candidates,
                        ),
                    }
                ]
            },
            config={"recursion_limit": AGENT_RECURSION_LIMIT},
        )

    decision = SourceDiscoveryDecision.model_validate(
        agent_result["structured_response"]
    )
    materialized_result = materialize_source_discovery_result(decision, state)
    return validate_result_evidence(materialized_result, state)
