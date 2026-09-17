"""Stage 1 网页采集适配层测试。"""

from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from source_discovery.stage1_adapter import (
    Stage1AdapterError,
    extract_source_discovery_links,
    read_page_for_discovery,
    resolve_collected_page_title,
)


class _PageHandler(BaseHTTPRequestHandler):
    """提供无需公网和浏览器兜底的可信本地 HTML。"""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 规定的方法名
        html = """<!doctype html>
<html><head><title>Local Product</title></head><body>
  <nav>
    <a href="/pricing#plans">Pricing</a>
    <a href="/docs?lang=zh#start" aria-label="Documentation">Docs</a>
  </nav>
  <h1>Product Overview</h1>
  <h2>Core Features</h2>
  <main><p>This local page contains complete product details, pricing, integrations,
  security information, usage guidance, customer support, and release notes.</p>
  <p><a href="/pricing#duplicate">Pricing duplicate</a></p></main>
  <footer><a href="/terms" title="Terms">Terms</a></footer>
</body></html>""".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, format: str, *args: object) -> None:
        return


class Stage1AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}"
        cls.url = f"{cls.base_url}/product"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_adapter_reuses_real_stage1_acquisition(self) -> None:
        result = read_page_for_discovery(self.url)

        self.assertEqual(result["requested_url"], self.url)
        self.assertEqual(result["final_url"], self.url)
        self.assertEqual(result["title"], "Local Product")
        self.assertIn("complete product details", result["main_content"])
        self.assertEqual(result["headings"], ["Product Overview", "Core Features"])
        self.assertIn(
            {
                "anchor_text": "Pricing",
                "href": f"{self.base_url}/pricing",
                "page_region": "nav",
            },
            result["links"],
        )
        self.assertIn(
            {
                "anchor_text": "Docs",
                "href": f"{self.base_url}/docs?lang=zh",
                "aria_label": "Documentation",
                "page_region": "nav",
            },
            result["links"],
        )
        self.assertIn(
            {
                "anchor_text": "Terms",
                "href": f"{self.base_url}/terms",
                "title": "Terms",
                "page_region": "footer",
            },
            result["links"],
        )
        self.assertEqual(
            sum(link["href"] == f"{self.base_url}/pricing" for link in result["links"]),
            1,
        )
        self.assertEqual(result["acquisition_method"], "static")
        self.assertEqual(result["quality_status"], "PASS")

    def test_raw_link_extraction_preserves_query_and_resolves_relative_urls(self) -> None:
        links = extract_source_discovery_links(
            b"""
            <html><body><main>
              <a href="../support?locale=zh#faq">Support</a>
              <a href="../support?locale=zh#other">Duplicate</a>
              <a href="javascript:void(0)">Action</a>
            </main></body></html>
            """,
            "https://docs.example.com/product/current/",
        )

        self.assertEqual(
            links,
            [
                {
                    "anchor_text": "Support",
                    "href": "https://docs.example.com/product/support?locale=zh",
                    "page_region": "main",
                }
            ],
        )

    def test_collected_title_uses_fixed_html_fallback_order(self) -> None:
        html = b"""
        <html><head>
          <title>HTML Title</title>
          <meta property="og:title" content="Open Graph Title">
          <meta name="twitter:title" content="Twitter Title">
        </head></html>
        """

        self.assertEqual(resolve_collected_page_title("Page Title", html), "Page Title")
        self.assertEqual(resolve_collected_page_title("", html), "HTML Title")
        self.assertEqual(
            resolve_collected_page_title(
                "",
                b'<meta property="og:title" content="Open Graph Title">'
                b'<meta name="twitter:title" content="Twitter Title">',
            ),
            "Open Graph Title",
        )
        self.assertEqual(
            resolve_collected_page_title(
                "",
                b'<meta name="twitter:title" content="Twitter Title">',
            ),
            "Twitter Title",
        )

    def test_quality_gate_rejection_becomes_stable_adapter_error(self) -> None:
        rejected = {
            "page_data": {},
            "quality_gate": {
                "status": "FAIL",
                "downstream_allowed": False,
                "reasons": [{"code": "dynamic_placeholder", "message": "loading"}],
            },
        }
        with patch(
            "source_discovery.stage1_adapter.acquire_page_with_browser_fallback",
            return_value=rejected,
        ):
            with self.assertRaises(Stage1AdapterError) as context:
                read_page_for_discovery("https://example.com")

        self.assertEqual(context.exception.error_type, "quality_rejected")
        self.assertIn("dynamic_placeholder", str(context.exception))


if __name__ == "__main__":
    unittest.main()
