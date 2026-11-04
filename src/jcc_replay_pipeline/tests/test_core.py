from __future__ import annotations

import copy
import unittest

from jcc_replay_pipeline import (
    PipelineContractError,
    build_coach_observation_event,
    build_partial_trait_request,
    run_partial_planner,
)
from jcc_human_coach.source import decode_event
from jcc_replay_pipeline.core import _binding_digest, _partial_digest


def manifest():
    return {
        "case_id": "demo-case-alpha", "frame_id": "demo-frame-a",
        "source_video_sha256": "a" * 64, "encoded_sha256": "b" * 64,
        "decode_sequence": 17,
        "pts": {"kind": "seconds_text", "value": "12.345678901"},
    }


def source(binding=None):
    binding = binding or manifest()
    return {
        "case_id": binding["case_id"], "frame_id": binding["frame_id"],
        # This intentionally insufficient 25B shape is diagnostic only: a
        # float can never supply an exact PTS identity.
        "decoded_pts_s": 12.345678901, "source_video_sha256": binding["source_video_sha256"],
        "encoded_sha256": binding["encoded_sha256"], "subject": "unknown", "scene": "unknown",
        "validity": "single_frame_only", "context_authority": "caller_declared_unverified",
    }


def vision_fixture():
    frame = source()
    return {
        "schema_version": "trait-vision-v1", "implementation_revision": "synthetic-test",
        "source_frame": copy.deepcopy(frame),
        "population": {
            "status": "development_candidate", "displayed_deployed": 3, "capacity": 5,
            "source_frame": copy.deepcopy(frame),
        },
        "trait_rows": [{
            "row_id": "demo-row", "status": "development_candidate",
            "label_status": "development_candidate", "ratio_status": "development_candidate",
            "display_label": "合成标签", "observed_count": 2, "displayed_thresholds": [4],
            "source_frame": copy.deepcopy(frame),
        }],
        "accuracy": None, "automatic_action": None,
        "status": "development_candidate", "reason_codes": [], "processing_seconds": 0.01,
    }


def envelope(vision=None, *, kind="synthetic", binding=None):
    return build_partial_trait_request(vision or vision_fixture(), source_kind=kind,
                                       frame_manifest=binding or manifest())


def coach_event(envelope_value, *, sequence=1):
    return build_coach_observation_event(
        envelope_value, run_partial_planner(envelope_value), stream_id="demo-stream", sequence=sequence,
        host_received_ms=101, emitted_ms=102, expires_ms=902,
    )


