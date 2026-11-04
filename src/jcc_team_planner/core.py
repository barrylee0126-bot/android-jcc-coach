"""Conservative, version-bound team candidate planning.

The planner is deliberately narrow: it enumerates legal board combinations
from a supplied board, bench and (at most one) supplied shop purchase.  It
compares declared trait thresholds; it does not estimate combat, roll odds,
unit strength, item effects, or a current live patch.
"""

from __future__ import annotations

from copy import deepcopy
from itertools import combinations
import json
import math
import re
from typing import Any

from jcc_recipe_candidate import analyze_request as recipe_candidates


REQUEST_FORMAT = "jcc-team-planner-request/v1"
RESULT_FORMAT = "jcc-team-planner/v1"
PARTIAL_HINT_REQUEST_FORMAT = "jcc-partial-trait-hint-request/v1"
PARTIAL_HINT_RESULT_FORMAT = "jcc-partial-trait-hint/v1"
_BINDING_KEYS = ("game", "region", "mode", "season", "patch", "rules_revision")
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}")
_MAX_UNITS = 12
_MAX_CAPACITY = 10
_MAX_SHOP = 5
_MAX_TRAITS = 32
_MAX_TRANSITIONS = 10
# Full combination materialization is deliberately kept small.  Larger pools
# use the separately labelled single-change scope below, rather than producing
# a slow answer that looks like a full search.
_MAX_ENUMERATED_LINEUPS = 5_000


def _issue(code: str, path: str, message_zh: str) -> dict[str, str]:
    return {"code": code, "path": path, "message_zh": message_zh}


def _unknown(issues: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "format_version": RESULT_FORMAT,
        "status": "unknown",
        "strategy_recommendation": False,
        "execution_eligible": False,
        "automatic_action": None,
        "binding": None,
        "data_kind": None,
        "lineup_candidates": None,
        "equipment_candidates": None,
        "transition_candidates": None,
        "issues": issues,
        "notes_zh": [
            "输入必须是同一明确版本、同一模式的完整已知棋盘、备战席、商店和羁绊门槛。",
            "unknown 不把未识别棋子、价格、装备、空位或羁绊门槛当成零。",
            "输出只比较已声明羁绊档位与资源约束，不是胜率、最优阵容或实际点击指令。",
        ],
    }


def _exact(value: Any, keys: set[str], path: str, issues: list[dict[str, str]]) -> bool:
    if type(value) is not dict or set(value) != keys:
        issues.append(_issue("shape_invalid", path, "字段缺失、字段多余或对象类型不正确。"))
        return False
    return True


