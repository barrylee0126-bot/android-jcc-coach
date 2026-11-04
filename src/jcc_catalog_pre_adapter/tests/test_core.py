from __future__ import annotations

from copy import deepcopy
import unittest

from jcc_catalog_pre_adapter import core


def request():
    return {
        "format_version": core.REQUEST_FORMAT,
        "source": {"status": "unknown", "snapshot_namespace": "sha256:" + "a" * 64},
        "traits": [{"source_id": "中文trait", "display_name": "Trait", "thresholds": [2, 4]}],
        "heroes": [{"source_id": "中文hero-slug", "display_name": "Hero", "trait_source_ids": ["中文trait"]}],
        "items": [
            {"source_id": "item-a", "display_name": "Part A", "component_source_ids": []},
            {"source_id": "item-b", "display_name": "Part B", "component_source_ids": []},
            {"source_id": "item-c", "display_name": "Made", "component_source_ids": ["item-a", "item-b"]},
        ],
        "conflicts": [],
    }


class PreAdapterTests(unittest.TestCase):
    def test_non_ascii_raw_ids_get_deterministic_ascii_keys_without_guessing(self):
        result = core.analyze_request(request())
        self.assertEqual(result["status"], "candidate")
        self.assertTrue(result["private_key_map"]["heroes"]["中文hero-slug"].startswith("src-hero-"))
        self.assertEqual(result["coverage"]["heroes"]["candidate_count"], 1)
        self.assertFalse(result["decision_eligible"])
        self.assertIsNone(result["action_advice"])

    def test_unknown_source_is_required_and_never_promoted(self):
        data = request()
        data["source"] = {"status": "known", "snapshot_namespace": "sha256:" + "a" * 64}
        result = core.analyze_request(data)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("source_must_be_unknown", [issue["code"] for issue in result["issues"]])

    def test_invalid_text_and_duplicate_source_ids_are_blocked_and_counted(self):
        data = request()
        data["traits"].append({"source_id": "中文trait", "display_name": "Trait Two", "thresholds": [3]})
        data["heroes"].append({"source_id": "bad\u0000id", "display_name": "Bad", "trait_source_ids": []})
        result = core.analyze_request(data)
        traits = result["records"]["traits"]
        self.assertTrue(all("duplicate_source_id" in row["reasons"] for row in traits))
        self.assertEqual(result["coverage"]["traits"]["input_total"], 2)
        self.assertEqual(result["coverage"]["traits"]["candidate_count"] + result["coverage"]["traits"]["blocked_count"], 2)
        self.assertIn("invalid_record", result["records"]["heroes"][-1]["reasons"])
        self.assertNotIn("bad\u0000id", result["private_key_map"]["heroes"])

    def test_missing_invalid_thresholds_and_blocked_references_are_preserved(self):
        data = request()
        data["traits"] = [
            {"source_id": "trait-empty", "display_name": "Empty", "thresholds": []},
            {"source_id": "trait-repeat", "display_name": "Repeat", "thresholds": [1, 1]},
        ]
        data["heroes"] = [{"source_id": "hero", "display_name": "Hero", "trait_source_ids": ["trait-empty", "missing"]}]
        result = core.analyze_request(data)
        self.assertEqual(result["records"]["traits"][0]["reasons"], ["thresholds_missing"])
        self.assertEqual(result["records"]["traits"][1]["reasons"], ["thresholds_invalid"])
        self.assertEqual(set(result["records"]["heroes"][0]["reasons"]), {"reference_missing", "reference_blocked"})
        self.assertEqual(result["coverage"]["heroes"]["input_total"], 1)

    def test_conflicts_block_attached_records_but_are_retained(self):
        data = request()
        data["conflicts"] = [{"conflict_id": "c-1", "entity_kind": "trait", "source_id": "中文trait", "field": "effect"}]
        result = core.analyze_request(data)
        self.assertEqual(result["conflicts"][0]["state"], "retained_unresolved")
        self.assertIn("unresolved_conflict", result["records"]["traits"][0]["reasons"])
        self.assertIn("reference_blocked", result["records"]["heroes"][0]["reasons"])

    def test_missing_component_and_recipe_cycle_are_blocked(self):
        data = request()
        data["items"] = [
            {"source_id": "item-a", "display_name": "A", "component_source_ids": ["item-b"]},
            {"source_id": "item-b", "display_name": "B", "component_source_ids": ["item-a"]},
            {"source_id": "item-c", "display_name": "C", "component_source_ids": ["missing"]},
        ]
        result = core.analyze_request(data)
        self.assertTrue(all("recipe_cycle" in row["reasons"] for row in result["records"]["items"][:2]))
        self.assertIn("reference_missing", result["records"]["items"][2]["reasons"])

    def test_only_true_cycle_members_are_recipe_cycles_and_dependents_propagate(self):
        data = request()
        data["items"] = [
            {"source_id": "top", "display_name": "Top", "component_source_ids": ["mid"]},
            {"source_id": "mid", "display_name": "Mid", "component_source_ids": ["bad"]},
            {"source_id": "bad", "display_name": "Bad", "component_source_ids": ["missing"]},
            {"source_id": "c", "display_name": "C", "component_source_ids": ["a"]},
            {"source_id": "a", "display_name": "A", "component_source_ids": ["b"]},
            {"source_id": "b", "display_name": "B", "component_source_ids": ["a"]},
        ]
        result = core.analyze_request(data)
        rows = {row["source_id"]: row for row in result["records"]["items"]}
        self.assertIn("reference_missing", rows["bad"]["reasons"])
        self.assertIn("reference_blocked", rows["mid"]["reasons"])
        self.assertIn("reference_blocked", rows["top"]["reasons"])
        self.assertEqual(rows["a"]["primary_block_reason"], "recipe_cycle")
        self.assertEqual(rows["b"]["primary_block_reason"], "recipe_cycle")
        self.assertIn("reference_blocked", rows["c"]["reasons"])
        self.assertNotIn("recipe_cycle", rows["c"]["reasons"])
        reordered = request()
        reordered["items"] = list(reversed(data["items"]))
        again = core.analyze_request(reordered)
        states = {row["source_id"]: (row["state"], tuple(row["reasons"])) for row in result["records"]["items"]}
        self.assertEqual(states, {row["source_id"]: (row["state"], tuple(row["reasons"])) for row in again["records"]["items"]})

    def test_canonical_collision_is_defensively_blocked(self):
        data = request()
        original = core.canonical_key
        try:
            core.canonical_key = lambda kind, source_id, snapshot_namespace: "src-forced-collision"
            result = core.analyze_request(data)
        finally:
            core.canonical_key = original
        self.assertTrue(all("canonical_key_collision" in row["reasons"] for row in result["records"]["items"]))
        self.assertEqual(result["private_key_map"]["items"], {})

    def test_snapshot_namespace_scopes_keys_and_is_not_source_authentication(self):
        data = request()
        other = deepcopy(data)
        other["source"]["snapshot_namespace"] = "sha256:" + "b" * 64
        first = core.analyze_request(data)
        second = core.analyze_request(other)
        self.assertNotEqual(first["private_key_map"]["heroes"]["中文hero-slug"], second["private_key_map"]["heroes"]["中文hero-slug"])
        self.assertEqual(first["source_status"], "unknown")
        malformed = deepcopy(data)
        malformed["source"]["snapshot_namespace"] = "same-source"
        self.assertEqual(core.analyze_request(malformed)["status"], "unknown")

    def test_primary_reason_counts_are_mutually_exclusive_while_reasons_can_overlap(self):
        data = request()
        data["conflicts"] = [{"conflict_id": "c", "entity_kind": "trait", "source_id": "中文trait", "field": "effect"}]
        data["traits"][0]["thresholds"] = []
        result = core.analyze_request(data)
        trait = result["records"]["traits"][0]
        self.assertEqual(trait["primary_block_reason"], "thresholds_missing")
        self.assertEqual(set(trait["reasons"]), {"thresholds_missing", "unresolved_conflict"})
        coverage = result["coverage"]["traits"]
        self.assertEqual(sum(coverage["primary_block_reason_counts"].values()), coverage["blocked_count"])
        self.assertGreater(sum(coverage["reason_occurrence_counts"].values()), coverage["blocked_count"])

    def test_malformed_rows_and_conflicts_are_controlled_and_keep_valid_denominators(self):
        data = request()
        data["heroes"].extend([
            {"source_id": [], "display_name": "Bad ID", "trait_source_ids": None},
            {"source_id": "missing-field"},
        ])
        data["conflicts"] = [{"conflict_id": "valid-id", "entity_kind": {}, "source_id": "中文trait", "field": "effect"}]
        result = core.analyze_request(data)
        self.assertEqual(result["status"], "candidate")
        self.assertFalse(result["decision_eligible"])
        self.assertEqual(result["coverage"]["heroes"]["input_total"], 3)
        self.assertEqual(result["coverage"]["heroes"]["blocked_count"], 2)
        self.assertEqual(result["conflicts"][0]["reasons"], ["conflict_invalid"])

    def test_nested_reference_values_do_not_throw_or_enter_dependency_graph(self):
        data = request()
        data["heroes"][0]["trait_source_ids"] = [{}]
        data["items"][0]["component_source_ids"] = [[]]
        result = core.analyze_request(data)
        self.assertEqual(result["status"], "candidate")
        self.assertIn("reference_invalid", result["records"]["heroes"][0]["reasons"])
        self.assertIn("reference_invalid", result["records"]["items"][0]["reasons"])
        self.assertFalse(result["decision_eligible"])

    def test_invalid_conflict_id_with_determinable_target_blocks_target(self):
        data = request()
        data["conflicts"] = [{"entity_kind": "trait", "source_id": "中文trait", "field": "effect"}]
        result = core.analyze_request(data)
        self.assertEqual(result["conflicts"][0]["reasons"], ["conflict_invalid"])
        self.assertIn("unresolved_conflict", result["records"]["traits"][0]["reasons"])
        self.assertEqual(result["records"]["traits"][0]["state"], "blocked")
        self.assertEqual(result["private_key_map"]["traits"], {})

    def test_recognizable_invalid_conflict_id_participates_in_duplicate_blocking(self):
        data = request()
        data["conflicts"] = [
            {"conflict_id": "shared", "entity_kind": "trait", "source_id": "中文trait", "field": "effect"},
            {"conflict_id": "shared", "entity_kind": "hero", "source_id": "中文hero-slug"},
        ]
        result = core.analyze_request(data)
        first, second = result["conflicts"]
        self.assertEqual(first["state"], "blocked")
        self.assertIn("duplicate_conflict_id", first["reasons"])
        self.assertEqual(second["state"], "blocked")
        self.assertEqual(set(second["reasons"]), {"conflict_invalid", "duplicate_conflict_id"})
        self.assertIn("unresolved_conflict", result["records"]["traits"][0]["reasons"])
        self.assertIn("unresolved_conflict", result["records"]["heroes"][0]["reasons"])

    def test_item_component_limit_matches_snapshot_eight_component_contract(self):
        components = [
            {"source_id": f"part-{index}", "display_name": f"Part {index}", "component_source_ids": []}
            for index in range(9)
        ]
        data = request()
        data["items"] = components + [{"source_id": "made", "display_name": "Made", "component_source_ids": [f"part-{index}" for index in range(8)]}]
        eight = core.analyze_request(data)
        made = next(row for row in eight["records"]["items"] if row["source_id"] == "made")
        self.assertEqual(made["state"], "candidate")
        data["items"][-1]["component_source_ids"].append("part-8")
        nine = core.analyze_request(data)
        made = next(row for row in nine["records"]["items"] if row["source_id"] == "made")
        self.assertIn("component_references_too_many", made["reasons"])
        self.assertEqual(made["state"], "blocked")
        self.assertEqual(nine["coverage"]["items"]["input_total"], 10)

    def test_duplicate_conflict_ids_are_blocked_and_not_attached(self):
        data = request()
        data["conflicts"] = [
            {"conflict_id": "same", "entity_kind": "trait", "source_id": "中文trait", "field": "a"},
            {"conflict_id": "same", "entity_kind": "trait", "source_id": "中文trait", "field": "b"},
        ]
        result = core.analyze_request(data)
        self.assertTrue(all(row["state"] == "blocked" and row["reasons"] == ["duplicate_conflict_id"] for row in result["conflicts"]))
        self.assertIn("unresolved_conflict", result["records"]["traits"][0]["reasons"])
        self.assertEqual(result["records"]["traits"][0]["state"], "blocked")
        self.assertEqual(result["private_key_map"]["traits"], {})

    def test_incomplete_row_with_identifiable_duplicate_source_id_blocks_both(self):
        data = request()
        data["traits"].append({"source_id": "中文trait"})
        result = core.analyze_request(data)
        rows = result["records"]["traits"]
        self.assertTrue(all("duplicate_source_id" in row["reasons"] for row in rows))
        self.assertEqual(result["coverage"]["traits"]["input_total"], 2)
        self.assertEqual(result["private_key_map"]["traits"], {})

    def test_1024_item_chain_is_iterative_and_not_a_cycle(self):
        data = request()
        data["items"] = [
            {"source_id": f"item-{index}", "display_name": f"Item {index}",
             "component_source_ids": [] if index == 1023 else [f"item-{index + 1}"]}
            for index in range(1024)
        ]
        result = core.analyze_request(data)
        self.assertEqual(result["coverage"]["items"], {
            "input_total": 1024, "candidate_count": 1024, "blocked_count": 0,
            "primary_block_reason_counts": {}, "reason_occurrence_counts": {},
        })

    def test_cycle_remains_visible_when_one_member_also_has_missing_dependency(self):
        data = request()
        data["items"] = [
            {"source_id": "a", "display_name": "A", "component_source_ids": ["b", "missing"]},
            {"source_id": "b", "display_name": "B", "component_source_ids": ["a"]},
        ]
        result = core.analyze_request(data)
        rows = {row["source_id"]: row for row in result["records"]["items"]}
        self.assertIn("recipe_cycle", rows["a"]["reasons"])
        self.assertIn("recipe_cycle", rows["b"]["reasons"])
        self.assertIn("reference_missing", rows["a"]["reasons"])

    def test_overlapping_missing_and_blocked_reasons_are_order_independent(self):
        data = request()
        data["items"] = [
            {"source_id": "a", "display_name": "A", "component_source_ids": ["missing", "b"]},
            {"source_id": "b", "display_name": "B", "component_source_ids": ["missing"]},
        ]
        first = core.analyze_request(data)
        reordered = request()
        reordered["items"] = list(reversed(data["items"]))
        second = core.analyze_request(reordered)
        one = {row["source_id"]: tuple(row["reasons"]) for row in first["records"]["items"]}
        two = {row["source_id"]: tuple(row["reasons"]) for row in second["records"]["items"]}
        self.assertEqual(one, two)
        self.assertEqual(set(one["a"]), {"reference_missing", "reference_blocked"})

    def test_empty_hero_trait_refs_are_blocked_and_not_exported_as_consumable_map(self):
        data = request()
        data["heroes"] = [{"source_id": "empty-hero", "display_name": "Hero", "trait_source_ids": []}]
        result = core.analyze_request(data)
        hero = result["records"]["heroes"][0]
        self.assertEqual(hero["reasons"], ["trait_references_empty"])
        self.assertEqual(hero["state"], "blocked")
        self.assertNotIn("empty-hero", result["private_key_map"]["heroes"])

    def test_duplicate_hero_trait_refs_are_blocked_for_downstream_contract(self):
        data = request()
        data["heroes"][0]["trait_source_ids"] = ["中文trait", "中文trait"]
        result = core.analyze_request(data)
        hero = result["records"]["heroes"][0]
        self.assertIn("trait_references_duplicate", hero["reasons"])
        self.assertEqual(hero["state"], "blocked")
        self.assertEqual(result["private_key_map"]["heroes"], {})

    def test_result_is_detached_from_input(self):
        data = request()
        result = core.analyze_request(data)
        data["traits"][0]["thresholds"].append(99)
        self.assertEqual(result["records"]["traits"][0]["source_refs"], [2, 4])


if __name__ == "__main__":
    unittest.main()
