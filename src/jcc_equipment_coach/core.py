"""Conservative equipment-choice analysis built on jcc_recipe_candidate.

The module deliberately evaluates only caller-declared facts.  It can show
which immediately craftable options contend for the same components, which
declared units still have a slot, and whether a declared future plan would
lose materials.  It never rates item strength or chooses an action.
"""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any
import unicodedata

from jcc_recipe_candidate import analyze_request as recipe_candidates


REQUEST_FORMAT = "jcc-equipment-coach-request/v1"
RESULT_FORMAT = "jcc-equipment-coach/v1"
MAX_INSTANCES = 128
MAX_RULES = 512
MAX_PLANS = 128
MAX_CONFLICT_CANDIDATES = 64
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}")


def _issue(code: str, path: str, message: str) -> dict:
    return {"code": code, "path": path, "message_zh": message}


def _unknown(issues: list[dict]) -> dict:
    return {
        "format_version": RESULT_FORMAT,
        "status": "unknown",
        "binding": None,
        "evidence": None,
        "data_kind": None,
        "recipe_status": None,
        "candidates": None,
        "material_conflicts": None,
        "material_conflict_scope": "not_evaluated",
        "reservation_plans": None,
        "issues": issues,
        "strategy_recommendation": False,
        "execution_eligible": False,
        "frame_binding_verification": "not_verified_by_module",
        "freshness_verification": "not_verified_by_module",
        "notes_zh": [
            "只比较调用方声明的配方、闲置散件、装备栏、携带适配和保留计划。",
            "不会从缺少的规则推断最佳装备、最佳携带者、即时战力、胜率或实际操作。",
            "素材冲突只说明这些选择不能同时从同一份起始库存完成，不代表其中任一项更强。",
        ],
    }


def _shape(value: Any, keys: set[str], path: str, issues: list[dict]) -> bool:
    if type(value) is not dict or set(value) != keys:
        issues.append(_issue("shape_invalid", path, "对象缺少必需字段、含未知字段或类型不正确。"))
        return False
    return True


def _id(value: Any) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _text(value: Any, *, allow_empty: bool = False) -> bool:
    return (type(value) is str and len(value) <= 1024 and value == value.strip()
            and (allow_empty or bool(value))
            and not any(unicodedata.category(char) in {"Cc", "Cs"} for char in value))


