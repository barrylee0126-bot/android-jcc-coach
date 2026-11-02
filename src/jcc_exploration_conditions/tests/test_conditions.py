"""Synthetic tests: no device, rule lookup, live frame, or player account is used."""

from copy import deepcopy
import json
import math
import unittest

from jcc_exploration_conditions import FORMAT_VERSION, assess_conditions, synthetic_snapshot


def by_kind(result, kind):
    return next(row for row in result["considerations"] if row["kind"] == kind)


class ExplorationConditionsTests(unittest.TestCase):
    def snapshot(self):
        return synthetic_snapshot()

    def evaluated(self, request):
        result = assess_conditions(request)
        self.assertEqual(result["status"], "evaluated", result["issues"])
        self.assertFalse(result["execution_eligible"])
        self.assertFalse(result["strategy_recommendation"])
        self.assertIsNone(result["automatic_action"])
        self.assertTrue(all(row["action"] is None and not row["execution_eligible"]
                            for row in result["considerations"]))
        return result

    def test_synthetic_conditions_have_no_execution_path(self):
        result = self.evaluated(self.snapshot())
        self.assertEqual(by_kind(result, "buy")["status"], "conditional")
        self.assertEqual(by_kind(result, "sell")["status"], "hold")
        self.assertEqual(by_kind(result, "field")["status"], "conditional")
        self.assertEqual(by_kind(result, "equipment")["status"], "hold")
        self.assertFalse(result["real_rules_verified"])

    def test_budget_uses_explicit_reserve_and_next_purchase_reserve(self):
        request = self.snapshot()
        request["fields"]["gold"]["value"] = 24
        self.assertEqual(by_kind(self.evaluated(request), "buy")["status"], "hold")
        request["fields"]["gold"]["value"] = 25
        self.assertEqual(by_kind(self.evaluated(request), "buy")["status"], "conditional")

    def test_unknown_budget_blocks_buy_instead_of_using_a_stale_candidate(self):
        request = self.snapshot()
        request["fields"]["gold"] = {"status": "stale", "value": None}
        result = self.evaluated(request)
        buy = by_kind(result, "buy")
        self.assertEqual(buy["status"], "unknown")
        self.assertIn("gold", [issue["field"] for issue in buy["issues"]])

    def test_nonessential_and_legal_are_both_required_for_sell(self):
        request = self.snapshot()
        request["fields"]["candidate_nonessential"]["value"] = True
        result = self.evaluated(request)
        self.assertEqual(by_kind(result, "sell")["status"], "conditional")
        request["fields"]["board_legal_after_sale"]["value"] = False
        self.assertEqual(by_kind(self.evaluated(request), "sell")["status"], "hold")

    def test_equipment_does_not_become_a_strength_claim(self):
        request = self.snapshot()
        for name in ("recipe_known", "components_match_recipe", "carrier_known", "carrier_has_slot",
                     "carrier_suits_recipe", "carrier_need_known"):
            request["fields"][name]["value"] = True
        row = by_kind(self.evaluated(request), "equipment")
        self.assertEqual(row["status"], "conditional")
        self.assertNotIn("最优", row["rationale_zh"])

    def test_equipment_needs_explicit_component_match_and_carrier_suitability(self):
        request = self.snapshot()
        for name in ("recipe_known", "components_match_recipe", "carrier_known", "carrier_has_slot",
                     "carrier_suits_recipe", "carrier_need_known"):
            request["fields"][name]["value"] = True
        request["fields"]["components_match_recipe"]["value"] = False
        self.assertEqual(by_kind(self.evaluated(request), "equipment")["status"], "hold")
        request["fields"]["components_match_recipe"]["value"] = True
        request["fields"]["carrier_suits_recipe"]["value"] = False
        self.assertEqual(by_kind(self.evaluated(request), "equipment")["status"], "hold")

    def test_live_candidate_is_still_conditional_and_never_executable(self):
        request = self.snapshot()
        request["sample_kind"] = "live_candidate"
        result = self.evaluated(request)
        self.assertEqual(result["sample_kind"], "live_candidate")
        self.assertFalse(result["execution_eligible"])

    def test_unknown_fields_must_not_carry_a_hidden_value(self):
        request = self.snapshot()
        request["fields"]["gold"] = {"status": "unknown", "value": 31}
        result = assess_conditions(request)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("unknown_value_present", [issue["code"] for issue in result["issues"]])

    def test_exact_field_set_and_format_are_required(self):
        request = self.snapshot()
        request["fields"]["invented"] = {"status": "known", "value": True}
        self.assertEqual(assess_conditions(request)["status"], "blocked")
        request = self.snapshot()
        request["format_version"] = FORMAT_VERSION + "-other"
        self.assertEqual(assess_conditions(request)["status"], "blocked")

    def test_non_text_sample_kind_and_field_status_are_blocked_without_throwing(self):
        for value in ([], {}, None, 0, True, math.nan, math.inf):
            with self.subTest(sample_kind=repr(value)):
                request = self.snapshot()
                request["sample_kind"] = value
                result = assess_conditions(request)
                self.assertEqual(result["status"], "blocked")
                self.assertIn("sample_kind_invalid", [issue["code"] for issue in result["issues"]])
                self.assertIsNone(result["sample_kind"])
                json.dumps(result, ensure_ascii=False, allow_nan=False)
            with self.subTest(field_status=repr(value)):
                request = self.snapshot()
                request["fields"]["gold"]["status"] = value
                result = assess_conditions(request)
                self.assertEqual(result["status"], "blocked")
                self.assertIn("field_status_invalid", [issue["code"] for issue in result["issues"]])

    def test_input_and_result_are_json_safe_and_do_not_mutate_input(self):
        request = self.snapshot()
        before = deepcopy(request)
        result = self.evaluated(request)
        self.assertEqual(request, before)
        json.dumps(result, ensure_ascii=False, allow_nan=False)
