"""解析安全 Tab Group 实际控制的局部业务内容范围。

本模块属于 Stage 1 V0.3-3C-1，只返回 Local Scope 的运行时 DOM 定位和诊断信息。
它不点击 Tab、不生成 Structured Blocks 或 content_hash，也不写入 Snapshot。调用方需要先
采集 baseline，再使用已经冻结的 3B 安全点击能力切换一次状态，最后调用解析函数。
"""

from __future__ import annotations

import hashlib
from typing import Any

from backend.interactive_state import discover_safe_tab_groups


MAX_SCOPE_ANCESTOR_DEPTH = 12


LOCAL_SCOPE_PROBE_SCRIPT = r"""
({ tablistPath, maxAncestorDepth }) => {
    const normalizeText = (value) => (value || "").replace(/\s+/g, " ").trim();
    const hasRole = (element, expectedRole) => {
        const roles = (element.getAttribute("role") || "")
            .split(/\s+/)
            .map((role) => role.toLowerCase())
            .filter(Boolean);
        return roles.includes(expectedRole);
    };
    const attributeIsTrue = (element, name) =>
        (element.getAttribute(name) || "").trim().toLowerCase() === "true";
    const isVisible = (element) => {
        if (!element || !element.isConnected) {
            return false;
        }
        let current = element;
        while (current) {
            if (
                current.hidden
                || current.hasAttribute("hidden")
                || attributeIsTrue(current, "aria-hidden")
            ) {
                return false;
            }
            const style = window.getComputedStyle(current);
            if (
                style.display === "none"
                || style.visibility === "hidden"
                || style.visibility === "collapse"
                || Number.parseFloat(style.opacity) === 0
            ) {
                return false;
            }
            current = current.parentElement;
        }
        return true;
    };
    const nearestOwningTablist = (tab) => {
        let current = tab.parentElement;
        while (current) {
            if (hasRole(current, "tablist")) {
                return current;
            }
            current = current.parentElement;
        }
        return null;
    };
    const buildRuntimeDomPath = (element) => {
        const parts = [];
        let current = element;
        while (current && current.nodeType === Node.ELEMENT_NODE) {
            const tag = current.tagName.toLowerCase();
            if (tag === "body") {
                parts.unshift("body");
                break;
            }
            const sameTagSiblings = current.parentElement
                ? Array.from(current.parentElement.children)
                    .filter((sibling) => sibling.tagName === current.tagName)
                : [current];
            parts.unshift(`${tag}:nth-of-type(${sameTagSiblings.indexOf(current) + 1})`);
            current = current.parentElement;
        }
        return parts;
    };
    const serializeVisibleNonControlContent = (element) => {
        if (!isVisible(element)) {
            return "";
        }
        if (
            hasRole(element, "tablist")
            || hasRole(element, "tab")
            || ["SCRIPT", "STYLE", "NOSCRIPT"].includes(element.tagName)
        ) {
            return "";
        }

        const directText = Array.from(element.childNodes)
            .filter((node) => node.nodeType === Node.TEXT_NODE)
            .map((node) => normalizeText(node.textContent))
            .filter(Boolean)
            .join(" ");
        const childContent = Array.from(element.children)
            .map((child) => serializeVisibleNonControlContent(child))
            .filter(Boolean)
            .join("");
        if (!directText && !childContent) {
            return "";
        }
        const tag = element.tagName.toLowerCase();
        return `<${tag}>${directText}${childContent}</${tag}>`;
    };

    const tablist = document.querySelector(tablistPath.join(" > "));
    if (!tablist || !hasRole(tablist, "tablist")) {
        return { error: "tab_group_runtime_locator_invalid" };
    }

    const tabs = Array.from(tablist.querySelectorAll("[role]"))
        .filter((element) =>
            hasRole(element, "tab") && nearestOwningTablist(element) === tablist
        );
    const selectedTabs = tabs.filter((tab) =>
        (tab.getAttribute("aria-selected") || "").trim().toLowerCase() === "true"
    );
    if (selectedTabs.length !== 1) {
        return { error: "selected_state_invalid" };
    }

    const selectedTab = selectedTabs[0];
    const selectedTabId = (selectedTab.id || "").trim();
    const controlledIds = (selectedTab.getAttribute("aria-controls") || "")
        .split(/\s+/)
        .filter(Boolean);
    const controlledElements = controlledIds
        .map((id) => document.getElementById(id))
        .filter(Boolean);

    let ariaMethod = null;
    let ariaTargets = [];
    if (controlledElements.length > 0) {
        ariaMethod = "aria_controls";
        ariaTargets = controlledElements;
    } else if (selectedTabId) {
        ariaTargets = Array.from(document.querySelectorAll("[role]"))
            .filter((element) => hasRole(element, "tabpanel"))
            .filter((panel) =>
                (panel.getAttribute("aria-labelledby") || "")
                    .split(/\s+/)
                    .filter(Boolean)
                    .includes(selectedTabId)
            );
        if (ariaTargets.length > 0) {
            ariaMethod = "tabpanel_aria_labelledby";
        }
    }

    const ariaScopes = ariaTargets.map((element) => ({
        runtime_dom_path: buildRuntimeDomPath(element),
        tag: element.tagName.toLowerCase(),
        role: element.getAttribute("role"),
        id: element.id || null,
        visible: isVisible(element),
        is_document_root: element === document.body || element === document.documentElement,
    }));

    const ancestorCandidates = [];
    let ancestor = tablist.parentElement;
    let depth = 1;
    let reachedDocumentRoot = false;
    let documentRootContent = null;
    while (ancestor && depth <= maxAncestorDepth) {
        if (ancestor === document.body || ancestor === document.documentElement) {
            reachedDocumentRoot = true;
            // 根节点只计算诊断指纹，用于说明变化是否只能在全页范围观察到；无论结果
            // 如何，html/body 都不会作为成功 Local Scope 返回。
            documentRootContent = serializeVisibleNonControlContent(ancestor);
            break;
        }
        const canonicalContent = serializeVisibleNonControlContent(ancestor);
        ancestorCandidates.push({
            depth,
            runtime_dom_path: buildRuntimeDomPath(ancestor),
            canonical_content: canonicalContent,
            content_chars: canonicalContent.length,
        });
        ancestor = ancestor.parentElement;
        depth += 1;
    }

    return {
        error: null,
        selected_tab: {
            text: normalizeText(selectedTab.innerText || selectedTab.textContent),
            id: selectedTabId || null,
            aria_controls: controlledIds,
        },
        aria_method: ariaMethod,
        aria_scopes: ariaScopes,
        ancestor_candidates: ancestorCandidates,
        reached_document_root: reachedDocumentRoot,
        document_root_content: documentRootContent,
    };
}
"""


