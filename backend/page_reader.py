"""读取指定网页，并提取后续数据采集流程需要的基础原始数据。

本模块负责 Task 1 的完整链路：校验 URL、发起 HTTP 请求、检查响应状态、
解析服务器返回的 HTML，最后通过命令行输出 JSON。

当前实现有意保持简单：BeautifulSoup 只解析 HTTP 响应中已经存在的 HTML，
不会像浏览器一样执行 JavaScript。因此，依赖 JavaScript 才显示正文的网页可能只能
获取到部分内容。页面中的导航栏、页脚等噪声也会暂时保留，后续由
Content Normalization（内容标准化）任务统一处理。
"""

from __future__ import annotations

# argparse 负责读取命令行参数；json 和 sys 分别用于生成 JSON、区分标准输出与标准错误。
import argparse
import json
import sys
from typing import Any

# urlsplit 用于把 URL 拆分成协议、主机、端口、路径等部分，便于在请求前完成结构校验。
from urllib.parse import urlsplit

# requests 负责真正的 HTTP 通信；BeautifulSoup 负责把返回的 HTML 解析成可提取文本的结构。
import requests
from bs4 import BeautifulSoup


DEFAULT_TIMEOUT_SECONDS = 10.0
# User-Agent 用于向服务器说明请求来自哪个客户端。部分网站会拒绝没有该请求头的访问，
# 使用项目自己的标识也比伪装成真实浏览器更清晰、诚实。
USER_AGENT = "Product-Intelligence-Agent/0.1"


