"""发现安全的标准 Tab Group，并建立 Interactive State 数据结构骨架。

本模块属于 Stage 1 V0.3-3 的第一步，只读取浏览器当前真实 DOM。它不会点击 Tab、不会
遍历其他交互状态，也不会生成页面 Block 或参与 Change Detection。后续状态遍历可以在
这里返回的 group、tab 和默认 state 骨架上继续扩展，而不需要重新定义数据语义。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


TAB_GROUP_DISCOVERY_SCRIPT = r"""
() => {
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

    const isDisabled = (element) => {
        const nativeDisabled = "disabled" in element && Boolean(element.disabled);
        return nativeDisabled
            || element.hasAttribute("disabled")
            || attributeIsTrue(element, "aria-disabled")
            || Boolean(element.closest("[inert]"));
    };

    const isVisible = (element) => {
        if (!element.isConnected) {
            return false;
        }

        // 可见性必须沿祖先链检查：子 Tab 自己可能是 display:block，但仍位于隐藏面板中。
        // 这里只判断 CSS/ARIA 的当前显示状态，不依据视口坐标，因此页面下方的 Tab 仍可发现。
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
            const index = sameTagSiblings.indexOf(current) + 1;
            parts.unshift(`${tag}:nth-of-type(${index})`);
            current = current.parentElement;
        }
        return parts;
    };

    const getHeadingLevel = (element) => {
        const nativeMatch = element.tagName.match(/^H([1-6])$/);
        if (nativeMatch) {
            return Number.parseInt(nativeMatch[1], 10);
        }
        if (hasRole(element, "heading")) {
            const ariaLevel = Number.parseInt(element.getAttribute("aria-level"), 10);
            return Number.isInteger(ariaLevel) && ariaLevel > 0 ? ariaLevel : 6;
        }
        return null;
    };

    const buildPrecedingHeadingPath = (tablist) => {
        const headingPath = [];
        const headingElements = Array.from(
            document.querySelectorAll("h1, h2, h3, h4, h5, h6, [role]")
        ).filter((element) =>
            /^H[1-6]$/.test(element.tagName) || hasRole(element, "heading")
        );

        for (const heading of headingElements) {
            // 只使用 Tab Group 之前的可见标题。后方卡片标题虽然可能位于同一容器内，
            // 但不代表当前 Tab Group 的所属区域，不能拿来构造持久化身份。
            const isBeforeTablist = Boolean(
                heading.compareDocumentPosition(tablist) & Node.DOCUMENT_POSITION_FOLLOWING
            );
            if (!isBeforeTablist || !isVisible(heading)) {
                continue;
            }
            const text = normalizeText(heading.innerText || heading.textContent);
            const level = getHeadingLevel(heading);
            if (!text || level === null) {
                continue;
            }
            while (
                headingPath.length > 0
                && headingPath[headingPath.length - 1].level >= level
            ) {
                headingPath.pop();
            }
            headingPath.push({ level, text });
        }
        return headingPath.map((heading) => heading.text);
    };

    const buildSemanticScopePath = (tablist) => {
        const ariaLabel = normalizeText(tablist.getAttribute("aria-label"));
        if (ariaLabel) {
            return [ariaLabel];
        }

        const labelledByIds = (tablist.getAttribute("aria-labelledby") || "")
            .split(/\s+/)
            .filter(Boolean);
        const labelledByTexts = labelledByIds
            .map((id) => document.getElementById(id))
            .filter(Boolean)
            .map((element) => normalizeText(element.innerText || element.textContent))
            .filter(Boolean);
        if (labelledByTexts.length > 0) {
            return labelledByTexts;
        }

        return buildPrecedingHeadingPath(tablist);
    };

    const tablists = Array.from(document.querySelectorAll("[role]"))
        .filter((element) => hasRole(element, "tablist"));
    const acceptedGroups = [];

    for (const tablist of tablists) {
        if (!isVisible(tablist) || isDisabled(tablist)) {
            continue;
        }

        // 只收集当前 tablist 真正拥有的 role=tab。嵌套 tablist 的 Tab 归属于内层组，
        // 不能被外层重复计数。
        const tabElements = Array.from(tablist.querySelectorAll("[role]"))
            .filter((element) =>
                hasRole(element, "tab")
                && nearestOwningTablist(element) === tablist
            );
        const tabs = tabElements.map((tab, index) => {
            const ariaSelectedValue = tab.getAttribute("aria-selected");
            const normalizedSelected = ariaSelectedValue === null
                ? null
                : ariaSelectedValue.trim().toLowerCase();
            return {
                runtime_locator: { tab_index: index },
                text: normalizeText(tab.innerText || tab.textContent),
                aria_selected: normalizedSelected === "true"
                    ? true
                    : normalizedSelected === "false"
                        ? false
                        : null,
                visible: isVisible(tab),
                disabled: isDisabled(tab),
            };
        });

        const actionableTabs = tabs.filter((tab) => tab.visible && !tab.disabled);
        const allActionableTabsDeclareSelection = actionableTabs.every(
            (tab) => tab.aria_selected === true || tab.aria_selected === false
        );
        const selectedTabs = actionableTabs.filter((tab) => tab.aria_selected === true);
        if (
            tabs.length < 2
            || actionableTabs.length < 2
            || !allActionableTabsDeclareSelection
            || selectedTabs.length !== 1
        ) {
            continue;
        }

        const selectedTab = selectedTabs[0];

        acceptedGroups.push({
            semantic_scope_path: buildSemanticScopePath(tablist),
            // DOM 路径和索引只帮助当前浏览器会话定位元素，不进入 State 的语义身份。
            runtime_dom_path: buildRuntimeDomPath(tablist),
            selected_tab_index: selectedTab.runtime_locator.tab_index,
            tabs,
        });
    }

    return acceptedGroups;
}
"""


def _normalize_semantic_path(path: list[str]) -> list[str]:
    """规范化语义路径，避免空白和 Unicode 表示差异制造不同身份。

    输入：从 ARIA、标题或 Tab 可见文字获得的路径片段。
    处理：使用 NFKC 统一兼容字符，合并连续空白，并删除空片段。
    输出：供 State 展示和身份计算共同使用的稳定字符串列表。
    """
    normalized_path: list[str] = []
    for segment in path:
        normalized_segment = re.sub(
            r"\s+", " ", unicodedata.normalize("NFKC", segment)
        ).strip()
        if normalized_segment:
            normalized_path.append(normalized_segment)
    return normalized_path


def build_interactive_state_key(
    scope_path: list[str], state_path: list[str]
) -> str:
    """由语义 scope 和状态路径确定性生成 Interactive State 身份。

    输入：语义区域路径和按父到子排列的交互状态文本。
    处理：先规范化空白与 Unicode，再使用 casefold 消除英文大小写造成的不稳定差异，
    最后对规范 JSON 计算 SHA-256。DOM 路径和任何运行时序号都不参与计算。
    输出：同一语义状态可重复生成的稳定 state_key。
    """
    identity = {
        "scope_path": [
            segment.casefold() for segment in _normalize_semantic_path(scope_path)
        ],
        "state_path": [
            segment.casefold() for segment in _normalize_semantic_path(state_path)
        ],
    }
    serialized_identity = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(serialized_identity.encode("utf-8")).hexdigest()
    return f"interactive-state:{digest}"


def create_interactive_state_skeleton(
    *,
    scope_path: list[str],
    state_path: list[str],
    is_default: bool,
) -> dict[str, Any]:
    """创建尚未采集内容的 Interactive State 最小结构。

    输入：所属区域的语义路径、由 Tab 可见文本组成的状态路径和是否为默认状态。
    处理：规范化语义文本并生成稳定 state_key，同时为后续 Blocks、Hash 和采集时间预留字段。
    输出：可直接序列化为 JSON 的状态字典。当前阶段不点击控件，因此 blocks 为空，
    content_hash 与 captured_at 为 None（JSON 中的 null）。
    """
    normalized_scope_path = _normalize_semantic_path(scope_path)
    normalized_state_path = _normalize_semantic_path(state_path)
    return {
        "state_key": build_interactive_state_key(
            normalized_scope_path, normalized_state_path
        ),
        "scope_path": normalized_scope_path,
        "state_path": normalized_state_path,
        "is_default": is_default,
        "blocks": [],
        "content_hash": None,
        "captured_at": None,
    }


def discover_safe_tab_groups(page: Any) -> list[dict[str, Any]]:
    """从 Playwright Page 的当前 DOM 发现可安全识别默认状态的 Tab Group。

    输入：已经完成基础渲染等待的 Playwright Page。
    处理：在浏览器内检查标准 role、祖先可见性和 disabled 状态；只接受至少两个可操作
    Tab，并要求这些 Tab 都显式声明 aria-selected=true/false，且恰好一个为 true。随后优先
    使用 aria-label/aria-labelledby、其次使用前置可见标题路径建立语义身份，全程不执行点击。
    输出：按 DOM 顺序排列的安全标签组；没有可信标签组时返回空列表。
    """
    raw_groups = page.evaluate(TAB_GROUP_DISCOVERY_SCRIPT)
    if not isinstance(raw_groups, list):
        return []

    groups: list[dict[str, Any]] = []
    for group_index, raw_group in enumerate(raw_groups):
        if not isinstance(raw_group, dict):
            continue
        raw_scope_path = raw_group.get("semantic_scope_path")
        runtime_dom_path = raw_group.get("runtime_dom_path")
        selected_tab_index = raw_group.get("selected_tab_index")
        tabs = raw_group.get("tabs")
        if (
            not isinstance(raw_scope_path, list)
            or not all(
                isinstance(path_part, str) and path_part
                for path_part in raw_scope_path
            )
            or not isinstance(runtime_dom_path, list)
            or not all(
                isinstance(path_part, str) and path_part
                for path_part in runtime_dom_path
            )
            or not isinstance(selected_tab_index, int)
            or not isinstance(tabs, list)
            or not 0 <= selected_tab_index < len(tabs)
        ):
            continue

        scope_path = _normalize_semantic_path(raw_scope_path)
        selected_tab = tabs[selected_tab_index]
        selected_tab_text = (
            selected_tab.get("text") if isinstance(selected_tab, dict) else None
        )
        state_path = (
            _normalize_semantic_path([selected_tab_text])
            if isinstance(selected_tab_text, str)
            else []
        )
        stable_state_available = bool(scope_path and state_path)
        states = []
        if stable_state_available:
            states.append(
                create_interactive_state_skeleton(
                    scope_path=scope_path,
                    state_path=state_path,
                    is_default=True,
                )
            )

        groups.append(
            {
                "scope_path": scope_path,
                "tabs": tabs,
                "stable_state_available": stable_state_available,
                "interactive_state_unavailable_reason": (
                    None
                    if stable_state_available
                    else (
                        "semantic_scope_unavailable"
                        if not scope_path
                        else "semantic_state_unavailable"
                    )
                ),
                # DOM 路径和序号只存在于运行时定位元数据中，不参与持久化语义身份。
                "runtime_locator": {
                    "group_index": group_index,
                    "dom_path": list(runtime_dom_path),
                    "selected_tab_index": selected_tab_index,
                },
                # states 使用列表是为了下一步遍历时追加非默认状态；本阶段最多只有当前默认项。
                "states": states,
            }
        )

    return groups
