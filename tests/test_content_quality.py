"""Stage 1 V0.2-2 Content Quality Gate（内容质量门槛）测试。"""

from __future__ import annotations

import unittest

from backend.content_quality import evaluate_content_quality


class ContentQualityTests(unittest.TestCase):
    """用可控 content/blocks 验证确定性规则，不依赖网络或真实网站。"""

    def test_empty_content_fails(self) -> None:
        result = evaluate_content_quality(
            "", [{"type": "paragraph", "text": "结构中仍有文字", "links": []}]
        )

        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["downstream_allowed"])
        self.assertIn("empty_content", [reason["code"] for reason in result["reasons"]])

    def test_empty_blocks_fail_even_when_content_exists(self) -> None:
        result = evaluate_content_quality("存在但无法追溯到 Block 的正文", [])

        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["downstream_allowed"])
        self.assertIn(
            "no_valid_blocks", [reason["code"] for reason in result["reasons"]]
        )

    def test_casee_short_loading_content_fails_as_dynamic_placeholder(self) -> None:
        content = "CaSee 凯见 加载中"
        blocks = [{"type": "paragraph", "text": content, "links": []}]

        result = evaluate_content_quality(content, blocks)

        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["downstream_allowed"])
        self.assertTrue(result["browser_fallback_recommended"])
        self.assertEqual(result["reasons"][0]["code"], "dynamic_placeholder")

    def test_short_chinese_or_english_loading_placeholder_fails(self) -> None:
        for content in ("产品页面正在加载", "Loading", "Please wait"):
            with self.subTest(content=content):
                result = evaluate_content_quality(
                    content,
                    [{"type": "paragraph", "text": content, "links": []}],
                )

                self.assertEqual(result["status"], "FAIL")
                self.assertEqual(result["reasons"][0]["code"], "dynamic_placeholder")

    def test_loading_word_in_long_normal_page_does_not_fail(self) -> None:
        content = (
            "Loading 状态只用于说明产品交互。"
            + "这里是已经由服务器返回的完整产品功能、价格、使用方法和限制说明。" * 10
        )
        blocks = [{"type": "paragraph", "text": content, "links": []}]

        result = evaluate_content_quality(content, blocks)

        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["downstream_allowed"])
        self.assertEqual(result["reasons"], [])

    def test_sparse_content_without_placeholder_returns_warning(self) -> None:
        content = "简短的产品介绍"
        blocks = [{"type": "paragraph", "text": content, "links": []}]

        result = evaluate_content_quality(content, blocks)

        self.assertEqual(result["status"], "WARNING")
        self.assertFalse(result["downstream_allowed"])
        self.assertTrue(result["browser_fallback_recommended"])
        self.assertEqual(result["reasons"][0]["code"], "sparse_content")

    def test_structured_normal_page_passes_with_expected_metrics(self) -> None:
        blocks = [
            {"type": "paragraph", "text": "完整产品正文", "links": []},
            {
                "type": "list",
                "ordered": False,
                "items": [{"text": "功能 A", "links": [], "children": []}],
            },
            {
                "type": "table",
                "headers": ["套餐", "价格"],
                "rows": [["专业版", "99 元"]],
            },
        ]
        content = "完整产品正文\n\n- 功能 A\n\n| 套餐 | 价格 |\n| 专业版 | 99 元 |"

        result = evaluate_content_quality(content, blocks)

        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["downstream_allowed"])
        self.assertFalse(result["browser_fallback_recommended"])
        self.assertEqual(result["metrics"]["block_count"], 3)
        self.assertEqual(result["metrics"]["paragraph_count"], 1)
        self.assertEqual(result["metrics"]["list_count"], 1)
        self.assertEqual(result["metrics"]["table_count"], 1)


if __name__ == "__main__":
    unittest.main()