def _identifier(value: Any) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _text(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= 512 and value == value.strip()


def _finite_int(value: Any, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _binding(value: Any, path: str, issues: list[dict[str, str]]) -> dict[str, str] | None:
    if not _exact(value, set(_BINDING_KEYS), path, issues):
        return None
    if not all(_text(value[key]) and value[key].lower() not in {"unknown", "current", "latest", "any"}
               for key in _BINDING_KEYS):
        issues.append(_issue("binding_invalid", path, "游戏、地区、模式、赛季、补丁和规则修订必须显式且非未知。"))
        return None
    return {key: value[key] for key in _BINDING_KEYS}


def _evidence(value: Any, path: str, issues: list[dict[str, str]]) -> tuple[str, list[str]] | None:
    if not _exact(value, {"kind", "refs"}, path, issues):
        return None
    if (type(value["kind"]) is not str or value["kind"] not in {"synthetic", "verified"}
            or type(value["refs"]) is not list or not value["refs"]):
        issues.append(_issue("evidence_invalid", path, "证据须声明为 synthetic 或 verified，并给出非空来源引用。"))
        return None
    if len(value["refs"]) > 32 or not all(_text(ref) for ref in value["refs"]) or len(set(value["refs"])) != len(value["refs"]):
        issues.append(_issue("evidence_invalid", path + ".refs", "来源引用须是非空、不重复文本列表。"))
        return None
    return value["kind"], list(value["refs"])


def _rule_binding(value: Any, path: str, binding: dict[str, str], issues: list[dict[str, str]],
                  fields: set[str]) -> dict | None:
    if not _exact(value, fields | {"rule_id", "binding"}, path, issues):
        return None
    if not _identifier(value["rule_id"]):
        issues.append(_issue("rule_invalid", path + ".rule_id", "规则 ID 须为明确标识。"))
    rule_binding = _binding(value["binding"], path + ".binding", issues)
    if rule_binding is not None and rule_binding != binding:
        issues.append(_issue("rule_binding_mismatch", path + ".binding", "计数或合星规则必须与本局版本绑定完全一致。"))
    return value if not issues else None


def _parse_catalog(request: dict[str, Any], issues: list[dict[str, str]]) -> tuple[dict[str, dict], dict[str, list[int]]] | None:
    units = request["unit_catalog"]
    traits = request["trait_catalog"]
    if type(units) is not list or not units or len(units) > _MAX_UNITS * 2:
        issues.append(_issue("unit_catalog_invalid", "$.unit_catalog", "棋子目录须是有界、非空的显式列表。"))
        return None
    if type(traits) is not list or not traits or len(traits) > _MAX_TRAITS:
        issues.append(_issue("trait_catalog_invalid", "$.trait_catalog", "羁绊目录须是有界、非空的显式列表。"))
        return None
    trait_map: dict[str, list[int]] = {}
    for index, row in enumerate(traits):
        path = f"$.trait_catalog[{index}]"
        if not _exact(row, {"trait_id", "thresholds"}, path, issues):
            continue
        trait_id, thresholds = row["trait_id"], row["thresholds"]
        if not _identifier(trait_id) or trait_id in trait_map or type(thresholds) is not list or not thresholds:
            issues.append(_issue("trait_catalog_invalid", path, "羁绊 ID 和门槛列表必须明确且不重复。"))
            continue
        if (not all(_finite_int(item, 1, _MAX_CAPACITY) for item in thresholds)
                or thresholds != sorted(set(thresholds))):
            issues.append(_issue("trait_threshold_invalid", path + ".thresholds", "羁绊门槛须为严格递增、1 至 10 的整数。"))
            continue
        trait_map[trait_id] = list(thresholds)
    unit_map: dict[str, dict] = {}
    for index, row in enumerate(units):
        path = f"$.unit_catalog[{index}]"
        if not _exact(row, {"unit_id", "cost", "traits", "role_tags"}, path, issues):
            continue
        unit_id, cost, unit_traits, roles = row["unit_id"], row["cost"], row["traits"], row["role_tags"]
        if not _identifier(unit_id) or unit_id in unit_map or not _finite_int(cost, 0, 99):
            issues.append(_issue("unit_catalog_invalid", path, "棋子 ID 须唯一，价格须为 0 至 99 的整数。"))
            continue
        if (type(unit_traits) is not list or not unit_traits or len(unit_traits) > 5
                or not all(_identifier(item) and item in trait_map for item in unit_traits)
                or len(set(unit_traits)) != len(unit_traits)):
            issues.append(_issue("unit_traits_invalid", path + ".traits", "棋子羁绊必须是目录中的不重复明确 ID。"))
            continue
        if (type(roles) is not list or not all(_identifier(item) for item in roles)
                or len(set(roles)) != len(roles)):
            issues.append(_issue("unit_roles_invalid", path + ".role_tags", "角色标签须为不重复的显式 ID；空列表可表示未声明职责。"))
            continue
        unit_map[unit_id] = {"unit_id": unit_id, "cost": cost, "traits": list(unit_traits), "role_tags": list(roles)}
    return (unit_map, trait_map) if not issues else None


def _parse_owned(rows: Any, name: str, units: dict[str, dict], issues: list[dict[str, str]]) -> list[dict] | None:
    if type(rows) is not list or len(rows) > _MAX_UNITS:
        issues.append(_issue("owned_units_invalid", "$." + name, "棋盘和备战席须是有界列表。"))
        return None
    output: list[dict] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        path = f"$.{name}[{index}]"
        if not _exact(row, {"instance_id", "unit_id", "star", "is_core", "equipped_item_ids"}, path, issues):
            continue
        instance_id, unit_id = row["instance_id"], row["unit_id"]
        if not _identifier(instance_id) or instance_id in seen or not _identifier(unit_id) or unit_id not in units:
            issues.append(_issue("owned_unit_invalid", path, "每个已持有棋子须使用唯一实例 ID，并映射到明确棋子目录。"))
            continue
        if not _finite_int(row["star"], 1, 4) or type(row["is_core"]) is not bool:
            issues.append(_issue("owned_unit_invalid", path, "星级须为 1 至 4 的整数，核心标志须为布尔值。"))
            continue
        equipped = row["equipped_item_ids"]
        if type(equipped) is not list or len(equipped) > 3 or not all(_identifier(item) for item in equipped):
            issues.append(_issue("equipped_items_invalid", path + ".equipped_item_ids", "已穿装备只接受至多三件明确 ID。"))
            continue
        seen.add(instance_id)
        output.append(deepcopy(row))
    return output if not issues else None


def _parse_shop(rows: Any, units: dict[str, dict], issues: list[dict[str, str]]) -> list[dict] | None:
    if type(rows) is not list or len(rows) > _MAX_SHOP:
        issues.append(_issue("shop_invalid", "$.shop", "商店须是至多五格的明确列表；空列表代表已知为空。"))
        return None
    output, slots = [], set()
    for index, row in enumerate(rows):
        path = f"$.shop[{index}]"
        if not _exact(row, {"slot", "unit_id", "cost"}, path, issues):
            continue
        slot, unit_id, cost = row["slot"], row["unit_id"], row["cost"]
        if (not _finite_int(slot, 1, _MAX_SHOP) or slot in slots or not _identifier(unit_id)
                or unit_id not in units or not _finite_int(cost, 0, 99) or cost != units[unit_id]["cost"]):
            issues.append(_issue("shop_invalid", path, "商店格、棋子及价格须明确；价格必须与调用方同一目录一致。"))
            continue
        slots.add(slot)
        output.append({"slot": slot, "unit_id": unit_id, "cost": cost})
    return output if not issues else None


def _has_unmodeled_merge(instances: list[dict], merge_count: int) -> bool:
    counts: dict[tuple[str, int], int] = {}
    for item in instances:
        key = (item["unit_id"], item["star"])
        counts[key] = counts.get(key, 0) + 1
    return any(count >= merge_count for count in counts.values())


def _would_merge_with_purchase(instances: list[dict], unit_id: str, merge_count: int) -> bool:
    return sum(item["unit_id"] == unit_id and item["star"] == 1 for item in instances) >= merge_count - 1


def _shop_instance_id(offer: dict, occupied_ids: set[str]) -> str:
    prefix = f"shop-candidate:{offer['slot']}:{offer['unit_id']}:"
    counter = 1
    while prefix + str(counter) in occupied_ids:
        counter += 1
    return prefix + str(counter)


def _trait_state(lineup: tuple[dict, ...], units: dict[str, dict], traits: dict[str, list[int]]) -> dict[str, dict]:
    counts = {trait_id: 0 for trait_id in traits}
    for instance in lineup:
        for trait_id in units[instance["unit_id"]]["traits"]:
            counts[trait_id] += 1
    result = {}
    for trait_id, thresholds in traits.items():
        active = [threshold for threshold in thresholds if threshold <= counts[trait_id]]
        result[trait_id] = {"trait_id": trait_id, "count": counts[trait_id],
                            "active_threshold": active[-1] if active else None,
                            "next_threshold": next((x for x in thresholds if x > counts[trait_id]), None)}
    return result


def _trait_changes(before: dict[str, dict], after: dict[str, dict]) -> list[dict]:
    changes = []
    for trait_id in before:
        old, new = before[trait_id], after[trait_id]
        if old["active_threshold"] != new["active_threshold"]:
            direction = "gained_or_upgraded" if (new["active_threshold"] or 0) > (old["active_threshold"] or 0) else "lost_or_downgraded"
            changes.append({"trait_id": trait_id, "from_active_threshold": old["active_threshold"],
                            "to_active_threshold": new["active_threshold"], "change": direction})
    return changes


def _candidate_key(item: dict) -> tuple:
    # This is a transparent trait-coverage ordering, not a power or win-rate score.
    active = sum(state["active_threshold"] is not None for state in item["traits"].values())
    tier_sum = sum(state["active_threshold"] or 0 for state in item["traits"].values())
    changes = len(item["changes"]["add_instance_ids"]) + len(item["changes"]["remove_instance_ids"])
    return (-active, -tier_sum, changes, item["purchase"]["cost"] if item["purchase"] else 0,
            tuple(item["lineup_instance_ids"]))


def _lineup_candidates(board: list[dict], bench: list[dict], shop: list[dict], capacity: int, bench_capacity: int,
                       gold: int, merge_count: int, automatic_merge: bool, count_duplicates: bool,
                       units: dict[str, dict], traits: dict[str, list[int]]) -> tuple[list[dict], list[dict], dict]:
    current = tuple(board)
    baseline = _trait_state(current, units, traits)
    all_owned = board + bench
    scenarios: list[tuple[dict | None, list[dict]]] = [(None, all_owned)]
    excluded: list[dict] = []
    occupied_ids = {item["instance_id"] for item in all_owned}
    for offer in shop:
        if offer["cost"] > gold:
            excluded.append({"slot": offer["slot"], "unit_id": offer["unit_id"], "reason": "insufficient_gold"})
            continue
        if len(bench) >= bench_capacity:
            excluded.append({"slot": offer["slot"], "unit_id": offer["unit_id"], "reason": "bench_full"})
            continue
        if not count_duplicates and any(x["unit_id"] == offer["unit_id"] for x in all_owned):
            excluded.append({"slot": offer["slot"], "unit_id": offer["unit_id"], "reason": "duplicate_counting_not_allowed"})
            continue
        if automatic_merge and sum(x["unit_id"] == offer["unit_id"] and x["star"] == 1 for x in all_owned) >= merge_count - 1:
            excluded.append({"slot": offer["slot"], "unit_id": offer["unit_id"], "reason": "would_require_unmodeled_merge"})
            continue
        candidate_id = f"shop-candidate:{offer['slot']}:{offer['unit_id']}:1"
        counter = 1
        while candidate_id in occupied_ids:
            counter += 1
            candidate_id = f"shop-candidate:{offer['slot']}:{offer['unit_id']}:{counter}"
        enriched = offer | {"candidate_instance_id": candidate_id}
        scenarios.append((enriched, all_owned + [{"instance_id": candidate_id,
                                                   "unit_id": offer["unit_id"], "star": 1,
                                                   "is_core": False, "equipped_item_ids": []}]))
    estimated = sum(math.comb(len(pool), min(capacity, len(pool))) for _, pool in scenarios)
    baseline_record = {"candidate_role": "current_baseline", "lineup_instance_ids": sorted(x["instance_id"] for x in current),
                       "purchase": None, "purchase_candidate_instance_id": None, "gold_after_purchase": gold,
                       "traits": baseline, "trait_changes": [], "changes": {"add_instance_ids": [], "remove_instance_ids": [],
                       "added_instance_details": [], "removed_instance_details": [], "removed_star_total": 0,
                       "removed_equipped_item_count": 0}, "preserved_core_instance_ids": sorted(x["instance_id"] for x in current if x["is_core"]),
                       "combat_power_evaluated": False}
    if estimated > _MAX_ENUMERATED_LINEUPS:
        board_by_id = {item["instance_id"]: item for item in board}
        board_ids = set(board_by_id)
        required_core_ids = {item["instance_id"] for item in all_owned if item["is_core"]}
        local: list[dict] = []
        seen_local: set[tuple[str, ...]] = set()
        def add_local(lineup: list[dict], purchase: dict | None) -> None:
            lineup_ids = {item["instance_id"] for item in lineup}
            # A core can start on the bench.  A local alternative may never
            # silently discard it just because the fallback does not enumerate
            # every full-board combination.
            if not required_core_ids.issubset(lineup_ids):
                return
            key = tuple(sorted(lineup_ids))
            if key in seen_local:
                return
            seen_local.add(key)
            removed = [deepcopy(board_by_id[item_id]) for item_id in sorted(board_ids - lineup_ids)]
            added = [deepcopy(item) for item in lineup if item["instance_id"] not in board_ids]
            state = _trait_state(tuple(lineup), units, traits)
            local.append({"candidate_role": "single_swap_local_only", "lineup_instance_ids": list(key),
                "purchase": None if purchase is None else {k: v for k, v in purchase.items() if k != "candidate_instance_id"},
                "purchase_candidate_instance_id": None if purchase is None else purchase["candidate_instance_id"],
                "gold_after_purchase": gold - purchase["cost"] if purchase else gold, "traits": state,
                "trait_changes": _trait_changes(baseline, state),
                "changes": {"add_instance_ids": sorted(lineup_ids - board_ids), "remove_instance_ids": sorted(board_ids - lineup_ids),
                    "added_instance_details": added, "removed_instance_details": removed,
                    "removed_star_total": sum(x["star"] for x in removed),
                    "removed_equipped_item_count": sum(len(x["equipped_item_ids"]) for x in removed)},
                "preserved_core_instance_ids": sorted(required_core_ids),
                "combat_power_evaluated": False})
        incoming = [(item, None) for item in bench]
        for purchase, pool in scenarios:
            if purchase is not None:
                incoming.append((next(item for item in pool if item["instance_id"] == purchase["candidate_instance_id"]), purchase))
        for item, purchase in incoming:
            if len(board) < capacity:
                add_local(board + [item], purchase)
            # Even with a free population slot, a player may compare a one-in,
            # one-out lineup (for example to avoid keeping a weak non-core
            # unit).  This is still within the declared single-change scope.
            for outgoing in board:
                if not outgoing["is_core"]:
                    add_local([row for row in board if row["instance_id"] != outgoing["instance_id"]] + [item], purchase)
        local.sort(key=_candidate_key)
        baseline_has_all_cores = required_core_ids.issubset(board_ids)
        local_core_status = (
            "satisfied_in_local_alternatives" if local
            else "single_change_cannot_preserve_all_known_cores"
            if not baseline_has_all_cores else "no_local_alternative"
        )
        return [baseline_record] + local[:2], excluded, {"status": "local_single_swap_only", "estimated_candidate_count": estimated,
            "enumerated_candidate_count": len(local), "displayed_candidate_count": 1 + min(2, len(local)), "display_truncated": len(local) > 2,
            "maximum_enumerated": _MAX_ENUMERATED_LINEUPS,
            "search_scope": "current_board_plus_one_bench_or_one_shop_purchase_single_change",
            "scope_exhaustive": True, "global_strategy_evaluated": False,
            "required_core_instance_ids": sorted(required_core_ids),
            "core_preservation_scope_status": local_core_status}
    candidates, seen = [], set()
    for purchase, pool in scenarios:
        size = min(capacity, len(pool))
        for lineup in combinations(pool, size):
            core_ids = {item["instance_id"] for item in pool if item["is_core"]}
            lineup_ids = {item["instance_id"] for item in lineup}
            if not core_ids.issubset(lineup_ids):
                continue
            stable_ids = tuple(sorted(lineup_ids))
            # A different purchase cannot be meaningful if it is not in the lineup.
            if purchase is not None and purchase["candidate_instance_id"] not in lineup_ids:
                continue
            uniqueness = (stable_ids, purchase["slot"] if purchase else None)
            if uniqueness in seen:
                continue
            seen.add(uniqueness)
            state = _trait_state(lineup, units, traits)
            board_by_id = {item["instance_id"]: item for item in board}
            board_ids = set(board_by_id)
            removed = [deepcopy(board_by_id[x]) for x in sorted(board_ids - lineup_ids)]
            added = [deepcopy(x) for x in lineup if x["instance_id"] not in board_ids]
            candidates.append({
                "candidate_role": "trait_alternative",
                "lineup_instance_ids": list(stable_ids),
                "purchase": None if purchase is None else {k: v for k, v in purchase.items() if k != "candidate_instance_id"},
                "purchase_candidate_instance_id": None if purchase is None else purchase["candidate_instance_id"],
                "gold_after_purchase": gold - purchase["cost"] if purchase else gold,
                "traits": state,
                "trait_changes": _trait_changes(baseline, state),
                "changes": {"add_instance_ids": sorted(lineup_ids - board_ids),
                            "remove_instance_ids": sorted(board_ids - lineup_ids), "added_instance_details": added,
                            "removed_instance_details": removed, "removed_star_total": sum(x["star"] for x in removed),
                            "removed_equipped_item_count": sum(len(x["equipped_item_ids"]) for x in removed)},
                "preserved_core_instance_ids": sorted(core_ids),
                "combat_power_evaluated": False,
            })
    candidates.sort(key=_candidate_key)
    nonbaseline = [x for x in candidates if x["lineup_instance_ids"] != baseline_record["lineup_instance_ids"]]
    displayed = [baseline_record] + nonbaseline[:2]
    return displayed, excluded, {"status": "complete", "estimated_candidate_count": estimated,
        "enumerated_candidate_count": len(candidates), "displayed_candidate_count": len(displayed),
        "display_truncated": len(nonbaseline) > 2, "maximum_enumerated": _MAX_ENUMERATED_LINEUPS,
        "search_scope": "explicit_board_bench_and_at_most_one_current_shop_purchase",
        "scope_exhaustive": True, "global_strategy_evaluated": False,
        "required_core_instance_ids": sorted(item["instance_id"] for item in all_owned if item["is_core"]),
        "core_preservation_scope_status": "satisfied_in_enumerated_candidates"}


def _transition_candidates(targets: Any, known_units: set[str], core_instances: list[dict], capacity: int,
                           shop: list[dict], units: dict[str, dict], traits: dict[str, list[int]],
                           issues: list[dict[str, str]]) -> list[dict] | None:
    if type(targets) is not list or len(targets) > _MAX_TRANSITIONS:
        issues.append(_issue("transitions_invalid", "$.transition_targets", "转型目标须为至多十个的列表。"))
        return None
    output, ids = [], set()
    shop_by_unit = {row["unit_id"]: row for row in shop}
    for index, target in enumerate(targets):
        path = f"$.transition_targets[{index}]"
        if not _exact(target, {"target_id", "required_unit_ids", "target_traits"}, path, issues):
            continue
        target_id, required, target_traits = target["target_id"], target["required_unit_ids"], target["target_traits"]
        if not _identifier(target_id) or target_id in ids or type(required) is not list or not required or len(required) > _MAX_UNITS:
            issues.append(_issue("transition_invalid", path, "转型须有唯一 ID 和有界、非空的目标棋子列表。"))
            continue
        if not all(_identifier(unit_id) and unit_id in units for unit_id in required) or len(set(required)) != len(required):
            issues.append(_issue("transition_invalid", path + ".required_unit_ids", "目标棋子必须是目录内不重复 ID。"))
            continue
        if type(target_traits) is not list or not target_traits:
            issues.append(_issue("transition_invalid", path + ".target_traits", "目标羁绊必须明确列出。"))
            continue
        normalized_traits = []
        for trait_index, row in enumerate(target_traits):
            trait_path = f"{path}.target_traits[{trait_index}]"
            if not _exact(row, {"trait_id", "threshold"}, trait_path, issues):
                continue
            if (not _identifier(row["trait_id"]) or type(row["threshold"]) is not int
                    or row["trait_id"] not in traits or row["threshold"] not in traits.get(row["trait_id"], [])):
                issues.append(_issue("transition_trait_invalid", trait_path, "目标羁绊和档位必须来自同一羁绊目录。"))
                continue
            normalized_traits.append(deepcopy(row))
        if issues:
            continue
        ids.add(target_id)
        missing = [unit_id for unit_id in required if unit_id not in known_units]
        available_shop = [deepcopy(shop_by_unit[unit_id]) for unit_id in missing if unit_id in shop_by_unit]
        core_unit_ids = {item["unit_id"] for item in core_instances}
        projected_count = len(required) + len(core_instances) - len(set(required) & core_unit_ids)
        target_counts = {trait_id: sum(trait_id in units[unit_id]["traits"] for unit_id in required)
                         for trait_id in traits}
        target_traits_satisfied = all(target_counts[row["trait_id"]] >= row["threshold"] for row in normalized_traits)
        output.append({"target_id": target_id, "required_unit_ids": list(required), "target_traits": normalized_traits,
                       "held_unit_ids": [unit_id for unit_id in required if unit_id in known_units],
                       "missing_unit_ids": missing, "shop_matches_for_missing": available_shop,
                       "current_shop_missing_purchase_cost_if_all_available": sum(row["cost"] for row in available_shop)
                       if len(available_shop) == len(missing) else None,
                       "required_plus_core_instance_count": projected_count, "population_feasible_with_cores": projected_count <= capacity,
                       "target_trait_requirements_satisfied_by_declared_required_units": target_traits_satisfied,
                       "equipment_fit_evaluated": False, "immediate_combat_power_evaluated": False,
                       "status": "partially_available" if available_shop else "requirements_only",
                       "note_zh": "商店缺口成本只描述当前可见缺失棋子的价格；不表示人口、核心保留、目标羁绊、装备承接或即时战力已经满足。"})
    return output if not issues else None


def _equipment(request: dict, binding: dict[str, str], lineups: list[dict],
               instances: dict[str, dict], issues: list[dict[str, str]]) -> dict[str, Any] | None:
    if "equipment" not in request:
        return None
    equipment = request["equipment"]
    if not _exact(equipment, {"recipe_request", "carrier_rules"}, "$.equipment", issues):
        return None
    rules = equipment["carrier_rules"]
    if type(rules) is not list or len(rules) > 64:
        issues.append(_issue("carrier_rules_invalid", "$.equipment.carrier_rules", "装备携带者规则须为有界列表；空列表表示尚未声明任何适配规则。"))
        return None
    result = recipe_candidates(equipment["recipe_request"])
    if result["status"] != "evaluated" or result["binding"] != binding:
        issues.append(_issue("recipe_analysis_unknown", "$.equipment.recipe_request", "复用的配方候选必须成功计算且与阵容局面版本完全一致。"))
        return None
    allowed: dict[str, set[str]] = {}
    for index, row in enumerate(rules):
        path = f"$.equipment.carrier_rules[{index}]"
        if not _exact(row, {"item_id", "carrier_instance_ids"}, path, issues):
            continue
        if (not _identifier(row["item_id"]) or row["item_id"] in allowed or type(row["carrier_instance_ids"]) is not list
                or not row["carrier_instance_ids"] or not all(_identifier(item) for item in row["carrier_instance_ids"])):
            issues.append(_issue("carrier_rules_invalid", path, "每个配方成品须声明一次、非空的适配实例列表。"))
            continue
        allowed[row["item_id"]] = set(row["carrier_instance_ids"])
    output = []
    unknown_item_ids = []
    for candidate in result["candidates"]:
        item_id = candidate["item_id"]
        if item_id not in allowed:
            unknown_item_ids.append(item_id)
            continue
        by_lineup = []
        for index, lineup in enumerate(lineups):
            eligible = sorted(instance_id for instance_id in lineup["lineup_instance_ids"]
                              if instance_id in allowed[item_id]
                              and len(instances[instance_id]["equipped_item_ids"]) < 3)
            if eligible:
                by_lineup.append({"lineup_candidate_index": index,
                                  "eligible_carrier_instance_ids": eligible})
        if by_lineup:
            output.append({"item_id": item_id, "components": candidate["components"],
                           "eligible_carriers_by_lineup": by_lineup,
                           "feasibility": candidate["feasibility"],
                           "note_zh": "散件可行性来自已复用的配方候选；仅列出调用方声明适配、且未满三件装备的上场实例，不比较装备强度或效果。"})
    return {"evaluation": "partial_unknown" if unknown_item_ids else "evaluated",
            "candidates": output, "unknown_item_ids": sorted(unknown_item_ids)} if not issues else None


def plan(request: Any) -> dict[str, Any]:
    """Enumerate explicit trait/lineup candidates without touching a device."""
    issues: list[dict[str, str]] = []
    required = {"format_version", "binding", "evidence", "phase", "level_capacity", "bench_capacity", "gold", "board", "bench",
                "shop", "unit_catalog", "trait_catalog", "trait_counting_rule", "merge_rule", "transition_targets"}
    if type(request) is not dict or set(request) - (required | {"equipment"}) or not required.issubset(request):
        return _unknown([_issue("shape_invalid", "$", "请求字段不完整或含未知字段。")])
    if request["format_version"] != REQUEST_FORMAT:
        return _unknown([_issue("format_unsupported", "$.format_version", "不支持的请求格式版本。")])
    binding = _binding(request["binding"], "$.binding", issues)
    evidence = _evidence(request["evidence"], "$.evidence", issues)
    if request["phase"] != "preparation":
        issues.append(_issue("phase_ineligible", "$.phase", "只在调用方明确为准备阶段时计算上阵候选。"))
    if (not _finite_int(request["level_capacity"], 1, _MAX_CAPACITY)
            or not _finite_int(request["bench_capacity"], 0, _MAX_UNITS)
            or not _finite_int(request["gold"], 0, 999)):
        issues.append(_issue("resources_invalid", "$.level_capacity", "人口上限和金币须为明确、范围内整数。"))
    counting = _rule_binding(request["trait_counting_rule"], "$.trait_counting_rule", binding, issues,
                             {"same_unit_id_counts_separately"}) if binding else None
    merge = _rule_binding(request["merge_rule"], "$.merge_rule", binding, issues,
                          {"one_star_to_two_star_count", "automatic_merge_on_purchase"}) if binding else None
    if counting is not None and type(counting["same_unit_id_counts_separately"]) is not bool:
        issues.append(_issue("trait_counting_invalid", "$.trait_counting_rule", "同名棋子计数规则必须明确为布尔值。"))
    if merge is not None and (not _finite_int(merge["one_star_to_two_star_count"], 2, 9)
                              or type(merge["automatic_merge_on_purchase"]) is not bool):
        issues.append(_issue("merge_rule_invalid", "$.merge_rule", "合星数量和购入后自动合星规则必须明确。"))
    parsed = _parse_catalog(request, issues)
    if parsed is None or binding is None or evidence is None:
        return _unknown(issues)
    units, traits = parsed
    board = _parse_owned(request["board"], "board", units, issues)
    bench = _parse_owned(request["bench"], "bench", units, issues)
    if board is not None and bench is not None:
        overlap = {row["instance_id"] for row in board} & {row["instance_id"] for row in bench}
        if overlap:
            issues.append(_issue("instance_duplicate", "$.bench", "同一实例不能同时在棋盘和备战席。"))
        if len(board) > request["level_capacity"]:
            issues.append(_issue("board_over_capacity", "$.board", "棋盘棋子数超过调用方声明的人口上限。"))
        core_count = sum(item["is_core"] for item in board + bench)
        if core_count > request["level_capacity"]:
            issues.append(_issue("core_over_capacity", "$.board", "调用方标为核心的棋子超过人口上限，不能安全保留所有核心。"))
        if len(bench) > request["bench_capacity"]:
            issues.append(_issue("bench_over_capacity", "$.bench", "备战席棋子数超过调用方声明容量。"))
        duplicate = len({item["unit_id"] for item in board + bench}) != len(board) + len(bench)
        if duplicate and (counting is None or counting["same_unit_id_counts_separately"] is not True):
            issues.append(_issue("duplicate_trait_counting_unknown", "$.trait_counting_rule", "同名实例存在但规则未明确允许分别计羁绊。"))
        if merge is not None and merge["automatic_merge_on_purchase"] and _has_unmodeled_merge(board + bench, merge["one_star_to_two_star_count"]):
            issues.append(_issue("unmodeled_merge_present", "$.board", "局面已出现本模块未模拟的合星。"))
    shop = _parse_shop(request["shop"], units, issues)
    if issues or board is None or bench is None or shop is None:
        return _unknown(issues)
    lineups, excluded_purchases, search = _lineup_candidates(board, bench, shop, request["level_capacity"], request["bench_capacity"],
                                                               request["gold"], merge["one_star_to_two_star_count"], merge["automatic_merge_on_purchase"],
                                                               counting["same_unit_id_counts_separately"], units, traits)
    known_units = {item["unit_id"] for item in board + bench}
    core_instances = [item for item in board + bench if item["is_core"]]
    transitions = _transition_candidates(request["transition_targets"], known_units, core_instances,
                                         request["level_capacity"], shop, units, traits, issues)
    instances = {item["instance_id"]: item for item in board + bench}
    for candidate in lineups:
        if candidate["purchase"]:
            instance_id = candidate["purchase_candidate_instance_id"]
            instances[instance_id] = {"instance_id": instance_id, "unit_id": candidate["purchase"]["unit_id"],
                                      "star": 1, "is_core": False, "equipped_item_ids": []}
    equipment_requested = "equipment" in request
    equipment = _equipment(request, binding, lineups, instances, issues) if equipment_requested else None
    if issues:
        return _unknown(issues)
    return {
        "format_version": RESULT_FORMAT,
        "status": "evaluated",
        "strategy_recommendation": "conditional_lineup_candidates_only",
        "execution_eligible": False,
        "automatic_action": None,
        "binding": binding,
        "data_kind": evidence[0],
        "evidence": {"kind": evidence[0], "refs": evidence[1]},
        "lineup_candidates": lineups,
        "lineup_search": search,
        "excluded_shop_purchases": excluded_purchases,
        "equipment_candidates": equipment["candidates"] if equipment_requested else None,
        "equipment_evaluation": equipment["evaluation"] if equipment_requested else "not_requested",
        "equipment_unknown_item_ids": equipment["unknown_item_ids"] if equipment_requested else None,
        "transition_candidates": transitions,
        "issues": [],
        "notes_zh": [
            "候选仅按已声明羁绊档位、人口、金币、核心保留及至多一张当前商店购买枚举。排序不是战力、胜率或最优阵容评分。",
            "同名实例、星级、价格、棋子羁绊和装备适配均以调用方明确输入为准；本模块不识别画面，也不认证调用方资料。",
            "装备候选复用 jcc_recipe_candidate 的独立可行性；各配方从同一库存单独判断，不能据此同时合成多个成品。",
        ],
    }


def plan_json(text: Any) -> dict[str, Any]:
    try:
        if type(text) is not str:
            raise ValueError("JSON text required")
        pairs = lambda rows: _no_duplicate_object(rows)
        request = json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return _unknown([_issue("json_invalid", "$", "JSON 无效、含重复键或非有限数；未生成候选。")])
    return plan(request)


def _partial_hint_issue(code: str, path: str, message_zh: str) -> dict[str, str]:
    return _issue(code, path, message_zh)


def _partial_hint_unknown(issues: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "format_version": PARTIAL_HINT_RESULT_FORMAT,
        "status": "unknown",
        "execution_eligible": False,
        "automatic_action": None,
        "provenance_authenticated": False,
        "withdraw_previous_action": True,
        "hint_state": "withdrawn_invalid_input",
        "observation": None,
        "observed_trait_gaps": None,
        "candidate_projection": None,
        "candidate_projection_state": "withheld_invalid_input",
        "trait_effect_comparison": {"status": "unknown", "comparison": "unknown"},
        "issues": issues,
    }


def _partial_status_value(value: Any, path: str, minimum: int, maximum: int,
                          issues: list[dict[str, str]]) -> dict[str, Any] | None:
    if type(value) is not dict or set(value) != {"status", "value"}:
        issues.append(_partial_hint_issue("partial_value_invalid", path, "状态值须含 status 和 value。"))
        return None
    if value["status"] == "unknown" and value["value"] is None:
        return {"status": "unknown", "value": None}
    if value["status"] == "known" and _finite_int(value["value"], minimum, maximum):
        return {"status": "known", "value": value["value"]}
    issues.append(_partial_hint_issue("partial_value_invalid", path, "known 值须为范围内整数；unknown 值须为 null。"))
    return None


def _partial_pts_value(value: Any, path: str, issues: list[dict[str, str]]) -> dict[str, Any] | None:
    """Keep caller-provided PTS as text so sub-millisecond identity is not lost."""
    if type(value) is not dict or set(value) != {"status", "value"}:
        issues.append(_partial_hint_issue("partial_pts_invalid", path, "PTS 须含 status 和原始秒数字符串 value。"))
        return None
    if value["status"] == "unknown" and value["value"] is None:
        return {"status": "unknown", "value": None}
    if (value["status"] == "known" and type(value["value"]) is str
            and re.fullmatch(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,9})?", value["value"])):
        return {"status": "known", "value": value["value"]}
    issues.append(_partial_hint_issue("partial_pts_invalid", path, "已知 PTS 须为调用方提供的非负秒数字符串；本模块不认证其是否原始精度或对应同一帧。未知时须为 null。"))
    return None


def _parse_partial_observation(value: Any, issues: list[dict[str, str]]) -> dict[str, Any] | None:
    path = "$.observation"
    fields = {"source_kind", "evidence_status", "case_id", "frame_ref", "source_sha256", "pts_seconds", "freshness", "same_frame"}
    if not _exact(value, fields, path, issues):
        return None
    if type(value["source_kind"]) is not str or value["source_kind"] not in {"replay", "live", "synthetic", "unknown"}:
        issues.append(_partial_hint_issue("partial_observation_invalid", path + ".source_kind", "来源须为 replay、live、synthetic 或 unknown。"))
    if type(value["evidence_status"]) is not str or value["evidence_status"] not in {"machine_candidate", "agent_visual", "human_verified", "unknown"}:
        issues.append(_partial_hint_issue("partial_observation_invalid", path + ".evidence_status", "证据状态须与来源类型独立声明。"))
    if not _text(value["case_id"]) or not _text(value["frame_ref"]):
        issues.append(_partial_hint_issue("partial_observation_invalid", path, "case_id 和帧引用须为非空文本；它们只是调用方声明。"))
    source_hash = value["source_sha256"]
    if source_hash is not None and (type(source_hash) is not str or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None):
        issues.append(_partial_hint_issue("partial_observation_invalid", path + ".source_sha256", "来源哈希须为小写 SHA-256 或 null。"))
    pts_seconds = _partial_pts_value(value["pts_seconds"], path + ".pts_seconds", issues)
    if (type(value["freshness"]) is not str or value["freshness"] not in {"current", "stale", "unknown"}
            or type(value["same_frame"]) is not bool):
        issues.append(_partial_hint_issue("partial_observation_invalid", path, "新鲜度和同帧标志须明确。"))
    if issues or pts_seconds is None:
        return None
    return {"source_kind": value["source_kind"], "evidence_status": value["evidence_status"],
            "case_id": value["case_id"], "frame_ref": value["frame_ref"], "source_sha256": source_hash,
            "pts_seconds": pts_seconds, "freshness": value["freshness"], "same_frame": value["same_frame"]}


def _parse_partial_rules_context(value: Any, issues: list[dict[str, str]]) -> dict[str, Any] | None:
    path = "$.rules_context"
    if not _exact(value, {"status", "binding", "refs"}, path, issues):
        return None
    if value["status"] == "unknown" and value["binding"] is None and value["refs"] == []:
        return {"status": "unknown", "binding": None, "refs": []}
    if type(value["status"]) is str and value["status"] == "known":
        binding = _binding(value["binding"], path + ".binding", issues)
        refs = value["refs"]
        if type(refs) is not list or not refs or not all(_text(ref) for ref in refs) or len(set(refs)) != len(refs):
            issues.append(_partial_hint_issue("partial_rules_invalid", path + ".refs", "已知规则上下文须附不重复、非空来源引用。"))
        if binding is not None and not issues:
            return {"status": "known", "binding": binding, "refs": list(refs)}
    else:
        issues.append(_partial_hint_issue("partial_rules_invalid", path, "未知规则上下文须使用 binding=null、refs=[]。"))
    return None


def _parse_partial_phase(value: Any, issues: list[dict[str, str]]) -> dict[str, Any] | None:
    path = "$.phase"
    if not _exact(value, {"status", "value"}, path, issues):
        return None
    if value["status"] == "unknown" and value["value"] is None:
        return {"status": "unknown", "value": None}
    if (type(value["status"]) is str and value["status"] == "known" and type(value["value"]) is str
            and value["value"] in {"preparation", "combat", "carousel", "selection", "transition", "other"}):
        return deepcopy(value)
    issues.append(_partial_hint_issue("partial_phase_invalid", path, "阶段未知时 value 须为 null；已知阶段须为受支持枚举。"))
    return None


def _parse_partial_capacity(value: Any, issues: list[dict[str, str]]) -> dict[str, Any] | None:
    path = "$.field_capacity"
    if not _exact(value, {"status", "current", "maximum"}, path, issues):
        return None
    if value["status"] == "unknown" and value["current"] is None and value["maximum"] is None:
        return {"status": "unknown", "current": None, "maximum": None}
    if (value["status"] == "known" and _finite_int(value["current"], 0, _MAX_CAPACITY)
            and _finite_int(value["maximum"], 1, _MAX_CAPACITY) and value["current"] <= value["maximum"]):
        return deepcopy(value)
    issues.append(_partial_hint_issue("partial_capacity_invalid", path, "人口未知时须全为 null；已知人数不得超过上限。"))
    return None


def _parse_partial_traits(value: Any, issues: list[dict[str, str]]) -> list[dict] | None:
    path = "$.active_traits"
    if type(value) is not list or len(value) > _MAX_TRAITS:
        issues.append(_partial_hint_issue("partial_traits_invalid", path, "可见羁绊须为有界列表；空列表仅代表未识别到可用羁绊。"))
        return None
    output, ids = [], set()
    for index, row in enumerate(value):
        row_path = f"{path}[{index}]"
        if not _exact(row, {"trait_id", "display_name", "count", "next_threshold"}, row_path, issues):
            continue
        if (not _identifier(row["trait_id"]) or row["trait_id"] in ids or not _text(row["display_name"])
                or not _finite_int(row["count"], 0, _MAX_CAPACITY)
                or (row["next_threshold"] is not None
                    and (not _finite_int(row["next_threshold"], 1, _MAX_CAPACITY)
                         or row["next_threshold"] <= row["count"]))):
            issues.append(_partial_hint_issue("partial_traits_invalid", row_path, "羁绊 ID、显示名、当前数和下一档须明确且一致。"))
            continue
        ids.add(row["trait_id"])
        output.append(deepcopy(row))
    return output if not issues else None


def _parse_partial_candidate(value: Any, issues: list[dict[str, str]]) -> dict[str, Any] | None:
    path = "$.candidate"
    if type(value) is not dict or type(value.get("status")) is not str or value.get("status") not in {"unknown", "known"}:
        issues.append(_partial_hint_issue("partial_candidate_invalid", path, "候选棋子须明确为 known 或 unknown。"))
        return None
    if value["status"] == "unknown":
        if set(value) != {"status"}:
            issues.append(_partial_hint_issue("partial_candidate_invalid", path, "未知候选不得填入推测身份、价格或羁绊。"))
            return None
        return {"status": "unknown"}
    fields = {"status", "candidate_ref", "legality", "trait_net_deltas_status", "trait_net_deltas",
              "net_delta_basis", "cost", "star", "equipped_item_count"}
    if not _exact(value, fields, path, issues):
        return None
    if (not _text(value["candidate_ref"]) or type(value["legality"]) is not str
            or value["legality"] not in {"known_legal", "known_illegal", "unknown"}
            or type(value["trait_net_deltas_status"]) is not str
            or value["trait_net_deltas_status"] not in {"known", "unknown"}):
        issues.append(_partial_hint_issue("partial_candidate_invalid", path, "候选身份、合法性和羁绊净增量状态须明确。"))
    cost = _partial_status_value(value["cost"], path + ".cost", 0, 99, issues)
    star = _partial_status_value(value["star"], path + ".star", 1, 4, issues)
    equipped = _partial_status_value(value["equipped_item_count"], path + ".equipped_item_count", 0, 3, issues)
    deltas = value["trait_net_deltas"]
    if type(deltas) is not list or len(deltas) > 5:
        issues.append(_partial_hint_issue("partial_candidate_invalid", path + ".trait_net_deltas", "候选羁绊净增量须为有界列表。"))
        return None
    normalized, trait_ids = [], set()
    for index, row in enumerate(deltas):
        row_path = f"{path}.trait_net_deltas[{index}]"
        if not _exact(row, {"trait_id", "net_count_delta"}, row_path, issues):
            continue
        if (not _identifier(row["trait_id"]) or row["trait_id"] in trait_ids
                or not _finite_int(row["net_count_delta"], 0, _MAX_CAPACITY)):
            issues.append(_partial_hint_issue("partial_candidate_invalid", row_path, "候选羁绊净增量须为不重复的明确非负整数。"))
            continue
        trait_ids.add(row["trait_id"])
        normalized.append(deepcopy(row))
    basis = value["net_delta_basis"]
    if type(basis) is not dict or set(basis) != {"status", "refs"}:
        issues.append(_partial_hint_issue("partial_candidate_invalid", path + ".net_delta_basis", "净增量依据须含 status 和 refs。"))
        basis = None
    elif basis["status"] == "unknown" and basis["refs"] == []:
        basis = {"status": "unknown", "refs": []}
    elif (basis["status"] == "declared_current_board_rules" and type(basis["refs"]) is list and basis["refs"]
          and all(_text(ref) for ref in basis["refs"]) and len(set(basis["refs"])) == len(basis["refs"])):
        basis = {"status": "declared_current_board_rules", "refs": list(basis["refs"])}
    else:
        issues.append(_partial_hint_issue("partial_candidate_invalid", path + ".net_delta_basis", "已知净增量须声明当前棋盘/同名/纹章/合星规则依据；未知时 refs 须为空。"))
        basis = None
    if value["trait_net_deltas_status"] == "unknown" and normalized:
        issues.append(_partial_hint_issue("partial_candidate_invalid", path + ".trait_net_deltas", "净增量未知时不得混入推测数值。"))
    if value["trait_net_deltas_status"] == "known" and (basis is None or basis["status"] != "declared_current_board_rules"):
        issues.append(_partial_hint_issue("partial_candidate_invalid", path + ".net_delta_basis", "已知净增量必须附当前棋盘规则的调用方声明依据。"))
    if issues or cost is None or star is None or equipped is None:
        return None
    return {"status": "known", "candidate_ref": value["candidate_ref"], "legality": value["legality"],
            "trait_net_deltas_status": value["trait_net_deltas_status"], "trait_net_deltas": normalized,
            "net_delta_basis": basis,
            "cost": cost, "star": star, "equipped_item_count": equipped}


def plan_partial_trait_hint(request: Any) -> dict[str, Any]:
    """Return a non-executable, evidence-labelled hint from a partial frame.

    This interface deliberately does not coerce a replay/machine observation
    into :func:`plan`'s complete, version-bound board model.  It can therefore
    preserve a useful 4/5, 2/3 observation while withdrawing action advice
    when identity, phase, freshness, or rules are not established.
    """
    issues: list[dict[str, str]] = []
    required = {"format_version", "observation", "rules_context", "phase", "field_capacity", "gold",
                "active_traits", "trait_observation_complete", "candidate"}
    if type(request) is not dict or set(request) != required:
        return _partial_hint_unknown([_partial_hint_issue("partial_shape_invalid", "$", "局部提示请求字段不完整或含未知字段。")])
    if request["format_version"] != PARTIAL_HINT_REQUEST_FORMAT:
        return _partial_hint_unknown([_partial_hint_issue("partial_format_unsupported", "$.format_version", "不支持的局部提示格式。")])
    observation = _parse_partial_observation(request["observation"], issues)
    rules_context = _parse_partial_rules_context(request["rules_context"], issues)
    phase = _parse_partial_phase(request["phase"], issues)
    capacity = _parse_partial_capacity(request["field_capacity"], issues)
    gold = _partial_status_value(request["gold"], "$.gold", 0, 999, issues)
    traits = _parse_partial_traits(request["active_traits"], issues)
    if type(request["trait_observation_complete"]) is not bool:
        issues.append(_partial_hint_issue("partial_traits_invalid", "$.trait_observation_complete", "须明确是否已完整识别全部在场羁绊。"))
    candidate = _parse_partial_candidate(request["candidate"], issues)
    if issues or observation is None or rules_context is None or phase is None or capacity is None or gold is None or traits is None or candidate is None:
        return _partial_hint_unknown(issues)

    gaps = [{"trait_id": row["trait_id"], "display_name": row["display_name"], "current_count": row["count"],
             "next_threshold": row["next_threshold"],
             "needed_for_next_threshold": None if row["next_threshold"] is None else row["next_threshold"] - row["count"]}
            for row in traits]
    projection: dict[str, Any] | None = None
    if candidate["status"] == "known":
        net_deltas = {row["trait_id"]: row["net_count_delta"] for row in candidate["trait_net_deltas"]}
        projected = []
        if candidate["trait_net_deltas_status"] == "known":
            for row in gaps:
                added = net_deltas.get(row["trait_id"], 0)
                after = row["current_count"] + added
                opened = row["next_threshold"] if row["next_threshold"] is not None and after >= row["next_threshold"] else None
                projected.append({"trait_id": row["trait_id"], "display_name": row["display_name"],
                                  "before_count": row["current_count"], "caller_declared_net_count_delta": added,
                                  "after_count_if_added_to_open_slot": after, "would_reach_next_threshold": opened})
        projection = {"candidate_ref": candidate["candidate_ref"], "legality": candidate["legality"],
                      "cost": candidate["cost"], "star": candidate["star"],
                      "equipped_item_count": candidate["equipped_item_count"],
                      "trait_net_deltas_status": candidate["trait_net_deltas_status"],
                      "net_delta_basis": candidate["net_delta_basis"],
                      "trait_changes_if_added_to_open_slot": projected if candidate["trait_net_deltas_status"] == "known" else None,
                      "scope_zh": "只适用于调用方已按当前棋盘及同名/纹章/合星规则计算净增量的空位上场；不自行推断候选原生羁绊，也不支持换下棋子或装备承接。"}

    missing_conditions: list[str] = []
    if observation["source_kind"] != "live":
        missing_conditions.append("source_not_live")
    if observation["freshness"] != "current":
        missing_conditions.append("frame_not_current")
    if not observation["same_frame"]:
        missing_conditions.append("fields_not_bound_to_same_frame")
    if observation["evidence_status"] != "human_verified":
        missing_conditions.append("observation_not_human_verified")
    if rules_context["status"] != "known":
        missing_conditions.append("rules_context_unknown")
    if phase["status"] != "known" or phase["value"] != "preparation":
        missing_conditions.append("preparation_phase_not_established")
    if capacity["status"] != "known" or capacity["current"] >= capacity["maximum"]:
        missing_conditions.append("open_field_slot_not_established")
    if not request["trait_observation_complete"]:
        missing_conditions.append("active_traits_incomplete")
    if candidate["status"] != "known":
        missing_conditions.append("candidate_identity_unknown")
    else:
        if candidate["legality"] != "known_legal":
            missing_conditions.append("candidate_legality_not_established")
        if candidate["trait_net_deltas_status"] != "known":
            missing_conditions.append("candidate_trait_net_deltas_unknown")
        if candidate["cost"]["status"] != "known" or gold["status"] != "known":
            missing_conditions.append("purchase_cost_or_gold_unknown")
        elif candidate["cost"]["value"] > gold["value"]:
            missing_conditions.append("candidate_not_affordable")

    if not missing_conditions:
        hint_state = "conditional_manual_candidate"
        withdraw = False
        conditional_hint = "按调用方同帧、准备阶段、空位、候选身份与规则上下文的声明，可把列出的羁绊变化作为玩家手动比较条件；本模块未认证这些声明，且不能据此断言真实战力更强。"
    elif observation["source_kind"] in {"replay", "synthetic"} or observation["freshness"] != "current":
        hint_state = "observation_only"
        withdraw = True
        conditional_hint = "这是非当前或非实机来源的局面观察；保留可见人数与羁绊缺口，不生成当前动作提示。消费端须据 withdraw_previous_action 实际清除旧提示。"
    else:
        hint_state = "withdrawn_missing_conditions"
        withdraw = True
        conditional_hint = "关键条件未建立；仅展示可见羁绊缺口，尚不能安全建议上棋子、购买或更换阵容。消费端须据 withdraw_previous_action 实际清除旧提示。"
    projection_visible = not missing_conditions and projection is not None
    return {
        "format_version": PARTIAL_HINT_RESULT_FORMAT,
        "status": "evaluated_partial_observation",
        "execution_eligible": False,
        "automatic_action": None,
        "provenance_authenticated": False,
        "withdraw_previous_action": withdraw,
        "hint_state": hint_state,
        "observation": observation,
        "rules_context": rules_context,
        "phase": phase,
        "field_capacity": capacity,
        "gold": gold,
        "trait_observation_complete": request["trait_observation_complete"],
        "observed_trait_gaps": gaps,
        "candidate_projection": projection if projection_visible else None,
        "candidate_projection_state": "visible" if projection_visible else "withheld_missing_conditions"
        if candidate["status"] == "known" else "not_available",
        "trait_effect_comparison": {
            "status": "unknown", "comparison": "unknown",
            "reason_zh": "局部提示未接收同版本、受益对象明确且量纲可比的羁绊效果规则；羁绊档位变化不等于真实战力变化。"},
        "conditions_to_confirm": missing_conditions,
        "conditional_hint_zh": conditional_hint,
        "issues": [],
    }


def plan_partial_trait_hint_json(text: Any) -> dict[str, Any]:
    try:
        if type(text) is not str:
            raise ValueError("JSON text required")
        request = json.loads(text, object_pairs_hook=_no_duplicate_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return _partial_hint_unknown([_partial_hint_issue("json_invalid", "$", "JSON 无效、含重复键或非有限数；未生成局部提示。")])
    return plan_partial_trait_hint(request)


def _no_duplicate_object(rows: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in rows:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
