"""Fictional inputs only: lexical candidates are not verified identities."""

from copy import deepcopy
import io
import json
import unittest

from PIL import Image

from jcc_catalog_snapshot.synthetic import synthetic_request
from jcc_shop_identity import REQUEST_FORMAT, catalog_namespace, resolve
from jcc_shop_probe import (Rect, ShopLayout, SlotLayout, display_text_binding_sha256,
                            display_text_slot_binding_sha256, probe_shop_display_text)


class FakeOCR:
    def __init__(self, names, confidence=0.9):
        self.names = names
        self.calls = 0
        self.confidence = confidence

    def recognize(self, _image):
        slot, view = divmod(self.calls, 2)
        self.calls += 1
        text = self.names[slot]
        if isinstance(text, tuple):
            text = text[view]
        if text is None:
            return []
        scale = view + 1
        return [{"text": text, "confidence": self.confidence, "x": 10 * scale,
                 "y": 74 * scale, "width": 40 * scale, "height": 12 * scale}]


def observation(names=None, *, with_evidence=False, color=(80, 70, 60), confidence=0.9):
    names = names or ["虚构守叶", "虚构旁人", "Ｃ甲", "不在名单", None]
    layout = ShopLayout("synthetic", (520, 110), tuple(
        SlotLayout(i + 1, Rect((x, 5, x + 100, 105)), Rect((x + 5, 75, x + 65, 100)),
                   Rect((x + 75, 75, x + 95, 100)), Rect((x + 5, 10, x + 95, 65)))
        for i, x in enumerate(range(0, 520, 104))))
    stream = io.BytesIO()
    Image.new("RGB", (520, 110), color).save(stream, format="PNG")
    result = probe_shop_display_text(stream.getvalue(), layout=layout, ocr=FakeOCR(names, confidence))
    summary = result.summary()
    return (summary, result.sanitization_evidence_v1()) if with_evidence else summary


def rebind_evidence(data):
    """Model an untrusted caller recomputing public, non-authenticating hashes."""
    summary = data["display_text"]
    evidence = data["sanitization_evidence"]
    evidence["display_summary_binding_sha256"] = display_text_binding_sha256(summary)
    for row in evidence["slots"]:
        row["display_slot_binding_sha256"] = display_text_slot_binding_sha256(summary, row["slot"])


def request():
    catalog = synthetic_request()
    catalog["heroes"][1]["display_name"] = "虚构旁人"
    catalog["heroes"].append({"hero_key": "fiction_c", "display_name": "Ｃ甲", "trait_keys": ["trait_leaf"]})
    text, evidence = observation(with_evidence=True)
    return {"format_version": REQUEST_FORMAT, "display_text": text,
            "sanitization_evidence": evidence,
            "catalog_request": catalog, "catalog_namespace": catalog_namespace(catalog),
            "catalog_context": {"coverage": "declared_complete", "conflicts": "none_declared"},
            "frame_context": {"frame_ref": text["frame_ref"], "freshness": "fresh",
                              "scope": "current_frame", "sample_kind": "synthetic"}}