def _counts(rows: list[dict]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        result[row["component_id"]] = row["count"]
    return result


def _required(candidate: dict) -> dict[str, int]:
    return _counts(candidate["required_counts"])


def _shortages(required: dict[str, int], available: dict[str, int]) -> list[dict]:
    return [{"component_id": component, "required_count": count,
             "available_count": available.get(component, 0),
             "shortage_count": count - available.get(component, 0)}
            for component, count in sorted(required.items())
            if count > available.get(component, 0)]


def _add_counts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    result = dict(left)
    for component, count in right.items():
        result[component] = result.get(component, 0) + count
    return result


def _valid_evidence(value: Any, path: str, expected_kind: str, issues: list[dict]) -> dict | None:
    if type(value) is not dict or value.get("kind") != expected_kind:
        issues.append(_issue("evidence_kind_mismatch", path, "证据类型须与配方、库存的声明完全一致。"))
        return None
    expected = {"kind", "refs"} | ({"verification_ref"} if expected_kind == "verified" else set())
    if not _shape(value, expected, path, issues):
        return None
    refs = value["refs"]
    if (type(refs) is not list or not 1 <= len(refs) <= 32
            or not all(_text(ref) for ref in refs) or len(set(refs)) != len(refs)
            or (expected_kind == "verified" and not _text(value["verification_ref"]))):
        issues.append(_issue("evidence_invalid", path, "证据须有不重复、非空的来源；核验数据另需核验记录。"))
        return None
    result = {"kind": expected_kind, "refs": list(refs)}
    if expected_kind == "verified":
        result["verification_ref"] = value["verification_ref"]
    return result


def _known_block(value: Any, extra: set[str], path: str, binding: dict,
                 evidence_kind: str, issues: list[dict]) -> tuple[dict | None, dict | None]:
    expected = {"status", "binding", "evidence"} | extra
    if type(value) is not dict or value.get("status") != "known":
        issues.append(_issue("input_not_known", path + ".status", "输入须显式声明为已知；不以缺失字段补默认值。"))
        return None, None
    if not _shape(value, expected, path, issues):
        return None, None
    if value["binding"] != binding:
        issues.append(_issue("version_mismatch", path + ".binding", "装备栏、适配规则和保留计划必须与配方使用同一规则版本。"))
        return None, None
    checked_evidence = _valid_evidence(value["evidence"], path + ".evidence", evidence_kind, issues)
    return value, checked_evidence


def _validate(request: Any, recipe: dict) -> tuple[dict | None, list[dict]]:
    issues: list[dict] = []
    if not _shape(request, {"format_version", "recipe_request", "carrier_state", "carrier_fit", "reservations"}, "$", issues):
        return None, issues
    if request["format_version"] != REQUEST_FORMAT:
        issues.append(_issue("format_unsupported", "$.format_version", "请求格式版本不受支持。"))
    binding, evidence_kind = recipe["binding"], recipe["data_kind"]
    state, state_evidence = _known_block(request["carrier_state"], {"instances"}, "$.carrier_state",
                                          binding, evidence_kind, issues)
    instances: dict[str, dict] = {}
    if state is not None:
        rows = state["instances"]
        if type(rows) is not list or len(rows) > MAX_INSTANCES:
            issues.append(_issue("carrier_instances_invalid", "$.carrier_state.instances", "装备携带者须为有界列表。"))
        else:
            for index, row in enumerate(rows):
                path = f"$.carrier_state.instances[{index}]"
                if not _shape(row, {"instance_id", "unit_id", "equipped_item_ids", "item_slot_capacity"}, path, issues):
                    continue
                instance_id, unit_id = row["instance_id"], row["unit_id"]
                equipped, capacity = row["equipped_item_ids"], row["item_slot_capacity"]
                if not _id(instance_id) or instance_id in instances or not _id(unit_id):
                    issues.append(_issue("carrier_instance_invalid", path, "棋子实例 ID 须唯一且字段均为合法 ID。"))
                elif (type(equipped) is not list or len(equipped) > 16 or not all(_id(item) for item in equipped)
                      or type(capacity) is not int or isinstance(capacity, bool) or not 0 <= capacity <= 16
                      or len(equipped) > capacity):
                    issues.append(_issue("carrier_slots_invalid", path, "已装备 ID 与装备格上限必须完整明确，且已装备数不能超过上限。"))
                else:
                    instances[instance_id] = {"instance_id": instance_id, "unit_id": unit_id,
                                              "equipped_item_ids": list(equipped), "item_slot_capacity": capacity}

    fit = request["carrier_fit"]
    rules: dict[str, dict] | None = None
    fit_evidence = None
    if type(fit) is dict and fit.get("status") == "unknown":
        if not _shape(fit, {"status", "reason_zh"}, "$.carrier_fit", issues) or not _text(fit.get("reason_zh")):
            issues.append(_issue("carrier_fit_unknown_invalid", "$.carrier_fit", "未知适配规则也须给出非空原因。"))
        rules = None
    else:
        fit, fit_evidence = _known_block(fit, {"complete_for_catalog", "rules"}, "$.carrier_fit",
                                          binding, evidence_kind, issues)
        if fit is not None:
            if type(fit["complete_for_catalog"]) is not bool:
                issues.append(_issue("carrier_fit_completeness_invalid", "$.carrier_fit.complete_for_catalog", "完整性标志须为布尔值。"))
            rows = fit["rules"]
            rules = {}
            if type(rows) is not list or len(rows) > MAX_RULES:
                issues.append(_issue("carrier_rules_invalid", "$.carrier_fit.rules", "适配规则须为有界列表。"))
            else:
                for index, row in enumerate(rows):
                    path = f"$.carrier_fit.rules[{index}]"
                    if not _shape(row, {"item_id", "carrier_instance_ids", "condition_zh"}, path, issues):
                        continue
                    item_id, carrier_ids, condition = row["item_id"], row["carrier_instance_ids"], row["condition_zh"]
                    if (not _id(item_id) or item_id in rules or type(carrier_ids) is not list
                            or not carrier_ids or len(carrier_ids) > MAX_INSTANCES
                            or not all(_id(value) and value in instances for value in carrier_ids)
                            or len(set(carrier_ids)) != len(carrier_ids)
                            or not _text(condition)):
                        issues.append(_issue("carrier_rule_invalid", path, "规则须为单一成品、非空且存在的棋子实例和条件说明。"))
                    else:
                        rules[item_id] = {"carrier_instance_ids": list(carrier_ids), "condition_zh": condition}

    reservations = request["reservations"]
    plans: list[dict] | None = None
    reservation_evidence = None
    if type(reservations) is dict and reservations.get("status") == "unknown":
        if not _shape(reservations, {"status", "reason_zh"}, "$.reservations", issues) or not _text(reservations.get("reason_zh")):
            issues.append(_issue("reservations_unknown_invalid", "$.reservations", "未知保留计划也须给出非空原因。"))
        plans = None
    else:
        reservations, reservation_evidence = _known_block(reservations, {"plans"}, "$.reservations",
                                                           binding, evidence_kind, issues)
        if reservations is not None:
            rows = reservations["plans"]
            plans = []
            known_components = {row["component_id"] for row in recipe["inventory_snapshot"]}
            known_components.update(request["recipe_request"].get("catalog", {}).get("component_ids", []))
            seen_plans: set[str] = set()
            if type(rows) is not list or len(rows) > MAX_PLANS:
                issues.append(_issue("reservation_plans_invalid", "$.reservations.plans", "保留计划须为有界列表。"))
            else:
                for index, row in enumerate(rows):
                    path = f"$.reservations.plans[{index}]"
                    if not _shape(row, {"plan_id", "required_components", "condition_zh"}, path, issues):
                        continue
                    plan_id, components, condition = row["plan_id"], row["required_components"], row["condition_zh"]
                    if not _id(plan_id) or plan_id in seen_plans or not _text(condition) or type(components) is not list or not components:
                        issues.append(_issue("reservation_plan_invalid", path, "计划 ID、条件和至少一个保留散件都须明确。"))
                        continue
                    seen_plans.add(plan_id)
                    counts: dict[str, int] = {}
                    valid = True
                    for component_index, component in enumerate(components):
                        component_path = f"{path}.required_components[{component_index}]"
                        if not _shape(component, {"component_id", "count"}, component_path, issues):
                            valid = False
                            continue
                        component_id, count = component["component_id"], component["count"]
                        if (not _id(component_id) or component_id not in known_components or type(count) is not int
                                or isinstance(count, bool) or not 1 <= count <= 16 or component_id in counts):
                            issues.append(_issue("reservation_component_invalid", component_path, "保留散件须来自已给目录、不得重复且数量须为正整数。"))
                            valid = False
                        else:
                            counts[component_id] = count
                    if valid:
                        plans.append({"plan_id": plan_id, "required_components": counts, "condition_zh": condition})

    if issues:
        return None, issues
    return {"binding": deepcopy(binding), "recipe_evidence": deepcopy(recipe["evidence"]),
            "data_kind": evidence_kind, "instances": instances, "state_evidence": state_evidence,
            "rules": rules, "fit_evidence": fit_evidence, "fit_complete": None if rules is None else fit["complete_for_catalog"],
            "fit_unknown_reason": None if rules is not None else request["carrier_fit"]["reason_zh"],
            "plans": plans, "reservation_evidence": reservation_evidence,
            "reservation_unknown_reason": None if plans is not None else request["reservations"]["reason_zh"]}, []


def _carrier_result(candidate: dict, data: dict) -> dict:
    item_id = candidate["item_id"]
    if data["rules"] is None:
        return {"status": "unknown", "reason_zh": data["fit_unknown_reason"], "available_carriers": None}
    rule = data["rules"].get(item_id)
    if rule is None:
        return {"status": "unknown_missing_declared_rule" if data["fit_complete"] else "unknown_incomplete_catalog",
                "reason_zh": "已声明适配规则没有该成品；未把这解释为没有实际适配者。",
                "available_carriers": None}
    available, full = [], []
    for instance_id in rule["carrier_instance_ids"]:
        instance = data["instances"][instance_id]
        row = {"instance_id": instance_id, "unit_id": instance["unit_id"],
               "slots_free": instance["item_slot_capacity"] - len(instance["equipped_item_ids"])}
        (available if row["slots_free"] > 0 else full).append(row)
    return {"status": "declared_available" if available else "declared_all_slots_full",
            "condition_zh": rule["condition_zh"], "available_carriers": available,
            "full_declared_carriers": full}


def _plan_shortages_after_crafting(plan_required: dict[str, int], crafted: dict[str, int],
                                   inventory: dict[str, int]) -> list[dict]:
    """Compare a plan with the actual remaining stock after one craft."""
    rows = []
    for component, required_count in sorted(plan_required.items()):
        remaining = inventory.get(component, 0) - crafted.get(component, 0)
        if required_count > remaining:
            rows.append({"component_id": component, "plan_required_count": required_count,
                         "remaining_count_after_crafting": remaining,
                         "shortage_count": required_count - remaining})
    return rows


def _reservation_result(candidate: dict, data: dict, inventory: dict[str, int]) -> list[dict] | None:
    if data["plans"] is None:
        return None
    current = _required(candidate)
    rows = []
    for plan in data["plans"]:
        before = _shortages(plan["required_components"], inventory)
        after = _plan_shortages_after_crafting(plan["required_components"], current, inventory)
        before_shortage = sum(row["shortage_count"] for row in before)
        after_shortage = sum(row["shortage_count"] for row in after)
        uses_reserved = any(current.get(component, 0) > 0 for component in plan["required_components"])
        rows.append({"plan_id": plan["plan_id"], "condition_zh": plan["condition_zh"],
                     "plan_already_unmet": bool(before), "shortages_before": before,
                     "shortages_after_crafting": after,
                     "uses_declared_reserved_components": uses_reserved,
                     "became_unreservable": not bool(before) and bool(after),
                     "worsened_existing_shortage": bool(before) and after_shortage > before_shortage})
    return rows


def _conflicts(candidates: list[dict], inventory: dict[str, int]) -> tuple[list[dict] | None, str]:
    if len(candidates) > MAX_CONFLICT_CANDIDATES:
        return None, "not_evaluated_candidate_limit"
    result = []
    for left_index, left in enumerate(candidates):
        left_required = _required(left)
        for right in candidates[left_index + 1:]:
            shortages = _shortages(_add_counts(left_required, _required(right)), inventory)
            if shortages:
                result.append({"candidate_item_ids": [left["item_id"], right["item_id"]],
                               "jointly_craftable": False, "shortages": shortages})
    return result, "evaluated_distinct_candidate_pairs_only"


def analyze_request(request: Any) -> dict:
    """Evaluate explicit alternatives without mutating the supplied request."""
    if type(request) is not dict or set(request) != {"format_version", "recipe_request", "carrier_state", "carrier_fit", "reservations"}:
        return _unknown([_issue("shape_invalid", "$", "请求须完整包含配方、装备栏、适配规则和保留计划。")])
    recipe = recipe_candidates(request["recipe_request"])
    if recipe["status"] != "evaluated":
        return _unknown([_issue("recipe_candidates_unknown", "$.recipe_request", "配方候选未知；不会把它解释为没有可合成装备。")]
                        + deepcopy(recipe["issues"]))
    data, issues = _validate(request, recipe)
    if data is None:
        return _unknown(issues)
    inventory = _counts(recipe["inventory_snapshot"])
    candidates = []
    for recipe_candidate in recipe["candidates"]:
        row = deepcopy(recipe_candidate)
        row["carrier_evaluation"] = _carrier_result(recipe_candidate, data)
        row["reservation_considerations"] = _reservation_result(recipe_candidate, data, inventory)
        candidates.append(row)
    conflicts, conflict_scope = _conflicts(recipe["candidates"], inventory)
    plans = None
    if data["plans"] is not None:
        plans = []
        for plan in data["plans"]:
            shortages = _shortages(plan["required_components"], inventory)
            plans.append({"plan_id": plan["plan_id"], "condition_zh": plan["condition_zh"],
                          "status": "currently_reservable" if not shortages else "already_unmet",
                          "shortages": shortages})
    evidence = {"recipe": deepcopy(data["recipe_evidence"]), "carrier_state": deepcopy(data["state_evidence"])}
    if data["fit_evidence"] is not None:
        evidence["carrier_fit"] = deepcopy(data["fit_evidence"])
    else:
        evidence["carrier_fit"] = {"status": "unknown", "reason_zh": data["fit_unknown_reason"]}
    if data["reservation_evidence"] is not None:
        evidence["reservations"] = deepcopy(data["reservation_evidence"])
    else:
        evidence["reservations"] = {"status": "unknown", "reason_zh": data["reservation_unknown_reason"]}
    return {"format_version": RESULT_FORMAT, "status": "evaluated", "binding": deepcopy(data["binding"]),
            "evidence": evidence, "data_kind": data["data_kind"], "recipe_status": "evaluated",
            "candidates": candidates, "material_conflicts": conflicts, "material_conflict_scope": conflict_scope,
            "reservation_plans": plans, "issues": [], "strategy_recommendation": False,
            "execution_eligible": False, "frame_binding_verification": "not_verified_by_module",
            "freshness_verification": "not_verified_by_module",
            "notes_zh": [
                "成品来自固定上游函数的单项可行性检查；这里只检查不同成品候选两两是否争用同一份闲置散件，不检查同一成品做多件或三个以上选择的共同可行性。",
                "携带者仅来自调用方提供、同版本绑定的适配规则；顺序不是强度排序。",
                "保留计划只在其声明条件成立时供玩家比较；不会预测刷新、掉落、转型成功或即时战力。",
                "本模块不读取画面，无法证明库存、装备栏和携带者来自同一帧，也无法证明输入仍新鲜；上游须绑定帧 ID、采集时间和过期策略。",
            ]}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def analyze_json(text: Any) -> dict:
    try:
        if type(text) is not str:
            raise ValueError("JSON text required")
        request = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        return _unknown([_issue("json_invalid", "$", "JSON 无效、含重复键或非有限数；未推断装备状态。")])
    return analyze_request(request)
