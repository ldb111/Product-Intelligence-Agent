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


if __name__ == "__main__":
    unittest.main()
