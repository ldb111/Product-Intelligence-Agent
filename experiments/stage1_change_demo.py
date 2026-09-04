"""人工演示 Stage 1 如何识别 Kimi Membership 的表格单元格变化。

本脚本只在内存中构造前后两份 Snapshot（快照），不会请求网页，也不会保存文件。
Canonical Content（规范文本）、Content Hash（内容哈希）、Change Detection（变化检测）
和 Contextual Diff（带上下文差异）全部复用 backend 中已经通过测试的生产实现。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


# 直接运行 experiments 下的脚本时，Python 默认从 experiments 目录查找模块。
# 将项目根目录加入搜索路径后，脚本才能复用 backend，而不需要复制正式业务逻辑。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.change_detection import detect_change  # noqa: E402
from backend.content_diff import build_diff_result  # noqa: E402
from backend.snapshot import EXTRACTION_VERSION, compute_content_hash  # noqa: E402
from backend.structured_content import serialize_blocks  # noqa: E402


DEMO_URL = "https://www.kimi.com/en/help/membership/membership-overview"
TABLE_HEADERS = ["Feature", "Andante", "Moderato", "Allegretto", "Allegro"]


def build_membership_blocks(allegro_project_limit: str) -> list[dict[str, Any]]:
    """构造演示需要的 Kimi Membership 标题和表格结构。

    输入：Allegro 套餐的项目数量。
    处理：保持标题、表头和其他套餐数据不变，只把输入值放入 Allegro 对应单元格。
    输出：可直接交给现有 Structured Content 和 Contextual Diff 的 Blocks 列表。
    """
    return [
        {
            "type": "heading",
            "level": 1,
            "text": "Kimi Membership",
            "links": [],
        },
        {
            "type": "heading",
            "level": 2,
            "text": "Membership plans",
            "links": [],
        },
        {
            "type": "table",
            "headers": TABLE_HEADERS,
            "rows": [
                ["Number of projects", "20", "20", "20", allegro_project_limit]
            ],
        },
    ]


def build_demo_snapshot(
    blocks: list[dict[str, Any]], captured_at: str
) -> dict[str, Any]:
    """使用现有 Stage 1 能力创建一份不落盘的演示快照。

    输入：Structured Blocks 和带时区的采集时间。
    处理：由现有 serialize_blocks 生成 content，再由现有 SHA-256 函数计算 content_hash；
    前后快照使用同一个 EXTRACTION_VERSION，确保变化判断来自正文而非抽取算法切换。
    输出：可交给 detect_change 的 Snapshot 字典，不执行文件保存。
    """
    content = serialize_blocks(blocks)
    return {
        "url": DEMO_URL,
        "requested_url": DEMO_URL,
        "final_url": DEMO_URL,
        "status_code": 200,
        "title": "Kimi Membership Plans - Kimi Help Center",
        "content": content,
        "blocks": blocks,
        "acquisition_method": "demo",
        "captured_at": captured_at,
        "content_hash": compute_content_hash(content),
        "extraction_version": EXTRACTION_VERSION,
    }


def build_demo_result() -> dict[str, Any]:
    """串联现有 Change Detection 和 Diff，生成最终人工演示结果。

    输入：无；场景固定为 Allegro 的 Number of projects 从 100 变为 200。
    处理：构造同版本前后快照，调用 detect_change 比较已有哈希，再调用
    build_diff_result 生成原有行级 diff 和 contextual_diff。
    输出：只保留人工验收要求的五个 JSON 字段。
    """
    previous_snapshot = build_demo_snapshot(
        build_membership_blocks("100"),
        "2026-09-03T10:00:00+08:00",
    )
    current_snapshot = build_demo_snapshot(
        build_membership_blocks("200"),
        "2026-09-03T11:00:00+08:00",
    )

    change_result = detect_change(
        {
            "is_first_scan": False,
            "previous_snapshot": previous_snapshot,
            "current_snapshot": current_snapshot,
        }
    )
    diff_result = build_diff_result(change_result)

    return {
        "changed": diff_result["changed"],
        "previous_content_hash": diff_result["previous_content_hash"],
        "current_content_hash": diff_result["current_content_hash"],
        "diff": diff_result["diff"],
        "contextual_diff": diff_result["contextual_diff"],
    }


def main() -> int:
    """将演示结果输出为 UTF-8 友好的合法 JSON，并返回成功退出码。"""
    print(json.dumps(build_demo_result(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
