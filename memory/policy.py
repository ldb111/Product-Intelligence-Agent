"""确定性 Memory Write Policy：安全、来源、长期价值和事实边界。"""

from __future__ import annotations

import re

from .schemas import MemoryCandidate, MemoryPolicyError, MemorySourceKind

FACT_SOURCE_KINDS = {"event", "evidence", "snapshot", "knowledge", "pricing", "web_page"}

SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_ -]?key|secret|password|passwd|token|authorization)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
)

TYPE_SIGNALS = {
    "focus": ("关注", "重点", "优先", "focus", "prioritize"),
    # “优先”既可能表达研究焦点，也可能表达明确偏好；两者都是允许的最近合法类型。
    "preference": ("偏好", "倾向", "默认", "更喜欢", "优先", "prefer", "preference", "prioritize"),
    "constraint": ("必须", "仅限", "不要", "不得", "约束", "must", "only", "avoid"),
    "goal": ("目标", "计划", "持续", "希望", "goal", "plan"),
    "decision": ("决定", "选择", "采用", "decision", "decided"),
    "hypothesis": ("假设", "推测", "可能", "认为", "hypothesis", "may"),
    "open_question": ("待确认", "尚未", "需要确认", "是否", "问题", "open question", "unresolved"),
}

TEMPORARY_OPERATION = re.compile(
    r"(?i)(?:临时|这次先|现在先|今天先|本次仅).{0,20}(?:运行|重跑|导出|下载|打开|关闭|提交|删除|安装|测试)|"
    r"(?:run|retry|export|download|open|close|commit|delete|install|test).{0,20}(?:this time|today|temporarily)"
)

FACT_ONLY = re.compile(
    r"(?i)^(?:.{0,30})(?:支持|上线|发布|定价|售价|每月|价格|版本|released|supports|pricing|costs)(?:.{0,80})$"
)


def contains_secret(value: str) -> bool:
    """只返回是否命中常见认证信息形态，不回显秘密内容。"""
    return any(pattern.search(value) for pattern in SECRET_PATTERNS)


def validate_source_before_model(source_text: str, source_reference: str | None = None) -> None:
    """在文本进入 Fast LLM 前执行安全门槛；成功无返回值。

    输入为本次有限用户/研究文本和可选来源引用。拒绝空白、过长和认证信息，避免
    Secret 即使最终不写库也已发送给外部模型。错误只说明可行动原因，不回显原文。
    """
    if not isinstance(source_text, str) or not source_text.strip():
        raise MemoryPolicyError("Memory 来源文本不能为空。")
    if len(source_text) > 4000:
        raise MemoryPolicyError("Memory 来源文本过长；请只提供直接相关的有限片段。")
    if contains_secret(source_text) or source_reference and contains_secret(source_reference):
        raise MemoryPolicyError("检测到认证或秘密信息，禁止进入 Memory 或模型输入。")


def validate_candidate(candidate: MemoryCandidate, *, source_text: str,
                       source_kind: MemorySourceKind) -> None:
    """验证一个 Candidate 是否可作为长期上下文；成功无返回值。

    程序要求候选逐字存在、来源不是事实仓库、具有对应长期语义线索，且不属于临时
    操作或明显事实。规则有意保守：无法确定时拒绝写入，而不是让模型扩大语义。
    """
    validate_source_before_model(source_text)
    if source_kind in FACT_SOURCE_KINDS:
        raise MemoryPolicyError("Structured State、Knowledge 或网页事实不得复制进 Memory。")
    if candidate.content not in source_text:
        raise MemoryPolicyError("候选内容不是来源文本中的逐字片段。")
    if contains_secret(candidate.content):
        raise MemoryPolicyError("候选包含认证或秘密信息。")
    if TEMPORARY_OPERATION.search(candidate.content):
        raise MemoryPolicyError("一次性临时操作不得写入长期记忆。")
    lowered_source = source_text.casefold()
    if not any(signal.casefold() in lowered_source for signal in TYPE_SIGNALS[candidate.memory_type]):
        raise MemoryPolicyError("来源没有直接表达该 Memory 类型的长期语义。")
    # 有关注/偏好等明确线索时，句中提到能力或市场仍是研究上下文；没有线索的事实句拒绝。
    if FACT_ONLY.fullmatch(candidate.content) and not any(
        signal.casefold() in candidate.content.casefold() for signals in TYPE_SIGNALS.values() for signal in signals
    ):
        raise MemoryPolicyError("明显事实知识不得作为长期记忆。")
