#!/usr/bin/env python3
"""Reproducible offline boundary checks for the local 18.6 ledger.

The 18.6 source is a partial, third-party-reprinted announcement.  This runner
therefore keeps source-backed announcement facts separate from fictional
fixtures used to exercise the existing planning APIs.  It never produces a
real-match action, a total combat-power score, or an item recommendation.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jcc_equipment_coach import analyze_request as analyze_equipment  # noqa: E402
from jcc_equipment_coach.synthetic import synthetic_request as equipment_fixture  # noqa: E402
from jcc_team_planner import plan as plan_team  # noqa: E402
from jcc_team_planner.fixtures import synthetic_request as team_fixture  # noqa: E402
from jcc_transition_coach import compare as compare_transition  # noqa: E402
from jcc_transition_coach.synthetic import request as transition_fixture  # noqa: E402


LEDGER_PATH = ROOT / "资料" / "版本数据" / "版本变动_18.6_第三方转述.json"
STATUS_PATH = ROOT / "资料" / "版本数据" / "当前版本状态_18.6_待绑定.json"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def ledger_entry(ledger: dict[str, Any], name: str, field: str) -> dict[str, Any]:
    for section in ledger["sections"]:
        for entry in section["entries"]:
            if entry.get("name") == name and entry.get("field") == field:
                return {"scope": section["scope"], "mode_specific": section["modeSpecific"], **entry}
    raise AssertionError(f"Missing announcement entry: {name}/{field}")


def percent(text: str) -> float:
    require(isinstance(text, str) and text.endswith("%"), f"Not a percentage: {text!r}")
    return float(text[:-1]) / 100.0


def scenario_ledger_boundary() -> dict[str, Any]:
    ledger, status = load_json(LEDGER_PATH), load_json(STATUS_PATH)
    require(ledger["version"] == "18.6", "18.6 ledger version changed")
    require(status["decisionEligibility"] is False, "announcement ledger must stay non-actionable")
    require(status["scope"]["targetClient"] == "unknown", "target client must remain unknown")
    require(status["scope"]["targetMatch"] == "unknown", "target match must remain unknown")
    require(status["coverage"]["fullRecipeCatalog"] == "unknown", "recipe catalog must remain unknown")
    require(all(section["modeSpecific"] is True for section in ledger["sections"]),
            "mode-specific sections must not look universal")
    return {
        "case": "18_6_announcement_boundary",
        "input_kind": "third_party_reprint_ledger",
        "result": "blocked_expected",
        "reason": "账本缺少客户端、对局、完整图鉴和完整配方绑定；策略资格为 false。",
    }


def scenario_known_component_rank_flips() -> dict[str, Any]:
    """Show that a documented patch can flip a *single-component* ordering.

    The base values and comparison alternatives are synthetic.  Only the three
    patch deltas read from the ledger are source-backed.  This is intentionally
    not a real unit, team, damage or item ranking.
    """
    ledger = load_json(LEDGER_PATH)
    elderwood = ledger_entry(ledger, "永恒之森", "9阶生命值加成")
    justice = ledger_entry(ledger, "正义之手", "全能吸血")
    radiant = ledger_entry(ledger, "光明版正义之手", "全能吸血")
    require(elderwood["scope"] == "自然之力" and elderwood["mode_specific"] is True,
            "Elderwood fact must stay Natural Force-only")
    require((elderwood["from"], elderwood["to"]) == ("25%", "10%"), "Elderwood delta changed")
    require((justice["from"], justice["to"]) == ("15%", "12%"), "Justice delta changed")
    require((radiant["from"], radiant["to"]) == ("30%", "25%"), "Radiant justice delta changed")
    require(justice["scope"] == "自然之力" and justice["mode_specific"] is True,
            "Hand of Justice fact must stay Natural Force-only")
    require(radiant["scope"] == "自然之力" and radiant["mode_specific"] is True,
            "Radiant Hand of Justice fact must stay Natural Force-only")

    # Synthetic, declared-only comparison baselines selected to straddle the
    # known deltas.  The engine does not claim either alternate exists in game.
    fictional_base_health = 1000
    fictional_other_health_component = 150
    old_elderwood_health = fictional_base_health * percent(elderwood["from"])
    new_elderwood_health = fictional_base_health * percent(elderwood["to"])
    require(old_elderwood_health > fictional_other_health_component > new_elderwood_health,
            "health comparison must intentionally straddle old/new values")

    fictional_other_omnivamp = 0.135
    old_justice = percent(justice["from"])
    new_justice = percent(justice["to"])
    require(old_justice > fictional_other_omnivamp > new_justice,
            "omnivamp comparison must intentionally straddle old/new values")

    fictional_other_radiant_omnivamp = 0.275
    old_radiant = percent(radiant["from"])
    new_radiant = percent(radiant["to"])
    require(old_radiant > fictional_other_radiant_omnivamp > new_radiant,
            "radiant omnivamp comparison must intentionally straddle old/new values")

    return {
        "case": "source_backed_component_only_rank_flips",
        "input_kind": "18.6 ledger facts plus declared fictional comparators",
        "known_facts": {
            "natural_force_9_elderwood_health": f"{elderwood['from']}→{elderwood['to']}",
            "natural_force_hand_of_justice_omnivamp": f"{justice['from']}→{justice['to']}",
            "natural_force_radiant_hand_of_justice_omnivamp": f"{radiant['from']}→{radiant['to']}",
        },
        "fictional_component_checks": {
            "health": {
                "base_health": fictional_base_health,
                "other_declared_component": fictional_other_health_component,
                "old_elderwood_component": old_elderwood_health,
                "new_elderwood_component": new_elderwood_health,
                "component_order": "old_elderwood > other > new_elderwood",
            },
            "omnivamp": {
                "other_declared_component": fictional_other_omnivamp,
                "old_justice_component": old_justice,
                "new_justice_component": new_justice,
                "component_order": "old_justice > other > new_justice",
            },
            "radiant_omnivamp": {
                "other_declared_component": fictional_other_radiant_omnivamp,
                "old_radiant_justice_component": old_radiant,
                "new_radiant_justice_component": new_radiant,
                "component_order": "old_radiant_justice > other > new_radiant_justice",
            },
        },
        "strategy_result": "component_only_flip_observed",
        "not_evaluated": "总战力、真实出装优先级、真实阵容强度、胜率、实际对局或当前平板版本。",
    }


def scenario_synthetic_trait_candidate() -> dict[str, Any]:
    result = plan_team(team_fixture())
    require(result["status"] == "evaluated" and result["data_kind"] == "synthetic",
            "synthetic team fixture must evaluate")
    candidate = next(item for item in result["lineup_candidates"]
                     if item["candidate_role"] == "trait_alternative" and item["purchase"] is None)
    leaf = candidate["traits"]["trait_leaf"]
    require(leaf["active_threshold"] == 2 and candidate["combat_power_evaluated"] is False,
            "fixture should expose a declared trait threshold without a power claim")
    return {
        "case": "synthetic_trait_threshold_comparison",
        "input_kind": "synthetic_fixture",
        "observed": "备战棋子上场后，声明的 trait_leaf 从未激活变为 2 阶。",
        "strategy_result": "conditional_trait_candidate_only",
        "not_evaluated": "真实战力、胜率、18.6棋子强度和实机可执行性。",
    }


def scenario_synthetic_equipment_conflict() -> dict[str, Any]:
    result = analyze_equipment(equipment_fixture())
    require(result["status"] == "evaluated" and result["data_kind"] == "synthetic",
            "synthetic equipment fixture must evaluate")
    require(len(result["candidates"]) == 2, "fixture should have two craftable alternatives")
    conflict = result["material_conflicts"]
    require(len(conflict) == 1 and conflict[0]["jointly_craftable"] is False,
            "fixture must surface the shared-component conflict")
    require(all(item["reservation_considerations"][0]["became_unreservable"] for item in result["candidates"]),
            "fixture must show declared transition-reservation pressure")
    return {
        "case": "synthetic_equipment_material_conflict",
        "input_kind": "synthetic_fixture",
        "observed": "两件可合成候选争用同一散件，任一候选都会破坏已声明的虚构转型保留。",
        "strategy_result": "compare_only_no_item_ranking",
        "not_evaluated": "正义之手或任何真实装备的强度、配方、携带者与当前库存。",
    }


def scenario_synthetic_transition_boundary() -> dict[str, Any]:
    fixture = transition_fixture()
    result = compare_transition(fixture["team_snapshot"], fixture["target_rule_evidence"])
    require(result["status"] == "evaluated" and result["data_kind"] == "synthetic",
            "synthetic transition fixture must evaluate")
    candidate = result["transition_candidates"][0]
    require(candidate["known_current_shop_purchase_cost_for_missing_units_only"] == 2,
            "fixture shop cost changed")
    require(candidate["immediate_transition_feasible"] is None,
            "transition fixture must not become an execution claim")
    require("equipped_unit_leaves_board_carrier_and_item_destination_unknown" in candidate["conditions_or_blocks"],
            "fixture must preserve the item-destination block")
    return {
        "case": "synthetic_transition_threshold",
        "input_kind": "synthetic_fixture",
        "observed": "当前商店补齐的已知缺口成本为 2，但离场装备去向未知。",
        "strategy_result": "conditional_or_blocked",
        "not_evaluated": "转型后即时战力、未来来牌概率、真实18.6路线。",
    }


def scenario_binding_mismatch_refusal() -> dict[str, Any]:
    fixture = transition_fixture()
    evidence = deepcopy(fixture["target_rule_evidence"])
    evidence[0]["binding"]["mode"] = "fiction-other-mode"
    result = compare_transition(fixture["team_snapshot"], evidence)
    require(result["status"] == "unknown", "mismatched route binding must be rejected")
    codes = {issue["code"] for issue in result["issues"]}
    # The transition API represents a mismatched route binding as an invalid
    # target-evidence row.  The important contract property is refusal, not a
    # more specific internal error name.
    require("target_evidence_invalid" in codes, "expected target evidence rejection")
    return {
        "case": "mode_or_version_mismatch_refusal",
        "input_kind": "synthetic_fixture_with_mismatched_binding",
        "strategy_result": "unknown_expected",
        "reason": "同一局面与目标路线的模式或版本绑定不一致，拒绝比较。",
    }


def main() -> int:
    scenarios = (
        scenario_ledger_boundary,
        scenario_known_component_rank_flips,
        scenario_synthetic_trait_candidate,
        scenario_synthetic_equipment_conflict,
        scenario_synthetic_transition_boundary,
        scenario_binding_mismatch_refusal,
    )
    results = [scenario() for scenario in scenarios]
    print(json.dumps({
        "format": "jcc-strategy-eval-18.6/v1",
        "status": "passed",
        "scenario_count": len(results),
        "results": results,
        "overall_boundary": (
            "Only Natural Force announcement deltas are source-backed. Existing planner, equipment and transition "
            "computations use fictional fixtures and are not current-match advice."
        ),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
