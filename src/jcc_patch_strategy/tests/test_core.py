from copy import deepcopy
import json
from pathlib import Path
import unittest

from jcc_patch_strategy import analyze, analyze_json
from jcc_patch_strategy.__main__ import synthetic_demo


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