class ShopIdentityTests(unittest.TestCase):
    def test_real_probe_and_catalog_adapters_unique_matches_are_only_candidates(self):
        data = request()
        before = deepcopy(data)
        result = resolve(data)
        slots = result["slots"]
        self.assertEqual([slot["catalog_match_status"] for slot in slots],
                         ["candidate", "candidate", "candidate", "unknown", "unknown"])
        self.assertEqual(slots[0]["candidate_ref"]["hero_key"], "hero_leaf_guard")
        self.assertEqual(slots[0]["candidate_ref"]["catalog_namespace"], data["catalog_namespace"])
        self.assertEqual(slots[2]["candidate_ref"]["hero_key"], "fiction_c")
        self.assertEqual(result["identity_status"], "unknown")
        self.assertEqual(result["target_client_binding"], "unknown")
        self.assertEqual(result["source_version_applicability"], "unknown")
        self.assertEqual(result["source_authentication"], "not_authenticated")
        self.assertEqual(result["sanitization_evidence_authentication"], "not_authenticated")
        self.assertEqual(result["frame_sample_kind"], "synthetic")
        self.assertEqual(result["catalog_data_kind"], "synthetic")
        self.assertFalse(result["decision_eligible"])
        self.assertIsNone(result["action_advice"])
        self.assertIsNone(result["accuracy"])
        self.assertEqual(data, before)
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_equal_names_and_normalization_collisions_are_ambiguous(self):
        for variant in ("虚构守叶", "虚 构 守叶"):
            data = request()
            data["catalog_request"]["heroes"][1]["display_name"] = variant
            data["catalog_namespace"] = catalog_namespace(data["catalog_request"])
            slot = resolve(data)["slots"][0]
            self.assertEqual(slot["catalog_match_status"], "unknown")
            self.assertEqual(slot["match_count"], 2)
            self.assertIsNone(slot["candidate_ref"])

    def test_fullwidth_collision_is_not_silently_picked(self):
        data = request()
        data["catalog_request"]["heroes"].append(
            {"hero_key": "fiction_c2", "display_name": "C甲", "trait_keys": ["trait_leaf"]})
        data["catalog_namespace"] = catalog_namespace(data["catalog_request"])
        slot = resolve(data)["slots"][2]
        self.assertEqual(slot["reasons"], ["catalog_match_ambiguous"])
        self.assertEqual(slot["match_count"], 2)

    def test_no_casefold_fuzzy_substring_or_script_conversion(self):
        for text in ("c甲", "C甲甲", "虚构守葉", "虚构守"):
            data = request()
            display, evidence = observation([text, None, None, None, None], with_evidence=True)
            data["display_text"] = display
            data["sanitization_evidence"] = evidence
            data["frame_context"]["frame_ref"] = display["frame_ref"]
            self.assertEqual(resolve(data)["slots"][0]["reasons"], ["no_catalog_match"])

    def test_low_missing_invalid_scores_and_view_mismatch_do_not_match(self):
        for bad in (0.49, None, True, float("nan"), float("inf"), 10**400, [], {}):
            data = request()
            data["display_text"]["slots"][0]["variants"][0]["raw_ocr_score"] = bad
            try:
                rebind_evidence(data)
            except ValueError:
                pass  # Non-finite summaries cannot have a strict JSON binding.
            slot = resolve(data)["slots"][0]
            self.assertEqual(slot["catalog_match_status"], "unknown")
            self.assertIsNone(slot["candidate_ref"])
            json.dumps(resolve(data), allow_nan=False)
        data = request()
        data["display_text"]["slots"][0]["variants"][1]["scale"] = 1
        rebind_evidence(data)
        self.assertEqual(resolve(data)["slots"][0]["reasons"], ["variant_contract_invalid"])

    def test_stale_unknown_forged_mismatched_frame_and_bad_slots_fail_closed(self):
        for freshness in ("stale", "unknown", None, [], {}):
            data = request()
            data["frame_context"]["freshness"] = freshness
            self.assertTrue(all(s["candidate_ref"] is None for s in resolve(data)["slots"]))
        data = request()
        data["frame_context"]["frame_ref"] = "sha256:" + "0" * 64
        self.assertEqual(resolve(data)["issues"], ["display_contract_invalid"])
        data = request()
        data["display_text"]["slots"][1]["slot"] = 1
        self.assertEqual(resolve(data)["issues"], ["slots_invalid"])

    def test_sample_kind_never_becomes_implicitly_real(self):
        data = request()
        data["frame_context"]["sample_kind"] = "unknown"
        result = resolve(data)
        self.assertEqual(result["frame_sample_kind"], "unknown")
        self.assertEqual(result["catalog_data_kind"], "synthetic")
        data["frame_context"]["sample_kind"] = "replay"
        self.assertEqual(resolve(data)["issues"], ["frame_kind_scope_conflict"])

    def test_explicit_historical_scope_can_match_stale_frame_only(self):
        data = request()
        data["frame_context"].update({"scope": "historical_frame", "freshness": "stale"})
        result = resolve(data)
        self.assertEqual(result["frame_scope"], "historical_frame")
        self.assertEqual(result["frame_ref"], data["display_text"]["frame_ref"])
        self.assertEqual(result["slots"][0]["catalog_match_status"], "candidate")
        self.assertEqual(result["identity_status"], "unknown")
        self.assertEqual(result["target_client_binding"], "unknown")
        self.assertFalse(result["decision_eligible"])
        self.assertIsNone(result["action_advice"])
        data["frame_context"]["frame_ref"] = "sha256:" + "0" * 64
        self.assertEqual(resolve(data)["issues"], ["display_contract_invalid"])

    def test_catalog_binding_coverage_and_conflicts_are_required(self):
        data = request()
        data["catalog_request"]["source"] = {"status": "unknown"}
        data["catalog_namespace"] = catalog_namespace(data["catalog_request"])
        self.assertEqual(resolve(data)["issues"], ["catalog_unknown"])
        for field, value in (("coverage", "unknown"), ("conflicts", "unknown"),
                             ("conflicts", "present"), ("conflicts", [])):
            data = request()
            data["catalog_context"][field] = value
            self.assertTrue(all(s["candidate_ref"] is None for s in resolve(data)["slots"]))

    def test_declared_scoped_hero_or_trait_conflicts_only_exclude_affected_heroes(self):
        for kind, key in (("hero", "hero_leaf_guard"), ("trait", "trait_guard")):
            data = request()
            data["catalog_context"]["conflicts"] = {
                "status": "declared_scoped", "catalog_namespace": data["catalog_namespace"],
                "records": [{"entity_kind": kind, "key": key,
                             "field_scope": "not_display_name_or_alias"}]}
            result = resolve(data)
            self.assertEqual(result["slots"][0]["reasons"], ["catalog_conflict_affects_match"])
            self.assertEqual(result["slots"][1]["catalog_match_status"], "candidate")
            self.assertEqual(result["slots"][2]["catalog_match_status"], "candidate")
            self.assertEqual(result["catalog_conflict_validation"], "caller_declaration_only")
            self.assertFalse(result["decision_eligible"])

    def test_conflict_ledger_must_bind_the_snapshot_and_have_known_non_name_targets(self):
        data = request()
        declared = {"status": "declared_scoped", "catalog_namespace": data["catalog_namespace"],
                    "records": [{"entity_kind": "hero", "key": "hero_leaf_guard",
                                 "field_scope": "not_display_name_or_alias"}]}
        bad_ledgers = []
        wrong = deepcopy(declared); wrong["catalog_namespace"] = "sha256:" + "0" * 64; bad_ledgers.append(wrong)
        wrong = deepcopy(declared); wrong["records"][0]["key"] = "unknown"; bad_ledgers.append(wrong)
        wrong = deepcopy(declared); wrong["records"][0]["field_scope"] = "display_name"; bad_ledgers.append(wrong)
        wrong = deepcopy(declared); wrong["records"][0]["field_scope"] = "unknown"; bad_ledgers.append(wrong)
        wrong = deepcopy(declared); wrong["records"][0]["entity_kind"] = []; bad_ledgers.append(wrong)
        wrong = deepcopy(declared); wrong["records"] = []; bad_ledgers.append(wrong)
        for ledger in bad_ledgers:
            with self.subTest(ledger=ledger):
                data = request(); data["catalog_context"]["conflicts"] = ledger
                result = resolve(data)
                self.assertEqual(result["issues"], ["catalog_conflicts_not_safely_scoped"])
                self.assertTrue(all(slot["candidate_ref"] is None for slot in result["slots"]))

    def test_conflicted_duplicate_name_remains_ambiguous(self):
        data = request()
        data["catalog_request"]["heroes"][1]["display_name"] = "虚构守叶"
        data["catalog_namespace"] = catalog_namespace(data["catalog_request"])
        data["catalog_context"]["conflicts"] = {
            "status": "declared_scoped", "catalog_namespace": data["catalog_namespace"],
            "records": [{"entity_kind": "hero", "key": "hero_leaf_guard",
                         "field_scope": "not_display_name_or_alias"}]}
        slot = resolve(data)["slots"][0]
        self.assertEqual(slot["reasons"], ["catalog_match_ambiguous"])
        self.assertEqual(slot["match_count"], 2)
        self.assertIsNone(slot["candidate_ref"])

    def test_declared_item_conflict_does_not_remove_name_only_hero_candidates(self):
        data = request()
        data["catalog_context"]["conflicts"] = {
            "status": "declared_scoped", "catalog_namespace": data["catalog_namespace"],
            "records": [{"entity_kind": "item", "key": "piece_x",
                         "field_scope": "not_display_name_or_alias"}]}
        self.assertEqual(resolve(data)["slots"][0]["catalog_match_status"], "candidate")

    def test_modified_catalog_does_not_reuse_the_old_namespace(self):
        data = request()
        original = data["catalog_namespace"]
        data["catalog_request"]["source"]["binding"]["season"] = "fiction_other"
        self.assertNotEqual(catalog_namespace(data["catalog_request"]), original)
        self.assertEqual(resolve(data)["issues"], ["catalog_namespace_mismatch"])
        data["catalog_namespace"] = catalog_namespace(data["catalog_request"])
        ref = resolve(data)["slots"][0]["candidate_ref"]
        self.assertNotEqual(ref["catalog_namespace"], original)
        self.assertEqual(ref["hero_key"], "hero_leaf_guard")

    def test_missing_fields_duplicate_ids_and_unresolved_catalog_requests_fail(self):
        data = request()
        del data["catalog_request"]["heroes"][0]["trait_keys"]
        data["catalog_namespace"] = catalog_namespace(data["catalog_request"])
        self.assertEqual(resolve(data)["issues"], ["catalog_unknown"])
        data = request()
        data["catalog_request"]["heroes"][1]["hero_key"] = "hero_leaf_guard"
        data["catalog_namespace"] = catalog_namespace(data["catalog_request"])
        self.assertEqual(resolve(data)["issues"], ["catalog_unknown"])

    def test_zero_width_or_invalid_names_do_not_become_unique(self):
        data = request()
        data["catalog_request"]["heroes"][1]["display_name"] = "虚构\u200b守叶"
        data["catalog_namespace"] = catalog_namespace(data["catalog_request"])
        self.assertEqual(resolve(data)["issues"], ["catalog_name_invalid"])
        data = request()
        data["display_text"]["slots"][0]["display_text_normalized_candidate"] = "虚构\u200b守叶"
        rebind_evidence(data)
        self.assertEqual(resolve(data)["slots"][0]["reasons"], ["display_text_invalid"])

    def test_malformed_requests_and_output_objects_are_not_accepted_as_input(self):
        for bad in (None, [], {}, "x", 0):
            self.assertEqual(resolve(bad)["issues"], ["request_invalid"])
        data = request()
        data["catalog_request"] = []
        self.assertEqual(resolve(data)["issues"], ["catalog_unknown"])
        data = request()
        data["catalog_request"]["heroes"][0]["hero_key"] = {}
        self.assertEqual(resolve(data)["issues"], ["catalog_unknown"])

    def test_low_evidence_or_unknown_never_inherits_old_candidate(self):
        data = request()
        self.assertIsNotNone(resolve(data)["slots"][0]["candidate_ref"])
        data["display_text"]["slots"][0]["status"] = "unknown"
        rebind_evidence(data)
        unknown = resolve(data)["slots"][0]
        self.assertEqual(unknown["identity_status"], "unknown")
        self.assertIsNone(unknown["candidate_ref"])
        data["frame_context"]["freshness"] = "stale"
        self.assertTrue(all(s["candidate_ref"] is None for s in resolve(data)["slots"]))

    def test_legacy_summary_without_safety_evidence_is_unknown(self):
        data = request()
        del data["sanitization_evidence"]
        result = resolve(data)
        self.assertEqual(result["issues"], ["sanitization_evidence_missing"])
        self.assertTrue(all(s["candidate_ref"] is None for s in result["slots"]))

    def test_real_probe_control_and_format_characters_never_resolve(self):
        for character in ("\t", "\n", "\r", "\u200b", "\ud800"):
            for view_index in (0, 1):
                with self.subTest(character=repr(character), view=view_index):
                    names = ["虚构守叶", "虚构守叶"]
                    names[view_index] = "虚构" + character + "守叶"
                    summary, evidence = observation([tuple(names), None, None, None, None],
                                                    with_evidence=True)
                    self.assertEqual(summary["slots"][0]["status"], "unknown")
                    safety = evidence["slots"][0]["variants"][view_index]
                    self.assertEqual(safety["raw_text_safety"], "unsafe_control_or_format")
                    self.assertIs(safety["contains_control_or_format"], True)
                    serialized = json.dumps(evidence, ensure_ascii=False)
                    self.assertNotIn("虚构", serialized)
                    data = request()
                    data["display_text"], data["sanitization_evidence"] = summary, evidence
                    data["frame_context"]["frame_ref"] = summary["frame_ref"]
                    self.assertIsNone(resolve(data)["slots"][0]["candidate_ref"])
                    # Merely changing a wrapper flag cannot override the probe's unknown slot.
                    safety["raw_text_safety"] = "safe"
                    safety["contains_control_or_format"] = False
                    self.assertIsNone(resolve(data)["slots"][0]["candidate_ref"])

    def test_real_probe_ordinary_spaces_can_match_without_exporting_ocr(self):
        summary, evidence = observation([("虚 构 守 叶", "虚构守叶"), None, None, None, None],
                                        with_evidence=True)
        data = request()
        data["display_text"], data["sanitization_evidence"] = summary, evidence
        data["frame_context"]["frame_ref"] = summary["frame_ref"]
        result = resolve(data)
        self.assertEqual(result["slots"][0]["catalog_match_status"], "candidate")
        self.assertNotIn("虚 构 守 叶", json.dumps(evidence, ensure_ascii=False))
        self.assertNotIn("虚构守叶", json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["identity_status"], "unknown")

    def test_real_probe_low_confidence_is_unknown_before_identity_lookup(self):
        summary, evidence = observation(with_evidence=True, confidence=0.49)
        self.assertEqual(summary["slots"][0]["status"], "unknown")
        for view in evidence["slots"][0]["variants"]:
            self.assertEqual(view["raw_text_safety"], "unknown")
            self.assertIsNone(view["contains_control_or_format"])
        data = request()
        data["display_text"], data["sanitization_evidence"] = summary, evidence
        data["frame_context"]["frame_ref"] = summary["frame_ref"]
        self.assertIsNone(resolve(data)["slots"][0]["candidate_ref"])

    def test_missing_unsafe_inconsistent_or_duplicate_safety_views_are_unknown(self):
        for mutation in ("missing", "unsafe", "unknown", "inconsistent", "boolean_type", "duplicate_scale"):
            with self.subTest(mutation=mutation):
                data = request()
                views = data["sanitization_evidence"]["slots"][0]["variants"]
                if mutation == "missing":
                    views.pop()
                elif mutation == "unsafe":
                    views[0]["raw_text_safety"] = "unsafe_control_or_format"
                    views[0]["contains_control_or_format"] = True
                elif mutation == "unknown":
                    views[0]["raw_text_safety"] = "unknown"
                    views[0]["contains_control_or_format"] = None
                elif mutation == "inconsistent":
                    views[0]["contains_control_or_format"] = True
                elif mutation == "boolean_type":
                    views[0]["contains_control_or_format"] = 0
                else:
                    views[1]["scale"] = 1
                result = resolve(data)
                self.assertIsNone(result["slots"][0]["candidate_ref"])
                self.assertEqual(result["slots"][1]["catalog_match_status"], "candidate")

    def test_wrapper_and_summary_frame_layout_and_complete_binding_must_match(self):
        for field in ("frame_ref", "source_sha256", "layout_id", "format", "normalization_policy"):
            with self.subTest(field=field):
                data = request()
                data["sanitization_evidence"][field] = "wrong"
                self.assertTrue(all(slot["candidate_ref"] is None for slot in resolve(data)["slots"]))
        data = request()
        data["display_text"]["slots"][0]["display_text_normalized_candidate"] = "虚构旁人"
        self.assertEqual(resolve(data)["issues"], ["sanitization_binding_mismatch"])
        data = request()
        _, other = observation(with_evidence=True, color=(81, 70, 60))
        data["sanitization_evidence"] = other
        self.assertEqual(resolve(data)["issues"], ["sanitization_evidence_invalid"])
        data = request()
        _, other = observation(["不同", "虚构旁人", "Ｃ甲", None, None], with_evidence=True)
        data["sanitization_evidence"] = other
        self.assertEqual(resolve(data)["issues"], ["sanitization_binding_mismatch"])

    def test_slot_binding_and_shape_prevent_swapping_or_reusing_evidence(self):
        data = request()
        rows = data["sanitization_evidence"]["slots"]
        first, second = rows[0]["display_slot_binding_sha256"], rows[1]["display_slot_binding_sha256"]
        rows[0]["display_slot_binding_sha256"], rows[1]["display_slot_binding_sha256"] = second, first
        result = resolve(data)
        self.assertIsNone(result["slots"][0]["candidate_ref"])
        self.assertIsNone(result["slots"][1]["candidate_ref"])
        self.assertEqual(result["slots"][2]["catalog_match_status"], "candidate")
        data = request()
        data["sanitization_evidence"]["slots"][1]["slot"] = 1
        self.assertEqual(resolve(data)["issues"], ["sanitization_slots_invalid"])

    def test_extra_raw_field_and_invalid_wrapper_scalars_are_rejected(self):
        for field, value in (("raw_ocr", "synthetic"), ("accuracy", 0),
                             ("human_annotations", False), ("independent_reviews", False),
                             ("actionable", 0), ("source_authentication", "authenticated")):
            with self.subTest(field=field):
                data = request()
                data["sanitization_evidence"][field] = value
                self.assertTrue(all(slot["candidate_ref"] is None for slot in resolve(data)["slots"]))


if __name__ == "__main__":
    unittest.main()
