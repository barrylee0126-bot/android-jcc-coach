"""Run a fully synthetic 18.6 patch-delta demonstration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jcc_team_planner.fixtures import synthetic_request

from .core import REQUEST_FORMAT, analyze


def _ledger() -> dict:
    root = Path(__file__).resolve().parents[2]
    return json.loads((root / "资料" / "版本数据" / "版本变动_18.6_第三方转述.json").read_text(encoding="utf-8"))


def _entries(ledger: dict, kind: str) -> list[dict]:
    for section in ledger["sections"]:
        if section.get("scope") == "自然之力" and section.get("kind") == kind:
            return section["entries"]
    raise ValueError(f"missing ledger section: {kind}")


def _hero_scenarios(ledger: dict) -> list[dict]:
    scenarios = []
    for index, entry in enumerate(_entries(ledger, "hero_balance"), 1):
        common = {"scenario_id": f"hero_{index:02d}", "hero_name": entry["name"], "field": entry["field"]}
        if entry["from"].endswith("%"):
            scenarios.append({**common, "declared_base_value": 1000, "application": "base_times_percent",
                              "synthetic_alternative_contribution": 0})
        else:
            scenarios.append({**common, "announced_value_ordinal": 1, "declared_multiplier_base": 1,
                              "application": "declared_multiplier_times_announced_value",
                              "synthetic_alternative_contribution": 0, "same_unit_assumption": True})
    return scenarios


def _augment_scenarios(ledger: dict) -> list[dict]:
    scenarios = []
    for index, entry in enumerate(_entries(ledger, "augment_balance"), 1):
        common = {"scenario_id": f"augment_{index:02d}", "augment_name": entry["name"], "field": entry["field"]}
        if entry["from"].endswith("%"):
            scenarios.append({**common, "declared_base_value": 1000, "application": "base_times_percent",
                              "synthetic_alternative_contribution": 0})
        else:
            scenarios.append({**common, "application": "announced_direct_value", "synthetic_alternative_value": 0,
                              "same_unit_assumption": True})
    return scenarios


def synthetic_demo() -> dict:
    snapshot = synthetic_request()
    ledger = _ledger()
    return {
        "format_version": REQUEST_FORMAT,
        "data_kind": "synthetic",
        "patch_ledger": ledger,
        "trait_scenarios": [{
            "scenario_id": "forest_nine_declared_base", "trait_name": "永恒之森", "field": "9阶生命值加成",
            "declared_base_value": 1000, "application": "base_times_percent",
            "synthetic_alternative_contribution": 175,
        }, {
            "scenario_id": "spirit_blossom_ordinal_1", "trait_name": "灵魂莲华", "field": "物理加成/法术加成",
            "announced_value_ordinal": 1, "declared_base_value": 1000, "application": "base_times_percent", "synthetic_alternative_contribution": 115,
        }, {
            "scenario_id": "spirit_blossom_ordinal_2", "trait_name": "灵魂莲华", "field": "物理加成/法术加成",
            "announced_value_ordinal": 2, "declared_base_value": 1000, "application": "base_times_percent", "synthetic_alternative_contribution": 290,
        }, {
            "scenario_id": "spirit_blossom_ordinal_3", "trait_name": "灵魂莲华", "field": "物理加成/法术加成",
            "announced_value_ordinal": 3, "declared_base_value": 1000, "application": "base_times_percent", "synthetic_alternative_contribution": 525,
        }, {
            "scenario_id": "spirit_blossom_ordinal_4", "trait_name": "灵魂莲华", "field": "物理加成/法术加成",
            "announced_value_ordinal": 4, "declared_base_value": 1000, "application": "base_times_percent", "synthetic_alternative_contribution": 600,
        }, {
            "scenario_id": "yordle_ordinal_1", "trait_name": "约德尔人和朋友", "field": "毛茸茸大朋友远程物理加成/法术加成",
            "announced_value_ordinal": 1, "declared_base_value": 1000, "application": "base_times_percent", "synthetic_alternative_contribution": 145,
        }, {
            "scenario_id": "yordle_ordinal_2", "trait_name": "约德尔人和朋友", "field": "毛茸茸大朋友远程物理加成/法术加成",
            "announced_value_ordinal": 2, "declared_base_value": 1000, "application": "base_times_percent", "synthetic_alternative_contribution": 290,
        }, {
            "scenario_id": "yordle_ordinal_3", "trait_name": "约德尔人和朋友", "field": "毛茸茸大朋友远程物理加成/法术加成",
            "announced_value_ordinal": 3, "declared_base_value": 1000, "application": "base_times_percent", "synthetic_alternative_contribution": 350,
        }, {
            "scenario_id": "rift_monster_power_ordinal_1", "trait_name": "峡谷野怪", "field": "7阶物理强度/法术强度",
            "declared_base_value": 1000, "application": "base_times_percent", "synthetic_alternative_contribution": 45,
        }, {
            "scenario_id": "rift_monster_resist_ordinal_1", "trait_name": "峡谷野怪", "field": "7阶护甲/魔抗",
            "declared_base_value": 1000, "application": "base_times_percent", "synthetic_alternative_contribution": 45,
        }],
        "equipment_scenarios": [{
            "scenario_id": "justice_declared_damage", "item_name": "正义之手", "field": "全能吸血",
            "declared_damage_amount": 1000, "synthetic_alternative_restoration": 130,
        }, {
            "scenario_id": "radiant_justice_declared_damage", "item_name": "光明版正义之手", "field": "全能吸血",
            "declared_damage_amount": 1000, "synthetic_alternative_restoration": 260,
        }],
        "hero_scenarios": _hero_scenarios(ledger),
        "augment_scenarios": _augment_scenarios(ledger),
        "synthetic_transition": {
            "team_planner_request": snapshot,
            "target_rule_evidence": [{"target_id": "leaf-shot", "binding": snapshot["binding"],
                                      "refs": ["synthetic:patch-transition-threshold"]}],
            "patch_trait_name": "永恒之森", "patch_trait_field": "9阶生命值加成",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="18.6 公告数值在 synthetic 局面中的离线差异演示。")
    parser.add_argument("--demo", action="store_true", help="运行内置 synthetic 演示。")
    args = parser.parse_args()
    if not args.demo:
        parser.error("仅支持 --demo；本模块不读取平板或真实对局。")
    print(json.dumps(analyze(synthetic_demo()), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
