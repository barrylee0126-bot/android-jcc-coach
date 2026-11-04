from copy import deepcopy
import json
import unittest

from jcc_equipment_coach import analyze_json, analyze_request
from jcc_equipment_coach.core import MAX_CONFLICT_CANDIDATES
from jcc_equipment_coach.synthetic import synthetic_request


def candidate(result, item_id):
    return next(row for row in result["candidates"] if row["item_id"] == item_id)


class EquipmentChoiceTests(unittest.TestCase):
    def evaluated(self, request):
        result = analyze_request(request)
        self.assertEqual(result["status"], "evaluated", result["issues"])
        self.assertEqual(result["issues"], [])
        self.assertFalse(result["strategy_recommendation"])
        self.assertFalse(result["execution_eligible"])
        return result

    def unknown(self, request, code):
        result = analyze_request(request)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["candidates"])
        self.assertIn(code, {row["code"] for row in result["issues"]})
        return result

    def test_shared_components_are_shown_as_a_non_simultaneous_choice(self):
        result = self.evaluated(synthetic_request())
        self.assertEqual(result["material_conflict_scope"], "evaluated_distinct_candidate_pairs_only")
        self.assertEqual(result["material_conflicts"], [{
            "candidate_item_ids": ["fiction_xx", "fiction_xy"], "jointly_craftable": False,
            "shortages": [{"component_id": "piece_x", "required_count": 3,
                           "available_count": 2, "shortage_count": 1}],
        }])
        info = candidate(result, "fiction_xx")["reservation_considerations"][0]
        self.assertTrue(info["uses_declared_reserved_components"])
        self.assertTrue(info["became_unreservable"])
        self.assertFalse(info["worsened_existing_shortage"])
        self.assertEqual(info["shortages_after_crafting"], [{"component_id": "piece_x", "plan_required_count": 1,
                                                              "remaining_count_after_crafting": 0, "shortage_count": 1}])
        self.assertTrue(candidate(result, "fiction_xy")["reservation_considerations"][0]["became_unreservable"])

    def test_extra_component_removes_the_conflict_without_ranking_a_choice(self):
        request = synthetic_request()
        request["recipe_request"]["inventory"]["entries"][0]["count"] = 3
        request["recipe_request"]["inventory"]["entries"][1]["count"] = 2
        result = self.evaluated(request)
        pairs = {tuple(row["candidate_item_ids"]) for row in result["material_conflicts"]}
        self.assertNotIn(("fiction_xx", "fiction_xy"), pairs)
        self.assertFalse(candidate(result, "fiction_xy")["reservation_considerations"][0]["became_unreservable"])
        self.assertEqual(result["candidates"][0]["item_id"], "fiction_xx")

    def test_carrier_slots_are_only_declarations_not_strength_ranking(self):
        result = self.evaluated(synthetic_request())
        xx = candidate(result, "fiction_xx")["carrier_evaluation"]
        self.assertEqual(xx["status"], "declared_available")
        self.assertEqual(xx["available_carriers"], [{"instance_id": "unit_a", "unit_id": "fiction_front", "slots_free": 3}])
        self.assertEqual(xx["full_declared_carriers"], [{"instance_id": "unit_full", "unit_id": "fiction_full", "slots_free": 0}])
        self.assertNotIn("rank", xx)
        self.assertNotIn("score", xx)

    def test_missing_rule_stays_unknown_when_catalog_is_not_complete(self):
        request = synthetic_request()
        request["recipe_request"]["inventory"]["entries"] = [
            {"component_id": "piece_y", "count": 2},
        ]
        result = self.evaluated(request)
        yy = candidate(result, "fiction_yy")["carrier_evaluation"]
        self.assertEqual(yy["status"], "unknown_incomplete_catalog")
        self.assertIsNone(yy["available_carriers"])

    def test_complete_catalog_still_does_not_turn_a_missing_rule_into_no_carrier(self):
        request = synthetic_request()
        request["carrier_fit"]["complete_for_catalog"] = True
        request["recipe_request"]["inventory"]["entries"] = [
            {"component_id": "piece_y", "count": 2},
        ]
        result = self.evaluated(request)
        self.assertEqual(candidate(result, "fiction_yy")["carrier_evaluation"]["status"],
                         "unknown_missing_declared_rule")

    def test_unknown_fit_and_reservation_are_not_empty_fact_claims(self):
        request = synthetic_request()
        request["carrier_fit"] = {"status": "unknown", "reason_zh": "同帧棋子装备栏尚未完整识别。"}
        request["reservations"] = {"status": "unknown", "reason_zh": "转型目标仍未确认。"}
        result = self.evaluated(request)
        self.assertEqual(candidate(result, "fiction_xx")["carrier_evaluation"]["status"], "unknown")
        self.assertIsNone(candidate(result, "fiction_xx")["reservation_considerations"])
        self.assertEqual(result["evidence"]["carrier_fit"]["status"], "unknown")

    def test_incomplete_inventory_fails_through_the_reused_recipe_contract(self):
        request = synthetic_request()
        request["recipe_request"]["inventory"]["complete"] = False
        self.unknown(request, "recipe_candidates_unknown")

    def test_carrier_state_version_mismatch_fails_closed(self):
        request = synthetic_request()
        request["carrier_state"]["binding"]["patch"] = "fiction_other_patch"
        self.unknown(request, "version_mismatch")

    def test_rule_must_reference_a_visible_instance(self):
        request = synthetic_request()
        request["carrier_fit"]["rules"][0]["carrier_instance_ids"] = ["absent_unit"]
        self.unknown(request, "carrier_rule_invalid")

    def test_reservation_must_use_known_component_and_explicit_condition(self):
        request = synthetic_request()
        request["reservations"]["plans"][0]["required_components"][0]["component_id"] = "not_in_catalog"
        self.unknown(request, "reservation_component_invalid")
        request = synthetic_request()
        request["reservations"]["plans"][0]["condition_zh"] = ""
        self.unknown(request, "reservation_plan_invalid")

    def test_already_unmet_reservation_is_distinct_from_crafting_conflict(self):
        request = synthetic_request()
        request["reservations"]["plans"][0]["required_components"][0]["count"] = 3
        result = self.evaluated(request)
        self.assertEqual(result["reservation_plans"][0]["status"], "already_unmet")
        info = candidate(result, "fiction_xx")["reservation_considerations"][0]
        self.assertTrue(info["plan_already_unmet"])
        self.assertTrue(info["uses_declared_reserved_components"])
        self.assertTrue(info["worsened_existing_shortage"])
        self.assertEqual(info["shortages_after_crafting"], [{"component_id": "piece_x", "plan_required_count": 3,
                                                              "remaining_count_after_crafting": 0, "shortage_count": 3}])

    def test_conflict_work_is_bounded_without_saying_candidates_are_ranked(self):
        request = synthetic_request()
        rows = request["recipe_request"]["catalog"]["recipes"]
        # Distinct item IDs all share the same valid component pair.
        for index in range(MAX_CONFLICT_CANDIDATES):
            rows.append({"item_id": f"fiction_more_{index}", "components": ["piece_x", "piece_x"]})
        # Recipe candidate rejects duplicate pairs, so use enough new components instead.
        request = synthetic_request()
        ids = [f"piece_more_{index}" for index in range(MAX_CONFLICT_CANDIDATES + 1)]
        request["recipe_request"]["catalog"]["component_ids"].extend(ids)
        request["recipe_request"]["inventory"]["entries"].extend({"component_id": value, "count": 2} for value in ids)
        request["recipe_request"]["catalog"]["recipes"].extend(
            {"item_id": f"fiction_more_{index}", "components": [value, value]}
            for index, value in enumerate(ids)
        )
        result = self.evaluated(request)
        self.assertGreater(len(result["candidates"]), MAX_CONFLICT_CANDIDATES)
        self.assertIsNone(result["material_conflicts"])
        self.assertEqual(result["material_conflict_scope"], "not_evaluated_candidate_limit")

    def test_json_duplicate_key_and_input_output_are_detached(self):
        self.assertEqual(analyze_json('{"format_version":"x","format_version":"y"}')["status"], "unknown")
        request = synthetic_request()
        before = deepcopy(request)
        result = self.evaluated(request)
        result["candidates"][0]["carrier_evaluation"]["available_carriers"].clear()
        result["binding"]["patch"] = "changed"
        self.assertEqual(request, before)
        # A normal JSON round trip is accepted and preserves the synthetic boundary.
        self.assertEqual(analyze_json(json.dumps(request))["data_kind"], "synthetic")

    def test_nested_unhashable_refs_and_carrier_ids_are_controlled_unknowns(self):
        request = synthetic_request()
        request["carrier_state"]["evidence"]["refs"] = [[]]
        self.unknown(request, "evidence_invalid")
        self.assertEqual(analyze_json(json.dumps(request))["status"], "unknown")
        request = synthetic_request()
        request["carrier_fit"]["rules"][0]["carrier_instance_ids"] = [{}]
        self.unknown(request, "carrier_rule_invalid")
        self.assertEqual(analyze_json(json.dumps(request))["status"], "unknown")

    def test_same_item_multiple_crafts_and_three_way_plans_are_out_of_scope(self):
        result = self.evaluated(synthetic_request())
        self.assertEqual(result["material_conflict_scope"], "evaluated_distinct_candidate_pairs_only")
        self.assertNotIn("same_item", json.dumps(result["material_conflicts"], ensure_ascii=False))
        self.assertEqual(result["frame_binding_verification"], "not_verified_by_module")
        self.assertEqual(result["freshness_verification"], "not_verified_by_module")


if __name__ == "__main__":
    unittest.main()
