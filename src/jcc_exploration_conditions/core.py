"""Explicit-input conditions for an exploratory match; never emits an action."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


FORMAT_VERSION = "jcc-exploration-conditions/v1"
MAX_EXACT_INTEGER = 2**53 - 1
FIELD_NAMES = (
    "gold", "reserve_gold", "next_purchase_reserve", "candidate_price",
    "candidate_immediate_upgrade", "bench_space_available", "preparation_phase",
    "sale_value", "candidate_nonessential", "board_legal_after_sale",
    "board_slot_available", "candidate_can_field_now",
    "components_known_unequipped", "recipe_known", "carrier_known",
    "components_match_recipe", "carrier_has_slot", "carrier_suits_recipe", "carrier_need_known",
)
BOOL_FIELDS = set(FIELD_NAMES) - {
    "gold", "reserve_gold", "next_purchase_reserve", "candidate_price", "sale_value",
}


def _issue(code: str, field: str | None, message: str) -> dict[str, str | None]:
    return {"code": code, "field": field, "message_zh": message}


def _known_int(row: dict | None) -> bool:
    return (type(row) is dict and row.get("status") == "known"
            and type(row.get("value")) is int
            and 0 <= row["value"] <= MAX_EXACT_INTEGER)


def _known_bool(row: dict | None) -> bool:
    return type(row) is dict and row.get("status") == "known" and type(row.get("value")) is bool


def _validate(request: Any) -> tuple[dict[str, dict] | None, list[dict]]:
    if type(request) is not dict or set(request) != {"format_version", "sample_kind", "fields"}:
        return None, [_issue("shape_invalid", None, "请求只能包含格式、样本类别和显式字段。")]
    if request["format_version"] != FORMAT_VERSION:
        return None, [_issue("format_unsupported", "format_version", "请求格式版本不受支持。")]
    if (type(request["sample_kind"]) is not str
            or request["sample_kind"] not in {"synthetic", "replay_candidate", "live_candidate", "manual_hypothesis"}):
        return None, [_issue("sample_kind_invalid", "sample_kind", "样本类别未知，不能给出条件结果。")]
    fields = request["fields"]
    if type(fields) is not dict or set(fields) != set(FIELD_NAMES):
        return None, [_issue("fields_invalid", "fields", "字段必须完整且没有未约定的附加字段。")]
    issues = []
    for name in FIELD_NAMES:
        row = fields[name]
        if type(row) is not dict or set(row) != {"status", "value"}:
            issues.append(_issue("field_shape_invalid", name, "字段须显式给出 status 和 value。"))
            continue
        if (type(row["status"]) is not str
                or row["status"] not in {"known", "unknown", "ambiguous", "stale", "not_applicable"}):
            issues.append(_issue("field_status_invalid", name, "字段状态未知，不能把它补成已知。"))
            continue
        if row["status"] == "known":
            valid = _known_bool(row) if name in BOOL_FIELDS else _known_int(row)
            if not valid:
                issues.append(_issue("known_value_invalid", name, "已知字段的值类型或范围不正确。"))
        elif row["value"] is not None:
            issues.append(_issue("unknown_value_present", name, "非已知字段必须为 null，不能偷偷携带候选值。"))
    return (deepcopy(fields) if not issues else None), issues


def _safe_sample_kind(request: Any) -> str | None:
    """Never reflect malformed caller data into a JSON result."""
    if type(request) is not dict or type(request.get("sample_kind")) is not str:
        return None
    kind = request["sample_kind"]
    return kind if kind in {"synthetic", "replay_candidate", "live_candidate", "manual_hypothesis"} else None


def _missing(fields: dict[str, dict], names: tuple[str, ...]) -> list[dict]:
    result = []
    for name in names:
        row = fields[name]
        valid = _known_bool(row) if name in BOOL_FIELDS else _known_int(row)
        if not valid:
            result.append(_issue("field_not_known", name, "该条件字段未知、冲突、过期或无效；保留未知。"))
    return result


def _consideration(kind: str, status: str, rationale: str, prerequisites: tuple[str, ...], issues: list[dict]) -> dict:
    return {
        "kind": kind,
        "status": status,
        "rationale_zh": rationale,
        "required_fields": list(prerequisites),
        "issues": issues,
        "action": None,
        "execution_eligible": False,
    }


def assess_conditions(request: Any) -> dict:
    """Return only conditional considerations from caller-supplied observations.

    This component intentionally neither reads a device nor ranks units, routes,
    recipes, or real game rules. `live_candidate` stays a machine candidate.
    """
    fields, request_issues = _validate(request)
    base = {
        "format_version": FORMAT_VERSION,
        "status": "blocked" if request_issues else "evaluated",
        "sample_kind": _safe_sample_kind(request),
        "execution_eligible": False,
        "strategy_recommendation": False,
        "automatic_action": None,
        "real_rules_verified": False,
        "notes_zh": [
            "只表达调用方显式提供字段之间的条件关系，不识别棋子、价格、版本或装备效果。",
            "结果不是购买、出售、上阵或装备的指令；所有 action 固定为 null。",
            "实时或回放机器候选不等于人工标注、版本绑定、可执行动作或上分能力。",
        ],
        "considerations": [],
        "issues": request_issues,
    }
    if fields is None:
        return base

    buy_required = ("gold", "reserve_gold", "next_purchase_reserve", "candidate_price",
                    "candidate_immediate_upgrade", "bench_space_available", "preparation_phase")
    buy_issues = _missing(fields, buy_required)
    if buy_issues:
        base["considerations"].append(_consideration(
            "buy", "unknown", "缺少预算、即时价值、位置或准备阶段字段，不能考虑购买。", buy_required, buy_issues))
    elif not fields["preparation_phase"]["value"]:
        base["considerations"].append(_consideration(
            "buy", "not_applicable", "当前不在明确准备阶段；不把旧商店或坐标延用到其他阶段。", buy_required, []))
    elif not fields["candidate_immediate_upgrade"]["value"] or not fields["bench_space_available"]["value"]:
        base["considerations"].append(_consideration(
            "buy", "hold", "候选未确认能立即改善局面或没有已知可用位置，暂不形成购买考虑。", buy_required, []))
    else:
        remaining = fields["gold"]["value"] - fields["candidate_price"]["value"]
        minimum = fields["reserve_gold"]["value"] + fields["next_purchase_reserve"]["value"]
        if remaining >= minimum:
            base["considerations"].append(_consideration(
                "buy", "conditional", "候选可立即改善且扣除显式价格后仍满足调用方给定的保留与下一次购买预留。", buy_required, []))
        else:
            base["considerations"].append(_consideration(
                "buy", "hold", "扣除显式价格后低于调用方的保留与下一次购买预留，保留预算。", buy_required, []))

    sell_required = ("sale_value", "candidate_nonessential", "board_legal_after_sale", "preparation_phase")
    sell_issues = _missing(fields, sell_required)
    if sell_issues:
        base["considerations"].append(_consideration(
            "sell", "unknown", "出售价值、非关键性或出售后的棋盘合法性不明确，不能考虑出售。", sell_required, sell_issues))
    elif fields["preparation_phase"]["value"] and fields["candidate_nonessential"]["value"] and fields["board_legal_after_sale"]["value"]:
        base["considerations"].append(_consideration(
            "sell", "conditional", "对象被调用方明确标为非关键，且出售后棋盘仍合法；可把出售列为候选。", sell_required, []))
    else:
        base["considerations"].append(_consideration(
            "sell", "hold", "对象关键、出售后合法性不成立或不在准备阶段，不形成出售考虑。", sell_required, []))

    field_required = ("board_slot_available", "candidate_can_field_now", "preparation_phase")
    field_issues = _missing(fields, field_required)
    if field_issues:
        base["considerations"].append(_consideration(
            "field", "unknown", "空位、候选可上场性或阶段不明确，不能考虑上阵。", field_required, field_issues))
    elif all(fields[name]["value"] for name in field_required):
        base["considerations"].append(_consideration(
            "field", "conditional", "调用方明确了准备阶段、合法空位和候选可立刻上场；可把上阵列为候选。", field_required, []))
    else:
        base["considerations"].append(_consideration(
            "field", "hold", "没有已知合法空位、候选不可立刻上场或不在准备阶段，暂不上阵。", field_required, []))

    equipment_required = ("components_known_unequipped", "recipe_known", "components_match_recipe",
                          "carrier_known", "carrier_has_slot", "carrier_suits_recipe",
                          "carrier_need_known", "preparation_phase")
    equipment_issues = _missing(fields, equipment_required)
    if equipment_issues:
        base["considerations"].append(_consideration(
            "equipment", "unknown", "散件与配方匹配、携带者适配、空槽、职责或阶段不明确，不能考虑装备。", equipment_required, equipment_issues))
    elif all(fields[name]["value"] for name in equipment_required):
        base["considerations"].append(_consideration(
            "equipment", "conditional", "调用方明确散件与配方匹配、携带者适配和职责；可把装备列为候选，仍不比较强度。", equipment_required, []))
    else:
        base["considerations"].append(_consideration(
            "equipment", "hold", "至少一项装备条件不成立，保留组件并等待下一次清晰观察。", equipment_required, []))
    return base


def synthetic_snapshot() -> dict:
    """A deliberately fictional fully-known input for tests and local demos."""
    values = {
        "gold": 31, "reserve_gold": 20, "next_purchase_reserve": 3, "candidate_price": 2,
        "sale_value": 1,
        "candidate_immediate_upgrade": True, "bench_space_available": True, "preparation_phase": True,
        "candidate_nonessential": False, "board_legal_after_sale": True,
        "board_slot_available": True, "candidate_can_field_now": True,
        "components_known_unequipped": True, "recipe_known": False, "components_match_recipe": False,
        "carrier_known": False, "carrier_has_slot": False, "carrier_suits_recipe": False,
        "carrier_need_known": False,
    }
    return {
        "format_version": FORMAT_VERSION,
        "sample_kind": "synthetic",
        "fields": {name: {"status": "known", "value": value} for name, value in values.items()},
    }
