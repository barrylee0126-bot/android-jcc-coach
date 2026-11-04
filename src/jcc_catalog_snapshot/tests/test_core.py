from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from jcc_catalog_snapshot import analyze_json, analyze_request
from jcc_catalog_snapshot.synthetic import synthetic_request


class SnapshotAdapterTests(unittest.TestCase):
    def candidate(self, request):
        result = analyze_request(request)
        self.assertEqual(result["status"], "candidate", result["issues"])
        self.assertFalse(result["decision_eligible"])
        self.assertIsNone(result["action_advice"])
        self.assertEqual(result["target_client_binding"], "unknown")
        return result

    def unknown(self, request, code):
        result = analyze_request(request)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["hero_traits"])
        self.assertIn(code, {issue["code"] for issue in result["issues"]})
        return result

    def test_maps_display_names_to_all_stable_keys_without_picking_one(self):
        result = self.candidate(synthetic_request())
        self.assertEqual(result["name_lookup"]["heroes"]["虚构守叶"], ["hero_leaf_guard", "hero_leaf_guard_variant"])
        self.assertEqual(result["hero_traits"]["hero_leaf_guard"], ["trait_leaf", "trait_guard"])
        self.assertEqual(result["trait_thresholds"]["trait_leaf"], [2, 4])
        self.assertEqual(result["item_components"]["item_xy"], ["piece_x", "piece_y"])

    def test_source_version_is_only_a_candidate_claim(self):
        request = synthetic_request()
        request["source"]["provenance"]["kind"] = "official_candidate"
        result = self.candidate(request)
        self.assertEqual(result["data_kind"], "official_candidate")
        self.assertEqual(result["source_version_applicability"], "unknown")
        self.assertEqual(result["source_binding"]["version"], "fiction_version")

    def test_missing_or_unknown_source_binding_never_becomes_current_rules(self):
        request = synthetic_request()
        request["source"]["binding"]["version"] = "unknown"
        self.unknown(request, "binding_invalid")
        request = synthetic_request()
        request["source"] = {"status": "unknown"}
        self.unknown(request, "source_not_known")

    def test_rejects_missing_trait_reference_and_recipe_component(self):
        request = synthetic_request()
        request["heroes"][0]["trait_keys"] = ["not_present"]
        self.unknown(request, "hero_invalid")
        request = synthetic_request()
        request["items"][2]["component_keys"] = ["not_present"]
        self.unknown(request, "item_component_missing")

    def test_thresholds_must_be_explicit_positive_strictly_increasing(self):
        for values in ([], [2, 2], [4, 2], [0, 2], [True, 2], ["2"]):
            request = synthetic_request()
            request["traits"][0]["thresholds"] = values
            with self.subTest(values=values):
                self.unknown(request, "trait_invalid" if values == [] else "trait_thresholds_invalid")

    def test_duplicate_keys_and_unknown_fields_fail_closed(self):
        request = synthetic_request()
        request["heroes"].append(deepcopy(request["heroes"][0]))
        self.unknown(request, "hero_invalid")
        request = synthetic_request()
        request["items"][0]["extra"] = "no"
        self.unknown(request, "shape_invalid")

    def test_json_duplicate_key_nonfinite_and_input_output_detachment(self):
        self.assertEqual(analyze_json('{"format_version":"x","format_version":"y"}')["status"], "unknown")
        self.assertEqual(analyze_json('{"format_version":NaN}')["status"], "unknown")
        request = synthetic_request()
        before = deepcopy(request)
        result = self.candidate(request)
        result["hero_traits"]["hero_leaf_guard"].clear()
        result["source_binding"]["version"] = "changed"
        self.assertEqual(request, before)
        self.assertEqual(analyze_json(json.dumps(request))["status"], "candidate")

    def test_malformed_provenance_values_are_controlled_for_api_json_and_cli(self):
        cases = [("kind", []), ("kind", {}), ("source_url", []), ("source_url", "https://"),
                 ("source_url", "https://example.invalid:99999/x"),
                 ("source_url", "https://example.invalid:abc/x"),
                 ("source_url", "https://exa\nmple.invalid/x"),
                 ("source_url", "https://host invalid/x")]
        for field, value in cases:
            request = synthetic_request()
            request["source"]["provenance"][field] = value
            with self.subTest(field=field, value=repr(value)):
                self.unknown(request, "provenance_invalid")
                self.assertEqual(analyze_json(json.dumps(request))["status"], "unknown")
        request = synthetic_request()
        request["source"]["provenance"]["kind"] = []
        project = Path(__file__).resolve().parents[3]
        environment = dict(os.environ, PYTHONPATH=str(project / "src"), PYTHONDONTWRITEBYTECODE="1")
        completed = subprocess.run([sys.executable, "-B", "-m", "jcc_catalog_snapshot", "--stdin"],
                                   input=json.dumps(request), text=True, capture_output=True,
                                   cwd=project, env=environment, check=False)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(json.loads(completed.stdout)["status"], "unknown")

    def test_rejects_self_and_multi_item_recipe_cycles_but_allows_double_component(self):
        request = synthetic_request()
        request["items"][2]["component_keys"] = ["item_xy"]
        self.unknown(request, "item_recipe_cycle")
        request = synthetic_request()
        request["items"][0]["component_keys"] = ["piece_y"]
        request["items"][1]["component_keys"] = ["piece_x"]
        self.unknown(request, "item_recipe_cycle")
        request = synthetic_request()
        request["items"][2]["component_keys"] = ["piece_x", "piece_x"]
        self.candidate(request)

    def test_common_chinese_and_english_version_placeholders_are_rejected(self):
        for token in ("unknown", "UNKNOWN", "N/A", "未知", "待核实", "未核实", "最新", "当前", "待定", "不明", "最新版本", "当前版本", "待核验"):
            request = synthetic_request()
            request["source"]["binding"]["version"] = token
            with self.subTest(token=token):
                self.unknown(request, "binding_invalid")


if __name__ == "__main__":
    unittest.main()
