from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jcc_catalog import Catalog, CatalogError, FieldRef, SourceSpec, load_manifest


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def write(self, name, value):
        path = self.base / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def catalog(self, records, **kwargs):
        return Catalog([SourceSpec("synthetic", "hero", self.write("records.json", records), **kwargs)])

    def assert_error(self, code, operation):
        with self.assertRaises(CatalogError) as raised:
            operation()
        self.assertEqual(code, raised.exception.code)

    def test_same_id_and_identical_variants_are_all_retained(self):
        records = [{"id": "shared", "name": "示例"}, {"id": "shared", "name": "示例+"},
                   {"id": "shared", "name": "示例+"}]
        catalog = self.catalog(records)
        found = catalog.find(external_id="shared")
        self.assertEqual(3, len(found))
        self.assertEqual(3, len({record["record_key"] for record in found}))
        self.assertEqual(["/0", "/1", "/2"], [record["record_pointer"] for record in found])
        self.assertEqual(records, [catalog.raw_record(record["record_key"]) for record in found])
        summary = catalog.summary()["snapshots"][0]
        self.assertEqual((1, 1, 2), (summary["distinct_external_ids"], summary["duplicate_id_groups"],
                                     summary["extra_records_with_duplicate_id"]))

    def test_unprovided_provenance_and_version_stay_unknown(self):
        catalog = self.catalog([{"name": "候选", "verifiedAt": "supplier-date", "source": "official-text", "cost": 2}])
        source = catalog.sources()[0]
        self.assertIsNone(source["source_url"])
        self.assertIsNone(source["snapshot"]["retrieved_at"])
        self.assertTrue(all(value is None for value in source["claimed_scope"].values()))
        self.assertEqual("third_party_only", source["verification"]["evidence"])
        evidence = catalog.field(catalog.find()[0]["record_key"], "/cost")
        self.assertEqual("candidate", evidence["status"])
        self.assertIsNone(evidence["value"])
        self.assertFalse(evidence["decision_eligible"])
        self.assertEqual([], catalog.find(claimed_scope={"patch": "99.1"}))
        self.assertEqual(1, len(catalog.find(claimed_scope={"patch": None})))

    def test_source_claims_are_not_verification_and_cannot_override_each_other(self):
        claims = {"patch": "99.1", "season": "S99", "theme": "示例主题", "verifiedAt": "supplier-date",
                  "validation": {"valid": True}}
        claims_path = self.write("claims.json", {"metadata": claims})
        catalog = self.catalog([{"cost": 2}], claims_path=claims_path, claims_pointer="/metadata",
                               claimed_scope={"patch": "99.2"},
                               source_url="https://example.invalid/data", retrieved_at="2026-01-01T00:00:00Z")
        source = catalog.sources()[0]
        self.assertEqual(claims, catalog.source_metadata(source["snapshot_id"]))
        self.assertIsNone(source["claimed_scope"]["patch"])
        self.assertEqual(["patch"], source["scope_claim_conflicts"])
        self.assertEqual(["99.2", "99.1"], [claim["value"] for claim in source["scope_evidence"]["patch"]])
        self.assertEqual("/metadata/patch", source["scope_evidence"]["patch"][1]["pointer"])
        self.assertEqual("S99", source["claimed_scope"]["season"])
        self.assertEqual("not_reviewed", source["verification"]["project_review"])

    def test_snapshot_and_field_locations_are_exact_and_stable(self):
        data = {"content": [{"id": "x", "a/b": {"~x": [3, 4]}, "recipe": ["A", "A"]}]}
        path = self.write("records.json", data)
        before = path.read_bytes()
        spec = SourceSpec("synthetic", "item", path, records_pointer="/content")
        catalog = Catalog([spec])
        key = catalog.find()[0]["record_key"]
        result = catalog.field(key, "/a~1b/~0x/1")["candidates"][0]
        self.assertEqual(4, result["source_value"])
        self.assertEqual("/content/0/a~1b/~0x/1", result["origin"]["json_pointer"])
        self.assertEqual(hashlib.sha256(before).hexdigest(), result["origin"]["sha256"])
        self.assertEqual(["A", "A"], catalog.field(key, "/recipe")["candidates"][0]["source_value"])
        self.assertEqual(key, Catalog([spec]).find()[0]["record_key"])
        self.assertEqual(before, path.read_bytes())
        self.assertEqual("missing", catalog.field(key, "/a~1b/~0x/01")["candidates"][0]["value_state"])

    def test_missing_null_empty_zero_false_and_empty_recipe_are_distinct(self):
        catalog = self.catalog([{"null": None, "empty": " ", "zero": 0, "false": False, "recipe": []}])
        key = catalog.find()[0]["record_key"]
        for pointer, state, status in (("/missing", "missing", "unknown"), ("/null", "null", "unknown"),
                                       ("/empty", "empty_text", "unknown"), ("/zero", "present", "candidate"),
                                       ("/false", "present", "candidate"), ("/recipe", "empty_container", "candidate")):
            with self.subTest(pointer=pointer):
                result = catalog.field(key, pointer)
                self.assertEqual(status, result["status"])
                self.assertEqual(state, result["candidates"][0]["value_state"])
                self.assertIsNone(result["value"])
        self.assertFalse(catalog.field(key, "/missing")["candidates"][0]["origin"]["locator_exists"])
        self.assertTrue(catalog.field(key, "/null")["candidates"][0]["origin"]["locator_exists"])

    def test_implicit_variants_are_not_semantic_conflicts(self):
        catalog = self.catalog([{"id": "same", "cost": 1}, {"id": "same", "cost": 2}])
        refs = [FieldRef(record["record_key"], "/cost") for record in catalog.find()]
        result = catalog.compare_fields(refs)
        self.assertEqual(("candidate", "variant_or_scope_difference"), (result["status"], result["comparison"]))
        self.assertEqual("unknown", result["scope_status"])
        self.assertIsNone(result["value"])

    def test_explicit_conflict_keeps_all_evidence_and_never_selects_a_winner(self):
        catalog = self.catalog([{"a": 1, "b": 2, "missing": None}])
        key = catalog.find()[0]["record_key"]
        refs = [FieldRef(key, "/a"), FieldRef(key, "/b"), FieldRef(key, "/missing")]
        result = catalog.compare_fields(refs, relationship="same_fact", reason="合成：预期同一数值")
        self.assertEqual("conflict", result["status"])
        self.assertEqual([1, 2, None], [item["source_value"] for item in result["candidates"]])
        self.assertEqual("incomplete", result["completeness"])
        self.assertEqual("not_determined", result["semantic_contradiction"])
        self.assertIsNone(result["value"])
        self.assertFalse(result["decision_eligible"])
        self.assert_error("missing_comparison_reason", lambda: catalog.compare_fields(refs, relationship="same_fact"))

    def test_different_games_or_patches_do_not_become_an_applicable_conflict(self):
        a = self.write("a.json", [{"name": "同名", "cost": 1}])
        b = self.write("b.json", [{"name": "同名", "cost": 2}])
        catalog = Catalog([SourceSpec("a", "hero", a, claimed_scope={"game": "game-a", "patch": "99.1"}),
                           SourceSpec("b", "hero", b, claimed_scope={"game": "game-b", "patch": "99.1"})])
        refs = [FieldRef(record["record_key"], "/cost") for record in catalog.find()]
        result = catalog.compare_fields(refs, relationship="same_fact", reason="合成对照")
        self.assertEqual(("candidate", "different_claimed_scope"), (result["status"], result["scope_status"]))
        self.assertEqual(1, len(catalog.find(claimed_scope={"game": "game-a"})))

    def test_agreement_is_not_verification_and_bool_is_not_integer(self):
        catalog = self.catalog([{"a": 1, "b": 1, "bool": True}])
        key = catalog.find()[0]["record_key"]
        result = catalog.compare_fields([FieldRef(key, "/a"), FieldRef(key, "/b")])
        self.assertEqual(("candidate", "agreeing_candidates"), (result["status"], result["comparison"]))
        result = catalog.compare_fields([FieldRef(key, "/a"), FieldRef(key, "/bool")],
                                        relationship="same_fact", reason="类型差异")
        self.assertEqual("conflict", result["status"])
        self.assert_error("unverified_field", lambda: catalog.require_verified_value(key, "/a"))

    def test_all_missing_comparison_is_unknown(self):
        catalog = self.catalog([{}])
        key = catalog.find()[0]["record_key"]
        result = catalog.compare_fields([FieldRef(key, "/absent"), FieldRef(key, "/also_absent")])
        self.assertEqual("unknown", result["status"])

    def test_public_results_cannot_mutate_the_catalog(self):
        catalog = self.catalog([{"name": "示例", "aliases": ["别名"], "stats": [{"hp": 10}]}])
        found = catalog.find(text="别名")
        key = found[0]["record_key"]
        found[0]["aliases"].append("污染")
        catalog.raw_record(key)["stats"][0]["hp"] = 100
        catalog.field(key, "/stats")["candidates"][0]["source_value"][0]["hp"] = 200
        catalog.sources()[0]["verification"]["decision_eligible"] = True
        self.assertEqual(10, catalog.raw_record(key)["stats"][0]["hp"])
        self.assertEqual([], catalog.find(text="污染"))
        self.assertFalse(catalog.sources()[0]["verification"]["decision_eligible"])

    def test_multiple_snapshots_of_same_source_are_preserved_and_duplicate_import_is_rejected(self):
        a = self.write("a.json", [{"id": "same", "cost": 1}])
        b = self.write("b.json", [{"id": "same", "cost": 2}])
        spec = SourceSpec("same-source", "hero", a)
        catalog = Catalog([spec, SourceSpec("same-source", "hero", b)])
        self.assertEqual(2, len(catalog.find(external_id="same")))
        self.assert_error("duplicate_snapshot", lambda: Catalog([spec, spec]))

    def test_bad_json_fails_without_losing_duplicate_fields_or_leaking_source_text(self):
        for data, code in ((b'{"secret": 1, "secret": 2}', "duplicate_json_key"),
                           (b'[{"value": NaN}]', "invalid_json"), (b'[{"value": 1e999}]', "invalid_json"),
                           (b'{broken-source-text', "invalid_json"), (b'\xff', "invalid_json"),
                           (b'[{"name":"\\ud800"}]', "invalid_json")):
            with self.subTest(code=code, data=data[:8]):
                path = self.base / "bad.json"
                path.write_bytes(data)
                self.assert_error(code, lambda: Catalog([SourceSpec("bad", "hero", path)]))

    def test_bad_shapes_limits_paths_and_unknown_keys_fail(self):
        self.assert_error("invalid_records", lambda: self.catalog({"not": "an array"}))
        self.assert_error("invalid_record", lambda: self.catalog([{}, 7]))
        self.assert_error("empty_sources", lambda: Catalog([]))
        self.assert_error("unreadable_file", lambda: Catalog([SourceSpec("x", "hero", self.base / "absent")]))
        path = self.write("records.json", [{}, {}])
        self.assert_error("file_too_large", lambda: Catalog([SourceSpec("x", "hero", path)], max_file_bytes=2))
        self.assert_error("too_many_records", lambda: Catalog([SourceSpec("x", "hero", path)], max_records=1))
        self.assert_error("invalid_timestamp", lambda: self.catalog([{}], retrieved_at="2026-01-01"))
        self.assert_error("invalid_scope", lambda: self.catalog([{}], claimed_scope={"patch": 99}))
        self.assert_error("invalid_source_url", lambda: self.catalog([{}], source_url="https://user:secret@example.invalid"))
        catalog = self.catalog([{}])
        key = catalog.find()[0]["record_key"]
        self.assert_error("invalid_pointer", lambda: catalog.field(key, "cost"))
        self.assert_error("invalid_pointer", lambda: catalog.field(key, "/bad~2escape"))
        self.assert_error("unknown_record", lambda: catalog.field("missing", "/x"))
        self.assert_error("duplicate_reference", lambda: catalog.compare_fields([FieldRef(key, "/x")] * 2))

    def test_manifest_resolves_relative_paths_and_rejects_silent_config_typos(self):
        self.write("records.json", [{"id": "x"}])
        manifest = self.write("manifest.json", {"schema_version": 1, "sources": [
            {"source_id": "synthetic", "kind": "hero", "path": "records.json"}]})
        self.assertEqual((self.base / "records.json").resolve(), load_manifest(manifest)[0].path)
        self.assertEqual(1, Catalog(load_manifest(manifest)).summary()["total_records"])
        for source in ({"source_id": "x", "kind": "hero", "path": "records.json", "verified": True},
                       {"source_id": "x", "kind": "hero", "path": None}):
            bad = self.write("bad-manifest.json", {"schema_version": 1, "sources": [source]})
            self.assert_error("invalid_manifest", lambda: Catalog(load_manifest(bad)))

    def test_cli_search_field_error_and_input_fingerprints(self):
        path = self.write("cli.json", [{"id": "same", "name": "示例", "cost": 2},
                                       {"id": "same", "name": "示例+", "cost": 3}])
        before = path.read_bytes()
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}

        def run(*arguments):
            return subprocess.run([sys.executable, "-B", "-m", "jcc_catalog", "--source", "hero", str(path), *arguments],
                                  env=env, capture_output=True, text=True, check=False)

        result = run("find", "--id", "same", "--limit", "1")
        self.assertEqual(0, result.returncode, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual((2, 1), (data["total"], len(data["records"])))
        key = data["records"][0]["record_key"]
        result = run("field", key, "/cost")
        self.assertEqual(2, json.loads(result.stdout)["candidates"][0]["source_value"])
        result = run("field", "unknown", "/cost")
        self.assertEqual(2, result.returncode)
        self.assertEqual("unknown_record", json.loads(result.stderr)["error"]["code"])
        self.assertEqual("", result.stdout)
        result = run("find", "--limit", "0")
        self.assertEqual("invalid_limit", json.loads(result.stderr)["error"]["code"])
        self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
