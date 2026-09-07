"""Task 1 网页读取功能的自动化测试。

测试使用本机临时 HTTP 服务器提供固定 HTML，既能覆盖真实的 HTTP 请求流程，
又不会因为外部网站内容变化或网络波动而产生不稳定结果。
"""

from __future__ import annotations

import io
import json
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from backend.browser_reader import BrowserReadError
from backend.page_reader import (
    PageReadError,
    acquire_page_with_browser_fallback,
    detect_static_interaction_signals,
    extract_page_data,
    main,
    read_page,
)
from backend.snapshot import EXTRACTION_VERSION, INTERACTIVE_STATE_SCHEMA_VERSION


def _browser_capture(
    html: str,
    final_url: str = "https://example.com/final",
    traversal_result: dict | None = None,
) -> dict:
    """构造可控浏览器结果，让 Fallback 测试不依赖真实 Chromium 或公网。"""
    result = {
        "status_code": 200,
        "final_url": final_url,
        "title": "Browser Rendered Page",
        "html": html.encode("utf-8"),
    }
    if traversal_result is not None:
        result["traversal_result"] = traversal_result
    return result


def _trusted_browser_capture() -> dict:
    """构造能够通过 Quality Gate 的浏览器正文。"""
    return _browser_capture(
        """
        <html><body>
          <h1>Rendered Product</h1>
          <p>This rendered page contains complete product details, pricing, security,
          integrations, team collaboration, usage guidance, and support information.</p>
          <ul><li>Feature A</li><li>Feature B</li></ul>
        </body></html>
        """,
        final_url="https://www.example.com/rendered-final",
    )


def _traversal_result(content_hash: str) -> dict:
    """构造正式 Orchestrator 形状的结果，用于验证主扫描集成而非底层点击。"""
    return {
        "status": "complete",
        "interactive_states": [
            {
                "state_key": "stable-state-key",
                "scope_path": ["Membership"],
                "state_path": ["Monthly"],
                "is_default": True,
                "blocks": [{"type": "paragraph", "text": content_hash}],
                "content_hash": content_hash,
                "captured_at": "2026-09-07T10:00:00+08:00",
            }
        ],
        "groups_discovered": 1,
        "groups_completed": 1,
        "non_default_states_captured": 0,
        "page_restored": True,
        "errors": [],
        "bounds": {
            "max_depth": 2,
            "max_actionable_options_per_group": 6,
            "max_non_default_states_per_page": 12,
        },
        "truncations": [],
        "skipped": [],
    }


