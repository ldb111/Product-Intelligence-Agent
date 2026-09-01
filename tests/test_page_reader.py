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
from unittest.mock import patch

import requests

from backend.page_reader import PageReadError, main, read_page


class _TestPageHandler(BaseHTTPRequestHandler):
    """为测试提供内容固定的成功页面和 404 错误页面。"""

    def do_GET(self) -> None:  # noqa: N802 - required name from BaseHTTPRequestHandler
        if self.path == "/ok":
            html = (
                "<!doctype html><html><head><title>Test Product Page</title></head>"
                "<body><h1>Product Alpha</h1><p>Server-rendered main text.</p>"
                "</body></html>"
            ).encode("utf-8")
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
            set(result), {"url", "status_code", "title", "content"}
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
        with redirect_stdout(stdout):
            exit_code = main([f"{self.base_url}/ok"])

        self.assertEqual(exit_code, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["title"], "Test Product Page")

    def test_cli_error_is_json_and_returns_nonzero(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = main([f"{self.base_url}/missing"])

        self.assertEqual(exit_code, 1)
        output = json.loads(stderr.getvalue())
        self.assertEqual(output["error"]["type"], "http_error")


if __name__ == "__main__":
    unittest.main()
