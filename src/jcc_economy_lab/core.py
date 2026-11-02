"""Pure, conditional cash/interest comparisons; never a match-state validator."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any


# Exact integers can cross a JSON/JavaScript boundary without rounding.
MAX_EXACT_INTEGER = 2**53 - 1
ORIGIN_LABELS = {
    "synthetic": "合成假设",
    "replay_candidate": "回放机器候选",
    "live_candidate": "实时机器候选",
    "manual_hypothesis": "手工假设",
}
BLOCKED_GOLD_STATES = {
    "unknown": "金币未知",
    "conflict": "金币候选冲突",
    "unreadable": "金币不可读",
    "stale": "金币候选已过期",
    "not_applicable": "该输入不适用于金币",
}
PREMISES = (
    "金币仅为候选或合成/手工假设；购买价格也是假设，未确认商店价格。",
    "规则参数未与本局地区、模式、赛季、补丁、强化及特殊效果绑定。",
    "未绑定回合计息时点及到账顺序；假定到计息时除该笔扣款外余额不再改变。",
    "未计入基础收入、胜负奖励、出售、掉落、刷新、经验及其他收支，不预测下轮总金币。",
    "仅比较现金与假设利息，不判断单位价值、备战席、购买合法性或最优买卖。",
)


@dataclass(frozen=True)
class GoldCandidate:
    value: int | None
    origin: str
    source_ref: str
    status: str = "candidate"
    # Any unresolved alternatives block calculation, including same-valued ones.
    alternatives: tuple[int, ...] = ()


@dataclass(frozen=True)
class HypotheticalPrice:
    value: int | None
    label: str = "假设购买"
    status: str = "hypothesis"


@dataclass(frozen=True)
class InterestAssumption:
    assumption_id: str
    gold_per_step: int | None
    interest_per_step: int | None
    max_interest: int | None
    basis_kind: str
    basis_note: str
    source_refs: tuple[str, ...]
    status: str = "hypothesis"


def _text(value: Any, limit: int = 1000) -> bool:
    return (type(value) is str and 0 < len(value.strip()) <= limit
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= MAX_EXACT_INTEGER


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message_zh": message}


def _gold_issues(gold: Any) -> list[dict[str, str]]:
    if type(gold) is not GoldCandidate:
        return [_issue("gold_input_invalid", "缺少显式金币候选。")]
    issues = []
    if type(gold.status) is not str or gold.status != "candidate":
        message = BLOCKED_GOLD_STATES.get(gold.status) if type(gold.status) is str else None
        issues.append(_issue("gold_" + gold.status if message else "gold_status_invalid",
                             (message or "金币状态不受支持") + "，不计算余额或利息。"))
    if type(gold.alternatives) not in (tuple, list):
        issues.append(_issue("gold_alternatives_invalid", "金币冲突信息格式无效。"))
    elif gold.alternatives:
        issues.append(_issue("gold_conflict", "仍有未解决的金币候选，不选择或平均其中一个。"))
    if not _integer(gold.value):
        issues.append(_issue("gold_value_invalid", "金币须为可读的非负整数；不将未知、负数或文本转成数字。"))
    if type(gold.origin) is not str or gold.origin not in ORIGIN_LABELS:
        issues.append(_issue("gold_origin_invalid", "金币来源类型未知，不标为实况。"))
    if not _text(gold.source_ref):
        issues.append(_issue("gold_source_missing", "缺少金币候选来源引用。"))
    return issues


def _rule_issues(rule: Any) -> list[dict[str, str]]:
    if rule is None:
        return [_issue("rule_missing", "缺少利息规则假设，不判断利息变化。")]
    if type(rule) is not InterestAssumption:
        return [_issue("rule_input_invalid", "利息规则格式无效，不判断利息变化。")]
    issues = []
    if type(rule.status) is not str or rule.status != "hypothesis":
        issues.append(_issue("rule_status_invalid", "规则未知、冲突、撤销或状态不受支持，不判断利息变化。"))
    if not all(_text(item) for item in (rule.assumption_id, rule.basis_note)):
        issues.append(_issue("rule_basis_missing", "规则假设缺少名称或前提说明。"))
    if type(rule.basis_kind) is not str or rule.basis_kind not in (
        "sourced_hypothesis", "synthetic_hypothesis",
    ):
        issues.append(_issue("rule_basis_invalid", "规则须显式标为有来源假设或合成假设。"))
    if (type(rule.source_refs) not in (tuple, list) or not rule.source_refs
            or not all(_text(ref, 2048) for ref in rule.source_refs)):
        issues.append(_issue("rule_source_missing", "缺少公开来源或合成假设的引用。"))
    if not (_integer(rule.gold_per_step, 1) and _integer(rule.interest_per_step, 1)
            and _integer(rule.max_interest)):
        issues.append(_issue("rule_parameters_invalid", "利息档距与每档收益须为正整数，上限须显式提供非负整数。"))
    return issues


def _price_issues(price: Any) -> list[dict[str, str]]:
    if type(price) is not HypotheticalPrice:
        return [_issue("price_input_invalid", "购买价格格式无效。")]
    issues = []
    if type(price.status) is not str or price.status != "hypothesis":
        issues.append(_issue("price_status_invalid", "价格未知、冲突或过期，本项不计算。"))
    if not _integer(price.value):
        issues.append(_issue("price_value_invalid", "假设价格须为可读的非负整数。"))
    if not _text(price.label, 200):
        issues.append(_issue("price_label_invalid", "价格标签无效。"))
    return issues


def _point(gold: int, rule: InterestAssumption) -> dict[str, int | bool]:
    steps = gold // rule.gold_per_step
    return {
        "uncapped_step_count": steps,
        "interest_gold": min(steps * rule.interest_per_step, rule.max_interest),
        "at_cap": steps * rule.interest_per_step >= rule.max_interest,
    }


def _rule_snapshot(rule: Any, issues: list[dict[str, str]]) -> dict:
    # Invalid objects are never echoed into JSON or quietly repaired.
    if issues:
        return {"status": "unavailable", "parameters": None, "issues": issues}
    return {
        "status": "hypothesis", "assumption_id": rule.assumption_id,
        "basis_kind": rule.basis_kind, "basis_note": rule.basis_note,
        "source_refs": list(rule.source_refs),
        "parameters": {"gold_per_step": rule.gold_per_step,
                       "interest_per_step": rule.interest_per_step,
                       "max_interest": rule.max_interest},
        "formula": "min((gold // gold_per_step) * interest_per_step, max_interest)",
        "rounding": "floor_non_negative_integers", "match_binding": "unverified",
        "issues": [],
    }


def _envelope() -> dict:
    return {
        "format_version": "jcc-economy-lab/v1",
        "analysis_kind": "conditional_economy_lab", "status": "blocked",
        "confirmed_game_state": False, "match_rule_binding": "unverified",
        "settlement_binding": "unbound", "execution_eligible": False,
        "strategy_recommendation": False,
        "comparison_semantics": "independent_alternatives_not_sequential_purchases",
        "uncertain_premises_zh": list(PREMISES),
        "gold_candidate": None, "rule_assumption": None,
        "issues": [], "comparisons": [],
    }


def analyze_purchases(
    gold: GoldCandidate,
    prices: list[int | HypotheticalPrice] | tuple[int | HypotheticalPrice, ...],
    rule: InterestAssumption | None = None,
) -> dict:
    """Compare independent hypothetical purchases, with no I/O or implicit rule.

    Unknown/conflicting gold blocks all arithmetic. A missing/invalid rule still
    permits labelled cash subtraction but leaves every interest result null.
    Neither candidate acceptance nor sourced hypotheses establish match facts.
    """
    result = _envelope()
    gold_issues = _gold_issues(gold)
    rule_issues = _rule_issues(rule)
    result["rule_assumption"] = _rule_snapshot(rule, rule_issues)
    result["issues"] = gold_issues + rule_issues
    if not gold_issues:
        result["gold_candidate"] = {
            "value": gold.value, "status": "candidate", "origin": gold.origin,
            "source_ref": gold.source_ref, "confirmed": False,
        }
    if type(prices) not in (list, tuple):
        result["issues"].append(_issue("prices_input_invalid", "价格必须是逐项假设的列表。"))
        return result
    if not prices:
        result["issues"].append(_issue("prices_empty", "没有提供待比较价格。"))
        return result

    origin_label = ORIGIN_LABELS[gold.origin] if not gold_issues else "条件计算"
    for index, raw_price in enumerate(prices):
        price = HypotheticalPrice(raw_price) if type(raw_price) is int else raw_price
        price_issues = _price_issues(price)
        row = {
            "index": index, "label": price.label if not price_issues else "无效价格项",
            "hypothetical_price": price.value if not price_issues else None,
            "status": "blocked", "within_candidate_balance": None,
            "gold_after_hypothetical_purchase": None,
            "interest_before": None, "interest_after": None,
            "interest_delta": None, "interest_effect": None,
            "issues": gold_issues + price_issues + rule_issues,
            "text_zh": "",
        }
        if gold_issues or price_issues:
            messages = gold_issues + price_issues
            row["text_zh"] = "条件计算暂停：" + messages[0]["message_zh"]
        elif price.value > gold.value:
            row["status"] = "insufficient_candidate_gold"
            row["within_candidate_balance"] = False
            row["issues"].append(_issue("price_exceeds_candidate_gold", "假设价格超过候选金币，不推导负余额或利息。"))
            row["text_zh"] = (f"{origin_label}：若有{gold.value}金，{price.value}金假设价格超出余额；"
                              "不推导买后余额或利息，实况未确认。")
        else:
            after = gold.value - price.value
            row["within_candidate_balance"] = True
            row["gold_after_hypothetical_purchase"] = after
            prefix = f"{origin_label}：若有{gold.value}金、花{price.value}金，余{after}金；"
            if rule_issues:
                row["status"] = "cash_only"
                row["text_zh"] = prefix + "规则缺失或无效，不判断利息，实况未确认。"
            else:
                before_point, after_point = _point(gold.value, rule), _point(after, rule)
                delta = after_point["interest_gold"] - before_point["interest_gold"]
                row.update(status="conditional", interest_before=before_point,
                           interest_after=after_point, interest_delta=delta,
                           interest_effect="reduced" if delta < 0 else "unchanged")
                change = f"少{-delta}金" if delta < 0 else "不变"
                row["text_zh"] = (
                    prefix + f"按每{rule.gold_per_step}金+{rule.interest_per_step}、"
                    f"封顶{rule.max_interest}的假设，利息"
                    f"{before_point['interest_gold']}→{after_point['interest_gold']}（{change}）；"
                    "未绑定本局结算。"
                )
        result["comparisons"].append(row)

    statuses = {row["status"] for row in result["comparisons"]}
    if statuses == {"conditional"}:
        result["status"] = "conditional"
    elif statuses == {"cash_only"}:
        result["status"] = "cash_only"
    elif statuses & {"conditional", "cash_only"}:
        result["status"] = "partial"
    return result


def _object(cls: type, value: Any) -> Any:
    if type(value) is not dict or set(value) - {field.name for field in fields(cls)}:
        raise ValueError("请求对象格式或字段无效。")
    return cls(**value)


def analyze_request(request: dict) -> dict:
    """JSON-shaped adapter for future candidate producers; does not read a file.

    This is a module-local request, not a GameState or shared schema adapter.
    Unknown keys (including verification/production flags) fail closed.
    """
    try:
        if type(request) is not dict or set(request) - {"gold", "prices", "rule"}:
            raise ValueError("请求顶层字段无效。")
        gold = _object(GoldCandidate, request["gold"])
        prices = request["prices"]
        if type(prices) is not list:
            raise ValueError("价格须为列表。")
        parsed_prices = [_object(HypotheticalPrice, price) if type(price) is dict else price
                         for price in prices]
        rule = _object(InterestAssumption, request["rule"]) if request.get("rule") is not None else None
    except (KeyError, TypeError, ValueError):
        result = _envelope()
        result["issues"] = [_issue("request_invalid", "请求格式、必需字段或未知字段有误，未做计算。")]
        return result
    return analyze_purchases(gold, parsed_prices, rule)
