"""Synthetic boundary tests and an independent consumptive reference."""

from copy import deepcopy
import itertools
import json
import unittest
from unittest.mock import patch

from jcc_recipe_candidate import analyze_json, analyze_request
from jcc_recipe_candidate import core
from jcc_recipe_candidate.synthetic import synthetic_request


def with_inventory(x=0, y=0, z=0):
    request = synthetic_request()
    request["inventory"]["entries"] = [
        {"component_id": component, "count": count}
        for component, count in zip(("piece_x", "piece_y", "piece_z"), (x, y, z))
    ]
    return request


def ids(result):
    return [row["item_id"] for row in result["candidates"]]


def independent_reference(request):
    # Independent approach: remove each required occurrence from a fresh copy.
    held = [row["component_id"] for row in request["inventory"]["entries"]
            for _ in range(row["count"])]
    result = []
    for recipe in request["catalog"]["recipes"]:
        pool = held[:]
        try:
            for component in recipe["components"]:
                pool.remove(component)
        except ValueError:
            continue
        result.append(recipe["item_id"])
    return result


class SyntheticFeasibilityTests(unittest.TestCase):
    def evaluated(self, request):
        result = analyze_request(request)
        self.assertEqual(result["status"], "evaluated", result["issues"])
        self.assertEqual(result["issues"], [])
        return result

    def test_shared_inventory_keeps_both_independent_alternatives(self):
        result = self.evaluated(with_inventory(2, 1))
        self.assertEqual(ids(result), ["fiction_xx", "fiction_xy"])
        self.assertEqual(result["candidate_semantics"], "independently_feasible_alternatives")
        self.assertEqual(result["simultaneous_feasibility"], "not_evaluated")
        self.assertFalse(result["inventory_consumed"])
        self.assertFalse(result["strategy_recommendation"])
        self.assertFalse(result["execution_eligible"])
        self.assertEqual(result["candidates"][0]["required_counts"], [{"component_id": "piece_x", "count": 2}])

    def test_single_double_mixed_and_insufficient_components(self):
        for inventory, expected in [((1, 0, 0), []), ((2, 0, 0), ["fiction_xx"]),
                                    ((1, 1, 0), ["fiction_xy"]), ((1, 0, 1), [])]:
            with self.subTest(inventory=inventory):
                self.assertEqual(ids(self.evaluated(with_inventory(*inventory))), expected)

    def test_32_inventory_grid_against_independent_reference(self):
        checked = 0
        for x, y, z in itertools.product(range(4), range(4), range(2)):
            request = with_inventory(x, y, z)
            with self.subTest(x=x, y=y, z=z):
                self.assertEqual(ids(self.evaluated(request)), independent_reference(request))
            checked += 1
        self.assertEqual(checked, 32)

    def test_four_copies_do_not_become_a_craft_count(self):
        result = self.evaluated(with_inventory(4))
        self.assertEqual(ids(result), ["fiction_xx"])
        self.assertEqual(result["inventory_snapshot"][0]["count"], 4)
        self.assertNotIn("max_craft_count", result["candidates"][0])

    def test_large_exact_counts_are_not_expanded_into_giant_lists(self):
        result = self.evaluated(with_inventory(core.MAX_COUNT))
        self.assertEqual(ids(result), ["fiction_xx"])
        self.assertEqual(result["inventory_snapshot"][0]["count"], core.MAX_COUNT)

    def test_known_empty_inventory_is_distinct_from_unknown(self):
        request = synthetic_request()
        request["inventory"]["entries"] = []
        result = self.evaluated(request)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["inventory_snapshot"], [])

    def test_known_empty_supplied_recipe_set_has_limited_scope(self):
        request = synthetic_request()
        request["catalog"]["recipes"] = []
        result = self.evaluated(request)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["catalog_scope"], "caller_supplied_recipes_only")

    def test_reversed_pair_is_accepted_and_input_order_is_not_a_rank(self):
        request = with_inventory(2, 2)
        request["catalog"]["recipes"].reverse()
        request["catalog"]["recipes"][1]["components"].reverse()
        self.assertEqual(ids(self.evaluated(request)), ["fiction_yy", "fiction_xy", "fiction_xx"])

    def test_explicit_caller_verified_declaration_remains_a_declaration(self):
        request = synthetic_request()  # The values remain fictional test values.
        for block in ("version", "inventory", "catalog"):
            request[block]["evidence"] = {"kind": "verified", "refs": ["synthetic:verification-source"],
                                          "verification_ref": "synthetic:verification-record"}
        result = self.evaluated(request)
        self.assertEqual(result["data_kind"], "verified")
        self.assertEqual(result["input_verification"], "caller_declared_only")
        self.assertFalse(result["execution_eligible"])

    def test_input_and_output_are_detached(self):
        request = synthetic_request()
        before = deepcopy(request)
        result = self.evaluated(request)
        self.assertEqual(request, before)
        result["evidence"]["version"]["refs"].append("synthetic:changed")
        result["binding"]["patch"] = "fiction_changed"
        result["inventory_snapshot"][0]["count"] = 0
        result["candidates"][0]["components"].clear()
        self.assertEqual(request, before)

    def test_normal_unicode_evidence_roundtrips_without_normalization(self):
        request = synthetic_request()
        refs = ["synthetic:中文/核验", "synthetic:caf\u00e9", "synthetic:cafe\u0301",
                "synthetic:👩\u200d🔬/🚀", "synthetic:العربية"]
        for block in ("version", "inventory", "catalog"):
            request[block]["evidence"] = {"kind": "verified", "refs": refs[:],
                                          "verification_ref": "synthetic:核验记录/🧪"}
        result = analyze_json(json.dumps(request, ensure_ascii=False))
        self.assertEqual(result["status"], "evaluated", result["issues"])
        self.assertEqual(ids(result), ["fiction_xx", "fiction_xy"])
        for block in ("version", "inventory", "catalog"):
            self.assertEqual(result["evidence"][block], request[block]["evidence"])