def _fingerprint(content: str) -> str:
    """把只用于范围比较的规范 DOM 内容转换成 SHA-256 运行时指纹。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _group_runtime_path(group: dict[str, Any]) -> list[str] | None:
    """读取 3A 提供的 Tab Group 运行时路径，不接触 semantic scope_path。"""
    runtime_locator = group.get("runtime_locator")
    if not isinstance(runtime_locator, dict):
        return None
    dom_path = runtime_locator.get("dom_path")
    if not isinstance(dom_path, list) or not all(
        isinstance(part, str) and part for part in dom_path
    ):
        return None
    return list(dom_path)


def _find_current_safe_group(
    page: Any, expected_group: dict[str, Any]
) -> dict[str, Any] | None:
    """重新执行冻结的 3A 发现，确认目标组仍是同一安全 Tab Group。"""
    expected_path = _group_runtime_path(expected_group)
    if expected_path is None:
        return None
    for current_group in discover_safe_tab_groups(page):
        if _group_runtime_path(current_group) == expected_path:
            return current_group
    return None


def _collect_probe(page: Any, group: dict[str, Any]) -> dict[str, Any]:
    """采集 ARIA 关系和候选祖先的非控制内容指纹。

    输入：当前仍安全的 Tab Group。
    处理：浏览器内排除 tablist/tab 控件及隐藏内容，再为每层非根祖先生成规范内容；Python
    只保留 SHA-256 指纹和长度，不把大段临时 DOM 放进诊断结果。
    输出：用于点击前后比较的纯运行时探针数据。
    """
    tablist_path = _group_runtime_path(group)
    if tablist_path is None:
        return {"success": False, "error": "tab_group_runtime_locator_invalid"}
    raw_probe = page.evaluate(
        LOCAL_SCOPE_PROBE_SCRIPT,
        {
            "tablistPath": tablist_path,
            "maxAncestorDepth": MAX_SCOPE_ANCESTOR_DEPTH,
        },
    )
    if not isinstance(raw_probe, dict) or raw_probe.get("error"):
        return {
            "success": False,
            "error": (
                raw_probe.get("error")
                if isinstance(raw_probe, dict)
                else "local_scope_probe_invalid"
            ),
        }

    candidates: list[dict[str, Any]] = []
    for candidate in raw_probe.get("ancestor_candidates", []):
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("canonical_content")
        runtime_dom_path = candidate.get("runtime_dom_path")
        if not isinstance(content, str) or not isinstance(runtime_dom_path, list):
            continue
        candidates.append(
            {
                "depth": candidate.get("depth"),
                "runtime_dom_path": list(runtime_dom_path),
                "content_fingerprint": _fingerprint(content),
                "content_chars": len(content),
            }
        )

    document_root_content = raw_probe.get("document_root_content")
    return {
        "success": True,
        "error": None,
        "tab_group_runtime_path": tablist_path,
        "selected_tab": raw_probe.get("selected_tab"),
        "aria_method": raw_probe.get("aria_method"),
        "aria_scopes": raw_probe.get("aria_scopes", []),
        "ancestor_candidates": candidates,
        "reached_document_root": bool(raw_probe.get("reached_document_root")),
        "document_root_content_fingerprint": (
            _fingerprint(document_root_content)
            if isinstance(document_root_content, str)
            else None
        ),
        "controls_excluded": ["tablist", "tab"],
    }


def _failure(code: str, message: str, diagnostics: dict[str, Any]) -> dict[str, Any]:
    """生成稳定的 Local Scope 失败结果。"""
    return {
        "success": False,
        "error": {"code": code, "message": message},
        "method": None,
        "runtime_locator": None,
        "diagnostics": diagnostics,
    }


def capture_local_scope_baseline(
    page: Any, tab_group: dict[str, Any]
) -> dict[str, Any]:
    """在安全点击前记录 Local Scope 解析基线。

    输入：Playwright Page 和 3A 已发现的安全 Tab Group。
    处理：重新确认组仍通过 3A，再记录明确 ARIA 关系与每层非根祖先的非控制内容指纹。
    输出：只供同一次浏览器会话后续解析使用的 baseline；不包含 State 内容或持久化身份。
    """
    current_group = _find_current_safe_group(page, tab_group)
    if current_group is None:
        return {
            "success": False,
            "error": {
                "code": "unsafe_tab_group",
                "message": "Tab group no longer passes Safe Tab Group Discovery.",
            },
        }
    probe = _collect_probe(page, current_group)
    if not probe.get("success"):
        return {
            "success": False,
            "error": {
                "code": probe.get("error", "local_scope_probe_failed"),
                "message": "Could not capture the Local Scope baseline.",
            },
        }
    return {"success": True, "error": None, "probe": probe}


def resolve_local_scope(
    page: Any,
    tab_group: dict[str, Any],
    baseline: dict[str, Any],
    safe_click_result: dict[str, Any],
) -> dict[str, Any]:
    """在一次成功安全切换后解析 Tab Group 的 Local Scope。

    输入：Page、原安全组、点击前 baseline，以及 3B click_safe_tab 的结果。
    处理：先验证 3B 成功且属于同一运行时组；优先采用当前选中 Tab 的明确 ARIA 内容关系。
    若没有明确 panel，则对比点击前后排除了 Tab 控件的祖先内容指纹，选择由近到远第一个
    发生主要非控制内容变化的容器。html/body 永远不能成为成功结果。
    输出：成功时仅返回 Local Scope 运行时 DOM 路径和诊断；失败时返回稳定原因码。
    """
    diagnostics: dict[str, Any] = {
        "controls_excluded": ["tablist", "tab"],
        "baseline_selected_tab": None,
        "current_selected_tab": None,
        "changed_candidates": [],
    }
    if baseline.get("success") is not True or not isinstance(
        baseline.get("probe"), dict
    ):
        return _failure(
            "invalid_baseline",
            "A successful pre-click Local Scope baseline is required.",
            diagnostics,
        )
    if safe_click_result.get("success") is not True:
        diagnostics["safe_click_error"] = safe_click_result.get("error")
        return _failure(
            "safe_click_not_validated",
            "Local Scope fallback requires a successfully validated Tab switch.",
            diagnostics,
        )

    baseline_probe = baseline["probe"]
    expected_path = _group_runtime_path(tab_group)
    if baseline_probe.get("tab_group_runtime_path") != expected_path:
        return _failure(
            "baseline_group_mismatch",
            "Local Scope baseline does not belong to the requested Tab group.",
            diagnostics,
        )
    click_runtime = safe_click_result.get("runtime")
    click_path = (
        click_runtime.get("tablist_dom_path")
        if isinstance(click_runtime, dict)
        else None
    )
    if expected_path is None or click_path != expected_path:
        return _failure(
            "click_group_mismatch",
            "Safe click result does not belong to the requested Tab group.",
            diagnostics,
        )

    current_group = _find_current_safe_group(page, tab_group)
    if current_group is None:
        return _failure(
            "unsafe_tab_group",
            "Tab group no longer passes Safe Tab Group Discovery after clicking.",
            diagnostics,
        )
    current_probe = _collect_probe(page, current_group)
    if not current_probe.get("success"):
        diagnostics["probe_error"] = current_probe.get("error")
        return _failure(
            "local_scope_probe_failed",
            "Could not inspect the Tab group after clicking.",
            diagnostics,
        )

    diagnostics["baseline_selected_tab"] = baseline_probe.get("selected_tab")
    diagnostics["current_selected_tab"] = current_probe.get("selected_tab")
    diagnostics["tab_group_runtime_path"] = expected_path

    aria_scopes = [
        scope
        for scope in current_probe.get("aria_scopes", [])
        if isinstance(scope, dict) and scope.get("visible")
    ]
    allowed_aria_scopes = [
        scope for scope in aria_scopes if not scope.get("is_document_root")
    ]
    if len(allowed_aria_scopes) == 1:
        selected_scope = allowed_aria_scopes[0]
        diagnostics["aria_relationship"] = {
            "method": current_probe.get("aria_method"),
            "scope_count": len(aria_scopes),
            "scope": selected_scope,
        }
        return {
            "success": True,
            "error": None,
            "method": current_probe.get("aria_method"),
            "runtime_locator": {
                "dom_path": list(selected_scope["runtime_dom_path"])
            },
            "diagnostics": diagnostics,
        }
    if aria_scopes and not allowed_aria_scopes:
        diagnostics["aria_relationship"] = {
            "method": current_probe.get("aria_method"),
            "scope_count": len(aria_scopes),
            "rejected_document_roots": aria_scopes,
        }

    baseline_candidates = {
        tuple(candidate.get("runtime_dom_path", [])): candidate
        for candidate in baseline_probe.get("ancestor_candidates", [])
        if isinstance(candidate, dict)
    }
    changed_candidates: list[dict[str, Any]] = []
    for current_candidate in current_probe.get("ancestor_candidates", []):
        if not isinstance(current_candidate, dict):
            continue
        path = tuple(current_candidate.get("runtime_dom_path", []))
        baseline_candidate = baseline_candidates.get(path)
        if baseline_candidate is None:
            continue
        before_fingerprint = baseline_candidate.get("content_fingerprint")
        after_fingerprint = current_candidate.get("content_fingerprint")
        if before_fingerprint == after_fingerprint:
            continue
        changed_candidates.append(
            {
                "depth": current_candidate.get("depth"),
                "runtime_dom_path": list(path),
                "before_fingerprint": before_fingerprint,
                "after_fingerprint": after_fingerprint,
                "before_content_chars": baseline_candidate.get("content_chars"),
                "after_content_chars": current_candidate.get("content_chars"),
            }
        )

    diagnostics["changed_candidates"] = changed_candidates
    if changed_candidates:
        # 探针按离 Tablist 由近到远排列，第一个变化祖先就是同时覆盖控件与主要变化内容的
        # 最小共同容器；更高层的页面区域虽然也会变化，但不会被扩大纳入 Local Scope。
        selected_candidate = changed_candidates[0]
        return {
            "success": True,
            "error": None,
            "method": "non_control_content_change",
            "runtime_locator": {
                "dom_path": list(selected_candidate["runtime_dom_path"])
            },
            "diagnostics": diagnostics,
        }

    root_content_changed = (
        baseline_probe.get("document_root_content_fingerprint")
        != current_probe.get("document_root_content_fingerprint")
    )
    if root_content_changed and current_probe.get("reached_document_root"):
        return _failure(
            "document_root_only",
            "Only html/body could cover the Tab group and changed content.",
            diagnostics,
        )
    return _failure(
        "no_non_control_content_change",
        "Only Tab control state changed; no trustworthy local business scope was found.",
        diagnostics,
    )
