from copy import deepcopy
import json
import unittest

from jcc_team_planner import plan, plan_json
from jcc_team_planner.fixtures import copied_request


class PlannerTests(unittest.TestCase):
    @staticmethod
    def recipe_request(binding, inventory_entries, recipes):
        return {
            "format_version": "jcc-recipe-candidate-request/v1",
            "version": {"status": "known", "binding": deepcopy(binding),
                        "evidence": {"kind": "synthetic", "refs": ["synthetic:r"]}},
            "inventory": {"status": "known", "binding": deepcopy(binding),
                          "evidence": {"kind": "synthetic", "refs": ["synthetic:r"]},
                          "availability": "unequipped_components", "complete": True,
                          "entries": inventory_entries},
            "catalog": {"status": "known", "binding": deepcopy(binding),
                        "evidence": {"kind": "synthetic", "refs": ["synthetic:r"]},
                        "component_ids": sorted({component for row in recipes for component in row["components"]}),
                        "recipes": recipes},
        }

    def evaluated(self, request=None):
        result = plan(request or copied_request())
        self.assertEqual("evaluated", result["status"], result["issues"])
        self.assertFalse(result["execution_eligible"])
        self.assertIsNone(result["automatic_action"])
        self.assertEqual("conditional_lineup_candidates_only", result["strategy_recommendation"])
        return result

    def unknown(self, request, code):
        result = plan(request)
        self.assertEqual("unknown", result["status"])
        self.assertIsNone(result["lineup_candidates"])
        self.assertIn(code, {row["code"] for row in result["issues"]})

    def test_enumerates_explicit_lineups_and_trait_deltas(self):
        result = self.evaluated()
        self.assertLessEqual(len(result["lineup_candidates"]), 3)
        self.assertEqual("current_baseline", result["lineup_candidates"][0]["candidate_role"])
        best = next(row for row in result["lineup_candidates"] if "bench-c" in row["lineup_instance_ids"])
        self.assertIn("own-a", best["preserved_core_instance_ids"])
        leaf = best["traits"]["trait_leaf"]
        self.assertEqual((2, 2), (leaf["count"], leaf["active_threshold"]))
        self.assertTrue(any(row["trait_id"] == "trait_leaf" for row in best["trait_changes"]))

    def test_purchase_is_affordable_and_used_or_absent(self):
        result = self.evaluated()
        for candidate in result["lineup_candidates"]:
            if candidate["purchase"]:
                self.assertGreaterEqual(candidate["gold_after_purchase"], 0)
                self.assertIn(candidate["purchase_candidate_instance_id"], candidate["lineup_instance_ids"])

    def test_no_fake_transition_cost_when_required_unit_not_in_shop(self):
        result = self.evaluated()
        target = result["transition_candidates"][0]
        self.assertEqual(["unit_shot"], target["missing_unit_ids"])
        self.assertEqual(2, target["current_shop_missing_purchase_cost_if_all_available"])
        request = copied_request()
        request["shop"] = []
        target = self.evaluated(request)["transition_candidates"][0]
        self.assertIsNone(target["current_shop_missing_purchase_cost_if_all_available"])
        self.assertFalse(target["immediate_combat_power_evaluated"])

    def test_unknown_for_incomplete_or_conflicting_state(self):
        request = copied_request()
        request["board"].append(deepcopy(request["bench"][0]))
        self.unknown(request, "instance_duplicate")
        request = copied_request()
        request["phase"] = "battle"
        self.unknown(request, "phase_ineligible")
        request = copied_request()
        request["shop"][0]["cost"] = 99
        self.unknown(request, "shop_invalid")

    def test_cores_cannot_be_silently_dropped(self):
        request = copied_request()
        request["level_capacity"] = 2
        request["bench"][0]["is_core"] = True
        request["board"][1]["is_core"] = True
        self.unknown(request, "core_over_capacity")

    def test_equipment_requires_actual_reused_recipe_result_and_matching_binding(self):
        request = copied_request()
        recipe_binding = deepcopy(request["binding"])
        request["equipment"] = {
            "recipe_request": self.recipe_request(recipe_binding,
                [{"component_id": "piece_x", "count": 1}, {"component_id": "piece_y", "count": 1}],
                [{"item_id": "item_xy", "components": ["piece_x", "piece_y"]}]),
            "carrier_rules": [{"item_id": "item_xy", "carrier_instance_ids": ["own-a"]}],
        }
        result = self.evaluated(request)
        self.assertEqual("item_xy", result["equipment_candidates"][0]["item_id"])
        self.assertIn("own-a", result["equipment_candidates"][0]["eligible_carriers_by_lineup"][0]["eligible_carrier_instance_ids"])
        request["equipment"]["recipe_request"]["version"]["binding"]["patch"] = "other"
        self.unknown(request, "recipe_analysis_unknown")

    def test_json_rejects_duplicate_and_nonfinite_values(self):
        self.assertEqual("unknown", plan_json('{"format_version":1,"format_version":2}')["status"])
        self.assertEqual("unknown", plan_json('{"format_version":NaN}')["status"])

    def test_duplicate_counting_capacity_merge_and_shop_id_are_explicit(self):
        request = copied_request()
        request["bench"].append({"instance_id": "other-guard", "unit_id": "unit_guard", "star": 1,
                                 "is_core": False, "equipped_item_ids": []})
        request["trait_counting_rule"]["same_unit_id_counts_separately"] = False
        self.unknown(request, "duplicate_trait_counting_unknown")
        request = copied_request()
        request["bench_capacity"] = 0
        self.unknown(request, "bench_over_capacity")
        request = copied_request()
        request["board"].append({"instance_id": "third-leaf", "unit_id": "unit_leaf_shot", "star": 1,
                                 "is_core": False, "equipped_item_ids": []})
        request["bench"].append({"instance_id": "second-leaf", "unit_id": "unit_leaf_shot", "star": 1,
                                 "is_core": False, "equipped_item_ids": []})
        self.unknown(request, "unmodeled_merge_present")
        request = copied_request()
        request["board"][0]["instance_id"] = "shop-candidate:2:unit_shot:1"
        result = self.evaluated(request)
        buys = [row for row in result["lineup_candidates"] if row["purchase"]]
        self.assertTrue(all(row["purchase_candidate_instance_id"] != "shop-candidate:2:unit_shot:1" for row in buys))

    def test_invalid_types_and_omitted_equipment_are_not_silently_evaluated(self):
        request = copied_request()
        request["evidence"]["kind"] = []
        self.unknown(request, "evidence_invalid")
        request = copied_request()
        request["transition_targets"][0]["target_traits"][0]["trait_id"] = []
        self.unknown(request, "transition_trait_invalid")
        request = copied_request()
        request["transition_targets"][0]["target_traits"][0]["threshold"] = True
        self.unknown(request, "transition_trait_invalid")
        request = copied_request()
        request["equipment"] = None
        self.unknown(request, "shape_invalid")
        result = self.evaluated()
        self.assertEqual("not_requested", result["equipment_evaluation"])
        self.assertIsNone(result["equipment_candidates"])

    def test_duplicate_purchase_counting_transition_cores_and_empty_carrier_rules(self):
        request = copied_request()
        request["shop"][0] = {"slot": 2, "unit_id": "unit_leaf_guard", "cost": 1}
        request["trait_counting_rule"]["same_unit_id_counts_separately"] = False
        result = self.evaluated(request)
        self.assertIn("duplicate_counting_not_allowed", {row["reason"] for row in result["excluded_shop_purchases"]})
        request = copied_request()
        request["merge_rule"]["automatic_merge_on_purchase"] = False
        request["bench"].append({"instance_id": "same-shot", "unit_id": "unit_shot", "star": 1,
                                 "is_core": False, "equipped_item_ids": []})
        request["bench"].append({"instance_id": "same-shot-2", "unit_id": "unit_shot", "star": 1,
                                 "is_core": False, "equipped_item_ids": []})
        result = self.evaluated(request)
        self.assertNotIn("would_require_unmodeled_merge", {row["reason"] for row in result["excluded_shop_purchases"]})
        request = copied_request()
        request["level_capacity"] = 2
        request["board"][1]["is_core"] = True
        request["transition_targets"][0]["required_unit_ids"] = ["unit_leaf_guard", "unit_shot"]
        result = self.evaluated(request)
        self.assertFalse(result["transition_candidates"][0]["population_feasible_with_cores"])
        request = copied_request()
        request["equipment"] = {"recipe_request": self.recipe_request(request["binding"],
                                [{"component_id": "piece_x", "count": 1}, {"component_id": "piece_y", "count": 1}],
                                [{"item_id": "item_xy", "components": ["piece_x", "piece_y"]}]),
                                "carrier_rules": []}
        result = self.evaluated(request)
        self.assertEqual("partial_unknown", result["equipment_evaluation"])
        self.assertEqual(["item_xy"], result["equipment_unknown_item_ids"])

    def test_equipment_is_partial_unknown_when_a_craftable_item_has_no_carrier_rule(self):
        request = copied_request()
        request["board"][0]["equipped_item_ids"] = ["full-a", "full-b", "full-c"]
        request["equipment"] = {"recipe_request": self.recipe_request(request["binding"],
                                [{"component_id": "piece_x", "count": 2}, {"component_id": "piece_y", "count": 1},
                                 {"component_id": "piece_z", "count": 1}],
                                [{"item_id": "item_xy", "components": ["piece_x", "piece_y"]},
                                 {"item_id": "item_xz", "components": ["piece_x", "piece_z"]}]),
                                "carrier_rules": [{"item_id": "item_xy", "carrier_instance_ids": ["own-a"]}]}
        result = self.evaluated(request)
        self.assertEqual("partial_unknown", result["equipment_evaluation"])
        self.assertEqual(["item_xz"], result["equipment_unknown_item_ids"])
        self.assertEqual([], result["equipment_candidates"])

    def test_baseline_search_metadata_and_removed_equipment_are_visible(self):
        request = copied_request()
        request["board"][1]["equipped_item_ids"] = ["fiction-item-a", "fiction-item-b", "fiction-item-c"]
        result = self.evaluated(request)
        self.assertEqual("complete", result["lineup_search"]["status"])
        self.assertGreaterEqual(result["lineup_search"]["enumerated_candidate_count"], len(result["lineup_candidates"]))
        self.assertTrue(any(row["changes"]["removed_equipped_item_count"] == 3 for row in result["lineup_candidates"][1:]))

    def test_large_pool_switches_to_explicit_single_swap_local_search(self):
        request = copied_request()
        for index in range(8):
            request["board"].append({"instance_id": f"board-extra-{index}", "unit_id": "unit_guard", "star": 2,
                                     "is_core": False, "equipped_item_ids": []})
        for index in range(10):
            request["bench"].append({"instance_id": f"extra-{index}", "unit_id": "unit_leaf_shot", "star": 2,
                                     "is_core": False, "equipped_item_ids": []})
        request["bench_capacity"] = 12
        request["level_capacity"] = 10
        request["merge_rule"]["automatic_merge_on_purchase"] = False
        request["bench"][0]["is_core"] = True
        result = self.evaluated(request)
        self.assertEqual("local_single_swap_only", result["lineup_search"]["status"])
        self.assertEqual("current_board_plus_one_bench_or_one_shop_purchase_single_change",
                         result["lineup_search"]["search_scope"])
        self.assertEqual("current_baseline", result["lineup_candidates"][0]["candidate_role"])
        self.assertTrue(all(row["candidate_role"] in {"current_baseline", "single_swap_local_only"}
                            for row in result["lineup_candidates"]))
        self.assertTrue(all("bench-c" in row["lineup_instance_ids"] for row in result["lineup_candidates"][1:]))

    def test_local_search_reports_when_one_change_cannot_preserve_all_bench_cores(self):
        request = copied_request()
        for index in range(8):
            request["board"].append({"instance_id": f"board-extra-{index}", "unit_id": "unit_guard", "star": 2,
                                     "is_core": False, "equipped_item_ids": []})
        for index in range(9):
            request["bench"].append({"instance_id": f"extra-{index}", "unit_id": "unit_leaf_shot", "star": 2,
                                     "is_core": False, "equipped_item_ids": []})
        request["bench"][0]["is_core"] = True
        request["bench"][1]["is_core"] = True
        request["bench_capacity"] = 12
        request["level_capacity"] = 10
        request["merge_rule"]["automatic_merge_on_purchase"] = False
        result = self.evaluated(request)
        self.assertEqual(["current_baseline"], [row["candidate_role"] for row in result["lineup_candidates"]])
        self.assertEqual("single_change_cannot_preserve_all_known_cores",
                         result["lineup_search"]["core_preservation_scope_status"])

    def test_local_search_with_an_open_slot_also_enumerates_one_for_one_swaps(self):
        request = copied_request()
        for index in range(5):
            request["board"].append({"instance_id": f"board-extra-{index}", "unit_id": "unit_guard", "star": 2,
                                     "is_core": False, "equipped_item_ids": []})
        for index in range(8):
            request["bench"].append({"instance_id": f"extra-{index}", "unit_id": "unit_leaf_shot", "star": 2,
                                     "is_core": False, "equipped_item_ids": []})
        request["bench_capacity"] = 10
        request["level_capacity"] = 8
        request["merge_rule"]["automatic_merge_on_purchase"] = False
        result = self.evaluated(request)
        self.assertEqual("local_single_swap_only", result["lineup_search"]["status"])
        # 9 bench candidates plus one affordable shop candidate, each with an
        # add-in alternative and 6 non-core board replacements: 10 * (1 + 6).
        self.assertEqual(70, result["lineup_search"]["enumerated_candidate_count"])
        self.assertTrue(result["lineup_search"]["scope_exhaustive"])

    def test_does_not_mutate_input(self):
        request = copied_request()
        before = json.loads(json.dumps(request))
        result = self.evaluated(request)
        result["lineup_candidates"][0]["lineup_instance_ids"].append("changed")
        self.assertEqual(before, request)


if __name__ == "__main__":
    unittest.main()
