"""V0.3-4A/4B 正式主扫描链路的本地三轮人工 E2E 验收。

脚本只负责提供同一个本地 HTTP URL、切换 fixture 中的可控价格，并调用正式
``backend.page_reader.main``。Traversal、Snapshot、Previous Snapshot、页面级变化检测
、State Matching 和 Contextual State Diff 全部由生产入口执行；这里不复制任何匹配、
点击或 Diff 业务逻辑。
"""

from __future__ import annotations

import io
import json
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from backend import page_reader
from backend import snapshot as snapshot_module


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "v034a_state_matching_e2e.html"
)
OUTPUT_ROOT = Path(__file__).parent / "output" / "v034a_state_matching_e2e"
FIXTURE_ROUTE = "/interactive-membership"


def _configure_utf8_output() -> None:
    """让 Windows 终端稳定显示中文路径、状态和 JSON。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _build_handler(
    fixture_template: str, fixture_values: dict[str, str]
) -> type[BaseHTTPRequestHandler]:
    """建立读取共享价格值的本地 HTTP Handler。

    输入：HTML 模板和三轮扫描之间会被脚本修改的价格字典。
    处理：每次请求都用当时的价格替换占位符，因此 URL 始终相同，响应内容可以受控变化。
    输出：供 ThreadingHTTPServer 使用的 Handler 类型。
    """

    class FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - 标准库要求的方法名
            if self.path.split("?", 1)[0] != FIXTURE_ROUTE:
                body = b"Not Found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            html = fixture_template.replace(
                "__MONTHLY_PRICE__", fixture_values["monthly_price"]
            ).replace("__YEARLY_PRICE__", fixture_values["yearly_price"])
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # 禁止缓存确保第三轮浏览器一定读取更新后的受控响应。
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            """关闭本地服务器访问日志，避免干扰正式扫描摘要。"""

    return FixtureHandler


def _run_formal_scan(url: str) -> dict[str, Any]:
    """通过正式 page_reader 命令入口执行一轮扫描并解析其 JSON 输出。

    本函数只捕获 stdout 以便形成验收摘要，不自行调用 Traversal、Snapshot History 或
    State Matching。正式入口返回非零退出码或非 JSON 时立即报告原始 stderr。
    """
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = page_reader.main([url])
    if exit_code != 0:
        raise RuntimeError(
            "正式扫描失败。\n"
            f"exit_code={exit_code}\nstdout={stdout.getvalue()}\n"
            f"stderr={stderr.getvalue()}"
        )
    try:
        result = json.loads(stdout.getvalue())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"正式扫描没有输出合法 JSON：{exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("正式扫描输出必须是 JSON object。")
    return result


def _run_scan_and_locate_snapshot(
    url: str, snapshot_directory: Path
) -> tuple[dict[str, Any], Path]:
    """执行正式扫描，并用目录前后差集找到本轮真正保存的 Snapshot 文件。"""
    before = set(snapshot_directory.glob("*.json"))
    result = _run_formal_scan(url)
    after = set(snapshot_directory.glob("*.json"))
    created = sorted(after - before)
    if len(created) != 1:
        raise RuntimeError(
            f"本轮预期新增 1 份 Snapshot，实际新增 {len(created)} 份。"
        )
    return result, created[0]


def _state_rows(scan_result: dict[str, Any]) -> list[dict[str, Any]]:
    """把正式 State Matching 结果整理成便于人工核对的逐状态摘要。

    First Scan 没有 state_changes，因此从 current Snapshot 显示 baseline；导航节点明确
    显示 excluded。第二、三轮只读取生产 matcher 的结果，不自行比较 Hash。
    """
    snapshot = scan_result["current_snapshot"]
    comparison = scan_result["interactive_state_comparison"]
    changes_by_key = {
        change["state_key"]: change
        for change in comparison.get("state_changes", [])
        if isinstance(change, dict) and isinstance(change.get("state_key"), str)
    }
    rows: list[dict[str, Any]] = []
    current_keys: set[str] = set()
    for state in snapshot.get("interactive_states", []):
        if not isinstance(state, dict):
            continue
        state_key = state.get("state_key")
        if not isinstance(state_key, str):
            continue
        current_keys.add(state_key)
        eligible = state.get("comparison_eligible", True) is not False
        change = changes_by_key.get(state_key)
        rows.append(
            {
                "state_path": state.get("state_path"),
                "comparison_eligible": eligible,
                "change_type": (
                    change.get("change_type")
                    if change is not None
                    else "baseline" if eligible else "excluded"
                ),
                "previous_hash": (
                    change.get("previous_content_hash") if change else None
                ),
                "current_hash": (
                    change.get("current_content_hash")
                    if change
                    else state.get("content_hash")
                ),
            }
        )

    # 当前 fixture 不预期 previous-only，但保留它可让摘要真实反映生产输出。
    for state_key, change in changes_by_key.items():
        if state_key in current_keys:
            continue
        rows.append(
            {
                "state_path": change.get("state_path"),
                "comparison_eligible": True,
                "change_type": change.get("change_type"),
                "previous_hash": change.get("previous_content_hash"),
                "current_hash": change.get("current_content_hash"),
            }
        )
    return rows


def _scan_summary(
    scan_number: int,
    scan_result: dict[str, Any],
    snapshot_path: Path,
) -> dict[str, Any]:
    """提取用户指定的每轮 Snapshot、遍历与状态比较字段。"""
    snapshot = scan_result["current_snapshot"]
    comparison = scan_result["interactive_state_comparison"]
    traversal = snapshot.get("interactive_state_traversal") or {}
    contextual_state_diff = scan_result.get("contextual_state_diff")
    if not isinstance(contextual_state_diff, list):
        raise RuntimeError("正式扫描输出缺少 list 类型的 contextual_state_diff。")
    return {
        "scan": scan_number,
        "snapshot_file": str(snapshot_path.resolve()),
        "snapshot_captured_at": snapshot.get("captured_at"),
        "interactive_state_schema_version": snapshot.get(
            "interactive_state_schema_version"
        ),
        "traversal_status": traversal.get("status"),
        "interactive_state_comparison_changed": comparison.get("changed"),
        "contextual_state_diff_count": len(contextual_state_diff),
        "state_results": _state_rows(scan_result),
    }


def _change_type_for_suffix(
    scan_result: dict[str, Any], suffix: str
) -> str | None:
    """从生产比较结果中查找以指定语义状态结尾的 change_type。"""
    for change in scan_result["interactive_state_comparison"].get(
        "state_changes", []
    ):
        state_path = change.get("state_path") if isinstance(change, dict) else None
        if isinstance(state_path, list) and state_path and state_path[-1] == suffix:
            return change.get("change_type")
    return None


def _field_contains_text(value: Any, field_name: str, expected_text: str) -> bool:
    """递归检查结构化 Diff 的指定字段是否包含验收文字。

    这里只读取正式 4B 输出进行 fixture 验收，不生成或修正 Diff。限定字段名可以避免
    Price 100 偶然出现在 current、Hash 或其他审计位置时造成假通过。
    """
    if isinstance(value, dict):
        for key, child in value.items():
            if key == field_name and expected_text in json.dumps(
                child, ensure_ascii=False
            ):
                return True
            if _field_contains_text(child, field_name, expected_text):
                return True
    elif isinstance(value, list):
        return any(
            _field_contains_text(child, field_name, expected_text)
            for child in value
        )
    return False


def _verify_expected_results(scan_results: list[dict[str, Any]]) -> dict[str, Any]:
    """核对三轮正式结果和导航节点排除规则，返回可保留的验收记录。"""
    first, second, third = scan_results
    first_comparison = first["interactive_state_comparison"]
    second_comparison = second["interactive_state_comparison"]
    third_comparison = third["interactive_state_comparison"]
    first_state_diff = first.get("contextual_state_diff")
    second_state_diff = second.get("contextual_state_diff")
    third_state_diff = third.get("contextual_state_diff")
    if not all(
        isinstance(value, list)
        for value in (first_state_diff, second_state_diff, third_state_diff)
    ):
        raise RuntimeError("三轮正式扫描必须都输出 list 类型 contextual_state_diff。")

    third_diff_paths = [
        diff.get("state_path")
        for diff in third_state_diff
        if isinstance(diff, dict)
    ]
    third_diff_keys = {
        diff.get("state_key")
        for diff in third_state_diff
        if isinstance(diff, dict) and isinstance(diff.get("state_key"), str)
    }

    checks = {
        "scan_1_is_baseline": (
            first_comparison.get("comparison_status") == "baseline"
            and first_comparison.get("changed") is None
            and bool(first["current_snapshot"].get("interactive_states"))
        ),
        "scan_2_states_are_unchanged": (
            second_comparison.get("changed") is False
            and _change_type_for_suffix(second, "Monthly") == "unchanged"
            and _change_type_for_suffix(second, "Yearly") == "unchanged"
        ),
        "scan_3_monthly_unchanged": (
            _change_type_for_suffix(third, "Monthly") == "unchanged"
        ),
        "scan_3_yearly_modified": (
            third_comparison.get("changed") is True
            and _change_type_for_suffix(third, "Yearly") == "modified"
        ),
        "scan_1_contextual_state_diff_empty": first_state_diff == [],
        "scan_2_contextual_state_diff_empty": second_state_diff == [],
        "scan_3_only_yearly_has_contextual_state_diff": (
            len(third_state_diff) == 1
            and third_diff_paths == [["Personal", "Yearly"]]
        ),
        "scan_3_previous_contains_price_100": _field_contains_text(
            third_state_diff, "previous", "Price 100"
        ),
        "scan_3_current_contains_price_120": _field_contains_text(
            third_state_diff, "current", "Price 120"
        ),
        "monthly_has_no_contextual_state_diff": all(
            not (isinstance(path, list) and path and path[-1] == "Monthly")
            for path in third_diff_paths
        ),
        "reference_has_no_contextual_state_diff": all(
            not (isinstance(path, list) and path and path[-1] == "Reference")
            for path in third_diff_paths
        ),
    }

    navigation_keys: set[str] = set()
    compared_keys: set[str] = set()
    for result in scan_results:
        for state in result["current_snapshot"].get("interactive_states", []):
            if isinstance(state, dict) and state.get("comparison_eligible") is False:
                state_key = state.get("state_key")
                if isinstance(state_key, str):
                    navigation_keys.add(state_key)
        for change in result["interactive_state_comparison"].get("state_changes", []):
            if isinstance(change, dict) and isinstance(change.get("state_key"), str):
                compared_keys.add(change["state_key"])

    checks["navigation_node_exists"] = bool(navigation_keys)
    checks["navigation_nodes_excluded_from_comparison"] = not bool(
        navigation_keys & compared_keys
    )
    checks["navigation_nodes_excluded_from_contextual_state_diff"] = not bool(
        navigation_keys & third_diff_keys
    )
    failed_checks = [name for name, passed in checks.items() if not passed]
    if failed_checks:
        raise RuntimeError(
            "V0.3-4A E2E 结果不符合预期：" + ", ".join(failed_checks)
        )
    return {
        "checks": checks,
        "navigation_state_keys": sorted(navigation_keys),
        "navigation_keys_found_in_state_changes": sorted(
            navigation_keys & compared_keys
        ),
        "navigation_keys_found_in_contextual_state_diff": sorted(
            navigation_keys & third_diff_keys
        ),
    }


def main() -> int:
    """启动本地服务，并通过正式扫描入口完成 baseline、unchanged、modified 三轮验收。"""
    _configure_utf8_output()
    if not FIXTURE_PATH.is_file():
        print(f"找不到 fixture：{FIXTURE_PATH}", file=sys.stderr)
        return 1

    run_id = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    run_directory = OUTPUT_ROOT / run_id
    snapshot_directory = run_directory / "snapshots"
    run_directory.mkdir(parents=True, exist_ok=False)
    snapshot_directory.mkdir()

    fixture_template = FIXTURE_PATH.read_text(encoding="utf-8")
    fixture_values = {"monthly_price": "10", "yearly_price": "100"}
    handler = _build_handler(fixture_template, fixture_values)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{server.server_port}{FIXTURE_ROUTE}"

    original_snapshot_directory = snapshot_module.DEFAULT_SNAPSHOT_DIRECTORY
    snapshot_module.DEFAULT_SNAPSHOT_DIRECTORY = snapshot_directory
    scan_results: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []

    try:
        scan_plan = [
            (1, "10", "100"),
            (2, "10", "100"),
            (3, "10", "120"),
        ]
        for scan_number, monthly_price, yearly_price in scan_plan:
            fixture_values["monthly_price"] = monthly_price
            fixture_values["yearly_price"] = yearly_price
            scan_result, snapshot_path = _run_scan_and_locate_snapshot(
                url, snapshot_directory
            )
            scan_results.append(scan_result)
            summary = _scan_summary(scan_number, scan_result, snapshot_path)
            summaries.append(summary)

            # 每轮立即保存正式完整输出；即使后续人工核验失败，已完成轮次仍可追溯。
            (run_directory / f"scan_{scan_number}_formal_output.json").write_text(
                json.dumps(scan_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"\n===== Scan {scan_number} =====")
            print(json.dumps(summary, ensure_ascii=False, indent=2))

        verification = _verify_expected_results(scan_results)
        scan_3_contextual_state_diff = scan_results[2]["contextual_state_diff"]
        acceptance_output = {
            "url": url,
            "fixture": str(FIXTURE_PATH.resolve()),
            "snapshot_directory": str(snapshot_directory.resolve()),
            "scans": summaries,
            "scan_3_contextual_state_diff": scan_3_contextual_state_diff,
            "verification": verification,
        }
        output_path = run_directory / "acceptance_summary.json"
        output_path.write_text(
            json.dumps(acceptance_output, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("\n===== E2E Verification =====")
        print(json.dumps(verification, ensure_ascii=False, indent=2))
        print("\n===== Scan 3 完整 contextual_state_diff =====")
        print(json.dumps(scan_3_contextual_state_diff, ensure_ascii=False, indent=2))
        print(f"\n完整验收输出已保留：{run_directory.resolve()}")
        return 0
    except (OSError, RuntimeError, KeyError, TypeError) as exc:
        failure_path = run_directory / "acceptance_failure.txt"
        failure_path.write_text(str(exc) + "\n", encoding="utf-8")
        print(f"\nE2E 验收中断：{exc}", file=sys.stderr)
        print(f"已完成输出保留在：{run_directory.resolve()}", file=sys.stderr)
        return 1
    finally:
        snapshot_module.DEFAULT_SNAPSHOT_DIRECTORY = original_snapshot_directory
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