class UnknownInputTests(unittest.TestCase):
    def unknown(self, request, code):
        # No failed/unknown request should ever invoke the original algorithm.
        with patch.object(core, "_run_upstream", side_effect=AssertionError("backend must not run")):
            result = analyze_request(request)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["candidates"])
        self.assertIsNone(result["inventory_snapshot"])
        self.assertIn(code, {issue["code"] for issue in result["issues"]})
        self.assertFalse(result["execution_eligible"])
        return result

    def test_missing_or_unknown_blocks_do_not_default_to_empty(self):
        for block in ("version", "inventory", "catalog"):
            with self.subTest(block=block, case="missing"):
                request = synthetic_request()
                del request[block]
                self.unknown(request, "shape_invalid")
            for status in ("unknown", "conflict", "stale", "candidate", None, True, []):
                with self.subTest(block=block, status=status):
                    request = synthetic_request()
                    request[block] = {"status": status}
                    self.unknown(request, "input_not_known")

    def test_non_objects_and_format_changes_are_unknown(self):
        for value in (None, [], 0, "unknown", True):
            self.unknown(value, "shape_invalid")
        request = synthetic_request()
        request["format_version"] = "jcc-recipe-candidate-request/future"
        self.unknown(request, "format_unsupported")

    def test_each_version_dimension_must_match_for_inventory_and_catalog(self):
        for block, field in itertools.product(("inventory", "catalog"), sorted(core._BINDING_FIELDS)):
            with self.subTest(block=block, field=field):
                request = synthetic_request()
                request[block]["binding"][field] = "fiction_other"
                self.unknown(request, "version_mismatch")

    def test_missing_blank_unknown_or_latest_version_is_not_a_binding(self):
        for value in ("", "unknown", "UNKNOWN", "latest", "current", "any", None, 18, []):
            request = synthetic_request()
            for block in ("version", "inventory", "catalog"):
                request[block]["binding"]["patch"] = value
            with self.subTest(value=value):
                self.unknown(request, "version_invalid")
        request = synthetic_request()
        del request["version"]["binding"]["rules_revision"]
        self.unknown(request, "shape_invalid")

    def test_evidence_must_be_explicit_and_complete(self):
        for value in (None, {}, {"kind": "candidate", "refs": ["synthetic:x"]},
                      {"kind": "synthetic", "refs": []}, {"kind": "synthetic", "refs": [""]},
                      {"kind": "synthetic", "refs": ["synthetic:\ud800"]},
                      {"kind": "synthetic", "refs": ["synthetic:x", "synthetic:x"]},
                      {"kind": "synthetic", "refs": [["synthetic:x"]]}):
            request = synthetic_request()
            request["catalog"]["evidence"] = value
            self.unknown(request, "evidence_insufficient")
        request = synthetic_request()
        request["catalog"]["evidence"] = {"kind": "verified", "refs": ["synthetic:x"]}
        self.unknown(request, "shape_invalid")

    def test_refs_reject_all_unicode_cc_characters(self):
        # Unicode Cc consists of the C0 range plus DEL and the C1 range.
        for codepoint in itertools.chain(range(0x20), range(0x7F, 0xA0)):
            for block in ("version", "inventory", "catalog"):
                with self.subTest(codepoint=f"U+{codepoint:04X}", block=block):
                    request = synthetic_request()
                    request[block]["evidence"]["refs"] = ["synthetic:before" + chr(codepoint) + "after"]
                    self.unknown(request, "evidence_insufficient")

    def test_verification_refs_reject_all_unicode_cc_characters(self):
        for codepoint in itertools.chain(range(0x20), range(0x7F, 0xA0)):
            for block in ("version", "inventory", "catalog"):
                with self.subTest(codepoint=f"U+{codepoint:04X}", block=block):
                    request = synthetic_request()
                    for name in ("version", "inventory", "catalog"):
                        request[name]["evidence"] = {"kind": "verified", "refs": ["synthetic:source"],
                                                     "verification_ref": "synthetic:record"}
                    request[block]["evidence"]["verification_ref"] = "synthetic:before" + chr(codepoint) + "after"
                    self.unknown(request, "evidence_insufficient")

    def test_verified_evidence_without_record_and_mixed_kinds_are_unknown(self):
        request = synthetic_request()
        request["catalog"]["evidence"] = {"kind": "verified", "refs": ["synthetic:x"], "verification_ref": ""}
        self.unknown(request, "evidence_insufficient")
        request["catalog"]["evidence"]["verification_ref"] = "synthetic:record"
        self.unknown(request, "evidence_kind_mismatch")

    def test_partial_or_unavailable_inventory_is_unknown_even_if_pair_present(self):
        for key, value, code in (("complete", False, "inventory_incomplete"),
                                 ("complete", 1, "inventory_incomplete"),
                                 ("availability", "equipped", "inventory_unavailable"),
                                 ("availability", "unknown", "inventory_unavailable"),
                                 ("entries", None, "inventory_invalid")):
            request = synthetic_request()
            request["inventory"][key] = value
            self.unknown(request, code)

    def test_quantities_are_exact_non_negative_integers(self):
        for count in (None, -1, True, False, 1.0, "2", core.MAX_COUNT + 1, float("inf"), []):
            request = synthetic_request()
            request["inventory"]["entries"][0]["count"] = count
            self.unknown(request, "count_invalid")

    def test_duplicate_inventory_rows_are_not_added(self):
        request = with_inventory(1)
        request["inventory"]["entries"].append({"component_id": "piece_x", "count": 1})
        self.unknown(request, "inventory_id_duplicate")

    def test_unknown_inventory_id_is_not_ignored(self):
        request = synthetic_request()
        request["inventory"]["entries"].append({"component_id": "piece_absent", "count": 0})
        self.unknown(request, "inventory_id_invalid")

    def test_recipe_must_be_a_pair_of_known_components(self):
        for pair in (None, [], ["piece_x"], ["piece_x"] * 3,
                     ["piece_x", "piece_missing"], ["piece_x", None], "piece_x"):
            request = with_inventory()
            request["catalog"]["recipes"][0]["components"] = pair
            self.unknown(request, "recipe_pair_invalid")
        request = synthetic_request()
        del request["catalog"]["recipes"][0]["components"]
        self.unknown(request, "shape_invalid")

    def test_duplicate_same_id_blocks_identical_and_conflicting_recipes_in_both_orders(self):
        for pair in (["piece_x", "piece_x"], ["piece_y", "piece_y"]):
            for reverse in (False, True):
                request = synthetic_request()
                rows = request["catalog"]["recipes"]
                rows.append({"item_id": "fiction_xx", "components": pair[:]})
                if reverse:
                    rows.reverse()
                self.unknown(request, "recipe_id_duplicate")

    def test_same_unordered_pair_cannot_point_to_two_products(self):
        request = synthetic_request()
        request["catalog"]["recipes"].append({"item_id": "fiction_other", "components": ["piece_y", "piece_x"]})
        self.unknown(request, "recipe_pair_duplicate")

    def test_illegal_empty_and_prototype_ids_are_rejected(self):
        for token in (None, "", " bad", "bad name", "x\n", "1piece", "x" * 65, [],
                      "constructor", "__proto__", "toString", "hasOwnProperty"):
            request = synthetic_request()
            request["catalog"]["component_ids"][0] = token
            self.unknown(request, "id_invalid")
            request = synthetic_request()
            request["catalog"]["recipes"][0]["item_id"] = token
            self.unknown(request, "id_invalid")

    def test_duplicate_component_catalog_and_chained_crafts_are_rejected(self):
        request = synthetic_request()
        request["catalog"]["component_ids"].append("piece_x")
        self.unknown(request, "component_id_duplicate")
        request = synthetic_request()
        request["catalog"]["recipes"][0]["item_id"] = "piece_x"
        self.unknown(request, "item_component_overlap")

    def test_one_bad_recipe_blocks_the_whole_set_instead_of_silent_filtering(self):
        request = synthetic_request()
        request["catalog"]["recipes"].append({"item_id": "fiction_bad", "components": []})
        self.unknown(request, "recipe_pair_invalid")

    def test_missing_recipe_id_null_rows_or_missing_recipe_set_are_unknown(self):
        for row in (None, {}, {"components": ["piece_x", "piece_x"]}):
            request = synthetic_request()
            request["catalog"]["recipes"].append(row)
            self.unknown(request, "shape_invalid")
        request = synthetic_request()
        request["catalog"]["recipes"] = None
        self.unknown(request, "recipes_invalid")

    def test_unknown_fields_cannot_promote_data_or_execution(self):
        for block in (None, "version", "inventory", "catalog"):
            request = synthetic_request()
            target = request if block is None else request[block]
            target["execution_eligible"] = True
            self.unknown(request, "shape_invalid")

    def test_collection_size_limits_fail_before_backend(self):
        request = synthetic_request()
        request["catalog"]["component_ids"] = ["piece_x"] * (core.MAX_COMPONENT_IDS + 1)
        self.unknown(request, "component_ids_invalid")
        request = synthetic_request()
        request["catalog"]["recipes"] *= core.MAX_RECIPES
        self.unknown(request, "recipes_invalid")
        request = synthetic_request()
        request["inventory"]["entries"] *= core.MAX_COMPONENT_IDS
        self.unknown(request, "inventory_invalid")

    def test_json_duplicate_keys_nan_and_malformed_text_do_not_lose_information(self):
        text = json.dumps(synthetic_request())
        invalid = ["", "{", None, "NaN", "Infinity",
                   text.replace('"count": 2', '"count": 1, "count": 2'),
                   text.replace('"count": 2', '"count": NaN')]
        with patch.object(core, "_run_upstream", side_effect=AssertionError("backend must not run")):
            for value in invalid:
                result = analyze_json(value)
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["candidates"])
                self.assertIn("json_invalid", {i["code"] for i in result["issues"]})


if __name__ == "__main__":
    unittest.main()
