"""读取指定网页，并提取后续数据采集流程需要的基础原始数据。

本模块负责 Stage 1 的网页读取链路：校验 URL、发起 HTTP 请求、检查响应状态、解析
服务器返回的 HTML、删除确定性的结构噪声，再生成 Structured Blocks 和兼容纯文本。

当前实现默认用 requests 获取服务器 HTML；当静态请求出现可恢复的网络失败，或
Quality Gate 判断静态结果不可信时，才使用 Chromium 执行 JavaScript 并重新采集。
两条路径最终复用同一套 Structured Blocks 解析。本任务不判断广告、Cookie 提示或
动态推荐等非确定性噪声，这些内容仍可能出现在最终结果中。
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

# 兼容两种现有运行方式：测试通过 backend.page_reader 导入模块，而 README 使用
# python backend/page_reader.py 直接运行文件。两种情况下都复用相同的历史查询和变化检测逻辑。
if __package__:
    from .browser_reader import BrowserReadError, read_browser_page
    from .change_detection import ChangeDetectionError, detect_change
    from .content_quality import evaluate_content_quality
    from .content_diff import ContentDiffError, build_diff_result
    from .snapshot import (
        SnapshotError,
        build_snapshot_history,
        create_snapshot,
        save_snapshot,
    )
    from .structured_content import extract_structured_blocks, serialize_blocks
else:
    from browser_reader import BrowserReadError, read_browser_page
    from change_detection import ChangeDetectionError, detect_change
    from content_quality import evaluate_content_quality
    from content_diff import ContentDiffError, build_diff_result
    from snapshot import (
        SnapshotError,
        build_snapshot_history,
        create_snapshot,
        save_snapshot,
    )
    from structured_content import extract_structured_blocks, serialize_blocks


DEFAULT_TIMEOUT_SECONDS = 10.0
# 这里只列出可以确定不属于产品正文的 HTML 标签。header、main、aside、a、button
# 可能包含有效产品信息，因此不能因为它们有时包含噪声就直接删除。
NOISE_TAG_NAMES = ("script", "style", "noscript", "nav", "footer")
# User-Agent 用于向服务器说明请求来自哪个客户端。部分网站会拒绝没有该请求头的访问，
# 使用项目自己的标识也比伪装成真实浏览器更清晰、诚实。
USER_AGENT = "Product-Intelligence-Agent/0.1"
STATIC_ACQUISITION_METHOD = "static"
BROWSER_ACQUISITION_METHOD = "browser"
# 只有静态网络采集已经实际尝试、但没有得到可用响应时才进入浏览器。输入格式和超时
# 参数错误属于调用者输入问题，换一个采集工具也无法修复，因此不包含在这个集合中。
RECOVERABLE_STATIC_ERROR_TYPES = {"timeout", "request_failed", "http_error"}


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


def fetch_html(
    url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> tuple[int, bytes, str]:
    """请求网页并返回状态码、原始 HTML 字节和重定向后的最终 URL。

    在整个链路中的职责：只处理 HTTP 通信，不负责理解页面内容。函数会发送 GET 请求，
    接收 requests.Response 响应对象，并在把 HTML 交给解析步骤前确认请求确实成功。

    输入：经过 validate_url 校验的 HTTP/HTTPS URL，以及允许等待的超时秒数。
    处理：携带项目 User-Agent 发起 GET 请求；正常跟随重定向；区分超时、其他网络异常
    和非 2xx HTTP 状态。
    输出：``(status_code, raw_html_bytes, final_url)``；失败时抛出 PageReadError。
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
    return response.status_code, response.content, response.url