class PageReadError(Exception):
    """表示一次可预期、可向用户明确说明的网页读取失败。

    requests 和 HTML 解析器会抛出多种底层异常。如果直接把这些异常暴露给命令行，
    调用者很难稳定区分 URL 无效、请求超时、网络失败等情况。本异常统一保存
    ``error_type`` 和可读消息，让 main 函数可以生成结构一致的错误 JSON。

    输入：稳定的错误类型标识和面向用户的错误说明。
    处理：保留异常消息，并额外记录错误类型。
    输出：由调用方抛出并在命令行入口统一转换为错误 JSON。
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def validate_url(url: str) -> str:
    """在发起网络请求之前校验 URL，并返回去除首尾空白后的 URL。

    在整个链路中的职责：尽早拦截空值、错误协议、缺少主机名或非法端口，避免把明显
    错误的输入交给网络层。这样既能减少无意义请求，也能返回比 requests 底层异常更清楚
    的业务错误。

    输入：调用者提供的 URL 字符串。
    处理：清除首尾空白，使用 urlsplit 拆分 URL，再检查协议、主机名和端口。
    输出：可用于请求的 URL 字符串；无效时抛出 PageReadError。
    """
    # 空字符串没有可请求的目标，必须在进入 urlsplit 和 requests 前直接拒绝。
    if not isinstance(url, str) or not url.strip():
        raise PageReadError("invalid_url", "URL cannot be empty.")

    cleaned_url = url.strip()
    try:
        # urlsplit 只负责按 URL 结构拆分，并不会访问网络，也不会保证所有字段都合法。
        # 例如它会把 https://example.com/path 拆成 scheme、netloc、path 等组成部分。
        parsed_url = urlsplit(cleaned_url)
        # 读取 port 属性时，urlsplit 才会检查端口能否转换为有效数字；主动访问它可以
        # 提前发现 ':abc' 等格式错误，避免错误延迟到真正请求时才出现。
        _ = parsed_url.port
    except ValueError as exc:
        raise PageReadError("invalid_url", f"Invalid URL: {exc}") from exc

    # requests 还支持其他协议适配方式，但当前业务只读取普通网页。限制为 HTTP/HTTPS
    # 可以防止把文件路径或其他协议误当作网页地址，也让后续响应处理保持明确。
    if parsed_url.scheme.lower() not in {"http", "https"}:
        raise PageReadError(
            "invalid_url", "URL scheme must be http or https."
        )
    # 只有协议而没有主机名（例如 'https://'）仍然无法定位服务器，因此也属于无效 URL。
    if not parsed_url.hostname:
        raise PageReadError("invalid_url", "URL must include a valid host.")

    return cleaned_url


def fetch_html(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[int, bytes]:
    """请求一个已校验的网页，并返回成功响应的状态码和原始 HTML 字节。

    在整个链路中的职责：只处理 HTTP 通信，不负责理解页面内容。函数会发送 GET 请求，
    接收 requests.Response 响应对象，并在把 HTML 交给解析步骤前确认请求确实成功。

    输入：经过 validate_url 校验的 HTTP/HTTPS URL，以及允许等待的超时秒数。
    处理：携带项目 User-Agent 发起 GET 请求；正常跟随重定向；区分超时、其他网络异常
    和非 2xx HTTP 状态。
    输出：``(status_code, raw_html_bytes)`` 元组；失败时抛出 PageReadError。
    """
    try:
        # requests.get 的主要输入是目标 URL、请求头和 timeout，返回值 Response 包含
        # 最终状态码、响应头及服务器返回的正文。timeout 防止服务器长期不响应时程序
        # 一直卡住；到达限制后 requests 会主动抛出 Timeout。
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
    except requests.Timeout as exc:
        # 超时通常意味着目标服务器响应过慢，和 DNS、证书、断网等请求失败的处理建议
        # 不同，因此单独标记为 timeout，方便用户或后续程序决定是否重试。
        raise PageReadError(
            "timeout", f"Request timed out after {timeout:g} seconds."
        ) from exc
    except requests.RequestException as exc:
        # requests 的其他可预期网络错误都继承 RequestException。统一转换后，命令行
        # 不会直接暴露难以处理的底层异常，同时仍在消息中保留真实失败原因。
        raise PageReadError("request_failed", f"HTTP request failed: {exc}") from exc

    # 404、500 等错误响应也经常带有结构完整的 HTML。如果先解析，错误页标题和正文
    # 可能被误当成真实产品页面，进而污染后续快照和变化检测，所以必须先检查状态码。
    # 当前业务只接受最终的 2xx 成功状态；requests 默认处理常见重定向后的最终响应。
    if not 200 <= response.status_code < 300:
        raise PageReadError(
            "http_error",
            f"Server returned unexpected HTTP status code {response.status_code}.",
        )

    # response.content 是服务器响应正文的原始 bytes（字节），尚未被本项目转换成页面
    # 文本。保留字节交给 BeautifulSoup，有助于解析器结合 HTML 信息识别字符编码。
    return response.status_code, response.content


def extract_page_data(html: bytes) -> tuple[str, str]:
    """从服务器返回的 HTML 中提取网页标题和基础页面文本。

    在整个链路中的职责：把 fetch_html 返回的原始字节转换成后续阶段可使用的字符串。

    输入：服务器响应正文的原始 HTML bytes（字节）。
    处理：BeautifulSoup 使用 Python 内置的 html.parser 构建 HTML 节点树，然后读取
    ``<title>`` 标签和所有节点中的文本。
    输出：``(title, content)`` 字符串元组；页面没有 title 标签时标题为空字符串。

    业务边界：BeautifulSoup 是 HTML 解析器，不是浏览器，不会执行 JavaScript。因此，
    JavaScript 运行后才出现的正文不在本函数的输入里，也就无法被提取。``get_text``
    会保留 HTML 中导航栏、页脚、菜单等文字；本任务需要的是原始页面文本，不在这里
    猜测哪些内容有用。这些噪声将在后续 Content Normalization（内容标准化）中处理。
    """
    try:
        # 解析后可以按标签访问页面结构，不需要用容易出错的字符串截取来读取 HTML。
        soup = BeautifulSoup(html, "html.parser")
        # title 从 HTML 的 <title> 标签取得；get_text 会合并标签内部文本并清理首尾空白。
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        # content 提取整棵 HTML 节点树中的文本，并用换行分隔各段，暂不做业务标准化。
        content = soup.get_text("\n", strip=True)
    except Exception as exc:
        # 将罕见的解析异常转换成统一业务异常，避免调用者把“解析失败”误解为正常空页面。
        raise PageReadError("parse_error", f"Failed to parse returned HTML: {exc}") from exc

    return title, content


def read_page(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """串联 URL 校验、网页请求和 HTML 解析，生成一次完整的网页读取结果。

    在整个链路中的职责：作为 Task 1 的业务入口，按固定顺序组织三个步骤，确保调用者
    不会漏掉 URL 校验或状态码检查，也让命令行和未来的其他调用方复用相同行为。

    输入：URL 字符串和可选的超时秒数。
    处理：先检查 timeout，再校验 URL、获取 HTML、提取标题和页面文本。
    输出：包含 ``url``、``status_code``、``title``、``content`` 的 Python dict（字典）。
    该字典仍是 Python 对象，最终由 main 中的 json.dumps 转换成 JSON 字符串。
    """
    # 非正数超时没有实际意义，也可能让 requests 产生不够直观的底层错误，因此提前拒绝。
    if timeout <= 0:
        raise PageReadError("invalid_timeout", "Timeout must be greater than zero.")

    # 三步各自只负责一种工作；在这里串联后，数据依次从 URL 变为 HTML，再变为结构化字典。
    validated_url = validate_url(url)
    status_code, html = fetch_html(validated_url, timeout)
    title, content = extract_page_data(html)

    return {
        "url": validated_url,
        "status_code": status_code,
        "title": title,
        "content": content,
    }


def build_parser() -> argparse.ArgumentParser:
    """创建并配置命令行参数解析器。

    argparse 让用户可以通过命令行传入必需的 URL 和可选的 ``--timeout``，并自动处理
    参数类型、帮助信息和缺少参数等基础问题，避免手工拆分 sys.argv。

    输入：无。
    处理：声明命令说明、URL 位置参数和 timeout 可选参数。
    输出：配置完成的 ArgumentParser，供 main 解析实际命令行输入。
    """
    parser = argparse.ArgumentParser(
        description="Read basic title and text from a server-rendered web page."
    )
    parser.add_argument("url", help="HTTP or HTTPS page URL to read")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行命令行流程，并用 JSON 和退出码向调用者报告结果。

    main 是命令行入口：它读取参数、调用 read_page，并把 Python dict（字典）通过
    json.dumps 序列化为合法 JSON 字符串。成功数据写入 stdout（标准输出），便于管道
    或其他程序继续读取；错误数据写入 stderr（标准错误），避免错误内容混入正常结果。

    输入：可选的参数列表；为 None 时 argparse 使用真实命令行参数。
    处理：解析参数、执行网页读取，并把成功或预期失败转换为对应 JSON。
    输出：成功返回退出码 0；失败返回退出码 1。操作系统和脚本调用者可据此快速判断
    命令是否成功，而不必先解析输出文本。
    """
    args = build_parser().parse_args(argv)

    try:
        result = read_page(args.url, args.timeout)
    except PageReadError as exc:
        error_result = {
            "error": {
                "type": exc.error_type,
                "message": str(exc),
                "url": args.url,
            }
        }
        # ensure_ascii=False 让中文保持可读；stderr 与退出码 1 共同表明本次命令失败。
        print(json.dumps(error_result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    # 正常结果单独写入 stdout，退出码 0 表示调用链完整成功。
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# Python 文件被直接运行时，__name__ 才等于 "__main__"；被测试或其他模块 import 时
# 不会自动执行命令行流程。SystemExit 会把 main 返回的 0 或 1 交给操作系统作为退出码。
if __name__ == "__main__":
    raise SystemExit(main())
