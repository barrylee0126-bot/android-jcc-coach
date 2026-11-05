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


def synthetic_demo() -> dict:
    snapshot = synthetic_request()
    return {
        "format_version": REQUEST_FORMAT,
        "data_kind": "synthetic",
        "patch_ledger": _ledger(),
        "trait_scenarios": [{
            "scenario_id": "forest_nine_declared_base", "trait_name": "永恒之森", "field": "9阶生命值加成",
            "declared_base_value": 1000, "application": "base_times_percent",
            "synthetic_alternative_contribution": 175,
        }],
        "equipment_scenarios": [{
            "scenario_id": "justice_declared_damage", "item_name": "正义之手", "field": "全能吸血",
            "declared_damage_amount": 1000, "synthetic_alternative_restoration": 130,
        }, {
            "scenario_id": "radiant_justice_declared_damage", "item_name": "光明版正义之手", "field": "全能吸血",
            "declared_damage_amount": 1000, "synthetic_alternative_restoration": 260,
        }],
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
