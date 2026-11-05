"""Conservative patch-delta calculations and synthetic strategy-chain checks.

The 18.6 ledger is a third-party reprint of a stated public-account notice.
It contains only announced deltas, not a complete game catalogue.  This
adapter therefore consumes a caller-provided ledger and only evaluates
synthetic arithmetic and an existing synthetic transition comparison.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any

from jcc_transition_coach import compare as compare_transition


REQUEST_FORMAT = "jcc-patch-strategy-request/v1"
RESULT_FORMAT = "jcc-patch-strategy/v1"
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}")
_PERCENT = re.compile(r"([0-9]+(?:\\.[0-9]+)?)%")

# This pin proves only that the caller supplied the exact reviewed ledger
# content. It cannot authenticate the third-party source behind that content.
_PUBLISHED_LEDGER_CANONICAL_SHA256 = "2eea57972e64506f84eb315cb531fea037f980d536be056a4565020243547637"
_PUBLISHED_SOURCE = {
    "classification": "third_party_reprint_stated_wechat",
    "url": "https://news.17173.com/content/11042026/105243261.shtml",
    "publishedAt": "2026-11-04T10:52:43+08:00",
    "announcedEffectiveAt": "2026-11-04T12:00:00+08:00",
    "officialPrimarySourceVerified": False,
    "scope": "转载页称全服不停机更新，具体时间以实际为准",
}
_PUBLISHED_BOUNDARY = {
    "fullCatalog": False,
    "fullRecipeCatalog": False,
    "targetClientBinding": "unknown",
    "targetMatchBinding": "unknown",
    "decisionEligibility": False,
}


def _issue(code: str, path: str, message_zh: str) -> dict[str, str]:
    return {"code": code, "path": path, "message_zh": message_zh}


def _unknown(issues: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "format_version": RESULT_FORMAT,
        "status": "unknown",
        "strategy_recommendation": False,
        "execution_eligible": False,
        "automatic_action": None,
        "patch_context": None,
        "trait_comparisons": None,
        "equipment_comparisons": None,
        "transition_comparison": None,
        "issues": issues,
        "notes_zh": [
            "本模块只计算公告中明确列出的数值差，并且只接受 synthetic 测试局面。",
            "它不确认默认平板、当前对局、完整棋子目录、装备配方、热修或胜率。",
            "unknown 不把未列入公告的规则、物品效果或棋子关系补成零。",
        ],
    }


def _exact(value: Any, keys: set[str], path: str, issues: list[dict[str, str]]) -> bool:
    if type(value) is not dict or set(value) != keys:
        issues.append(_issue("shape_invalid", path, "字段缺失、字段多余或对象类型不正确。"))
        return False
    return True


def _finite_nonnegative(value: Any) -> bool:
    return type(value) in {int, float} and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _percent(value: Any) -> float | None:
    if type(value) is not str:
        return None
    match = _PERCENT.fullmatch(value.strip())
    return float(match.group(1)) / 100 if match else None


def _canonical_digest(value: dict) -> str | None:
    try:
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _entry_index(ledger: Any, issues: list[dict[str, str]]) -> dict[tuple[str, str, str], dict] | None:
    """Extract only explicit numeric named fields from a constrained ledger."""
    if type(ledger) is not dict:
        issues.append(_issue("ledger_invalid", "$.patch_ledger", "补丁账本必须是对象。"))
        return None
    expected = {"format", "version", "source", "evidenceBoundary", "sections", "unresolved"}
    if set(ledger) != expected or ledger.get("format") != "jcc-version-change-ledger/v1" or ledger.get("version") != "18.6":
        issues.append(_issue("ledger_invalid", "$.patch_ledger", "只接受完整的 18.6 变更账本。"))
        return None
    source = ledger.get("source")
    boundary = ledger.get("evidenceBoundary")
    if not _exact(source, set(_PUBLISHED_SOURCE), "$.patch_ledger.source", issues):
        issues.append(_issue("ledger_source_invalid", "$.patch_ledger.source", "来源必须完整且与已发布 18.6 公告账本一致。"))
        return None
    if not _exact(boundary, set(_PUBLISHED_BOUNDARY), "$.patch_ledger.evidenceBoundary", issues):
        issues.append(_issue("ledger_boundary_invalid", "$.patch_ledger.evidenceBoundary", "证据边界必须完整且与已发布 18.6 公告账本一致。"))
        return None
    if source != _PUBLISHED_SOURCE:
        issues.append(_issue("ledger_source_invalid", "$.patch_ledger.source", "来源字段未与已发布 18.6 公告账本精确匹配。"))
        return None
    if boundary != _PUBLISHED_BOUNDARY:
        issues.append(_issue("ledger_boundary_invalid", "$.patch_ledger.evidenceBoundary", "不完整目录和不可决策边界不得被改写。"))
        return None
    if _canonical_digest(ledger) != _PUBLISHED_LEDGER_CANONICAL_SHA256:
        issues.append(_issue("ledger_binding_mismatch", "$.patch_ledger", "账本内容未精确匹配已发布的 18.6 公告账本；未计算。"))
        return None
    sections = ledger.get("sections")
    if type(sections) is not list or not sections:
        issues.append(_issue("ledger_invalid", "$.patch_ledger.sections", "账本须含非空分区列表。"))
        return None
    output: dict[tuple[str, str, str], dict] = {}
    for section_index, section in enumerate(sections):
        path = f"$.patch_ledger.sections[{section_index}]"
        if type(section) is not dict or section.get("scope") != "自然之力" or section.get("modeSpecific") is not True:
            continue
        kind = section.get("kind")
        if type(kind) is not str:
            issues.append(_issue("ledger_invalid", path + ".kind", "自然之力分区必须有文本 kind。"))
            continue
        entries = section.get("entries")
        if type(entries) is not list:
            issues.append(_issue("ledger_invalid", path + ".entries", "自然之力分区的条目必须是列表。"))
            continue
        for entry_index, entry in enumerate(entries):
            if type(entry) is not dict:
                issues.append(_issue("ledger_invalid", f"{path}.entries[{entry_index}]", "账本条目必须是对象。"))
                continue
            name, field = entry.get("name"), entry.get("field")
            if type(name) is str and type(field) is str and _percent(entry.get("from")) is not None and _percent(entry.get("to")) is not None:
                key = (kind, name, field)
                if key in output:
                    issues.append(_issue("ledger_ambiguous", f"{path}.entries[{entry_index}]", "同一分区、名称和字段出现多个百分比变更，不能任选其一。"))
                else:
                    output[key] = deepcopy(entry)
    return output if not issues else None


def _trait_comparisons(rows: Any, index: dict[tuple[str, str, str], dict], issues: list[dict[str, str]]) -> list[dict] | None:
    if type(rows) is not list or not rows or len(rows) > 16:
        issues.append(_issue("trait_scenarios_invalid", "$.trait_scenarios", "羁绊测试局面须为有界、非空列表。"))
        return None
    output = []
    for row_index, row in enumerate(rows):
        path = f"$.trait_scenarios[{row_index}]"
        if not _exact(row, {"scenario_id", "trait_name", "field", "declared_base_value", "application", "synthetic_alternative_contribution"}, path, issues):
            continue
        if not _ID.fullmatch(row["scenario_id"]) or type(row["trait_name"]) is not str or type(row["field"]) is not str:
            issues.append(_issue("trait_scenario_invalid", path, "测试 ID、羁绊名称和字段必须明确。"))
            continue
        if (row["application"] != "base_times_percent" or not _finite_nonnegative(row["declared_base_value"])
                or not _finite_nonnegative(row["synthetic_alternative_contribution"])):
            issues.append(_issue("trait_scenario_invalid", path, "仅支持以声明基数乘公告百分比的非负 synthetic 测试。"))
            continue
        entry = index.get(("trait_balance", row["trait_name"], row["field"]))
        if entry is None:
            issues.append(_issue("announced_trait_delta_missing", path, "该羁绊字段不在自然之力 18.6 公告百分比变更中。"))
            continue
        before, after = _percent(entry["from"]), _percent(entry["to"])
        assert before is not None and after is not None
        base = float(row["declared_base_value"])
        alternative = float(row["synthetic_alternative_contribution"])
        before_value, after_value = base * before, base * after

        def winner(value: float) -> str:
            if value > alternative:
                return "announced_trait_in_this_synthetic_math_only"
            if value < alternative:
                return "synthetic_alternative_in_this_synthetic_math_only"
            return "tie_in_this_synthetic_math_only"

        output.append({
            "scenario_id": row["scenario_id"], "trait_name": row["trait_name"], "field": row["field"],
            "scope": "自然之力", "mode_specific": True,
            "declared_base_value": base, "application": row["application"],
            "synthetic_alternative_contribution": alternative,
            "announced_before_percent": before, "announced_after_percent": after,
            "synthetic_before_contribution": before_value,
            "synthetic_after_contribution": after_value,
            "synthetic_delta": after_value - before_value,
            "synthetic_winner_before": winner(before_value),
            "synthetic_winner_after": winner(after_value),
            "combat_power_evaluated": False,
            "note_zh": "只计算调用方虚构基数上的公告百分比贡献；不等同真实棋子生命、最终战力或上阵建议。",
        })
    return output if not issues else None


def _equipment_comparisons(rows: Any, index: dict[tuple[str, str, str], dict], issues: list[dict[str, str]]) -> list[dict] | None:
    if type(rows) is not list or not rows or len(rows) > 16:
        issues.append(_issue("equipment_scenarios_invalid", "$.equipment_scenarios", "装备测试局面须为有界、非空列表。"))
        return None
    output = []
    for row_index, row in enumerate(rows):
        path = f"$.equipment_scenarios[{row_index}]"
        keys = {"scenario_id", "item_name", "field", "declared_damage_amount", "synthetic_alternative_restoration"}
        if not _exact(row, keys, path, issues):
            continue
        if (not _ID.fullmatch(row["scenario_id"]) or type(row["item_name"]) is not str or type(row["field"]) is not str
                or not _finite_nonnegative(row["declared_damage_amount"]) or not _finite_nonnegative(row["synthetic_alternative_restoration"])):
            issues.append(_issue("equipment_scenario_invalid", path, "装备测试字段必须是明确的非负 synthetic 数值。"))
            continue
        entry = index.get(("item_balance_not_recipe_catalog", row["item_name"], row["field"]))
        if entry is None:
            issues.append(_issue("announced_equipment_delta_missing", path, "该装备字段不在自然之力 18.6 公告百分比变更中。"))
            continue
        before, after = _percent(entry["from"]), _percent(entry["to"])
        assert before is not None and after is not None
        damage, alternative = float(row["declared_damage_amount"]), float(row["synthetic_alternative_restoration"])
        old_restore, new_restore = damage * before, damage * after
        def winner(value: float) -> str:
            if value > alternative:
                return "announced_item_in_this_synthetic_math_only"
            if value < alternative:
                return "synthetic_alternative_in_this_synthetic_math_only"
            return "tie_in_this_synthetic_math_only"
        output.append({
            "scenario_id": row["scenario_id"], "item_name": row["item_name"], "field": row["field"],
            "scope": "自然之力", "mode_specific": True,
            "declared_damage_amount": damage, "synthetic_alternative_restoration": alternative,
            "announced_before_percent": before, "announced_after_percent": after,
            "synthetic_before_restoration": old_restore, "synthetic_after_restoration": new_restore,
            "synthetic_delta": new_restore - old_restore,
            "synthetic_winner_before": winner(old_restore), "synthetic_winner_after": winner(new_restore),
            "actual_recipe_known": False, "actual_carrier_evaluated": False,
            "note_zh": "比较只基于调用方虚构的伤害量和替代恢复量；公告未提供完整配方或携带者规则。",
        })
    return output if not issues else None


def _transition(row: Any, index: dict[tuple[str, str, str], dict], issues: list[dict[str, str]]) -> dict | None:
    required = {"team_planner_request", "target_rule_evidence", "patch_trait_name", "patch_trait_field"}
    if not _exact(row, required, "$.synthetic_transition", issues):
        return None
    trait_name, field = row["patch_trait_name"], row["patch_trait_field"]
    key = ("trait_balance", trait_name, field)
    if type(trait_name) is not str or type(field) is not str or key not in index:
        issues.append(_issue("transition_patch_delta_missing", "$.synthetic_transition", "转型测试必须引用公告中明确的自然之力百分比字段。"))
        return None
    snapshot = row["team_planner_request"]
    if type(snapshot) is not dict or snapshot.get("evidence", {}).get("kind") != "synthetic":
        issues.append(_issue("transition_not_synthetic", "$.synthetic_transition.team_planner_request", "转型测试只能使用明确声明为 synthetic 的 team planner 快照。"))
        return None
    comparison = compare_transition(snapshot, row["target_rule_evidence"])
    if comparison.get("status") != "evaluated":
        issues.append(_issue("transition_chain_unknown", "$.synthetic_transition", "现有转型比较器未接受 synthetic 快照；不推断转型。"))
        return None
    entry = index[key]
    return {
        "scope": "synthetic_transition_threshold_comparison",
        "patch_scope": "自然之力",
        "patch_trait_name": trait_name,
        "patch_trait_field": field,
        "announced_before_percent": _percent(entry["from"]), "announced_after_percent": _percent(entry["to"]),
        "transition_chain": comparison,
        "actual_18_6_unit_mapping_evaluated": False,
        "actual_18_6_transition_recommendation": False,
        "note_zh": "现有转型比较器只验证调用方虚构快照中的人口、羁绊门槛与可见缺口；未把虚构棋子映射成公告中的真实棋子。",
    }


def analyze(request: Any) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    required = {"format_version", "data_kind", "patch_ledger", "trait_scenarios", "equipment_scenarios", "synthetic_transition"}
    if not _exact(request, required, "$", issues):
        return _unknown(issues)
    if request["format_version"] != REQUEST_FORMAT:
        return _unknown([_issue("format_unsupported", "$.format_version", "不支持的补丁策略请求格式。")])
    if request["data_kind"] != "synthetic":
        return _unknown([_issue("data_kind_ineligible", "$.data_kind", "公告局部规则只能先在 synthetic 测试局面中计算。")])
    index = _entry_index(request["patch_ledger"], issues)
    if index is None:
        return _unknown(issues)
    traits = _trait_comparisons(request["trait_scenarios"], index, issues)
    equipment = _equipment_comparisons(request["equipment_scenarios"], index, issues)
    transition = _transition(request["synthetic_transition"], index, issues)
    if issues or traits is None or equipment is None or transition is None:
        return _unknown(issues)
    source = request["patch_ledger"]["source"]
    return {
        "format_version": RESULT_FORMAT,
        "status": "evaluated",
        "strategy_recommendation": False,
        "execution_eligible": False,
        "automatic_action": None,
        "data_kind": "synthetic",
        "patch_context": {
            "version": "18.6", "scope": "自然之力", "mode_specific": True,
            "source_classification": source["classification"], "source_url": source["url"],
            "target_client_binding": "unknown", "target_match_binding": "unknown",
            "full_catalog": False, "full_recipe_catalog": False,
            "ledger_binding": "canonical_content_match_to_published_ledger_only_not_source_authentication",
        },
        "trait_comparisons": traits,
        "equipment_comparisons": equipment,
        "transition_comparison": transition,
        "issues": [],
        "notes_zh": [
            "结果展示公告数值在虚构局面中的差异，不能生成购买、出售、合成、上阵或转型指令。",
            "装备恢复量和羁绊贡献均使用调用方声明的 synthetic 数值；不包含真实伤害、减伤、治疗限制或完整装备规则。",
            "默认平板、目标对局、热修、完整英雄目录和完整合成配方仍未知，实战策略资格保持 false。",
        ],
    }


def _no_duplicate_object(rows: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in rows:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def analyze_json(text: Any) -> dict[str, Any]:
    try:
        if type(text) is not str:
            raise ValueError("JSON text required")
        request = json.loads(text, object_pairs_hook=_no_duplicate_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return _unknown([_issue("json_invalid", "$", "JSON 无效、含重复键或非有限数；未计算补丁差异。")])
    return analyze(request)
