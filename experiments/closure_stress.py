"""RC 前的合成压力检查；与真实独立留出集无关，不得计入独立质量指标。"""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.runners import event_detection_independent as runner
from event_core import detect_event
from event_core.schemas import DetectionInput, StateContext


def main():
    """将明确的同项前后状态交给真实检测器，验证保守规则没有禁用整个 Path A。

    输入仅为虚构示例产品的价格与维护说明，预期只用于调用后的本地检查。每项只调用
    一次，异常仅记录类型，输出到 tmp；不访问示例网页或修改任何冻结 Gold。
    """
    runner.runtime_metadata()
    samples = [
        ("明确调价", "标准套餐每月收费100元。", "标准套餐每月收费80元。", "event"),
        ("纯维护", "本版本修复日志时间戳排版问题。", "本版本修复日志空格排版问题。", "no_event"),
    ]
    rows = []
    for name, previous, current, expected in samples:
        data = DetectionInput(competitor="合成压力示例", source_url="https://example.com/synthetic",
            source_title="合成压力材料", source_type="product_page", source_credibility="official",
            detection_path="state_comparison", state_context=StateContext(comparison_status="comparable"),
            previous_state=previous, current_state=current,
            contextual_diff=[{"previous": previous, "current": current}],
            previous_observed_at="2026-09-01T00:00:00Z", current_observed_at="2026-09-02T00:00:00Z")
        try:
            result = detect_event(data)
            decision = runner.normalize_detection(result)["decision"]
            rows.append({"name": name, "expected": expected, "actual": decision,
                         "pass": decision == expected, "result": result.model_dump(mode="json")})
        except Exception as exc:
            rows.append({"name": name, "pass": False, "error_type": type(exc).__name__})
    target = ROOT / "tmp" / "evals" / "closure" / ("stress_" + runner._run_id() + ".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"evaluation_use": "synthetic_development_stress", "cases": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"pass": sum(item["pass"] for item in rows), "total": len(rows), "output": str(target)}))
    return int(not all(item["pass"] for item in rows))


if __name__ == "__main__":
    raise SystemExit(main())
