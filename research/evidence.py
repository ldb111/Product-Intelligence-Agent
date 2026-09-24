"""任务内来源登记与输出校验；Triage 和 Research 共用同一套引用边界。"""

import hashlib
import re

from .schemas import Citation, FollowUpAnswer, ResearchReport, VerificationResult


class EvidenceError(ValueError):
    """不合格引用不能被包装成可消费的研究结果。"""


def no_generated_urls(value) -> None:
    """遍历模型输出，拒绝手写地址；引用只能使用程序提供的 ID。"""
    if isinstance(value, dict):
        for item in value.values():
            no_generated_urls(item)
    elif isinstance(value, list):
        for item in value:
            no_generated_urls(item)
    elif isinstance(value, str) and re.search(r"(?:https?://|www\.)", value, re.I):
        raise EvidenceError("输出含模型生成的地址。")


class EvidenceRegistry:
    """每次任务独立登记；重复来源复用 ID，搜索线索永远不能升级为已读证据。"""

    def __init__(self):
        self.items: dict[str, Citation] = {}
        self._keys: dict[tuple, str] = {}

    def add(self, **data) -> Citation:
        """接收工具实际返回的来源，按类型和原始身份去重，输出不可变引用对象。"""
        key = (data["source"], data["domain"], data["source_id"], data["content"])
        if key in self._keys:
            return self.items[self._keys[key]]
        prefix = {"event": "event_evidence", "page": "research_evidence", "search": "candidate",
                  "knowledge": "knowledge", "memory": "memory"}[data["source"]]
        number = sum(item.source == data["source"] for item in self.items.values()) + 1
        result_id = f"{prefix}_{number:03d}"
        # 同一原始来源经 Event、RAG、页面多次到达，也不能被当作独立交叉印证。
        origin = data.get("url") or data.get("source_reference") or data["source_id"]
        data["source_group"] = "source_" + hashlib.sha256(origin.encode("utf-8")).hexdigest()[:16]
        result = Citation(result_id=result_id, **data)
        self.items[result_id] = result
        self._keys[key] = result_id
        return result

    def get(self, result_id: str, *, factual: bool = False) -> Citation:
        """查回真实引用；事实模式排除 Memory、搜索摘要和待确认来源。"""
        item = self.items.get(result_id)
        if item is None:
            raise EvidenceError("引用不在本次来源登记表中。")
        if factual and (item.source in {"memory", "search"} or item.status not in {"confirmed", "read"}):
            raise EvidenceError("上下文或未读线索不能作为产品事实。")
        return item

    def model_items(self, ids=None) -> list[dict]:
        """提供最小语义证据，地址和存储路径留在程序端；保留域、可信度和原文。"""
        selected = self.items if ids is None else ids
        return [{k: v for k, v in self.get(i).model_dump().items()
                 if k in {"result_id", "source", "source_group", "domain", "title", "content", "credibility", "status"}}
                for i in selected]

    def check_support(self, support) -> list[Citation]:
        """核对每个引用是实际事实来源，且逐字片段存在；不声称能证明语义蕴含。"""
        results = []
        for quote in support:
            item = self.get(quote.result_id, factual=True)
            if not quote.excerpt.strip() or quote.excerpt not in item.content:
                raise EvidenceError("引用片段不在对应原文中。")
            results.append(item)
        return results

    def validate_report(self, report: ResearchReport) -> list[Citation]:
        """校验报告所有声明，强制自身比较有双域直接引用，最后补齐来源信息。"""
        no_generated_urls(report.model_dump())
        claims = [*report.event_summary, *report.background, *report.competitor_context,
                  *report.own_product_comparison.claims, *report.implications, *report.watch_next]
        used = set()
        for claim in claims:
            items = self.check_support(claim.support)
            if claim.kind == "fact" and any(i.source == "page" and i.credibility != "official" for i in items):
                raise EvidenceError("第三方页面仅能作为待核实推论，不能覆盖官方事实。")
            used.update(i.result_id for i in items)
        for claim in report.own_product_comparison.claims:
            items = self.check_support(claim.support)
            if not any(i.source == "knowledge" and i.domain == "own_product" for i in items):
                raise EvidenceError("自身产品比较缺少直接知识证据。")
            if not any(i.domain == "competitor" for i in items):
                raise EvidenceError("产品比较缺少竞品证据。")
        if any(c.kind == "fact" for c in [*report.implications, *report.watch_next]):
            raise EvidenceError("影响和行动建议必须标记为推论或建议。")
        if set(report.evidence_refs) != used:
            raise EvidenceError("总引用列表与逐项证据不一致。")
        return [self.get(i, factual=True) for i in dict.fromkeys(report.evidence_refs)]

    def validate_follow_up(self, answer: FollowUpAnswer) -> list[Citation]:
        """沿用正式研究引用门槛校验一次事件追问，不降低 Fact 证据标准。"""
        no_generated_urls(answer.model_dump())
        used = set()
        for claim in answer.answer:
            items = self.check_support(claim.support)
            if claim.kind == "fact" and any(
                item.source == "page" and item.credibility != "official" for item in items
            ):
                raise EvidenceError("第三方页面仅能作为待核实推论，不能覆盖官方事实。")
            used.update(item.result_id for item in items)
        if set(answer.evidence_refs) != used:
            raise EvidenceError("总引用列表与逐项证据不一致。")
        return [self.get(item, factual=True) for item in dict.fromkeys(answer.evidence_refs)]

    def validate_verification(self, result: VerificationResult, initial_ids: set[str]) -> list[Citation]:
        """验证只能使用本轮新读到的证据；缺少直接官方支持时不能定论。

        找不到结果不构成否定证据。逐字引用只是最低机械门槛，支持或否定的语义
        仍由 Research 判断并供用户复核，不会自动生成或改写 Event Core 对象。
        """
        no_generated_urls(result.model_dump())
        items = self.check_support(result.support)
        used = {i.result_id for i in items}
        if used != set(result.new_evidence_refs) or used & initial_ids:
            raise EvidenceError("核验新引用必须对应本次取得的新证据。")
        if result.decision != "still_pending" and not any(i.credibility == "official" for i in items):
            raise EvidenceError("无法确认：缺少直接官方新证据。")
        if result.decision == "still_pending" and not result.unresolved_questions:
            raise EvidenceError("待核验结果必须保留未解决问题。")
        return [self.get(i, factual=True) for i in dict.fromkeys(result.new_evidence_refs)]
