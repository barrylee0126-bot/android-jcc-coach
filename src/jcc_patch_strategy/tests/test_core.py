from copy import deepcopy
import json
from pathlib import Path
import unittest

from jcc_patch_strategy import analyze, analyze_json
from jcc_patch_strategy.__main__ import synthetic_demo
from jcc_patch_strategy.core import _percent


class PatchStrategyTests(unittest.TestCase):
    def request(self):
        return synthetic_demo()

    def evaluated(self, request=None):
        result = analyze(request or self.request())
        self.assertEqual("evaluated", result["status"], result["issues"])
        self.assertFalse(result["strategy_recommendation"])
        self.assertFalse(result["execution_eligible"])
        self.assertIsNone(result["automatic_action"])
        return result

    def test_end_to_end_announced_trait_and_equipment_deltas(self):
        result = self.evaluated()
        trait = result["trait_comparisons"][0]
        self.assertEqual((250.0, 100.0, -150.0), (trait["synthetic_before_contribution"], trait["synthetic_after_contribution"], trait["synthetic_delta"]))
        self.assertEqual("announced_trait_in_this_synthetic_math_only", trait["synthetic_winner_before"])
        self.assertEqual("synthetic_alternative_in_this_synthetic_math_only", trait["synthetic_winner_after"])
        justice, radiant = result["equipment_comparisons"]
        self.assertEqual((150.0, 120.0, -30.0), (justice["synthetic_before_restoration"], justice["synthetic_after_restoration"], justice["synthetic_delta"]))
        self.assertEqual("announced_item_in_this_synthetic_math_only", justice["synthetic_winner_before"])
        self.assertEqual("synthetic_alternative_in_this_synthetic_math_only", justice["synthetic_winner_after"])
        self.assertEqual((300.0, 250.0), (radiant["synthetic_before_restoration"], radiant["synthetic_after_restoration"]))
        self.assertEqual("announced_item_in_this_synthetic_math_only", radiant["synthetic_winner_before"])
        self.assertEqual("synthetic_alternative_in_this_synthetic_math_only", radiant["synthetic_winner_after"])

    def test_existing_transition_chain_runs_but_cannot_be_mapped_to_real_units(self):
        result = self.evaluated()
        transition = result["transition_comparison"]
        self.assertEqual("evaluated", transition["transition_chain"]["status"])
        self.assertFalse(transition["actual_18_6_unit_mapping_evaluated"])
        self.assertFalse(transition["actual_18_6_transition_recommendation"])
        candidate = transition["transition_chain"]["transition_candidates"][0]
        self.assertIn("operation_sequence_not_modeled", candidate["conditions_or_blocks"])
        self.assertFalse(candidate["immediate_combat_power_evaluated"])
        self.assertTrue(transition["patch_delta_read"])
        self.assertFalse(transition["patch_used_in_transition_score"])
        self.assertEqual("patch_not_scored_by_existing_transition_chain", transition["transition_score_status"])

    def test_tiered_trait_values_use_declared_ordinals_without_threshold_inference(self):
        result = self.evaluated()
        values = {row["scenario_id"]: row for row in result["trait_comparisons"]}
        self.assertEqual(10, len(values))
        expected = {
            "spirit_blossom_ordinal_1": (1, 4, 120.0, 120.0),
            "spirit_blossom_ordinal_2": (2, 4, 300.0, 300.0),
            "spirit_blossom_ordinal_3": (3, 4, 500.0, 550.0),
            "spirit_blossom_ordinal_4": (4, 4, 550.0, 650.0),
            "yordle_ordinal_1": (1, 3, 150.0, 150.0),
            "yordle_ordinal_2": (2, 3, 280.0, 300.0),
            "yordle_ordinal_3": (3, 3, 330.0, 380.0),
        }
        for scenario_id, (ordinal, count, before, after) in expected.items():
            row = values[scenario_id]
            self.assertEqual((ordinal, count), (row["announced_value_ordinal"], row["announced_value_count"]))
            self.assertEqual((before, after), (row["synthetic_before_contribution"], row["synthetic_after_contribution"]))
            self.assertTrue(row["announced_value_ordinal_is_not_activation_threshold"])
        self.assertEqual((50.0, 40.0), (values["rift_monster_power_ordinal_1"]["synthetic_before_contribution"], values["rift_monster_power_ordinal_1"]["synthetic_after_contribution"]))
        self.assertEqual((50.0, 40.0), (values["rift_monster_resist_ordinal_1"]["synthetic_before_contribution"], values["rift_monster_resist_ordinal_1"]["synthetic_after_contribution"]))

        request = self.request()
        request["trait_scenarios"][1].pop("announced_value_ordinal")
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("announced_value_ordinal_invalid", {x["code"] for x in result["issues"]})

    def test_hero_and_augment_entry_points_cover_announced_numeric_categories(self):
        result = self.evaluated()
        self.assertEqual(16, len(result["hero_comparisons"]))
        self.assertEqual(6, len(result["augment_comparisons"]))
        raw_hero = result["hero_comparisons"][0]
        self.assertEqual(("raw", "法术加成", 1, 3), (raw_hero["announced_encoding"], raw_hero["announced_unit"], raw_hero["announced_value_ordinal"], raw_hero["announced_value_count"]))
        percent_hero = result["hero_comparisons"][3]
        self.assertEqual(("percent", 150.0, 130.0), (percent_hero["announced_encoding"], percent_hero["synthetic_before_component"], percent_hero["synthetic_after_component"]))
        self.assertEqual("percent", result["augment_comparisons"][0]["announced_encoding"])
        self.assertEqual("raw", result["augment_comparisons"][2]["announced_encoding"])

        request = self.request()
        request["hero_scenarios"][0].update({"announced_value_ordinal": 2, "declared_multiplier_base": 2, "synthetic_alternative_contribution": 685})
        request["hero_scenarios"][3]["synthetic_alternative_contribution"] = 140
        request["augment_scenarios"][0]["synthetic_alternative_contribution"] = 110
        request["augment_scenarios"][2]["synthetic_alternative_value"] = 9
        result = self.evaluated(request)
        hero_raw, hero_percent = result["hero_comparisons"][0], result["hero_comparisons"][3]
        augment_percent, augment_raw = result["augment_comparisons"][0], result["augment_comparisons"][2]
        self.assertEqual((670.0, 700.0), (hero_raw["synthetic_before_component"], hero_raw["synthetic_after_component"]))
        self.assertEqual(("synthetic_alternative_in_this_synthetic_math_only", "announced_hero_in_this_synthetic_math_only"), (hero_raw["synthetic_winner_before"], hero_raw["synthetic_winner_after"]))
        self.assertEqual(("announced_hero_in_this_synthetic_math_only", "synthetic_alternative_in_this_synthetic_math_only"), (hero_percent["synthetic_winner_before"], hero_percent["synthetic_winner_after"]))
        self.assertEqual(("synthetic_alternative_in_this_synthetic_math_only", "announced_augment_in_this_synthetic_math_only"), (augment_percent["synthetic_winner_before"], augment_percent["synthetic_winner_after"]))
        self.assertEqual(("synthetic_alternative_in_this_synthetic_math_only", "announced_augment_in_this_synthetic_math_only"), (augment_raw["synthetic_winner_before"], augment_raw["synthetic_winner_after"]))

    def test_decimal_percent_and_transition_anchor_do_not_create_a_patch_score(self):
        self.assertEqual(0.105, _percent("10.5%"))
        self.assertEqual(0.005, _percent("0.5%"))
        first = self.evaluated()
        request = self.request()
        request["synthetic_transition"].update({"patch_trait_name": "峡谷野怪", "patch_trait_field": "7阶物理强度/法术强度"})
        second = self.evaluated(request)
        self.assertEqual(first["transition_comparison"]["transition_chain"], second["transition_comparison"]["transition_chain"])
        self.assertFalse(second["transition_comparison"]["patch_used_in_transition_score"])

    def test_malformed_api_values_fail_closed_without_type_or_overflow_exceptions(self):
        request = self.request()
        request["trait_scenarios"][0]["scenario_id"] = None
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("trait_scenario_invalid", {x["code"] for x in result["issues"]})

        request = self.request()
        request["equipment_scenarios"][0]["scenario_id"] = []
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("equipment_scenario_invalid", {x["code"] for x in result["issues"]})

        request = self.request()
        request["synthetic_transition"]["patch_trait_name"] = []
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("transition_patch_delta_missing", {x["code"] for x in result["issues"]})

        request = self.request()
        request["synthetic_transition"]["team_planner_request"]["evidence"] = None
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("transition_not_synthetic", {x["code"] for x in result["issues"]})

        request = self.request()
        request["trait_scenarios"][0]["declared_base_value"] = 10 ** 10000
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("trait_scenario_invalid", {x["code"] for x in result["issues"]})

    def test_non_synthetic_context_and_missing_announced_fields_fail_closed(self):
        request = self.request()
        request["data_kind"] = "replay"
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("data_kind_ineligible", {x["code"] for x in result["issues"]})
        request = self.request()
        request["trait_scenarios"][0]["field"] = "未列入公告"
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("announced_trait_delta_missing", {x["code"] for x in result["issues"]})

    def test_ledger_cannot_be_upgraded_to_complete_or_decision_eligible(self):
        request = self.request()
        request["patch_ledger"]["evidenceBoundary"]["fullCatalog"] = True
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("ledger_boundary_invalid", {x["code"] for x in result["issues"]})

    def test_section_kind_prevents_equipment_from_being_treated_as_a_trait(self):
        request = self.request()
        request["trait_scenarios"][0].update({"trait_name": "正义之手", "field": "全能吸血"})
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("announced_trait_delta_missing", {x["code"] for x in result["issues"]})

    def test_transition_patch_field_must_be_a_trait_entry(self):
        request = self.request()
        request["synthetic_transition"].update({"patch_trait_name": "正义之手", "patch_trait_field": "全能吸血"})
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("transition_patch_delta_missing", {x["code"] for x in result["issues"]})

    def test_tampered_ledger_content_or_source_fails_closed(self):
        request = self.request()
        for section in request["patch_ledger"]["sections"]:
            if section.get("kind") == "item_balance_not_recipe_catalog":
                for entry in section["entries"]:
                    if entry.get("name") == "正义之手" and entry.get("field") == "全能吸血":
                        entry["to"] = "99%"
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("ledger_binding_mismatch", {x["code"] for x in result["issues"]})

        request = self.request()
        request["patch_ledger"]["source"]["url"] = "https://example.invalid"
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("ledger_source_invalid", {x["code"] for x in result["issues"]})

    def test_incomplete_source_or_boundary_is_structured_unknown_not_an_exception(self):
        request = self.request()
        request["patch_ledger"]["source"] = {"classification": "third_party_reprint_stated_wechat"}
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("ledger_source_invalid", {x["code"] for x in result["issues"]})

        request = self.request()
        request["patch_ledger"]["evidenceBoundary"] = {"decisionEligibility": False}
        result = analyze(request)
        self.assertEqual("unknown", result["status"])
        self.assertIn("ledger_boundary_invalid", {x["code"] for x in result["issues"]})

    def test_json_rejects_duplicate_and_nonfinite_values(self):
        self.assertEqual("unknown", analyze_json('{"format_version": 1, "format_version": 2}')["status"])
        self.assertEqual("unknown", analyze_json('{"format_version": NaN}')["status"])

    def test_real_ledger_is_loaded_and_not_mutated(self):
        request = self.request()
        before = json.dumps(request, ensure_ascii=False, sort_keys=True)
        self.evaluated(request)
        self.assertEqual(before, json.dumps(request, ensure_ascii=False, sort_keys=True))
        root = Path(__file__).resolve().parents[3]
        ledger = json.loads((root / "资料" / "版本数据" / "版本变动_18.6_第三方转述.json").read_text(encoding="utf-8"))
        self.assertEqual("18.6", ledger["version"])
        self.assertFalse(ledger["evidenceBoundary"]["decisionEligibility"])


if __name__ == "__main__":
    unittest.main()
