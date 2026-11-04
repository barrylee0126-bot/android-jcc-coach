import copy
import json
import unittest

from jcc_transition_coach import REQUEST_FORMAT, compare, compare_json
from jcc_transition_coach.synthetic import copied_request


class TransitionCoachTests(unittest.TestCase):
    def evaluated(self, wrapper=None):
        wrapper = copied_request() if wrapper is None else wrapper
        result = compare(wrapper["team_snapshot"], wrapper["target_rule_evidence"])
        self.assertEqual(result["status"], "evaluated", result)
        return result

    def unknown(self, wrapper, code):
        result = compare(wrapper["team_snapshot"], wrapper["target_rule_evidence"])
        self.assertEqual(result["status"], "unknown", result)
        self.assertIn(code, [row["code"] for row in result["issues"]], result)

    def test_visible_missing_unit_exposes_traits_cost_swap_and_item_block(self):
        item = self.evaluated()["transition_candidates"][0]
        self.assertEqual(item["missing_unit_ids"], ["unit_shot"])
        self.assertEqual(item["visible_shop_matches_for_missing"], [{"slot": 2, "unit_id": "unit_shot", "cost": 2}])
        self.assertEqual(item["known_current_shop_purchase_cost_for_missing_units_only"], 2)
        self.assertFalse(item["known_current_shop_purchase_cost_is_total_transition_cost"])
        self.assertEqual(item["gold_after_current_shop_missing_unit_purchases_if_all_visible"], 2)
        self.assertEqual(item["current_board_to_bench_instance_ids"], ["own-b"])
        self.assertEqual(item["bench_to_board_retained_instance_ids"], ["bench-c"])
        self.assertEqual(item["equipped_board_instance_ids_with_unknown_item_destination"], ["own-b"])
        self.assertIn("equipped_unit_leaves_board_carrier_and_item_destination_unknown", item["conditions_or_blocks"])
        self.assertIsNone(item["immediate_transition_feasible"])
        self.assertFalse(bool(item["immediate_transition_feasible"]))
        self.assertTrue(item["final_bench_capacity_sufficient_if_declared_moves_complete"])
        self.assertEqual(item["operation_sequence_feasibility"], "unknown_not_modeled")
        self.assertIn({"trait_id": "trait_leaf", "from_active_threshold": None, "to_active_threshold": 2,
                       "change": "gained_or_upgraded"}, item["declared_base_trait_changes_from_current_board"])
        self.assertEqual(item["actual_trait_changes_from_current_board"], "unknown_unmodeled_emblems_and_modifiers")

    def test_shop_absence_keeps_immediate_cost_unknown(self):
        wrapper = copied_request()
        wrapper["team_snapshot"]["shop"] = []
        item = self.evaluated(wrapper)["transition_candidates"][0]
        self.assertEqual(item["missing_unit_ids_not_in_current_shop"], ["unit_shot"])
        self.assertIsNone(item["known_current_shop_purchase_cost_for_missing_units_only"])
        self.assertEqual(item["catalog_cost_for_not_visible_missing"][0]["catalog_cost"], 2)

    def test_core_conflict_blocks_even_when_target_otherwise_available(self):
        wrapper = copied_request()
        target = wrapper["team_snapshot"]["transition_targets"][0]
        target["required_unit_ids"] = ["unit_leaf_shot", "unit_shot"]
        target["target_traits"] = [{"trait_id": "trait_shot", "threshold": 2}]
        item = self.evaluated(wrapper)["transition_candidates"][0]
        self.assertEqual(item["core_instance_ids_not_in_target"], ["own-a"])
        self.assertIn("core_not_in_target", item["conditions_or_blocks"])

    def test_open_slots_and_overpopulation_are_both_explicit_blocks(self):
        wrapper = copied_request()
        target = wrapper["team_snapshot"]["transition_targets"][0]
        target["required_unit_ids"] = ["unit_leaf_guard", "unit_leaf_shot"]
        target["target_traits"] = [{"trait_id": "trait_leaf", "threshold": 2}]
        item = self.evaluated(wrapper)["transition_candidates"][0]
        self.assertEqual(item["target_open_slots"], 1)
        self.assertIn("target_has_open_slots_and_does_not_describe_fillers", item["conditions_or_blocks"])
        wrapper = copied_request()
        wrapper["team_snapshot"]["level_capacity"] = 2
        item = self.evaluated(wrapper)["transition_candidates"][0]
        self.assertIn("target_over_population", item["conditions_or_blocks"])

    def test_missing_route_evidence_and_binding_mismatch_fail_closed(self):
        wrapper = copied_request()
        wrapper["target_rule_evidence"] = []
        self.unknown(wrapper, "target_evidence_invalid")
        wrapper = copied_request()
        wrapper["target_rule_evidence"][0]["binding"]["patch"] = "wrong"
        self.unknown(wrapper, "target_evidence_invalid")

    def test_shared_team_snapshot_unknown_stays_unknown(self):
        wrapper = copied_request()
        wrapper["team_snapshot"]["phase"] = "combat"
        self.unknown(wrapper, "team_snapshot_unknown")

    def test_empty_target_set_is_not_a_successful_comparison(self):
        wrapper = copied_request()
        wrapper["team_snapshot"]["transition_targets"] = []
        wrapper["target_rule_evidence"] = []
        self.unknown(wrapper, "targets_missing")

    def test_strict_json_and_input_no_mutation(self):
        wrapper = copied_request()
        before = copy.deepcopy(wrapper)
        self.evaluated(wrapper)
        self.assertEqual(wrapper, before)
        self.assertEqual(compare_json(json.dumps(wrapper))["status"], "evaluated")
        duplicate = '{"format_version":"%s","format_version":"bad"}' % REQUEST_FORMAT
        self.assertEqual(compare_json(duplicate)["status"], "unknown")
        self.assertEqual(compare_json('{"x":NaN}')["status"], "unknown")

    def test_snapshot_and_route_evidence_are_separate_caller_declarations(self):
        result = self.evaluated()
        self.assertEqual(result["data_kind_scope"], "snapshot_only; route evidence separately caller_declared")
        self.assertEqual(result["route_evidence_verification"], "caller_declared_only")
        self.assertFalse(result["strategy_recommendation"])
        self.assertEqual(result["comparison_scope"], "conditional_target_transition_comparison_only")
        route = result["transition_candidates"][0]["target_rule_evidence"]
        self.assertEqual(route["verification"], "caller_declared_only")

    def test_unknown_operation_value_cannot_be_consumed_as_true(self):
        item = self.evaluated()["transition_candidates"][0]
        self.assertIsNone(item["immediate_transition_feasible"])
        self.assertFalse(bool(item["immediate_transition_feasible"]))
        self.assertEqual(item["operation_sequence_feasibility"], "unknown_not_modeled")


if __name__ == "__main__":
    unittest.main()
