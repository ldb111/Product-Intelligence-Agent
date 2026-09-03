"""临时诊断真实网页文本在 Stage 1 各处理阶段是否发生损坏。

本文件不属于生产链路，也不会保存 Snapshot（页面快照）。它只复用现有网页请求、
噪声标签列表和 normalize_content，依次观察原始 HTML、BeautifulSoup 页面树、删除
噪声后的页面树、raw_text 和最终 content，方便定位文字从哪一步开始断裂或丢失。

诊断结束后可以整体删除本文件，不会影响现有业务模块。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, UnicodeDammit


# 直接运行 python experiments/text_damage_diagnostic.py 时，Python 默认只把 experiments
# 加入模块搜索路径。这里仅为临时脚本补入项目根目录，以便复用 backend 中的现有逻辑。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.page_reader import (  # noqa: E402 - 项目根目录需先加入搜索路径
    NOISE_TAG_NAMES,
    PageReadError,
    fetch_html,
    normalize_content,
    validate_url,
)


STAGE_LABELS = {
    "A_raw_html": "A. requests 返回的原始 HTML",
    "B_initial_soup": "B. BeautifulSoup 初次解析后的页面树",
    "C_noise_removed_soup": "C. 删除 NOISE_TAG_NAMES 后的页面树",
    "D_raw_text": 'D. soup.get_text("\\n") 生成的 raw_text',
    "E_normalized_content": "E. normalize_content 最终 content",
}

KIMI_TARGET_PAGES = (
    {
        "url": "https://www.kimi.com/products/",
        "targets": (
            "自动执行定时任务",
            "端到端处理复杂的多文件工程任务",
            "Kimi Platform",
        ),
    },
    {
        "url": "https://www.kimi.com/en/help/membership/membership-overview",
        "targets": (
            "Available",
            "Number of projects",
            "Goal Mode",
        ),
    },
)

OCTOREPORT_TARGET_PAGES = (
    {
        "url": "https://www.z-report.cn/changelog",
        "targets": (
            "第一时间看到更新",
            "隐私政策",
            "无据结论自动标红",
            "性能与可靠性批次",
            "LLM 多厂商路由修复",
            "来源可信度：可为信源",
        ),
    },
    {
        "url": "https://www.z-report.cn/docs/features/sources/web-email",
        "targets": (
            "数据源管理 - 总览",
            "原子计费机制",
            "若 Firecrawl 失败，自动降级至 Browserless",
            "适用于批量爬取多个页面的场景",
            "每个页面单独保存，自动去重",
            "提取邮件主题、发件人、正文内容",
        ),
    },
)

# 保留上一轮 Kimi 配置，并把不同网站的诊断目标按组管理。这样后续复查时只需选择
# 目标组，不必改动诊断算法，也不会把任何站点专用规则带进生产代码。
TARGET_PAGE_GROUPS = {
    "kimi": KIMI_TARGET_PAGES,
    "octoreport": OCTOREPORT_TARGET_PAGES,
}


def build_stage_representations(html: bytes) -> dict[str, str]:
    """从同一份原始 HTML 构造 A～E 五个可检查的文本表示。

    在诊断链路中的职责：把生产流程中关键边界的中间状态完整保留下来，使某个目标词
    在哪一步消失可以被逐阶段观察，而不是只看到最终 content。

    输入：fetch_html 返回的原始 HTML bytes（字节）。
    处理：解码原始 HTML；分别解析初始树和噪声删除树；生成 raw_text；最后调用当前
    生产函数 normalize_content。各阶段使用独立的 BeautifulSoup 对象，避免原地修改串扰。
    输出：按 A～E 阶段名称组织的字符串字典。
    """
    # UnicodeDammit 只负责根据字节序标记、HTML 声明等推断字符编码，便于把原始 bytes
    # 作为可读字符串检查；它不会执行 JavaScript，也不会改变生产解析流程。
    decoded_html = UnicodeDammit(html, is_html=True).unicode_markup
    raw_html = decoded_html if decoded_html is not None else html.decode(
        "utf-8", errors="replace"
    )

    initial_soup = BeautifulSoup(html, "html.parser")

    noise_removed_soup = BeautifulSoup(html, "html.parser")
    # 这里故意复现当前 normalize_content 的确定性标签删除步骤，用于单独观察阶段 C/D；
    # 不修改生产函数，也不加入任何 Kimi 专用规则。
    for noise_tag in noise_removed_soup.find_all(NOISE_TAG_NAMES):
        noise_tag.decompose()

    raw_text = noise_removed_soup.get_text("\n")

    # normalize_content 会原地删除标签，因此给它一棵新的页面树，确保前面保存的 B/C
    # 阶段仍保持原样，诊断结果不会因为对象被复用而失真。
    normalized_soup = BeautifulSoup(html, "html.parser")
    normalized_content = normalize_content(normalized_soup)

    return {
        "A_raw_html": raw_html,
        "B_initial_soup": str(initial_soup),
        "C_noise_removed_soup": str(noise_removed_soup),
        "D_raw_text": raw_text,
        "E_normalized_content": normalized_content,
    }


def _find_nearby_snippet(target: str, stage_text: str, radius: int = 100) -> str:
    """优先定位完整目标，否则查找最长局部匹配，并返回其附近上下文。"""
    match_index = -1
    matched_fragment = ""

    # 从完整目标和最长子串开始尝试，首次命中就是最接近目标的位置。目标文字通常很短，
    # 这种简单查找足以诊断，同时避免引入模糊搜索第三方依赖。
    for fragment_length in range(len(target), 0, -1):
        for start in range(0, len(target) - fragment_length + 1):
            fragment = target[start : start + fragment_length].strip()
            if not fragment:
                continue
            candidate_index = stage_text.find(fragment)
            if candidate_index != -1:
                match_index = candidate_index
                matched_fragment = fragment
                break
        if match_index != -1:
            break

    if match_index == -1:
        return "[未找到目标文本的可定位片段]"

    snippet_start = max(0, match_index - radius)
    snippet_end = min(
        len(stage_text), match_index + len(matched_fragment) + radius
    )
    snippet = stage_text[snippet_start:snippet_end]
    # 把换行显示为可见的 \n，输出 JSON 后更容易观察文字是否被换行或标签切断。
    return snippet.replace("\r", "\\r").replace("\n", "\\n")


def _remove_whitespace(text: str) -> str:
    """删除诊断字符串中的空白，用于区分“节点换行”和真正的字符损坏。

    这只是辅助判断：精确命中仍以原始文本为准。若精确目标不存在，但去除空白后字符
    顺序完整，说明更可能是换行或空格把文本隔开，而不是字符被删除或重复。
    """
    return "".join(text.split())


def diagnose_target(target: str, stages: dict[str, str]) -> dict[str, Any]:
    """检查一个目标文本在 A～E 阶段的完整存在情况并定位首次损坏。

    输入：目标文本，以及 build_stage_representations 产生的五阶段字符串。
    处理：逐阶段做精确子串判断并统计出现次数；提取附近片段；寻找第一次“上一阶段
    存在、下一阶段不存在”或出现次数上升的转换。
    输出：每阶段命中、次数、片段，以及首次缺失、首次损坏、首次次数上升和诊断说明。
    """
    stage_results: dict[str, dict[str, Any]] = {}
    compact_target = _remove_whitespace(target)

    for stage_name, stage_text in stages.items():
        occurrence_count = stage_text.count(target)
        exists = occurrence_count > 0
        stage_results[stage_name] = {
            "label": STAGE_LABELS[stage_name],
            "exists_complete": exists,
            "occurrence_count": occurrence_count,
            # 该字段不能代替精确判断，只用于确认字符是否仍按原顺序存在、但被空白隔开。
            "exists_ignoring_whitespace": (
                compact_target in _remove_whitespace(stage_text)
            ),
            # 即使完整命中也输出附近片段，便于人工观察目标周围是否有字符或整段重复。
            "nearby_snippet": _find_nearby_snippet(target, stage_text),
        }

    stage_names = list(stages)
    first_missing_stage = next(
        (
            stage_name
            for stage_name in stage_names
            if not stage_results[stage_name]["exists_complete"]
        ),
        None,
    )
    first_damage_stage = None
    first_occurrence_increase_stage = None

    # “处理造成损坏”必须能观察到从完整存在变为不完整；如果原始 HTML 一开始就没有
    # 完整文本，不能把责任错误归给 BeautifulSoup 或 normalize_content。
    for previous_stage, current_stage in zip(stage_names, stage_names[1:]):
        if (
            stage_results[previous_stage]["exists_complete"]
            and not stage_results[current_stage]["exists_complete"]
        ):
            first_damage_stage = current_stage
            break

    # 完整目标出现次数增加不一定就是错误（页面可能本来有多处相同文案），但如果次数
    # 在某个处理步骤后上升，它是“重复由哪一步引入”的重要诊断信号，应明确输出。
    for previous_stage, current_stage in zip(stage_names, stage_names[1:]):
        if (
            stage_results[current_stage]["occurrence_count"]
            > stage_results[previous_stage]["occurrence_count"]
        ):
            first_occurrence_increase_stage = current_stage
            break

    present_stages = [
        stage_name
        for stage_name in stage_names
        if stage_results[stage_name]["exists_complete"]
    ]

    # 浏览器渲染时，相邻文本节点会视觉连续；但原始 HTML 字符串中间可能夹着 <strong>
    # 等标签。用空分隔符提取 A 阶段的文本，可以确认“完整句子是否只是被标签拆开”。
    source_nodes_join_to_target = target in BeautifulSoup(
        stages["A_raw_html"], "html.parser"
    ).get_text("")

    if first_damage_stage is not None:
        diagnosis = (
            f"目标文本首次在 {STAGE_LABELS[first_damage_stage]} 由完整变为不完整；"
            "请结合附近片段判断是标签删除、换行切分、字符丢失还是字符重复。"
        )
    elif not present_stages and source_nodes_join_to_target:
        diagnosis = (
            "A 阶段的原始 HTML 已用标签把目标句子拆成多个相邻文本节点，浏览器视觉上可"
            "连续显示；B/C 未造成新的字符变化。D 阶段 get_text(\"\\n\") 在节点边界加入"
            "换行，E 阶段保留该换行。去除空白后目标字符顺序完整，没有发现字符丢失或重复。"
        )
    elif not present_stages:
        diagnosis = (
            "目标文本从 A 阶段起就未完整出现，无法归因于后续解析或标准化；"
            "可能未包含在服务器 HTML、由 JavaScript 后续渲染、被转义或已在源数据中断裂。"
        )
    elif first_missing_stage is not None:
        first_present_stage = present_stages[0]
        diagnosis = (
            f"目标文本在较早表示中未以连续字面量出现，但从 "
            f"{STAGE_LABELS[first_present_stage]} 开始完整出现；未观察到后续处理造成的丢失。"
        )
    elif first_occurrence_increase_stage is not None:
        diagnosis = (
            f"目标文本在 A～E 均完整存在，但完整目标出现次数首次在 "
            f"{STAGE_LABELS[first_occurrence_increase_stage]} 上升；"
            "请结合各阶段片段判断是合理重复还是处理引入的重复。"
        )
    else:
        diagnosis = (
            "目标文本在 A～E 所有阶段均完整存在，未观察到处理阶段造成的断裂、丢失或"
            "完整目标次数上升；可结合附近片段继续检查局部字符重复。"
        )

    return {
        "target": target,
        "stages": stage_results,
        "first_missing_stage": first_missing_stage,
        "first_damage_stage": first_damage_stage,
        "first_occurrence_increase_stage": first_occurrence_increase_stage,
        "source_nodes_join_to_target": source_nodes_join_to_target,
        "diagnosis": diagnosis,
    }


def diagnose_page(url: str, targets: tuple[str, ...], timeout: float) -> dict[str, Any]:
    """请求一个真实页面，并对全部目标文本运行五阶段诊断。

    输入：页面 URL、目标文本元组和请求超时秒数。
    处理：复用生产代码完成 URL 校验和一次 HTTP 请求，再构造 A～E 中间表示并逐词检查。
    输出：包含 URL、HTTP 状态码和每个目标文本诊断结果的字典。
    """
    validated_url = validate_url(url)
    status_code, html = fetch_html(validated_url, timeout)
    stages = build_stage_representations(html)

    return {
        "url": validated_url,
        "status_code": status_code,
        "targets": [diagnose_target(target, stages) for target in targets],
    }


def main(argv: list[str] | None = None) -> int:
    """运行指定网站组的真实页面诊断，将合法 UTF-8 JSON 写入标准输出。"""
    parser = argparse.ArgumentParser(
        description="Diagnose text integrity across the Stage 1 HTML pipeline."
    )
    parser.add_argument(
        "--timeout", type=float, default=20.0, help="request timeout in seconds"
    )
    parser.add_argument(
        "--site",
        choices=tuple(TARGET_PAGE_GROUPS),
        default="octoreport",
        help="target page group (default: octoreport)",
    )
    args = parser.parse_args(argv)

    results: list[dict[str, Any]] = []
    has_error = False

    for page in TARGET_PAGE_GROUPS[args.site]:
        try:
            results.append(
                diagnose_page(page["url"], page["targets"], args.timeout)
            )
        except PageReadError as exc:
            has_error = True
            results.append(
                {
                    "url": page["url"],
                    "error": {"type": exc.error_type, "message": str(exc)},
                }
            )

    # Windows 终端可能默认使用 GBK，遇到网页中的 Emoji 会抛出 UnicodeEncodeError。
    # 诊断输出统一使用 UTF-8，既保证 JSON 可完整写出，也不会改变任何网页处理结果。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"pages": results}, ensure_ascii=False, indent=2))
    return 1 if has_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
