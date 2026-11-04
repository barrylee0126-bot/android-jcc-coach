"""A conservative adapter over the shared team-planner snapshot contract."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from jcc_team_planner import plan as team_plan


REQUEST_FORMAT = "jcc-transition-coach-request/v1"
RESULT_FORMAT = "jcc-transition-coach/v1"
_ID_LIMIT = 64


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
        "transition_candidates": None,
        "issues": issues,
        "notes_zh": [
            "转型比较只消费 team planner 已验证形状的同版本准备阶段快照。",
            "unknown 不把商店外来牌、装备去向、目标外空位或策略来源缺失解释成零。",
            "本模块不评估即时战力、胜率、未来来牌、装备效果或最优阵容，也不产生出售或点击指令。",
        ],
    }


def _exact(value: Any, keys: set[str], path: str, issues: list[dict[str, str]]) -> bool:
    if type(value) is not dict or set(value) != keys:
        issues.append(_issue("shape_invalid", path, "字段缺失、字段多余或对象类型不正确。"))
        return False
    return True


def _text(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= 1024 and value == value.strip()


def _target_evidence(value: Any, binding: dict[str, str], target_ids: set[str], issues: list[dict[str, str]]) -> dict[str, list[str]] | None:
    if type(value) is not list or len(value) != len(target_ids):
        issues.append(_issue("target_evidence_invalid", "$.target_rule_evidence", "每个目标路线必须各有一条同版本、非空策略依据。"))
        return None
    output: dict[str, list[str]] = {}
    keys = {"target_id", "binding", "refs"}
    for index, row in enumerate(value):
        path = f"$.target_rule_evidence[{index}]"
        if not _exact(row, keys, path, issues):
            continue
        target_id, rule_binding, refs = row["target_id"], row["binding"], row["refs"]
        if (not isinstance(target_id, str) or target_id not in target_ids or target_id in output
                or rule_binding != binding or type(refs) is not list or not refs or len(refs) > 16
                or not all(_text(ref) for ref in refs) or len(set(refs)) != len(refs)):
            issues.append(_issue("target_evidence_invalid", path, "目标 ID、版本绑定和来源引用必须明确且与局面完全一致。"))
            continue
        output[target_id] = list(refs)
    return output if not issues and set(output) == target_ids else None


def _traits(unit_ids: list[str], units: dict[str, dict], trait_catalog: dict[str, list[int]]) -> dict[str, dict]:
    counts = {trait_id: 0 for trait_id in trait_catalog}
    for unit_id in unit_ids:
        for trait_id in units[unit_id]["traits"]:
            counts[trait_id] += 1
    return {
        trait_id: {
            "trait_id": trait_id, "count": counts[trait_id],
            "active_threshold": max((tier for tier in thresholds if tier <= counts[trait_id]), default=None),
            "next_threshold": next((tier for tier in thresholds if tier > counts[trait_id]), None),
        }
        for trait_id, thresholds in trait_catalog.items()
    }


def _trait_changes(before: dict[str, dict], after: dict[str, dict]) -> list[dict]:
    result = []
    for trait_id, old in before.items():
        new = after[trait_id]
        if old["active_threshold"] != new["active_threshold"]:
            old_tier, new_tier = old["active_threshold"] or 0, new["active_threshold"] or 0
            result.append({
                "trait_id": trait_id, "from_active_threshold": old["active_threshold"],
                "to_active_threshold": new["active_threshold"],
                "change": "gained_or_upgraded" if new_tier > old_tier else "lost_or_downgraded",
            })
    return result


def _retained(board: list[dict], bench: list[dict], target_units: list[str]) -> list[dict]:
    """Retain facts conservatively; ordering is never a combat-strength score."""
    board_ids = {row["instance_id"] for row in board}
    retained = []
    for unit_id in target_units:
        matches = [row for row in board + bench if row["unit_id"] == unit_id]
        if matches:
            retained.append(sorted(matches, key=lambda row: (
                not row["is_core"], -len(row["equipped_item_ids"]), -row["star"],
                0 if row["instance_id"] in board_ids else 1, row["instance_id"]
            ))[0])
    return retained


def _candidate(target: dict, route_evidence: dict[str, Any], request: dict, units: dict[str, dict],
               trait_catalog: dict[str, list[int]]) -> dict:
    board, bench, shop = request["board"], request["bench"], request["shop"]
    target_units, capacity, bench_capacity, gold = (target["required_unit_ids"], request["level_capacity"],
                                                     request["bench_capacity"], request["gold"])
    kept = _retained(board, bench, target_units)
    kept_ids, held_unit_ids = {row["instance_id"] for row in kept}, {row["unit_id"] for row in kept}
    missing = [unit_id for unit_id in target_units if unit_id not in held_unit_ids]
    offers_by_unit: dict[str, list[dict]] = {}
    for offer in shop:
        offers_by_unit.setdefault(offer["unit_id"], []).append(offer)
    visible = [deepcopy(sorted(offers_by_unit[unit_id], key=lambda row: row["slot"])[0])
               for unit_id in missing if unit_id in offers_by_unit]
    not_visible = [unit_id for unit_id in missing if unit_id not in offers_by_unit]
    board_ids = {row["instance_id"] for row in board}
    kept_board_ids, kept_bench_ids = kept_ids & board_ids, kept_ids - board_ids
    board_to_bench = [row for row in board if row["instance_id"] not in kept_board_ids]
    projected_bench = len(bench) - len(kept_bench_ids) + len(board_to_bench)
    core_outside = [row for row in board + bench if row["is_core"] and row["instance_id"] not in kept_ids]
    equipped_outside = [row for row in board_to_bench if row["equipped_item_ids"]]
    current_traits = _traits([row["unit_id"] for row in board], units, trait_catalog)
    declared_target_traits = _traits(target_units, units, trait_catalog)
    declared_target_goals_met = all(declared_target_traits[row["trait_id"]]["count"] >= row["threshold"]
                           for row in target["target_traits"])
    all_missing_visible_cost = sum(row["cost"] for row in visible) if not not_visible else None
    blocks = []
    if len(target_units) > capacity:
        blocks.append("target_over_population")
    if len(target_units) < capacity:
        blocks.append("target_has_open_slots_and_does_not_describe_fillers")
    if core_outside:
        blocks.append("core_not_in_target")
    if equipped_outside:
        blocks.append("equipped_unit_leaves_board_carrier_and_item_destination_unknown")
    if projected_bench > bench_capacity:
        blocks.append("bench_capacity_after_swap_insufficient")
    if not_visible:
        blocks.append("target_units_not_in_current_shop")
    if all_missing_visible_cost is not None and all_missing_visible_cost > gold:
        blocks.append("insufficient_gold_for_all_visible_missing_units")
    if not declared_target_goals_met:
        blocks.append("declared_base_target_does_not_meet_its_trait_goal")
    # Final-slot arithmetic is not an operation proof.  The module does not
    # model a game-version-specific buy/move/bench sequence or its UI states.
    blocks.append("operation_sequence_not_modeled")
    return {
        "target_id": target["target_id"],
        "target_rule_evidence": route_evidence,
        "target_unit_ids": list(target_units),
        "target_traits": deepcopy(target["target_traits"]),
        "declared_base_target_trait_requirements_satisfied": declared_target_goals_met,
        "declared_base_target_traits": declared_target_traits,
        "declared_base_trait_changes_from_current_board": _trait_changes(current_traits, declared_target_traits),
        "actual_trait_changes_from_current_board": "unknown_unmodeled_emblems_and_modifiers",
        "retained_instance_ids": sorted(kept_ids),
        "retention_selection_basis": "core_then_equipped_count_then_star_then_current_board; not combat_power",
        "current_board_to_bench_instance_ids": sorted(row["instance_id"] for row in board_to_bench),
        "bench_to_board_retained_instance_ids": sorted(kept_bench_ids),
        "core_instance_ids_not_in_target": sorted(row["instance_id"] for row in core_outside),
        "equipped_board_instance_ids_with_unknown_item_destination": sorted(row["instance_id"] for row in equipped_outside),
        "missing_unit_ids": missing,
        "visible_shop_matches_for_missing": visible,
        "missing_unit_ids_not_in_current_shop": not_visible,
        "catalog_cost_for_not_visible_missing": [
            {"unit_id": unit_id, "catalog_cost": units[unit_id]["cost"], "immediate_purchase_cost": None,
             "note_zh": "当前商店未见该棋子；目录价格不代表何时会来牌或可以购买。"}
            for unit_id in not_visible
        ],
        "known_current_shop_purchase_cost_for_missing_units_only": all_missing_visible_cost,
        "known_current_shop_purchase_cost_is_total_transition_cost": False,
        "gold_after_current_shop_missing_unit_purchases_if_all_visible": (
            gold - all_missing_visible_cost if all_missing_visible_cost is not None else None
        ),
        "projected_bench_count_after_immediate_swap": projected_bench,
        "bench_capacity": bench_capacity,
        "final_bench_capacity_sufficient_if_declared_moves_complete": projected_bench <= bench_capacity,
        "operation_sequence_feasibility": "unknown_not_modeled",
        "target_open_slots": capacity - len(target_units),
        "status": "conditional_or_blocked",
        # Deliberately null: a non-empty "unknown" string is truthy in Python
        # and JavaScript, so consumers could misread it as permission to act.
        "immediate_transition_feasible": None,
        "conditions_or_blocks": blocks,
        "immediate_combat_power_evaluated": False,
        "future_shop_probability_evaluated": False,
        "note_zh": "棋盘离场实例不是出售指令。最终备战席容量通过不证明购买和移动中间步骤合法；真实羁绊还受未建模纹章等修饰影响，因此保持未知。",
    }


def compare(team_planner_request: Any, target_rule_evidence: Any) -> dict[str, Any]:
    """Compare declared targets after the shared planner accepts the snapshot."""
    source = team_plan(team_planner_request)
    if source["status"] != "evaluated":
        return _unknown([_issue("team_snapshot_unknown", "$.team_snapshot",
                                "共享阵容快照未通过 team planner 校验；未比较转型。")])
    request = team_planner_request
    targets = request["transition_targets"]
    if not targets:
        return _unknown([_issue("targets_missing", "$.team_snapshot.transition_targets",
                                "至少需要一条明确目标路线；空列表不能产生转型比较。")])
    target_ids = {row["target_id"] for row in targets}
    issues: list[dict[str, str]] = []
    refs = _target_evidence(target_rule_evidence, source["binding"], target_ids, issues)
    if refs is None:
        return _unknown(issues)
    units = {row["unit_id"]: row for row in request["unit_catalog"]}
    trait_catalog = {row["trait_id"]: row["thresholds"] for row in request["trait_catalog"]}
    return {
        "format_version": RESULT_FORMAT,
        "status": "evaluated",
        "strategy_recommendation": False,
        "comparison_scope": "conditional_target_transition_comparison_only",
        "execution_eligible": False,
        "automatic_action": None,
        "binding": source["binding"],
        "data_kind": source["data_kind"],
        "data_kind_scope": "snapshot_only; route evidence separately caller_declared",
        "snapshot_evidence": source["evidence"],
        "route_evidence_verification": "caller_declared_only",
        "source_snapshot_format": request["format_version"],
        "transition_candidates": [_candidate(row, {"binding": deepcopy(source["binding"]), "refs": refs[row["target_id"]],
                                                      "verification": "caller_declared_only"}, request, units, trait_catalog)
                                  for row in targets],
        "issues": [],
        "notes_zh": [
            "本模块直接消费 jcc_team_planner 已接受的事实快照；不另建棋子、容量、商店或版本规则。",
            "局面证据与路线依据分层保留。两者均由调用方声明；路线引用和字符串内容不能将整条路线升级为 verified。",
            "结果是目标差异比较，不判断哪条路线更强、更值得转、何时来牌或装备应给谁。",
        ],
    }


def _no_duplicate_object(rows: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in rows:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def compare_json(text: Any) -> dict[str, Any]:
    try:
        if type(text) is not str:
            raise ValueError("JSON text required")
        wrapper = json.loads(text, object_pairs_hook=_no_duplicate_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return _unknown([_issue("json_invalid", "$", "JSON 无效、含重复键或非有限数；未比较转型。")])
    issues: list[dict[str, str]] = []
    if not _exact(wrapper, {"format_version", "team_snapshot", "target_rule_evidence"}, "$", issues):
        return _unknown(issues)
    if wrapper["format_version"] != REQUEST_FORMAT:
        return _unknown([_issue("format_unsupported", "$.format_version", "不支持的请求格式版本。")])
    return compare(wrapper["team_snapshot"], wrapper["target_rule_evidence"])
