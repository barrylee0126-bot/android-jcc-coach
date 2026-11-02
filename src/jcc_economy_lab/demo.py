"""Self-contained, deliberately synthetic examples. No replay/sample access."""

from .assumptions import ten_gold_five_cap_hypothesis
from .core import GoldCandidate, InterestAssumption, analyze_purchases


def demo_cases() -> list[dict]:
    base = ten_gold_five_cap_hypothesis()
    changed_cap = InterestAssumption(
        "synthetic-cap7", 10, 1, 7, "synthetic_hypothesis",
        "纯合成：将上限改为7以比较参数敏感性，不代表本局存在对应强化。",
        ("synthetic:demo-cap7",),
    )
    changed_steps = InterestAssumption(
        "synthetic-step5-reward2-cap7", 5, 2, 7, "synthetic_hypothesis",
        "纯合成：每5金增加2金利息、封顶7；不是游戏规则。", ("synthetic:demo-steps",),
    )
    no_interest = InterestAssumption(
        "synthetic-no-interest", 10, 1, 0, "synthetic_hypothesis",
        "纯合成：假定不产生利息；不对应本机模式。", ("synthetic:demo-zero-cap",),
    )
    cases = [
        ("同一余额的有变化与无变化", 31, [1, 2, 32], base, "candidate"),
        ("上限与边界", 55, [0, 5, 6, 55], base, "candidate"),
        ("同一55金改用上限7", 55, [5, 6], changed_cap, "candidate"),
        ("自定义档距与收益", 20, [1, 5], changed_steps, "candidate"),
        ("显式零利息", 20, [1, 20], no_interest, "candidate"),
        ("规则缺失仅扣款", 31, [2], None, "candidate"),
        ("未知不计算", None, [1], base, "unknown"),
        ("冲突即使带值也不计算", 31, [1], base, "conflict"),
    ]
    return [
        {"label": "纯合成演示：" + label,
         "analysis": analyze_purchases(GoldCandidate(value, "synthetic", f"synthetic:demo-{i}", status),
                                       prices, rule)}
        for i, (label, value, prices, rule, status) in enumerate(cases)
    ]
