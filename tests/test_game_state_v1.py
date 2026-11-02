"""Synthetic contract cases only; no pixels, device connections or accuracy claims."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest

from src.jcc_device.game_state_contract import (
    ContractViolation,
    iter_observations,
    load_json,
    make_validator,
    validate_annotations,
    validate_state,
)


PROJECT = Path(__file__).resolve().parents[1]
EXAMPLE = PROJECT / "schemas/examples/game-state-v1.example.json"


def set_at(state, path, value):
    node = state
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def unknown(field, *, source=None, observed=None, completed=None):
    field.update(status="unknown", value=None, expires_host_monotonic_ms=None,
                 reason="not_attempted", method={"kind": "unavailable", "name": "synthetic-no-attempt", "version": None})
    field.pop("last_known_value", None)
    field.pop("candidates", None)
    if source is not None:
        field.update(source_frame_id=source, observed_host_monotonic_ms=observed,
                     completed_host_monotonic_ms=completed)


class GameStateV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = make_validator()  # Also validates the schema against its metaschema.
        cls.example = load_json(EXAMPLE)

    def valid(self, state, previous=None):
        validate_state(state, previous, validator=self.validator)

    def invalid(self, state, *, previous=None, message=None):
        with self.assertRaises(ContractViolation) as captured:
            self.valid(state, previous)
        if message:
            self.assertIn(message, str(captured.exception))

    def next_snapshot(self):
        state = deepcopy(self.example)
        state["snapshot_revision"] += 1
        state["assembled_host_monotonic_ms"] += 10
        return state

    def test_schema_and_example(self):
        self.assertEqual(self.validator.schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(self.example["sample_kind"], "synthetic")
        self.valid(self.example)
        def check_local_refs(node):
            if isinstance(node, dict):
                if "$ref" in node:
                    self.assertTrue(node["$ref"].startswith("#/$defs/"))
                    self.assertIn(node["$ref"].removeprefix("#/$defs/"), self.validator.schema["$defs"])
                for value in node.values():
                    check_local_refs(value)
            elif isinstance(node, list):
                for value in node:
                    check_local_refs(value)
        check_local_refs(self.validator.schema)

    def test_all_unknown_is_a_valid_result(self):
        state = deepcopy(self.example)
        for _, field in iter_observations(state):
            unknown(field)
        self.valid(state)

    def test_null_root_is_not_a_snapshot(self):
        self.invalid(None)

    def test_false_affordance_is_recognized_not_unknown(self):
        state = deepcopy(self.example)
        state["affordances"]["shop_purchase"] = deepcopy(state["hp"])
        state["affordances"]["shop_purchase"]["value"] = False
        self.valid(state)

    def test_explicit_non_applicability(self):
        state = deepcopy(self.example)
        unknown(state["round"])
        state["round"].update(applicability="not_applicable", reason="not_applicable")
        self.valid(state)

    def test_extension_and_confidence_variants(self):
        variants = [
            None,
            {"kind": "raw_score", "value": 73.0, "scale": "synthetic_detector_0_100"},
            {"kind": "model_self_report", "value": "high", "scale": "synthetic_low_medium_high"},
            {"kind": "calibrated_probability", "value": 0.8,
             "scale": "probability_0_1", "calibration_ref": "synthetic-calibration-not-real"},
        ]
        for confidence in variants:
            with self.subTest(confidence=confidence):
                state = deepcopy(self.example)
                state["hp"]["confidence"] = confidence
                state["extensions"]["demo:atomic_value"] = deepcopy(state["hp"])
                state["extensions"]["demo:atomic_value"]["value"] = {"label": "synthetic", "points": [1, 2]}
                self.valid(state)

    def test_catalog_reference_is_scoped_and_optional(self):
        state = deepcopy(self.example)
        state["shop"][1]["content"]["value"]["catalog_ref"] = {
            "dataset_id": "synthetic-dataset-v1", "entity_kind": "unit", "entity_id": "synthetic-unit-a", "variant": None,
        }
        self.valid(state)

    def test_no_unverified_mode_specific_numeric_caps(self):
        state = deepcopy(self.example)
        state["hp"]["value"] = -10
        state["gold"] = deepcopy(state["hp"])
        state["gold"]["value"] = 10000
        self.valid(state)  # Type contract, not a claim that these are legal game values.

    def test_device_time_is_recorded_without_cross_clock_comparison(self):
        state = deepcopy(self.example)
        state["frames"][1]["device_capture_time"] = {
            "clock_id": "synthetic-device-clock", "ticks_ms": 1000000000, "provenance": "synthetic-device-api",
        }
        self.valid(state)

    def test_verified_stream_scoped_version_can_outlive_a_scene_epoch(self):
        state = deepcopy(self.example)
        state["context_epoch"] = state["frames"][1]["context_epoch"] = 1
        version = deepcopy(state["hp"])
        version.update(scope="stream", source_frame_id="synthetic-frame-0",
                       observed_host_monotonic_ms=900, completed_host_monotonic_ms=940,
                       method={"kind": "metadata", "name": "synthetic-verification", "version": None},
                       value={"region": "synthetic", "season": "synthetic", "patch": "synthetic",
                              "verification_ref": "synthetic-evidence"})
        state["game_version"] = version
        self.valid(state)

    def test_invalid_structures(self):
        # Each mutation violates a wire-level constraint; none needs real game facts.
        cases = [
            ("recognized_null", ("hp", "value"), None),
            ("unknown_nonnull", ("mode", "value"), "guessed-mode"),
            ("conflict_nonnull", ("gold", "value"), 40),
            ("stale_nonnull", ("level", "value"), 6),
            ("stale_wrong_type", ("level", "last_known_value"), "6"),
            ("recognized_history", ("hp", "last_known_value"), 72),
            ("unknown_history", ("mode", "last_known_value"), "synthetic-mode"),
            ("unknown_expiry", ("mode", "expires_host_monotonic_ms"), 2000),
            ("recognized_reason", ("hp", "reason"), "guessed"),
            ("recognized_unavailable", ("hp", "method", "kind"), "unavailable"),
            ("recognized_not_applicable", ("hp", "applicability"), "not_applicable"),
            ("recognized_undetermined", ("hp", "applicability"), "undetermined"),
            ("nonapplicability_reason", ("mode", "applicability"), "not_applicable"),
            ("reason_applicability", ("mode", "reason"), "not_applicable"),
            ("scene_unknown_literal", ("scene", "value"), "unknown"),
            ("phase_unknown_literal", ("phase", "value"), "unknown"),
            ("boolean_is_not_hp", ("hp", "value"), True),
            ("fractional_gold", ("gold", "candidates", 0, "value"), 40.5),
            ("level_zero", ("level", "last_known_value"), 0),
            ("round_string", ("round", "value"), "3-2"),
            ("round_stage_zero", ("round", "value", "stage"), 0),
            ("source_absent_value", ("hp", "source_frame_id"), None),
            ("negative_host_time", ("hp", "observed_host_monotonic_ms"), -1),
            ("negative_frame_sequence", ("frames", 0, "sequence"), -1),
            ("conflict_candidate_type", ("gold", "candidates", 0, "value"), "40"),
            ("single_candidate", ("gold", "candidates"), [deepcopy(self.example["gold"]["candidates"][0])]),
            ("candidate_null", ("gold", "candidates", 0, "value"), None),
            ("stale_candidate", ("gold", "candidates", 0, "expires_host_monotonic_ms"), None),
            ("bare_confidence", ("hp", "confidence"), 0.99),
            ("calibrated_without_evidence", ("hp", "confidence"),
             {"kind": "calibrated_probability", "value": 0.8, "scale": "probability_0_1"}),
            ("bad_probability", ("hp", "confidence"),
             {"kind": "calibrated_probability", "value": 1.1, "scale": "probability_0_1", "calibration_ref": "synthetic"}),
            ("core_stream_scope", ("hp", "scope"), "stream"),
            ("unsupported_root", ("device_id",), "synthetic-device-id-must-be-rejected"),
            ("unsupported_field", ("hp", "raw_guess"), 72),
            ("missing_namespace", ("extensions", "price"), deepcopy(self.example["hp"])),
            ("bare_extension", ("extensions", "demo:price"), 3),
            ("out_of_bounds_box", ("shop", 1, "content", "region", "x_max"), 1.1),
            ("recognized_shop_without_region", ("shop", 1, "content", "region"), None),
            ("unknown_shop_kind", ("shop", 1, "content", "value", "kind"), "guessed"),
            ("empty_is_not_unit", ("shop", 2, "content", "value", "display_name"), "synthetic"),
            ("bare_catalog_id", ("shop", 1, "content", "value", "catalog_ref"), {"entity_id": "synthetic"}),
            ("duplicate_slot", ("shop", 2, "slot"), 2),
            ("reordered_slots", ("shop",), list(reversed(deepcopy(self.example["shop"])))),
            ("four_slots", ("shop",), deepcopy(self.example["shop"][:4])),
            ("six_slots", ("shop",), deepcopy(self.example["shop"] + [self.example["shop"][0]])),
        ]
        for name, path, value in cases:
            with self.subTest(name=name):
                state = deepcopy(self.example)
                set_at(state, path, value)
                self.assertTrue(list(self.validator.iter_errors(state)), name)
                self.invalid(state)

    def test_missing_required_provenance_and_state_details(self):
        paths = [
            ("hp", "source_frame_id"), ("hp", "observed_host_monotonic_ms"),
            ("hp", "completed_host_monotonic_ms"), ("hp", "method"), ("hp", "expires_host_monotonic_ms"),
            ("level", "last_known_value"), ("gold", "candidates"), ("frames", 1, "image"),
        ]
        for path in paths:
            with self.subTest(path=path):
                state = deepcopy(self.example)
                node = state
                for key in path[:-1]:
                    node = node[key]
                del node[path[-1]]
                self.invalid(state)

    def test_version_requires_complete_verification(self):
        for missing in ("region", "season", "patch", "verification_ref"):
            with self.subTest(missing=missing):
                state = deepcopy(self.example)
                state["game_version"] = deepcopy(state["hp"])
                state["game_version"]["value"] = dict(region="synthetic", season="synthetic", patch="synthetic", verification_ref="synthetic")
                del state["game_version"]["value"][missing]
                self.invalid(state)

    def test_invalid_semantics(self):
        cases = [
            ("dangling_source", ("hp", "source_frame_id"), "missing-frame"),
            ("anchor_missing", ("frame_id",), "missing-frame"),
            ("anchor_old", ("frame_id",), "synthetic-frame-0"),
            ("clock_mismatch", ("hp", "observed_host_monotonic_ms"), 1001),
            ("completion_before_reception", ("hp", "completed_host_monotonic_ms"), 999),
            ("completion_after_assembly", ("hp", "completed_host_monotonic_ms"), 1121),
            ("expired_at_boundary", ("hp", "expires_host_monotonic_ms"), 1120),
            ("deadline_before_reception", ("hp", "expires_host_monotonic_ms"), 999),
            ("stale_before_deadline", ("level", "expires_host_monotonic_ms"), 1500),
            ("invalid_stale_reason", ("level", "reason"), "context_changed"),
            ("expired_candidate", ("gold", "candidates", 0, "expires_host_monotonic_ms"), 1120),
            ("candidate_future_completion", ("gold", "candidates", 0, "completed_host_monotonic_ms"), 1100),
            ("candidate_dangling_source", ("gold", "candidates", 0, "source_frame_id"), "missing-frame"),
            ("same_conflict_values", ("gold", "candidates", 1, "value"), 40),
            ("same_json_numeric_values", ("gold", "candidates", 1, "value"), 40.0),
            ("anchor_epoch_mismatch", ("context_epoch",), 1),
            ("zero_area_box", ("shop", 1, "content", "region", "x_min"), self.example["shop"][1]["content"]["region"]["x_max"]),
            ("inverted_box", ("frames", 1, "image", "game_viewport", "x_min"), 1),
            ("orientation_mismatch", ("frames", 1, "image", "orientation"), "portrait"),
            ("geometry_change_without_epoch", ("frames", 1, "image", "width_px"), 1279),
            ("duplicate_id", ("frames", 0, "frame_id"), "synthetic-frame-1"),
            ("duplicate_sequence", ("frames", 0, "sequence"), 1),
            ("future_frame", ("frames", 1, "host_received_monotonic_ms"), 1200),
            ("frame_reception_regressed", ("frames", 0, "host_received_monotonic_ms"), 1050),
            ("epoch_regressed", ("frames", 0, "context_epoch"), 1),
            ("non_finite", ("hp", "confidence"), {"kind": "raw_score", "value": float("inf"), "scale": "synthetic"}),
            ("device_uses_host_clock_id", ("frames", 1, "device_capture_time"),
             {"clock_id": self.example["host_clock_id"], "ticks_ms": 50, "provenance": "synthetic"}),
        ]
        for name, path, value in cases:
            with self.subTest(name=name):
                state = deepcopy(self.example)
                set_at(state, path, value)
                self.assertFalse(list(self.validator.iter_errors(state)), name)
                self.invalid(state)

    def test_conflict_envelope_tracks_the_newest_candidate(self):
        state = deepcopy(self.example)
        state["gold"].update(source_frame_id="synthetic-frame-0", observed_host_monotonic_ms=900)
        self.invalid(state, message="newest candidate frame")

    def test_context_invalidation(self):
        state = deepcopy(self.example)
        state["context_epoch"] = state["frames"][1]["context_epoch"] = 1
        state["hp"].update(source_frame_id="synthetic-frame-0", observed_host_monotonic_ms=900)
        self.invalid(state, message="obsolete context")

    def test_unchanged_snapshot_can_advance_revision(self):
        self.valid(self.next_snapshot(), self.example)

    def test_late_recognition_cannot_overwrite_a_newer_unknown(self):
        previous = deepcopy(self.example)
        unknown(previous["hp"])
        state = self.next_snapshot()
        state["hp"].update(source_frame_id="synthetic-frame-0", observed_host_monotonic_ms=900,
                           completed_host_monotonic_ms=1100)
        self.valid(state)  # Its shape and standalone timings are valid.
        self.invalid(state, previous=previous, message="newer field watermark")

    def test_late_unknown_cannot_erase_a_newer_recognized_value(self):
        state = self.next_snapshot()
        unknown(state["hp"], source="synthetic-frame-0", observed=900, completed=1100)
        self.valid(state)
        self.invalid(state, previous=self.example, message="newer field watermark")

    def test_same_frame_disagreement_requires_conflict(self):
        state = self.next_snapshot()
        state["hp"]["value"] = 73
        self.valid(state)
        self.invalid(state, previous=self.example, message="require a conflict")

    def test_same_frame_processing_does_not_refresh_expiry(self):
        state = self.next_snapshot()
        state["hp"]["expires_host_monotonic_ms"] = 3000
        self.valid(state)
        self.invalid(state, previous=self.example, message="cannot renew")

    def test_stale_history_preserves_value_and_provenance(self):
        state = self.next_snapshot()
        state["hp"].update(status="stale", value=None, last_known_value=72, reason="invalidated")
        self.valid(state, self.example)
        for key, value in (("last_known_value", 73), ("completed_host_monotonic_ms", 1100),
                           ("expires_host_monotonic_ms", 1900)):
            with self.subTest(key=key):
                changed = deepcopy(state)
                changed["hp"][key] = value
                self.valid(changed)
                self.invalid(changed, previous=self.example, message="stale history must preserve")

    def test_revision_and_clock_context(self):
        for key, value in (("snapshot_revision", 2), ("assembled_host_monotonic_ms", 1110),
                           ("host_clock_id", "another-synthetic-clock"), ("stream_id", "another-synthetic-stream"),
                           ("freshness_policy_id", "another-synthetic-policy")):
            with self.subTest(key=key):
                state = self.next_snapshot()
                state[key] = value
                self.valid(state)
                self.invalid(state, previous=self.example)

    def test_field_watermark_cannot_disappear(self):
        state = self.next_snapshot()
        state["affordances"] = {}
        self.valid(state)
        self.invalid(state, previous=self.example, message="field disappeared")

    def test_published_frame_metadata_is_immutable(self):
        state = self.next_snapshot()
        state["frames"][0]["device_capture_time"] = {
            "clock_id": "synthetic-device-clock", "ticks_ms": 5, "provenance": "synthetic",
        }
        self.valid(state)
        self.invalid(state, previous=self.example, message="immutable")

    def test_new_scene_and_geometry_accept_fresh_unknowns(self):
        state = self.next_snapshot()
        frame = deepcopy(state["frames"][-1])
        frame.update(frame_id="synthetic-frame-2", sequence=2, context_epoch=1, host_received_monotonic_ms=1140)
        frame["image"].update(width_px=800, height_px=1280, orientation="portrait", calibration_id=None)
        state["frames"].append(frame)
        state.update(frame_id=frame["frame_id"], context_epoch=1, assembled_host_monotonic_ms=1200)
        for _, field in iter_observations(state):
            if field["scope"] == "context" and field["status"] != "stale":
                unknown(field, source=frame["frame_id"], observed=1140, completed=1180)
                field["region"] = None
        self.valid(state, self.example)


class SampleAnnotationV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = make_validator(definition="sample_annotation")
        cls.example = load_json(PROJECT / "schemas/examples/sample-annotation-v1.example.json")

    def valid(self, records):
        validate_annotations(records, validator=self.validator)

    def invalid(self, records, message=None):
        with self.assertRaises(ContractViolation) as captured:
            self.valid(records)
        if message:
            self.assertIn(message, str(captured.exception))

    def simulated_manifest_record(self, number=1, *, split="development"):
        # Entirely fabricated metadata held in memory. No actual image or human
        # annotation exists, and these fixtures are never exported as real data.
        record = deepcopy(self.example)
        record.update(sample_kind="real", sample_id=f"synthetic-fixture-{number}",
                      recording_session_id=f"synthetic-session-{number}", dedupe_group_id=f"synthetic-dedupe-{number}",
                      annotation_method="human", reviewer_id="synthetic-reviewer", review_status="independently_reviewed",
                      split=split, asset={"asset_ref": f"synthetic-asset-{number}", "sha256": f"{number:064x}",
                                         "access": "local_only", "redaction": "none"})
        return record

    def test_synthetic_placeholder(self):
        self.valid([self.example])
        self.assertEqual(self.example["split"], "quarantine")
        self.assertIsNone(self.example["asset"])

    def test_empty_manifest_has_no_collected_samples(self):
        self.valid([])

    def test_manifest_must_be_a_list(self):
        self.invalid(None)

    def test_synthetic_record_cannot_enter_acceptance(self):
        record = deepcopy(self.example)
        record["split"] = "acceptance"
        self.invalid([record])

    def test_real_record_requires_local_asset_and_human_annotation(self):
        for key, value in (("asset", None), ("annotation_method", "synthetic")):
            with self.subTest(key=key):
                record = self.simulated_manifest_record()
                record[key] = value
                self.invalid([record])

    def test_acceptance_requires_review(self):
        for key, value in (("review_status", "draft"), ("reviewer_id", None)):
            with self.subTest(key=key):
                record = self.simulated_manifest_record(split="acceptance")
                record[key] = value
                self.invalid([record])

    def test_reviewer_is_independent(self):
        record = self.simulated_manifest_record(split="acceptance")
        record["reviewer_id"] = record["annotator_id"]
        self.invalid([record], "independent review")

    def test_sample_ids_are_unique(self):
        self.invalid([self.example, deepcopy(self.example)], "duplicate sample")

    def test_recording_sessions_cannot_cross_splits(self):
        development = self.simulated_manifest_record(1)
        acceptance = self.simulated_manifest_record(2, split="acceptance")
        acceptance["recording_session_id"] = development["recording_session_id"]
        self.invalid([development, acceptance], "recording_session_id: group cannot span")

    def test_dedupe_groups_cannot_cross_splits(self):
        development = self.simulated_manifest_record(1)
        acceptance = self.simulated_manifest_record(2, split="acceptance")
        acceptance["dedupe_group_id"] = development["dedupe_group_id"]
        self.invalid([development, acceptance], "dedupe_group_id: group cannot span")

    def test_acceptance_dedupes_representatives(self):
        first = self.simulated_manifest_record(1, split="acceptance")
        second = self.simulated_manifest_record(2, split="acceptance")
        second["dedupe_group_id"] = first["dedupe_group_id"]
        self.invalid([first, second], "one representative")

    def test_identical_images_share_a_dedupe_group(self):
        first = self.simulated_manifest_record(1)
        second = self.simulated_manifest_record(2)
        second["asset"]["sha256"] = first["asset"]["sha256"]
        self.invalid([first, second], "identical images")

    def test_label_state_relationships(self):
        cases = [
            {"truth_status": "known", "value": 72},  # Applicability still undetermined.
            {"applicability": "applicable", "truth_status": "known", "value": None, "reason": None},
            {"applicability": "applicable", "truth_status": "unknown", "value": 72},
            {"applicability": "not_applicable", "reason": "unreadable"},
            {"applicability": "applicable", "reason": "not_applicable"},
        ]
        for change in cases:
            with self.subTest(change=change):
                record = deepcopy(self.example)
                record["labels"]["hp"].update(change)
                self.invalid([record])

    def test_known_synthetic_label_requires_an_atomic_correct_type(self):
        record = deepcopy(self.example)
        record["labels"]["hp"].update(applicability="applicable", truth_status="known", value=72, reason=None)
        self.valid([record])
        record["labels"]["hp"]["value"] = "72"
        self.invalid([record])

    def test_known_shop_label_requires_a_location(self):
        record = deepcopy(self.example)
        record["labels"]["shop"][0]["content"].update(applicability="applicable", truth_status="known",
                                                  value={"kind": "empty"}, reason=None)
        self.invalid([record])

    def test_invalid_geometry_and_clock_metadata(self):
        record = deepcopy(self.example)
        record["frame"]["image"]["game_viewport"]["x_min"] = 1
        self.invalid([record], "positive area")
        record = deepcopy(self.example)
        record["frame"]["device_capture_time"] = {
            "clock_id": record["host_clock_id"], "ticks_ms": 1000, "provenance": "synthetic",
        }
        self.invalid([record], "distinct clock domain")


if __name__ == "__main__":
    unittest.main()