class _TestPageHandler(BaseHTTPRequestHandler):
    """为测试提供内容固定的成功页面和 404 错误页面。"""

    def do_GET(self) -> None:  # noqa: N802 - required name from BaseHTTPRequestHandler
        if self.path == "/ok":
            html = (
                "<!doctype html><html><head><title>Test Product Page</title></head>"
                "<body><h1>Product Alpha</h1><p>Server-rendered main text.</p>"
                "<ul><li>Reliable product feature</li></ul>"
                "<table><tr><th>Plan</th><th>Price</th></tr>"
                "<tr><td>Pro</td><td>99</td></tr></table>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if self.path == "/placeholder":
            html = (
                "<!doctype html><html><head><title>CaSee Test</title></head>"
                "<body><p>CaSee 凯见 加载中</p></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if self.path == "/sparse":
            html = (
                "<!doctype html><html><head><title>Sparse Test</title></head>"
                "<body><p>Short product description.</p></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if self.path == "/normalization":
            # 这个可控页面同时包含必须删除和必须保留的标签，用于验证完整 HTTP 流程。
            html = b"""<!doctype html>
<html>
  <head>
    <title>Normalization Test Page</title>
    <style>.style-noise { display: none; }</style>
    <script>script noise</script>
  </head>
  <body>
    <header>Useful Product Header</header>
    <nav>Navigation Noise</nav>
    <main>
      <h1>Main Product Content</h1>
      <p>Alpha      Beta</p>
      <a href="/pricing">Pricing Link</a>
      <button>Start Trial</button>
      <noscript>Noscript Noise</noscript>
    </main>
    <aside>Useful Related Information</aside>
    <footer>Footer Noise</footer>
  </body>
</html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if self.path == "/interactive":
            html = b"""<!doctype html>
<html><head><title>Interactive Product Page</title></head><body>
  <h1>Membership plans</h1>
  <div role="tablist">
    <button role="tab" aria-selected="true">Personal</button>
    <button role="tab" aria-selected="false">Team</button>
  </div>
  <p>This server response already contains a complete product overview, pricing,
  usage limits, integrations, security details, and customer support information.</p>
</body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        error_html = b"<html><title>Not Found</title><body>Error page</body></html>"
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(error_html)))
        self.end_headers()
        self.wfile.write(error_html)

    def log_message(self, format: str, *args: object) -> None:
        """关闭临时服务器的访问日志，避免无关日志干扰测试结果。"""


class PageReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 端口传 0 表示由操作系统分配可用端口，避免测试与本机已有服务发生端口冲突。
        # 后台线程让临时服务器能在测试调用 read_page 时同时响应真实 HTTP 请求。
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _TestPageHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)

    def test_success_returns_required_data_from_real_http_response(self) -> None:
        result = read_page(f"{self.base_url}/ok")

        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["title"], "Test Product Page")
        self.assertIn("Product Alpha", result["content"])
        self.assertIn("Server-rendered main text.", result["content"])
        self.assertEqual(
            set(result),
            {
                "url",
                "requested_url",
                "final_url",
                "status_code",
                "title",
                "content",
                "blocks",
                "acquisition_method",
            },
        )
        self.assertIsInstance(result["blocks"], list)
        self.assertEqual(result["url"], f"{self.base_url}/ok")
        self.assertEqual(result["requested_url"], f"{self.base_url}/ok")
        self.assertEqual(result["final_url"], f"{self.base_url}/ok")
        self.assertEqual(result["acquisition_method"], "static")

    def test_normalization_removes_noise_and_preserves_allowed_content(self) -> None:
        url = f"{self.base_url}/normalization"
        result = read_page(url)

        # url 保持用户请求身份；requested_url、final_url 和 acquisition_method 只增加
        # 采集审计信息，不改变正文结构。
        self.assertEqual(result["url"], url)
        self.assertEqual(result["requested_url"], url)
        self.assertEqual(result["final_url"], url)
        self.assertEqual(result["acquisition_method"], "static")
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["title"], "Normalization Test Page")
        self.assertEqual(
            set(result),
            {
                "url",
                "requested_url",
                "final_url",
                "status_code",
                "title",
                "content",
                "blocks",
                "acquisition_method",
            },
        )

        content = result["content"]
        # script、style、noscript、nav、footer 及其内部文本都应在转纯文本前被删除。
        self.assertNotIn("script noise", content)
        self.assertNotIn("style-noise", content)
        self.assertNotIn("Noscript Noise", content)
        self.assertNotIn("Navigation Noise", content)
        self.assertNotIn("Footer Noise", content)

        # 以下标签可能包含有效产品信息，标准化时只能保留，不能做猜测性删除。
        self.assertIn("Main Product Content", content)
        self.assertIn("Useful Product Header", content)
        self.assertIn("Useful Related Information", content)
        self.assertIn("Pricing Link", content)
        self.assertIn("Start Trial", content)

    def test_normalization_cleans_whitespace_without_flattening_all_text(self) -> None:
        html = b"""
        <main>
            <p>  First       line\twith spaces  </p>



            <p>  Second line  </p>
        </main>
        """

        _, content = extract_page_data(html)

        self.assertIn("First line with spaces", content)
        self.assertIn("Second line", content)
        self.assertNotIn("  ", content)
        self.assertNotIn("\n\n\n", content)
        self.assertIn("\n", content)
        self.assertTrue(all(line == line.strip() for line in content.splitlines()))

    def test_static_interaction_signals_use_standard_aria_semantics(self) -> None:
        signals = detect_static_interaction_signals(
            b"""
            <div role="tablist">
              <button role="tab" aria-selected="true">Overview</button>
              <button role="tab" aria-selected="false">Team</button>
            </div>
            """
        )

        self.assertEqual(signals, ["role_tablist", "role_tab", "aria_selected"])
        self.assertEqual(
            detect_static_interaction_signals(
                b'<div class="tabs"><button>Ordinary button</button></div>'
            ),
            [],
        )

    def test_http_error_does_not_return_error_page_as_success(self) -> None:
        with self.assertRaises(PageReadError) as raised:
            read_page(f"{self.base_url}/missing")

        self.assertEqual(raised.exception.error_type, "http_error")
        self.assertIn("404", str(raised.exception))

    def test_invalid_url_is_rejected_before_request(self) -> None:
        # patch 替换真实 requests.get；assert_not_called 用来证明无效 URL 在联网前已被拦截。
        with patch("backend.page_reader.requests.get") as mocked_get:
            with self.assertRaises(PageReadError) as raised:
                read_page("not-a-url")

        self.assertEqual(raised.exception.error_type, "invalid_url")
        mocked_get.assert_not_called()

    def test_timeout_has_clear_error_type(self) -> None:
        # 用 Mock（模拟）稳定触发超时，不需要真的等待某个外部服务器超时。
        with patch(
            "backend.page_reader.requests.get",
            side_effect=requests.Timeout("simulated timeout"),
        ):
            with self.assertRaises(PageReadError) as raised:
                read_page("https://example.com", timeout=0.1)

        self.assertEqual(raised.exception.error_type, "timeout")
        self.assertIn("0.1 seconds", str(raised.exception))

    def test_connection_failure_has_clear_error_type(self) -> None:
        with patch(
            "backend.page_reader.requests.get",
            side_effect=requests.ConnectionError("simulated connection failure"),
        ):
            with self.assertRaises(PageReadError) as raised:
                read_page("https://example.com")

        self.assertEqual(raised.exception.error_type, "request_failed")
        self.assertIn("simulated connection failure", str(raised.exception))

    def test_cli_success_output_is_valid_json(self) -> None:
        stdout = io.StringIO()
        # CLI 成功时现在会保存 Snapshot。测试把默认目录替换成临时目录，确保自动化
        # 测试不会在正式 data/snapshots 中留下文件。
        with patch("backend.page_reader.read_browser_page") as mocked_browser:
            with TemporaryDirectory() as temporary_directory:
                with patch(
                    "backend.snapshot.DEFAULT_SNAPSHOT_DIRECTORY",
                    Path(temporary_directory),
                ):
                    with redirect_stdout(stdout):
                        exit_code = main([f"{self.base_url}/ok"])

                snapshot_files = list(Path(temporary_directory).glob("*.json"))
                saved_snapshot = json.loads(
                    snapshot_files[0].read_text(encoding="utf-8")
                )

        self.assertEqual(exit_code, 0)
        output = json.loads(stdout.getvalue())
        self.assertTrue(output["is_first_scan"])
        self.assertIsNone(output["changed"])
        self.assertIsNone(output["diff"])
        self.assertIsNone(output["previous_content_hash"])
        self.assertEqual(
            output["current_snapshot"]["title"], "Test Product Page"
        )
        self.assertEqual(
            output["current_snapshot"]["blocks"],
            read_page(f"{self.base_url}/ok")["blocks"],
        )
        self.assertEqual(
            output["current_snapshot"]["extraction_version"], EXTRACTION_VERSION
        )
        self.assertEqual(output["current_snapshot"]["acquisition_method"], "static")
        self.assertEqual(output["current_snapshot"]["url"], f"{self.base_url}/ok")
        self.assertEqual(
            output["current_snapshot"]["final_url"], f"{self.base_url}/ok"
        )
        self.assertIn("captured_at", output["current_snapshot"])
        self.assertEqual(
            output["current_content_hash"],
            output["current_snapshot"]["content_hash"],
        )
        self.assertIsNone(output["previous_snapshot"])
        self.assertEqual(output["quality_gate"]["status"], "PASS")
        self.assertTrue(output["quality_gate"]["downstream_allowed"])
        self.assertEqual(output["acquisition_method"], "static")
        self.assertEqual(output["requested_url"], f"{self.base_url}/ok")
        self.assertEqual(output["final_url"], f"{self.base_url}/ok")
        self.assertIsNone(output["browser_quality_gate"])
        self.assertIsNone(output["static_acquisition_error"])
        self.assertEqual(output["static_interaction_signals"], [])
        self.assertIsNone(output["browser_trigger_reason"])
        self.assertEqual(
            output["interactive_state_comparison"]["comparison_status"],
            "baseline",
        )
        self.assertIsNone(output["interactive_state_comparison"]["changed"])
        self.assertNotIn("interactive_state_schema_version", saved_snapshot)
        self.assertNotIn("interactive_states", saved_snapshot)
        self.assertNotIn("interactive_state_traversal", saved_snapshot)
        mocked_browser.assert_not_called()
        self.assertEqual(len(snapshot_files), 1)

    def test_main_persists_and_matches_same_interactive_state_across_scans(self) -> None:
        """正式入口应把 Traversal 写入历史，并在第二次扫描返回 unchanged。"""
        url = f"{self.base_url}/interactive"
        rendered_html = """
        <html><body>
          <h1>Membership plans</h1>
          <p>This rendered membership page contains complete pricing, feature,
          integration, security, support, and usage details for product research.</p>
          <ul><li>Stable feature A</li><li>Stable feature B</li></ul>
        </body></html>
        """
        browser_results = [
            _browser_capture(
                rendered_html,
                traversal_result=_traversal_result("same-state-hash"),
            ),
            _browser_capture(
                rendered_html,
                traversal_result=_traversal_result("same-state-hash"),
            ),
        ]

        with patch(
            "backend.page_reader.read_browser_page", side_effect=browser_results
        ):
            with TemporaryDirectory() as temporary_directory:
                with patch(
                    "backend.snapshot.DEFAULT_SNAPSHOT_DIRECTORY",
                    Path(temporary_directory),
                ):
                    first_stdout = io.StringIO()
                    with redirect_stdout(first_stdout):
                        first_exit_code = main([url])

                    second_stdout = io.StringIO()
                    with redirect_stdout(second_stdout):
                        second_exit_code = main([url])

                snapshot_files = list(Path(temporary_directory).glob("*.json"))
                saved_snapshots = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in snapshot_files
                ]

        first_output = json.loads(first_stdout.getvalue())
        second_output = json.loads(second_stdout.getvalue())
        self.assertEqual((first_exit_code, second_exit_code), (0, 0))
        self.assertEqual(
            first_output["interactive_state_comparison"]["comparison_status"],
            "baseline",
        )
        self.assertIsNone(first_output["interactive_state_comparison"]["changed"])
        self.assertEqual(
            first_output["interactive_state_comparison"]["state_changes"], []
        )
        self.assertEqual(first_output["contextual_state_diff"], [])
        self.assertFalse(second_output["interactive_state_comparison"]["changed"])
        self.assertEqual(
            second_output["interactive_state_comparison"]["state_changes"][0][
                "change_type"
            ],
            "unchanged",
        )
        self.assertEqual(second_output["contextual_state_diff"], [])
        self.assertEqual(len(saved_snapshots), 2)
        for snapshot in saved_snapshots:
            self.assertEqual(
                snapshot["interactive_state_schema_version"],
                INTERACTIVE_STATE_SCHEMA_VERSION,
            )
            self.assertEqual(
                snapshot["interactive_states"][0]["content_hash"],
                "same-state-hash",
            )
            self.assertEqual(
                snapshot["interactive_state_traversal"]["status"], "complete"
            )

    def test_main_reports_modified_interactive_state_on_second_scan(self) -> None:
        """页面级内容可保持相同，但一个交互状态 Hash 改变时应独立返回 modified。"""
        url = f"{self.base_url}/interactive"
        rendered_html = """
        <html><body>
          <h1>Membership plans</h1>
          <p>This rendered membership page contains complete pricing, feature,
          integration, security, support, and usage details for product research.</p>
          <ul><li>Stable feature A</li><li>Stable feature B</li></ul>
        </body></html>
        """
        browser_results = [
            _browser_capture(
                rendered_html,
                traversal_result=_traversal_result("old-state-hash"),
            ),
            _browser_capture(
                rendered_html,
                traversal_result=_traversal_result("new-state-hash"),
            ),
        ]

        with patch(
            "backend.page_reader.read_browser_page", side_effect=browser_results
        ):
            with TemporaryDirectory() as temporary_directory:
                with patch(
                    "backend.snapshot.DEFAULT_SNAPSHOT_DIRECTORY",
                    Path(temporary_directory),
                ):
                    with redirect_stdout(io.StringIO()):
                        first_exit_code = main([url])
                    second_stdout = io.StringIO()
                    with redirect_stdout(second_stdout):
                        second_exit_code = main([url])

        second_output = json.loads(second_stdout.getvalue())
        state_comparison = second_output["interactive_state_comparison"]
        self.assertEqual((first_exit_code, second_exit_code), (0, 0))
        self.assertTrue(state_comparison["changed"])
        self.assertEqual(
            state_comparison["state_changes"][0]["change_type"], "modified"
        )
        self.assertEqual(
            state_comparison["state_changes"][0]["previous_content_hash"],
            "old-state-hash",
        )
        self.assertEqual(
            state_comparison["state_changes"][0]["current_content_hash"],
            "new-state-hash",
        )
        self.assertEqual(len(second_output["contextual_state_diff"]), 1)
        detailed_state_diff = second_output["contextual_state_diff"][0]
        self.assertEqual(detailed_state_diff["state_key"], "stable-state-key")
        self.assertEqual(detailed_state_diff["previous_hash"], "old-state-hash")
        self.assertEqual(detailed_state_diff["current_hash"], "new-state-hash")
        self.assertEqual(
            detailed_state_diff["contextual_diff"][0]["previous"],
            "old-state-hash",
        )
        self.assertEqual(
            detailed_state_diff["contextual_diff"][0]["current"],
            "new-state-hash",
        )
        # 新状态比较不会替换原有页面级 Change Detection / Diff。
        self.assertFalse(second_output["changed"])
        self.assertIsNone(second_output["diff"])

    def test_static_pass_with_tab_signals_uses_browser_enrichment(self) -> None:
        with patch(
            "backend.page_reader.read_browser_page",
            return_value=_trusted_browser_capture(),
        ) as mocked_browser:
            result = acquire_page_with_browser_fallback(
                f"{self.base_url}/interactive"
            )

        self.assertEqual(result["static_quality_gate"]["status"], "PASS")
        self.assertEqual(result["browser_quality_gate"]["status"], "PASS")
        self.assertEqual(result["page_data"]["acquisition_method"], "browser")
        self.assertEqual(
            result["static_interaction_signals"],
            ["role_tablist", "role_tab", "aria_selected"],
        )
        self.assertEqual(result["browser_trigger_reason"], "interactive_structure")
        mocked_browser.assert_called_once_with(f"{self.base_url}/interactive")

    def test_static_fail_calls_browser_and_browser_fail_does_not_save(self) -> None:
        stdout = io.StringIO()
        browser_result = _browser_capture(
            "<html><body><p>CaSee 凯见 加载中</p></body></html>"
        )

        with patch(
            "backend.page_reader.read_browser_page", return_value=browser_result
        ) as mocked_browser:
            with patch("backend.page_reader.create_snapshot") as mocked_create:
                with patch("backend.page_reader.save_snapshot") as mocked_save:
                    with redirect_stdout(stdout):
                        exit_code = main([f"{self.base_url}/placeholder"])

        output = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(output["status_code"], 200)
        self.assertEqual(output["acquisition_method"], "browser")
        self.assertEqual(output["static_quality_gate"]["status"], "FAIL")
        self.assertEqual(output["browser_quality_gate"]["status"], "FAIL")
        self.assertEqual(output["quality_gate"]["status"], "FAIL")
        self.assertFalse(output["quality_gate"]["downstream_allowed"])
        self.assertEqual(
            output["quality_gate"]["reasons"][0]["code"],
            "dynamic_placeholder",
        )
        mocked_browser.assert_called_once_with(f"{self.base_url}/placeholder")
        mocked_create.assert_not_called()
        mocked_save.assert_not_called()

    def test_static_http_error_browser_pass_saves_trusted_snapshot(self) -> None:
        stdout = io.StringIO()

        with patch(
            "backend.page_reader.read_browser_page",
            return_value=_trusted_browser_capture(),
        ) as mocked_browser:
            with TemporaryDirectory() as temporary_directory:
                with patch(
                    "backend.snapshot.DEFAULT_SNAPSHOT_DIRECTORY",
                    Path(temporary_directory),
                ):
                    with redirect_stdout(stdout):
                        exit_code = main([f"{self.base_url}/missing"])
                snapshot_files = list(Path(temporary_directory).glob("*.json"))

        output = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(output["acquisition_method"], "browser")
        self.assertEqual(output["static_acquisition_error"]["type"], "http_error")
        self.assertIsNone(output["static_quality_gate"])
        self.assertEqual(output["browser_quality_gate"]["status"], "PASS")
        self.assertEqual(len(snapshot_files), 1)
        mocked_browser.assert_called_once_with(f"{self.base_url}/missing")

    def test_static_request_failure_and_timeout_call_browser(self) -> None:
        for error_type in ("request_failed", "timeout"):
            with self.subTest(error_type=error_type):
                static_error = PageReadError(error_type, f"simulated {error_type}")
                with patch(
                    "backend.page_reader.read_page", side_effect=static_error
                ):
                    with patch(
                        "backend.page_reader.read_browser_page",
                        return_value=_trusted_browser_capture(),
                    ) as mocked_browser:
                        result = acquire_page_with_browser_fallback(
                            "https://example.com"
                        )

                self.assertEqual(result["page_data"]["acquisition_method"], "browser")
                self.assertEqual(result["quality_gate"]["status"], "PASS")
                self.assertEqual(
                    result["static_acquisition_error"]["type"], error_type
                )
                mocked_browser.assert_called_once_with("https://example.com")

    def test_invalid_url_does_not_call_browser(self) -> None:
        with patch("backend.page_reader.read_browser_page") as mocked_browser:
            with self.assertRaises(PageReadError) as raised:
                acquire_page_with_browser_fallback("ftp://example.com/file")

        self.assertEqual(raised.exception.error_type, "invalid_url")
        mocked_browser.assert_not_called()

    def test_static_error_and_browser_fail_do_not_save_snapshot(self) -> None:
        stdout = io.StringIO()
        static_error = PageReadError("request_failed", "simulated static failure")
        browser_result = _browser_capture(
            "<html><body><p>CaSee 凯见 加载中</p></body></html>"
        )

        with patch("backend.page_reader.read_page", side_effect=static_error):
            with patch(
                "backend.page_reader.read_browser_page", return_value=browser_result
            ):
                with patch("backend.page_reader.create_snapshot") as mocked_create:
                    with patch("backend.page_reader.save_snapshot") as mocked_save:
                        with redirect_stdout(stdout):
                            exit_code = main(["https://example.com"])

        output = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(output["quality_gate"]["status"], "FAIL")
        self.assertEqual(
            output["static_acquisition_error"]["type"], "request_failed"
        )
        mocked_create.assert_not_called()
        mocked_save.assert_not_called()

    def test_static_error_and_browser_exception_do_not_save_snapshot(self) -> None:
        stderr = io.StringIO()
        static_error = PageReadError("timeout", "simulated static timeout")

        with patch("backend.page_reader.read_page", side_effect=static_error):
            with patch(
                "backend.page_reader.read_browser_page",
                side_effect=BrowserReadError(
                    "browser_timeout", "simulated browser timeout"
                ),
            ):
                with patch("backend.page_reader.create_snapshot") as mocked_create:
                    with patch("backend.page_reader.save_snapshot") as mocked_save:
                        with redirect_stderr(stderr):
                            exit_code = main(["https://example.com"])

        output = json.loads(stderr.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(output["error"]["type"], "browser_timeout")
        self.assertEqual(output["static_acquisition_error"]["type"], "timeout")
        mocked_create.assert_not_called()
        mocked_save.assert_not_called()

    def test_static_warning_calls_browser_and_browser_warning_does_not_save(self) -> None:
        stdout = io.StringIO()
        browser_result = _browser_capture(
            "<html><body><p>Still short product description.</p></body></html>"
        )

        with patch(
            "backend.page_reader.read_browser_page", return_value=browser_result
        ) as mocked_browser:
            with patch("backend.page_reader.create_snapshot") as mocked_create:
                with patch("backend.page_reader.save_snapshot") as mocked_save:
                    with redirect_stdout(stdout):
                        exit_code = main([f"{self.base_url}/sparse"])

        output = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(output["static_quality_gate"]["status"], "WARNING")
        self.assertEqual(output["browser_quality_gate"]["status"], "WARNING")
        self.assertEqual(output["quality_gate"]["status"], "WARNING")
        self.assertFalse(output["quality_gate"]["downstream_allowed"])
        mocked_browser.assert_called_once_with(f"{self.base_url}/sparse")
        mocked_create.assert_not_called()
        mocked_save.assert_not_called()

    def test_browser_pass_uses_rendered_content_and_saves_snapshot(self) -> None:
        stdout = io.StringIO()
        rendered_html = """
        <html><head><title>Fallback title</title></head><body>
          <h1>Rendered Product</h1>
          <p>This browser-rendered product page now contains complete feature details.</p>
          <p>It includes pricing, team collaboration, security, integrations, and usage guidance.</p>
          <ul><li>Feature A</li><li>Feature B</li></ul>
        </body></html>
        """
        browser_result = _browser_capture(
            rendered_html, final_url="https://www.example.com/rendered-final"
        )

        with patch(
            "backend.page_reader.read_browser_page", return_value=browser_result
        ) as mocked_browser:
            with TemporaryDirectory() as temporary_directory:
                with patch(
                    "backend.snapshot.DEFAULT_SNAPSHOT_DIRECTORY",
                    Path(temporary_directory),
                ):
                    with redirect_stdout(stdout):
                        exit_code = main([f"{self.base_url}/placeholder"])
                snapshot_files = list(Path(temporary_directory).glob("*.json"))
                saved_snapshot = json.loads(
                    snapshot_files[0].read_text(encoding="utf-8")
                )

        output = json.loads(stdout.getvalue())
        current_snapshot = output["current_snapshot"]
        self.assertEqual(exit_code, 0)
        self.assertEqual(output["static_quality_gate"]["status"], "FAIL")
        self.assertEqual(output["browser_quality_gate"]["status"], "PASS")
        self.assertEqual(output["quality_gate"]["status"], "PASS")
        self.assertEqual(output["acquisition_method"], "browser")
        self.assertEqual(
            output["final_url"], "https://www.example.com/rendered-final"
        )
        self.assertEqual(current_snapshot["acquisition_method"], "browser")
        self.assertEqual(current_snapshot["url"], f"{self.base_url}/placeholder")
        self.assertEqual(
            current_snapshot["requested_url"], f"{self.base_url}/placeholder"
        )
        self.assertEqual(
            current_snapshot["final_url"], "https://www.example.com/rendered-final"
        )
        self.assertIn("Rendered Product", current_snapshot["content"])
        self.assertEqual(len(snapshot_files), 1)
        self.assertEqual(saved_snapshot["acquisition_method"], "browser")
        self.assertEqual(
            saved_snapshot["final_url"], "https://www.example.com/rendered-final"
        )
        mocked_browser.assert_called_once_with(f"{self.base_url}/placeholder")

    def test_browser_timeout_returns_clear_error_and_does_not_save(self) -> None:
        stderr = io.StringIO()

        with patch(
            "backend.page_reader.read_browser_page",
            side_effect=BrowserReadError(
                "browser_timeout", "Simulated bounded browser timeout."
            ),
        ):
            with patch("backend.page_reader.create_snapshot") as mocked_create:
                with patch("backend.page_reader.save_snapshot") as mocked_save:
                    with redirect_stderr(stderr):
                        exit_code = main([f"{self.base_url}/placeholder"])

        output = json.loads(stderr.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(output["error"]["type"], "browser_timeout")
        self.assertIn("browser timeout", output["error"]["message"].lower())
        mocked_create.assert_not_called()
        mocked_save.assert_not_called()

    def test_cli_error_is_json_and_returns_nonzero(self) -> None:
        stderr = io.StringIO()
        with patch("backend.page_reader.read_browser_page") as mocked_browser:
            with redirect_stderr(stderr):
                exit_code = main(["not-a-url"])

        self.assertEqual(exit_code, 1)
        output = json.loads(stderr.getvalue())
        self.assertEqual(output["error"]["type"], "invalid_url")
        mocked_browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