def extract_page_data_with_blocks(
    html: bytes,
) -> tuple[str, str, list[dict[str, Any]]]:
    """从 HTML 一次提取标题、Structured Blocks 和兼容 content。

    在整个链路中的职责：这是 Stage 1 V0.2-1 的结构化解析入口。它先建立页面树并读取
    title，再删除确定性噪声，从剩余 DOM 直接提取五类 Block，最后由 Block 稳定生成
    content。这样表格、列表和代码结构不会在第一步就退化成无法恢复的一段纯文本。

    输入：服务器返回的原始 HTML bytes（字节）。
    处理：BeautifulSoup 解析、title 提取、噪声删除、Block 提取和 content 序列化。
    输出：``(title, content, blocks)``；解析失败时抛出 PageReadError。

    业务边界：这里只读取服务器 HTML，不执行 JavaScript；第一版只支持 heading、
    paragraph、list、table、code，不进行主内容识别或网站专用解析。
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        blocks, content = build_structured_content(soup)
    except Exception as exc:
        raise PageReadError("parse_error", f"Failed to parse returned HTML: {exc}") from exc

    return title, content, blocks


def extract_page_data(html: bytes) -> tuple[str, str]:
    """从服务器返回的 HTML 中提取标题和标准化后的页面文本。

    在整个链路中的职责：把 fetch_html 返回的原始字节转换成后续阶段可使用的字符串。

    输入：服务器响应正文的原始 HTML bytes（字节）。
    处理：复用 extract_page_data_with_blocks 完成结构化解析，再只返回旧接口需要的部分。
    输出：``(title, content)`` 字符串元组；页面没有 title 标签时标题为空字符串。

    业务边界：BeautifulSoup 是 HTML 解析器，不是浏览器，不会执行 JavaScript。因此，
    JavaScript 运行后才出现的正文不在本函数的输入里，也就无法被提取。当前标准化只
    删除明确指定的标签，不进行通用主内容提取或网站专用判断。
    """
    title, content, _ = extract_page_data_with_blocks(html)
    return title, content


def build_structured_content(
    soup: BeautifulSoup,
) -> tuple[list[dict[str, Any]], str]:
    """删除确定性噪声，并从剩余 DOM 构造 blocks 和 content。

    在整个链路中的职责：把噪声清理与 Structured Blocks 提取按固定顺序连接起来，确保
    所有调用方都不会先丢失 HTML 结构再尝试恢复表格、列表或代码。

    输入：由 BeautifulSoup 解析完成、仍然保留 HTML 标签结构的页面树。
    处理：删除 script、style、noscript、nav、footer，再提取五类 Block 并序列化。
    输出：``(blocks, content)``；blocks 保留结构，content 兼容现有字符串链路。

    业务边界：这里只删除能够确定为结构噪声的标签。main、header、aside、a 和 button
    仍然保留。广告、Cookie 提示、动态日期和随机推荐等需要更复杂判断，本任务不处理。
    """
    # decompose 会把标签和它包含的全部内容一起从页面树中移除。必须在 get_text 之前做，
    # 否则脚本代码、导航文字等已经混入纯文本，之后很难可靠判断它们原本来自哪个标签。
    for noise_tag in soup.find_all(NOISE_TAG_NAMES):
        noise_tag.decompose()

    blocks = extract_structured_blocks(soup)
    return blocks, serialize_blocks(blocks)


def normalize_content(soup: BeautifulSoup) -> str:
    """保留旧调用接口，但最终 content 现在由 Structured Blocks 生成。

    输入：BeautifulSoup 页面树。
    处理：复用 build_structured_content 完成噪声删除、结构提取和稳定序列化。
    输出：供 Snapshot、Content Hash、Change Detection 和 Diff 继续使用的 content 字符串。

    该函数不再直接调用 soup.get_text("\\n")。blocks 由 read_page 单独返回；旧调用方只
    需要 content 时仍可继续使用本函数，因此现有函数名称和参数保持不变。
    """
    _, content = build_structured_content(soup)
    return content


def _build_page_result(
    *,
    requested_url: str,
    final_url: str,
    status_code: int,
    html: bytes,
    acquisition_method: str,
    browser_title: str | None = None,
) -> dict[str, Any]:
    """把静态或浏览器 HTML 统一转换为同一种页面数据结构。

    输入：请求 URL、最终 URL、状态码、HTML、采集方式，以及浏览器可选标题。
    处理：复用现有 Structured Blocks 解析；浏览器明确返回标题时优先使用该值。
    输出：可交给 Quality Gate，并在 PASS 后交给 create_snapshot 的页面字典。

    ``url`` 暂时继续等于 requested_url，保持当前历史查询身份稳定；final_url 单独保存，
    避免重定向信息丢失，但本任务不扩展完整 URL Identity 规则。
    """
    extracted_title, content, blocks = extract_page_data_with_blocks(html)
    title = browser_title if browser_title else extracted_title
    return {
        "url": requested_url,
        "requested_url": requested_url,
        "final_url": final_url,
        "status_code": status_code,
        "title": title,
        "content": content,
        "blocks": blocks,
        "acquisition_method": acquisition_method,
    }


def read_page(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """串联 URL 校验、网页请求和 HTML 解析，生成一次完整的网页读取结果。

    在整个链路中的职责：作为 Task 1 的业务入口，按固定顺序组织三个步骤，确保调用者
    不会漏掉 URL 校验或状态码检查，也让命令行和未来的其他调用方复用相同行为。

    输入：URL 字符串和可选的超时秒数。
    处理：先检查 timeout，再校验 URL、获取 HTML、提取标题、blocks 和兼容页面文本。
    输出：页面原有字段，以及 requested_url、final_url、acquisition_method 审计字段。
    该字典仍是 Python 对象，最终由 main 中的 json.dumps 转换成 JSON 字符串。
    """
    # 非正数超时没有实际意义，也可能让 requests 产生不够直观的底层错误，因此提前拒绝。
    if timeout <= 0:
        raise PageReadError("invalid_timeout", "Timeout must be greater than zero.")

    # 三步各自只负责一种工作；在这里串联后，数据依次从 URL 变为 HTML，再变为结构化字典。
    validated_url = validate_url(url)
    status_code, html, final_url = fetch_html(validated_url, timeout)
    return _build_page_result(
        requested_url=validated_url,
        final_url=final_url,
        status_code=status_code,
        html=html,
        acquisition_method=STATIC_ACQUISITION_METHOD,
    )


def acquire_page_with_browser_fallback(
    url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """执行“静态优先、质量不合格时浏览器兜底”的完整采集决策。

    输入：用户指定 URL 和静态 HTTP 请求超时。
    处理：先调用 read_page。静态内容 PASS 时立即返回；质量 WARNING/FAIL，或静态请求
    出现可恢复的 timeout、request_failed、http_error 时，改用 browser_reader 获取渲染
    DOM，复用同一 Structured Blocks 入口，再运行一次 Quality Gate。
    输出：选中的 page_data、最终 quality_gate、静态/浏览器质量结果，以及可选的静态
    采集错误审计信息。

    本函数只选择可信采集结果，不创建或保存 Snapshot。浏览器结果仍不合格时也会返回，
    由 main 在 create_snapshot 之前统一 Fail Closed。
    """
    static_quality: dict[str, Any] | None = None
    static_acquisition_error: dict[str, str] | None = None

    try:
        static_page_data = read_page(url, timeout)
    except PageReadError as exc:
        if exc.error_type not in RECOVERABLE_STATIC_ERROR_TYPES:
            # invalid_url、invalid_timeout 和 parse_error 不属于浏览器能够合理修复的网络
            # 采集失败。直接保留原错误，也避免为明显无效输入启动昂贵的 Chromium。
            raise
        static_acquisition_error = {
            "type": exc.error_type,
            "message": str(exc),
        }
        # 能进入此分支说明 read_page 已成功完成 URL 校验并在后续网络步骤失败。再次调用
        # validate_url 只为取得去除首尾空白后的稳定 requested_url，不会发起网络请求。
        requested_url = validate_url(url)
    else:
        requested_url = static_page_data["requested_url"]
        static_quality = evaluate_content_quality(
            static_page_data["content"], static_page_data["blocks"]
        )
        if static_quality["downstream_allowed"]:
            return {
                "page_data": static_page_data,
                "quality_gate": static_quality,
                "static_quality_gate": static_quality,
                "browser_quality_gate": None,
                "static_acquisition_error": None,
            }

    try:
        browser_capture = read_browser_page(requested_url)
    except BrowserReadError as exc:
        # 浏览器错误仍由 main 按原有方式输出。若此前静态请求也失败，把该事实附在异常上，
        # 让最终错误 JSON 能同时说明两次采集都未成功，而无需引入复杂 tracing 系统。
        exc.static_acquisition_error = static_acquisition_error
        raise

    browser_page_data = _build_page_result(
        requested_url=requested_url,
        final_url=browser_capture["final_url"],
        status_code=browser_capture["status_code"],
        html=browser_capture["html"],
        acquisition_method=BROWSER_ACQUISITION_METHOD,
        browser_title=browser_capture["title"],
    )
    browser_quality = evaluate_content_quality(
        browser_page_data["content"], browser_page_data["blocks"]
    )
    return {
        "page_data": browser_page_data,
        "quality_gate": browser_quality,
        "static_quality_gate": static_quality,
        "browser_quality_gate": browser_quality,
        "static_acquisition_error": static_acquisition_error,
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
    """执行 Stage 1 网页监控链路，并用 JSON 与退出码报告结果。

    main 是命令行入口：它读取参数并执行“静态优先、浏览器兜底”的采集流程。最终
    Quality Gate 只有 PASS 才调用 create_snapshot 和 save_snapshot；WARNING/FAIL 会
    直接返回诊断结果，避免污染历史。保存成功后才查询 Previous Snapshot、比较
    content_hash，并根据 changed 决定是否生成行级 Diff。

    输入：可选的参数列表；为 None 时 argparse 使用真实命令行参数。
    处理：读取并评估网页；可信时保存、查询历史、判断变化并按需生成 Diff。
    输出：完整可信链路返回 0；请求错误或质量门槛拒绝下游时返回 1。
    """
    args = build_parser().parse_args(argv)

    try:
        acquisition_result = acquire_page_with_browser_fallback(
            args.url, args.timeout
        )
        page_data = acquisition_result["page_data"]
        quality_result = acquisition_result["quality_gate"]
        if not quality_result["downstream_allowed"]:
            # 浏览器已经返回并完成解析，但最终内容仍不足以成为可信历史。这里在
            # create_snapshot 之前返回，所以不会写文件，也不会触发历史比较。
            rejected_result = {
                **page_data,
                "quality_gate": quality_result,
                "static_quality_gate": acquisition_result["static_quality_gate"],
                "browser_quality_gate": acquisition_result["browser_quality_gate"],
                "static_acquisition_error": acquisition_result[
                    "static_acquisition_error"
                ],
            }
            print(json.dumps(rejected_result, ensure_ascii=False, indent=2))
            return 1

        current_snapshot = create_snapshot(page_data)
        save_snapshot(current_snapshot)
        snapshot_history = build_snapshot_history(current_snapshot)
        change_result = detect_change(snapshot_history)
        result = build_diff_result(change_result)
        result["quality_gate"] = quality_result
        result["static_quality_gate"] = acquisition_result["static_quality_gate"]
        result["browser_quality_gate"] = acquisition_result["browser_quality_gate"]
        result["static_acquisition_error"] = acquisition_result[
            "static_acquisition_error"
        ]
        # 成功输出虽然已经保留完整 current_snapshot，但把三个采集审计字段放在顶层，
        # 可以让命令行调用者无需理解历史结构就直接看出正文来源和重定向结果。
        result["acquisition_method"] = page_data["acquisition_method"]
        result["requested_url"] = page_data["requested_url"]
        result["final_url"] = page_data["final_url"]
    except (
        PageReadError,
        BrowserReadError,
        SnapshotError,
        ChangeDetectionError,
        ContentDiffError,
    ) as exc:
        error_result = {
            "error": {
                "type": exc.error_type,
                "message": str(exc),
                "url": args.url,
            }
        }
        static_acquisition_error = getattr(exc, "static_acquisition_error", None)
        if static_acquisition_error is not None:
            error_result["static_acquisition_error"] = static_acquisition_error
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