class ReplayPipelineTests(unittest.TestCase):
    def test_hash_bound_candidate_translates_without_catalog_identity(self):
        value = envelope()
        request = value["partial_request"]
        self.assertEqual(request["observation"]["source_kind"], "synthetic")
        self.assertEqual(request["observation"]["pts_seconds"]["value"], "12.345678901")
        self.assertEqual(request["field_capacity"], {"status": "known", "current": 3, "maximum": 5})
        self.assertEqual(request["active_traits"], [{
            "trait_id": "display-row-demo-row", "display_name": "合成标签", "count": 2, "next_threshold": 4,
        }])
        binding = value["replay_binding"]
        self.assertEqual(binding["encoded_sha256"], "b" * 64)
        self.assertEqual(binding["pts_evidence"], "external_manifest_caller_declared")
        self.assertFalse(binding["provenance_authenticated"])

    def test_a_different_encoded_frame_cannot_reuse_the_same_binding(self):
        wrong = manifest(); wrong["encoded_sha256"] = "c" * 64
        with self.assertRaisesRegex(PipelineContractError, "vision_source_binding_invalid"):
            envelope(binding=wrong)

    def test_population_and_trait_rows_must_bind_to_the_top_frame(self):
        population_mismatch = vision_fixture()
        population_mismatch["population"]["source_frame"]["frame_id"] = "demo-frame-b"
        with self.assertRaisesRegex(PipelineContractError, "vision_population_binding_invalid"):
            envelope(population_mismatch)
        trait_mismatch = vision_fixture()
        trait_mismatch["trait_rows"][0]["source_frame"]["encoded_sha256"] = "c" * 64
        with self.assertRaisesRegex(PipelineContractError, "vision_trait_binding_invalid"):
            envelope(trait_mismatch)

    def test_unknown_or_partial_trait_rows_never_retain_old_counts(self):
        candidate = vision_fixture()
        row = candidate["trait_rows"][0]
        row.update(status="unknown", ratio_status="unknown", observed_count=2, displayed_thresholds=[4])
        with self.assertRaisesRegex(PipelineContractError, "vision_trait_unknown_residue"):
            envelope(candidate)
        partial_label = vision_fixture()
        row = partial_label["trait_rows"][0]
        row.update(status="unknown", ratio_status="unknown", observed_count=None, displayed_thresholds=None)
        self.assertEqual(envelope(partial_label)["partial_request"]["active_traits"], [])

    def test_unknown_root_or_capacity_residue_never_promotes_old_fields(self):
        root_unknown = vision_fixture(); root_unknown["status"] = "unknown"
        with self.assertRaisesRegex(PipelineContractError, "vision_source_binding_invalid"):
            envelope(root_unknown)
        impossible_empty_slots = vision_fixture(); impossible_empty_slots["population"]["empty_slots"] = 0
        with self.assertRaisesRegex(PipelineContractError, "vision_population_candidate_invalid"):
            envelope(impossible_empty_slots)

    def test_integer_timebase_is_preserved_not_rounded_for_25d(self):
        ticks = manifest()
        ticks["decode_sequence"] = 21
        ticks["pts"] = {"kind": "integer_timebase", "ticks": 12_345_678_901,
                         "time_base_num": 1, "time_base_den": 1_000_000_000}
        value = envelope(binding=ticks)
        self.assertEqual(value["partial_request"]["observation"]["pts_seconds"], {"status": "unknown", "value": None})
        self.assertEqual(value["replay_binding"]["pts"], ticks["pts"])
        self.assertEqual(coach_event(value)["coach_event"]["source_sequence"], 21)
        changed_ticks = copy.deepcopy(ticks); changed_ticks["pts"]["ticks"] += 1
        changed = envelope(binding=changed_ticks)
        with self.assertRaisesRegex(PipelineContractError, "planner_receipt_binding_mismatch"):
            build_coach_observation_event(changed, run_partial_planner(value), stream_id="demo-stream", sequence=1,
                                          host_received_ms=101, emitted_ms=102, expires_ms=902)

    def test_calls_partial_planner_and_only_observation_can_reach_coach(self):
        value = envelope(kind="replay")
        receipt = run_partial_planner(value)
        result = receipt["partial_result"]
        self.assertEqual(result["hint_state"], "observation_only")
        self.assertTrue(result["withdraw_previous_action"])
        self.assertIsNone(result["candidate_projection"])
        event = coach_event(value)["coach_event"]
        decode_event(event)
        self.assertEqual(event["source_kind"], "replay_candidate")
        self.assertEqual(event["source_sequence"], 17)
        self.assertEqual(event["evidence_status"], "machine_candidate")
        self.assertIsNone(event["advice"])
        self.assertIn("不提供建议", event["observation"]["unknown_zh"])
        synthetic_event = coach_event(envelope(kind="synthetic"))["coach_event"]
        self.assertEqual(synthetic_event["source_kind"], "synthetic")
        self.assertTrue(synthetic_event["observation"]["title_zh"].startswith("合成机器候选"))
        self.assertNotIn("回放", synthetic_event["observation"]["title_zh"])
        self.assertTrue(synthetic_event["context_version"].startswith("synthetic-bind-"))
        self.assertTrue(synthetic_event["evidence_id"].startswith("synthetic-"))

    def test_obviously_conflicting_visual_pts_fail_closed_without_recovering_precision(self):
        root_conflict = vision_fixture(); root_conflict["source_frame"]["decoded_pts_s"] = 99.0
        with self.assertRaisesRegex(PipelineContractError, "vision_source_binding_invalid"):
            envelope(root_conflict)
        population_conflict = vision_fixture(); population_conflict["population"]["source_frame"]["decoded_pts_s"] = 99.0
        with self.assertRaisesRegex(PipelineContractError, "vision_population_binding_invalid"):
            envelope(population_conflict)
        trait_conflict = vision_fixture(); trait_conflict["trait_rows"][0]["source_frame"]["decoded_pts_s"] = 99.0
        with self.assertRaisesRegex(PipelineContractError, "vision_trait_binding_invalid"):
            envelope(trait_conflict)

    def test_pts_float_tolerance_is_only_manifest_serialization_not_same_frame_identity(self):
        serialized = vision_fixture()
        for source_frame in (serialized["source_frame"], serialized["population"]["source_frame"],
                             serialized["trait_rows"][0]["source_frame"]):
            source_frame["decoded_pts_s"] = 12.345679
        self.assertEqual(envelope(serialized)["partial_request"]["field_capacity"]["current"], 3)
        outside = vision_fixture()
        for source_frame in (outside["source_frame"], outside["population"]["source_frame"],
                             outside["trait_rows"][0]["source_frame"]):
            source_frame["decoded_pts_s"] = 12.345680
        with self.assertRaisesRegex(PipelineContractError, "vision_source_binding_invalid"):
            envelope(outside)
        child_mismatch = vision_fixture()
        child_mismatch["population"]["source_frame"]["decoded_pts_s"] = 12.345679
        with self.assertRaisesRegex(PipelineContractError, "vision_population_binding_invalid"):
            envelope(child_mismatch)

    def test_binding_and_partial_mutation_are_rejected_before_25d_or_25e(self):
        value = envelope(kind="replay")
        receipt = run_partial_planner(value)
        for field, replacement in (("execution_eligible", True), ("strategy_verified", True), ("vision_accuracy", .999)):
            tampered = copy.deepcopy(value); tampered["replay_binding"][field] = replacement
            with self.assertRaisesRegex(PipelineContractError, "pipeline_binding_invalid"):
                build_coach_observation_event(tampered, receipt, stream_id="demo-stream", sequence=1,
                                              host_received_ms=101, emitted_ms=102, expires_ms=902)
        polluted = copy.deepcopy(value); polluted["partial_request"]["unexpected"] = []
        with self.assertRaisesRegex(PipelineContractError, "pipeline_binding_invalid"):
            run_partial_planner(polluted)

    def test_recomputed_nonsecret_digests_do_not_make_malformed_traits_safe(self):
        original = envelope(kind="replay")
        receipt = run_partial_planner(original)
        tampered = copy.deepcopy(original)
        tampered["partial_request"]["active_traits"] = [{
            "trait_id": [], "display_name": "合成标签", "count": 2, "next_threshold": 4,
        }]
        # An attacker can recompute both public hashes; neither is treated as
        # authentication.  Consumption must still fail in a controlled form.
        tampered["replay_binding"]["partial_request_sha256"] = _partial_digest(tampered["partial_request"])
        receipt["binding_digest"] = _binding_digest(tampered)
        with self.assertRaisesRegex(PipelineContractError, "partial_active_traits_invalid"):
            build_coach_observation_event(tampered, receipt, stream_id="demo-stream", sequence=1,
                                          host_received_ms=101, emitted_ms=102, expires_ms=902)

    def test_long_row_id_and_non_scalar_residue_are_controlled_failures(self):
        long_id = vision_fixture(); long_id["trait_rows"][0]["row_id"] = "r" * 53
        with self.assertRaisesRegex(PipelineContractError, "vision_trait_candidate_invalid"):
            envelope(long_id)
        invalid_empty_slots = vision_fixture(); invalid_empty_slots["population"]["empty_slots"] = []
        with self.assertRaisesRegex(PipelineContractError, "vision_population_candidate_invalid"):
            envelope(invalid_empty_slots)
        huge_pts = vision_fixture(); huge_pts["source_frame"]["decoded_pts_s"] = 10 ** 100
        with self.assertRaisesRegex(PipelineContractError, "vision_source_binding_invalid"):
            envelope(huge_pts)

    def test_long_seconds_text_is_preserved_but_25d_pts_becomes_unknown(self):
        precise = manifest(); precise["pts"] = {"kind": "seconds_text", "value": "12.3456789012"}
        value = envelope(binding=precise)
        self.assertEqual(value["replay_binding"]["pts"]["value"], "12.3456789012")
        self.assertEqual(value["partial_request"]["observation"]["pts_seconds"], {"status": "unknown", "value": None})
        self.assertEqual(coach_event(value)["coach_event"]["source_sequence"], 17)

    def test_25d_result_cannot_substitute_another_frames_facts(self):
        value = envelope(kind="replay")
        receipt = run_partial_planner(value)
        receipt["partial_result"]["field_capacity"] = {"status": "known", "current": 4, "maximum": 5}
        with self.assertRaisesRegex(PipelineContractError, "partial_result_binding_mismatch"):
            build_coach_observation_event(value, receipt, stream_id="demo-stream", sequence=1,
                                          host_received_ms=101, emitted_ms=102, expires_ms=902)

    def test_receipt_from_frame_a_cannot_feed_frame_b(self):
        first = envelope(kind="replay")
        receipt = run_partial_planner(first)
        changed = vision_fixture()
        for row in (changed["source_frame"], changed["population"]["source_frame"], changed["trait_rows"][0]["source_frame"]):
            row["encoded_sha256"] = "c" * 64
        second_manifest = manifest(); second_manifest["encoded_sha256"] = "c" * 64
        second = envelope(changed, kind="replay", binding=second_manifest)
        with self.assertRaisesRegex(PipelineContractError, "planner_receipt_binding_mismatch"):
            build_coach_observation_event(second, receipt, stream_id="demo-stream", sequence=1,
                                          host_received_ms=101, emitted_ms=102, expires_ms=902)

    def test_malformed_manifest_and_host_timing_fail_closed(self):
        broken = vision_fixture(); broken["trait_rows"] = "not-a-list"
        with self.assertRaisesRegex(PipelineContractError, "vision_source_binding_invalid"):
            envelope(broken)
        bad_pts = manifest(); bad_pts["pts"] = []
        with self.assertRaisesRegex(PipelineContractError, "manifest_pts_shape"):
            envelope(binding=bad_pts)
        bad_sequence = manifest(); bad_sequence["decode_sequence"] = []
        with self.assertRaisesRegex(PipelineContractError, "frame_manifest_invalid"):
            envelope(binding=bad_sequence)
        value = envelope(kind="replay")
        with self.assertRaisesRegex(PipelineContractError, "coach_event_input_invalid"):
            build_coach_observation_event(value, run_partial_planner(value), stream_id="demo-stream", sequence=1,
                                          host_received_ms=9, emitted_ms=10, expires_ms=10011)
        with self.assertRaises(PipelineContractError):
            build_partial_trait_request(vision_fixture(), source_kind=[], frame_manifest=manifest())
