"""临时文本损坏诊断工具的本地自动化测试。"""

from __future__ import annotations

import unittest

from experiments.text_damage_diagnostic import (
    OCTOREPORT_TARGET_PAGES,
    build_stage_representations,
    diagnose_target,
)


class TextDamageDiagnosticTests(unittest.TestCase):
    """使用可控 HTML 验证阶段检查和首次损坏定位，不访问外部网站。"""

    def test_octoreport_configuration_contains_two_pages_and_twelve_targets(
        self,
    ) -> None:
        """固定本轮真实诊断范围，避免漏查页面或目标文本。"""
        self.assertEqual(len(OCTOREPORT_TARGET_PAGES), 2)
        self.assertEqual(
            sum(len(page["targets"]) for page in OCTOREPORT_TARGET_PAGES),
            12,
        )

    def test_detects_text_first_removed_with_noise_tag(self) -> None:
        html = (
            "<html><body><nav>导航目标文本</nav>"
            "<main>始终保留文本</main></body></html>"
        ).encode("utf-8")
        stages = build_stage_representations(html)

        removed_result = diagnose_target("导航目标文本", stages)
        preserved_result = diagnose_target("始终保留文本", stages)

        self.assertTrue(removed_result["stages"]["B_initial_soup"]["exists_complete"])
        self.assertFalse(
            removed_result["stages"]["C_noise_removed_soup"]["exists_complete"]
        )
        self.assertEqual(
            removed_result["first_damage_stage"], "C_noise_removed_soup"
        )
        self.assertIsNone(preserved_result["first_damage_stage"])
        self.assertTrue(
            preserved_result["stages"]["E_normalized_content"]["exists_complete"]
        )
        self.assertEqual(
            preserved_result["stages"]["E_normalized_content"]["occurrence_count"],
            1,
        )
        self.assertIn(
            "始终保留文本",
            preserved_result["stages"]["E_normalized_content"]["nearby_snippet"],
        )

    def test_reports_snippet_and_normalization_stage_loss(self) -> None:
        html = b"<html><body><main>Alpha  Beta</main></body></html>"
        stages = build_stage_representations(html)

        result = diagnose_target("Alpha  Beta", stages)

        self.assertTrue(result["stages"]["D_raw_text"]["exists_complete"])
        self.assertFalse(
            result["stages"]["E_normalized_content"]["exists_complete"]
        )
        self.assertEqual(result["first_damage_stage"], "E_normalized_content")
        self.assertIsNotNone(
            result["stages"]["E_normalized_content"]["nearby_snippet"]
        )

    def test_structured_content_repairs_html_node_split_without_character_loss(
        self,
    ) -> None:
        """A/D 的节点拆分应在 Structured Blocks 生成的 E 阶段重新连续。"""
        html = "<p>适用于<strong>批量爬取多个页面</strong>的场景</p>".encode(
            "utf-8"
        )
        stages = build_stage_representations(html)

        result = diagnose_target("适用于批量爬取多个页面的场景", stages)

        self.assertFalse(result["stages"]["A_raw_html"]["exists_complete"])
        self.assertFalse(result["stages"]["D_raw_text"]["exists_complete"])
        self.assertTrue(
            result["stages"]["E_normalized_content"]["exists_complete"]
        )
        self.assertTrue(
            result["stages"]["D_raw_text"]["exists_ignoring_whitespace"]
        )
        self.assertTrue(result["source_nodes_join_to_target"])
        self.assertIsNone(result["first_damage_stage"])
        self.assertIn("开始完整出现", result["diagnosis"])
