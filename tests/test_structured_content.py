"""Stage 1 V0.2-1 Structured Blocks（结构化内容块）的自动化测试。"""

from __future__ import annotations

import unittest

from backend.page_reader import extract_page_data_with_blocks


class StructuredContentTests(unittest.TestCase):
    """使用可控本地 HTML 验证结构提取，不依赖真实网站和网络。"""

    def _extract(self, html: str) -> tuple[str, list[dict[str, object]]]:
        """简化测试调用，返回由同一组 blocks 生成的 content 和 blocks。"""
        _, content, blocks = extract_page_data_with_blocks(html.encode("utf-8"))
        return content, blocks

    def test_chinese_inline_elements_do_not_split_sentence(self) -> None:
        content, blocks = self._extract(
            "<p>适用于<strong>批量爬取多个页面</strong>的场景</p>"
        )

        self.assertEqual(
            blocks,
            [
                {
                    "type": "paragraph",
                    "text": "适用于批量爬取多个页面的场景",
                    "links": [],
                }
            ],
        )
        self.assertEqual(content, "适用于批量爬取多个页面的场景")

    def test_english_word_split_by_span_is_rejoined(self) -> None:
        content, blocks = self._extract("<p>Av<span>ail</span>able</p>")

        self.assertEqual(blocks[0]["text"], "Available")
        self.assertEqual(content, "Available")

    def test_table_preserves_headers_and_rows(self) -> None:
        content, blocks = self._extract(
            """
            <table>
              <tr><th>Feature</th><th>Andante</th><th>Moderato</th><th>Allegretto</th><th>Allegro</th></tr>
              <tr><td>Number of projects</td><td>20</td><td>20</td><td>20</td><td>100</td></tr>
            </table>
            """
        )

        self.assertEqual(
            blocks[0],
            {
                "type": "table",
                "headers": [
                    "Feature",
                    "Andante",
                    "Moderato",
                    "Allegretto",
                    "Allegro",
                ],
                "rows": [["Number of projects", "20", "20", "20", "100"]],
            },
        )
        self.assertIn("| Feature | Andante | Moderato | Allegretto | Allegro |", content)
        self.assertIn("| Number of projects | 20 | 20 | 20 | 100 |", content)

    def test_nested_list_preserves_parent_child_relationship(self) -> None:
        content, blocks = self._extract(
            """
            <ul>
              <li>父项
                <ol>
                  <li>子项
                    <ul><li>孙项</li></ul>
                  </li>
                </ol>
              </li>
              <li>同级项</li>
            </ul>
            """
        )

        top_list = blocks[0]
        child_list = top_list["items"][0]["children"][0]
        grandchild_list = child_list["items"][0]["children"][0]

        self.assertFalse(top_list["ordered"])
        self.assertEqual(top_list["items"][0]["text"], "父项")
        self.assertTrue(child_list["ordered"])
        self.assertEqual(child_list["items"][0]["text"], "子项")
        self.assertEqual(grandchild_list["items"][0]["text"], "孙项")
        self.assertIn("- 父项\n  1. 子项\n    - 孙项", content)

    def test_pre_code_is_kept_as_one_block_with_layout(self) -> None:
        content, blocks = self._extract(
            "<pre><code>def <span>hello</span>():\n    return 1\n</code></pre>"
        )

        self.assertEqual(
            blocks,
            [{"type": "code", "text": "def hello():\n    return 1\n"}],
        )
        self.assertIn("def hello():\n    return 1", content)
        self.assertEqual(content.count("def hello"), 1)

    def test_links_are_retained_in_heading_paragraph_and_list_item(self) -> None:
        _, blocks = self._extract(
            """
            <h2><a href="/release">Release</a> Notes</h2>
            <p>Read <a href="https://example.com/docs">documentation</a>.</p>
            <ul><li><a href="/pricing">Pricing</a> details</li></ul>
            """
        )

        self.assertEqual(blocks[0]["text"], "Release Notes")
        self.assertEqual(
            blocks[0]["links"], [{"text": "Release", "href": "/release"}]
        )
        self.assertEqual(
            blocks[1]["links"],
            [{"text": "documentation", "href": "https://example.com/docs"}],
        )
        self.assertEqual(
            blocks[2]["items"][0]["links"],
            [{"text": "Pricing", "href": "/pricing"}],
        )

    def test_noise_tags_are_removed_while_allowed_content_remains(self) -> None:
        content, blocks = self._extract(
            """
            <script>script noise</script><style>style noise</style>
            <noscript>noscript noise</noscript><nav>nav noise</nav>
            <footer>footer noise</footer>
            <header>Product Header</header><aside>Useful Aside</aside>
            <a href="/product">Product Link</a><button>Start Trial</button>
            """
        )

        for noise_text in (
            "script noise",
            "style noise",
            "noscript noise",
            "nav noise",
            "footer noise",
        ):
            self.assertNotIn(noise_text, content)

        self.assertIn("Product Header", content)
        self.assertIn("Useful Aside", content)
        self.assertIn("Product Link", content)
        self.assertIn("Start Trial", content)
        self.assertTrue(all(block["type"] == "paragraph" for block in blocks))

    def test_repeated_article_and_div_containers_form_generic_card_groups(self) -> None:
        content, blocks = self._extract(
            """
            <main>
              <section>
                <article><h3>Alpha</h3><p>First body</p><a href="/a">Details</a></article>
                <article><h3>Beta</h3><p>Second body</p><a href="/b">Details</a></article>
              </section>
              <section>
                <div><h3>Basic</h3><p>For individuals</p></div>
                <div><h3>Pro</h3><p><span>For</span> teams</p></div>
              </section>
            </main>
            """
        )

        self.assertEqual([block["type"] for block in blocks], ["group", "group"])
        article_cards = blocks[0]["cards"]
        div_cards = blocks[1]["cards"]
        self.assertEqual([card["title"] for card in article_cards], ["Alpha", "Beta"])
        self.assertEqual([card["title"] for card in div_cards], ["Basic", "Pro"])
        self.assertEqual(article_cards[0]["blocks"][1]["text"], "First body")
        self.assertEqual(
            article_cards[0]["links"], [{"text": "Details", "href": "/a"}]
        )
        # group 只增加结构，不应让 card 文本在兼容 content 中重复出现。
        self.assertEqual(content.count("First body"), 1)
        self.assertLess(content.index("Alpha"), content.index("Beta"))
        self.assertLess(content.index("Basic"), content.index("Pro"))

    def test_card_preserves_definition_list_key_value_relationships(self) -> None:
        _, blocks = self._extract(
            """
            <section>
              <div><h3>Starter</h3><dl><dt>Projects</dt><dd>20</dd><dt>Storage</dt><dd>10 GB</dd></dl></div>
              <div><h3>Team</h3><dl><dt>Projects</dt><dd>100</dd><dt>Storage</dt><dd>50 GB</dd></dl></div>
            </section>
            """
        )

        cards = blocks[0]["cards"]
        self.assertEqual(
            cards[0]["key_values"],
            [
                {"key": "Projects", "value": "20"},
                {"key": "Storage", "value": "10 GB"},
            ],
        )
        self.assertEqual(cards[1]["key_values"][0], {"key": "Projects", "value": "100"})

    def test_card_preserves_strikethrough_and_other_dom_text_marks(self) -> None:
        _, blocks = self._extract(
            """
            <section>
              <article><h3>Starter</h3><p><del>99</del> 49 <strong>Limited offer</strong></p></article>
              <article><h3>Team</h3><p><del>199</del> 99 <strong>Best value</strong></p></article>
            </section>
            """
        )

        first_marks = blocks[0]["cards"][0]["text_marks"]
        self.assertIn({"text": "99", "marks": ["strikethrough"]}, first_marks)
        self.assertIn(
            {"text": "Limited offer", "marks": ["strong"]}, first_marks
        )

    def test_repeated_layout_divs_without_card_title_remain_flat_blocks(self) -> None:
        _, blocks = self._extract(
            """
            <section>
              <div><p>普通布局区域 A</p></div>
              <div><p>普通布局区域 B</p></div>
              <div><p>普通布局区域 C</p></div>
            </section>
            """
        )

        self.assertEqual([block["type"] for block in blocks], ["paragraph"] * 3)
        self.assertEqual(
            [block["text"] for block in blocks],
            ["普通布局区域 A", "普通布局区域 B", "普通布局区域 C"],
        )

    def test_complete_repeated_sibling_sequence_keeps_first_copy(self) -> None:
        content, blocks = self._extract(
            """
            <section>
              <article><p>A</p></article>
              <article><p>B</p></article>
              <article><p>C</p></article>
              <article><p>A</p></article>
              <article><p>B</p></article>
              <article><p>C</p></article>
            </section>
            """
        )

        self.assertEqual([block["text"] for block in blocks], ["A", "B", "C"])
        self.assertEqual(content.count("A"), 1)
        self.assertEqual(content.count("B"), 1)
        self.assertEqual(content.count("C"), 1)

    def test_long_complete_repeated_card_sequence_keeps_five_cards(self) -> None:
        cards = "".join(
            f'<article data-card="{index}"><p>Card {index}</p></article>'
            for index in range(1, 6)
        )
        _, blocks = self._extract(f"<section>{cards}{cards}</section>")

        self.assertEqual(
            [block["text"] for block in blocks],
            ["Card 1", "Card 2", "Card 3", "Card 4", "Card 5"],
        )

    def test_same_text_in_different_parents_is_not_globally_deduplicated(self) -> None:
        _, blocks = self._extract(
            """
            <meta charset="utf-8">
            <section><p>申请试用</p></section>
            <aside><p>申请试用</p></aside>
            """
        )

        self.assertEqual([block["text"] for block in blocks], ["申请试用", "申请试用"])

    def test_two_legal_identical_paragraphs_are_preserved(self) -> None:
        _, blocks = self._extract(
            "<section><p>合法重复正文</p><p>合法重复正文</p></section>"
        )

        self.assertEqual(len(blocks), 2)
        self.assertEqual([block["text"] for block in blocks], ["合法重复正文"] * 2)

    def test_incomplete_repeated_sequence_is_preserved(self) -> None:
        _, blocks = self._extract(
            """
            <section>
              <article><p>A</p></article><article><p>B</p></article>
              <article><p>C</p></article><article><p>A</p></article>
              <article><p>B</p></article>
            </section>
            """
        )

        self.assertEqual(
            [block["text"] for block in blocks], ["A", "B", "C", "A", "B"]
        )

    def test_partially_similar_sequences_are_preserved(self) -> None:
        _, blocks = self._extract(
            """
            <section>
              <article><p>A</p></article><article><p>B</p></article>
              <article><p>C</p></article><article><p>A</p></article>
              <article><p>B</p></article><article><p>Changed C</p></article>
            </section>
            """
        )

        self.assertEqual(len(blocks), 6)
        self.assertEqual(blocks[-1]["text"], "Changed C")

    def test_repeated_single_element_pattern_is_not_treated_as_sequence(self) -> None:
        _, blocks = self._extract(
            "<section>" + "<p>Same button</p>" * 6 + "</section>"
        )

        self.assertEqual(len(blocks), 6)


if __name__ == "__main__":
    unittest.main()
