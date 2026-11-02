"""Synthetic pixels/provenance only. No device, real asset, model or human label."""

from dataclasses import replace
import io
import json
import unittest
from unittest.mock import patch

from PIL import Image

from jcc_device.latest import Frame, FrameRef
from jcc_device.recognition import PixelROI
from jcc_device.roi_recognition import CANDIDATE, FieldROI, ImageGeometry, ROILayout
from jcc_device.scene_evidence import (
    ClockReading, CueSpec, FieldWitness, FrameWitness, LayoutKey, SceneLayout,
    assess_fields, bind_frame, probe_numbers, probe_scene, summarize_fields,
)


SIZE = (120, 80)
GEOMETRY = ImageGeometry(SIZE)
CLOCK = "synthetic-clock"
KEY = LayoutKey("synthetic-layout", GEOMETRY, "synthetic", "screenshot")
FULL = PixelROI("all-verifier-pixels", SIZE, (0, 0, *SIZE))
VALUE = (20, 20, 60, 50)
NAMES = ("round", "hp", "gold", "level")


def frame(*, size=SIZE, sequence=1, pixel=0, exif=None, fmt="PNG"):
    with Image.new("RGB", size, (3, 7, 11)) as image, io.BytesIO() as output:
        image.putpixel((21, 21), (pixel, 200, 202))
        kwargs = {}
        if exif is not None:
            metadata = Image.Exif()
            metadata[274] = exif
            kwargs["exif"] = metadata
        image.save(output, format=fmt, **kwargs)
        return Frame(FrameRef("synthetic-stream", sequence, 1000, "synthetic-utc"), output.getvalue())


def item(text="3-2", **kwargs):
    return dict(text=text, x=2, y=3, width=9, height=8, confidence=.9, **kwargs)


def text_item(text, **kwargs):
    data = item(text)
    data.update(kwargs)
    return data


class QueueOCR:
    def __init__(self, responses, *, error=None):
        self.responses = iter(responses)
        self.calls = []
        self.error = error

    def recognize(self, data):
        with Image.open(io.BytesIO(data)) as image:
            self.calls.append((image.format, image.size, image.getpixel((1, 1))))
        if self.error:
            raise self.error
        return next(self.responses)


def scene_layout(key=KEY, *, phase=False, blocker=False, scale=1):
    definitions = [("round_header", (5, 5, 45, 30), ()),
                   ("shop_experience", (5, 40, 55, 75), ("BUY XP",)),
                   ("shop_refresh", (65, 40, 115, 75), ("REFRESH",))]
    if phase:
        definitions += [("phase_preparation", (65, 5, 115, 30), ("PREP",)),
                        ("phase_combat", (45, 30, 80, 55), ("COMBAT",))]
    if blocker:
        definitions += [("blocker_dialog", (45, 30, 80, 55), ("SETTINGS",))]
    return SceneLayout(key, tuple(CueSpec(code, PixelROI(code, SIZE, box), texts, scale)
                                  for code, box, texts in definitions))


def scene(f=None, *, key=KEY, layout=None, texts=("3-2", "BUY XP", "REFRESH"), responses=None, ocr=None):
    f = frame() if f is None else f
    responses = [[text_item(text)] if text is not None else [] for text in texts] if responses is None else responses
    ocr = QueueOCR(responses) if ocr is None else ocr
    with patch("jcc_device.scene_evidence.time.monotonic_ns", return_value=1100):
        return probe_scene(f, key=key, host_clock_id=CLOCK, context_epoch=0,
                           layout=scene_layout(key) if layout is None else layout, ocr=ocr)


def number_layout(key=KEY, *, scale=1):
    return ROILayout(key.layout_id, key.geometry, key.sample_kind,
                     tuple(FieldROI(name, PixelROI(name, SIZE, VALUE), scale) for name in NAMES))


def numbers(f=None, *, key=KEY, texts=("3-2", "100", "42", "7"), layouts=None):
    f = frame() if f is None else f
    layouts = (number_layout(key),) if layouts is None else layouts
    ocr = QueueOCR([[text_item(t)] if t is not None else [] for t in texts])
    with patch("jcc_device.scene_evidence.time.monotonic_ns", return_value=1300):
        return probe_numbers(f, key=key, host_clock_id=CLOCK, context_epoch=0, layouts=layouts, ocr=ocr)


def witness(binding, *, review="synthetic", fields=None, coverage=(FULL,), **kwargs):
    fields = (tuple(FieldWitness(name, "global_round" if name == "round" else "self_" + name,
                                "clear", (PixelROI(name, SIZE, VALUE),)) for name in NAMES)
              if fields is None else fields)
    args = dict(scene="battle", phase="preparation", visibility="clear", review_status=review,
                verification_ref="synthetic-test-only-no-human", coverage_regions=coverage,
                fields=fields, completed_monotonic_ns=1500, expires_monotonic_ns=3000)
    args.update(kwargs)
    return FrameWitness(binding, **args)


def assess(s, n, w=None, *, current=None, now=2000, clock=CLOCK, max_age=4000):
    return assess_fields(s, n, current=s.binding if current is None else current,
                         now=ClockReading(clock, now), max_frame_age_ns=max_age, witness=w)


class SceneCueTests(unittest.TestCase):
    def test_round_and_two_shop_labels_only_make_battle_hypothesis(self):
        result = scene()
        self.assertEqual((result.scene.status, result.scene.value), (CANDIDATE, "battle"))
        self.assertEqual((result.phase.status, result.phase.value, result.phase.reason),
                         ("unknown", None, "explicit_phase_cue_missing"))

    def test_shop_during_combat_does_not_become_preparation(self):
        result = scene(layout=scene_layout(phase=True), texts=("3-2", "BUY XP", "REFRESH", None, "COMBAT"))
        self.assertEqual(result.phase.value, "combat")

    def test_explicit_preparation_label_remains_only_candidate(self):
        result = scene(layout=scene_layout(phase=True), texts=("3-2", "BUY XP", "REFRESH", "PREP", None))
        self.assertEqual((result.phase.status, result.phase.value), (CANDIDATE, "preparation"))
        self.assertEqual(result.summary()["recognized_count"], 0)

    def test_disagreeing_phase_cues_are_unknown(self):
        result = scene(layout=scene_layout(phase=True), texts=("3-2", "BUY XP", "REFRESH", "PREP", "COMBAT"))
        self.assertEqual((result.phase.value, result.phase.reason), (None, "phase_cue_conflict"))

    def test_numeric_string_alone_or_in_wrong_label_crop_is_not_scene(self):
        for texts in (("3-2", None, None), ("3-2", "4", "2"), ("32", "BUY XP", "REFRESH")):
            result = scene(texts=texts)
            self.assertEqual(result.scene.status, "unknown")
            self.assertIsNone(result.scene.value)

    def test_background_battle_labels_do_not_override_detected_dialog(self):
        result = scene(layout=scene_layout(blocker=True), texts=("3-2", "BUY XP", "REFRESH", "SETTINGS"))
        self.assertEqual((result.scene.value, result.scene.reason), (None, "blocking_cue_present"))

    def test_missing_blocker_is_not_proof_of_clear_visibility(self):
        result = scene(layout=scene_layout(blocker=True), texts=("3-2", "BUY XP", "REFRESH", None))
        self.assertEqual(result.scene.status, CANDIDATE)
        decisions = assess(result, numbers())
        self.assertTrue(all(d.reason == "frame_witness_missing" for d in decisions))
        self.assertFalse(hasattr(result, "unobscured"))

    def test_counterfeit_label_screen_still_cannot_authorize_fields(self):
        # These generated pixels + fake OCR have every positive label, but no
        # verified game context. This documents a deliberate classifier limit.
        result = scene()
        self.assertTrue(all(d.status == "unknown" and d.candidate_value is None
                            for d in assess(result, numbers())))

    def test_exact_labels_do_not_substring_match_or_repair(self):
        for label in ("BUY XP 4", "NOT BUY XP", "BUY  XP", "buy xp", "BUV XP"):
            result = scene(texts=("3-2", label, "REFRESH"))
            self.assertEqual(result.scene.status, "unknown")

    def test_crop_bytes_and_scale_inverse_map_are_exact(self):
        layout = SceneLayout(KEY, (CueSpec("round_header", PixelROI("a", SIZE, VALUE), scale=2),))
        ocr = QueueOCR([[text_item("3-2", x=2, y=4, width=10, height=8)]])
        result = scene(layout=layout, ocr=ocr)
        self.assertEqual(ocr.calls, [("PNG", (80, 60), (3, 7, 11))])
        self.assertEqual(result.cues[0].text_region, (21, 22, 26, 26))
        self.assertEqual(result.dependencies, (VALUE,))

    def test_no_text_multi_text_invalid_score_edge_and_backend_errors_abstain(self):
        cases = (([], "no_text"), ([text_item("3-2"), text_item("3-2")], "ambiguous_ocr_items"),
                 ([text_item("3-2", confidence=.49)], "low_raw_score"),
                 ([text_item("3-2", x=0)], "text_touches_crop_edge"),
                 ([text_item("3-2", x=-1)], "invalid_ocr_response"),
                 ([text_item("3-2", confidence=float("nan"))], "invalid_ocr_response"),
                 ([text_item("3-2", width=True)], "invalid_ocr_response"),
                 ([{"text": "3-2"}], "invalid_ocr_response"),
                 ([None], "invalid_ocr_response"), ({}, "invalid_ocr_response"))
        for response, reason in cases:
            with self.subTest(reason=reason):
                result = scene(responses=[response, [text_item("BUY XP")], [text_item("REFRESH")]])
                self.assertEqual(result.cues[0].reason, reason)
                self.assertEqual(result.scene.status, "unknown")
        result = scene(ocr=QueueOCR([], error=RuntimeError("private OCR content")))
        self.assertTrue(all(c.reason == "ocr_failed" for c in result.cues))
        self.assertNotIn("private OCR content", json.dumps(result.summary()))

    def test_wrong_geometry_corrupt_exif_or_unsupported_image_never_call_ocr(self):
        for f, reason in ((frame(size=(121, 80)), "image_geometry_mismatch"),
                          (replace(frame(), jpeg=b"not an image"), "image_unreadable"),
                          (frame(exif=6), "unapplied_exif_rotation"),
                          (frame(fmt="BMP"), "unsupported_image")):
            with self.subTest(reason=reason):
                ocr = QueueOCR([])
                result = scene(f, ocr=ocr)
                self.assertEqual(result.failure_reason, reason)
                self.assertEqual(ocr.calls, [])

    def test_same_size_different_capture_mode_origin_rotation_or_layout_is_rejected(self):
        for key in (replace(KEY, capture_kind="video_frame"), replace(KEY, sample_kind="live"),
                    replace(KEY, sample_kind="replay"), replace(KEY, mode_key="some-mode"),
                    replace(KEY, layout_id="other-layout"), replace(KEY, geometry=ImageGeometry(SIZE, 180))):
            with self.subTest(key=repr(key)):
                ocr = QueueOCR([])
                result = scene(key=key, layout=scene_layout(), ocr=ocr)
                self.assertEqual(result.failure_reason, "layout_mismatch")
                self.assertEqual(ocr.calls, [])

    def test_missing_configuration_and_method_stay_unknown(self):
        f = frame()
        result = probe_scene(f, key=KEY, host_clock_id=CLOCK, context_epoch=0, layout=None, ocr=None)
        self.assertEqual(result.failure_reason, "layout_unconfigured")
        result = probe_scene(f, key=KEY, host_clock_id=CLOCK, context_epoch=0, layout=scene_layout(), ocr=None)
        self.assertTrue(all(c.reason == "ocr_unavailable" for c in result.cues))

    def test_unknown_and_blocker_search_regions_are_kept_as_dependencies(self):
        result = scene(layout=scene_layout(blocker=True), texts=("3-2", "BUY XP", "REFRESH", None))
        self.assertEqual(len(result.dependencies), 4)
        self.assertIn((45, 30, 80, 55), result.dependencies)


class FieldEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.s = scene()
        self.n = numbers()
        self.w = witness(self.s.binding)

    def assert_unknown(self, decisions, reason):
        self.assertEqual(len(decisions), 4)
        self.assertTrue(all((d.status, d.candidate_value, d.reason) == ("unknown", None, reason) for d in decisions))

    def test_explicit_synthetic_context_yields_only_four_candidates(self):
        decisions = assess(self.s, self.n, self.w)
        self.assertEqual([d.field_name for d in decisions], list(NAMES))
        self.assertEqual([d.status for d in decisions], [CANDIDATE] * 4)
        self.assertEqual([d.candidate_value for d in decisions], [{"stage": 3, "step": 2}, 100, 42, 7])
        self.assertEqual(summarize_fields(decisions)["recognized_count"], 0)

    def test_missing_context_or_machine_guess_does_not_become_human_review(self):
        self.assert_unknown(assess(self.s, self.n), "frame_witness_missing")
        self.assert_unknown(assess(self.s, self.n, replace(self.w, review_status=CANDIDATE)), "frame_witness_unreviewed")
        self.assert_unknown(assess(self.s, self.n, replace(self.w, review_status="independently_reviewed")), "frame_witness_unreviewed")

    def test_real_branch_rejects_synthetic_and_agent_review_declarations(self):
        # Merely virtual replay metadata, never a real image or review result.
        key = replace(KEY, sample_kind="replay")
        s, n = scene(key=key), numbers(key=key)
        for review in ("synthetic", CANDIDATE):
            self.assert_unknown(assess(s, n, witness(s.binding, review=review)), "frame_witness_unreviewed")
        decisions = assess(s, n, witness(s.binding, review="independently_reviewed"))
        self.assertTrue(all(d.status == CANDIDATE for d in decisions))

    def test_each_full_source_binding_component_blocks_wrong_frame(self):
        source = self.s.binding.source
        changes = (replace(source, stream_id="other"), replace(source, sequence=2),
                   replace(source, host_received_monotonic_ns=1001),
                   replace(source, host_received_utc="other"),
                   replace(source, host_received_time_source="other"))
        bindings = [replace(self.s.binding, source=s) for s in changes]
        bindings += [replace(self.s.binding, host_clock_id="other"),
                     replace(self.s.binding, context_epoch=1),
                     replace(self.s.binding, layout_key=replace(KEY, mode_key="other")),
                     replace(self.s.binding, image_sha256="0" * 64)]
        for binding in bindings:
            with self.subTest(binding=repr(binding)):
                self.assert_unknown(assess(self.s, self.n, self.w, current=binding), "frame_binding_mismatch")

    def test_same_frameref_different_actual_pixels_cannot_share_evidence(self):
        n = numbers(frame(pixel=1))
        self.assertEqual(n.binding.source, self.s.binding.source)
        self.assertNotEqual(n.binding.image_sha256, self.s.binding.image_sha256)
        self.assert_unknown(assess(self.s, n, self.w), "frame_binding_mismatch")

    def test_static_review_cannot_authorize_next_frame_even_with_same_pixels(self):
        s, n = scene(frame(sequence=2)), numbers(frame(sequence=2))
        self.assertEqual(s.binding.image_sha256, self.s.binding.image_sha256)
        self.assert_unknown(assess(s, n, self.w), "witness_binding_mismatch")

    def test_other_clock_future_results_and_exact_age_deadline_are_unknown(self):
        self.assert_unknown(assess(self.s, self.n, self.w, clock="wrong"), "clock_domain_mismatch")
        self.assert_unknown(assess(self.s, self.n, self.w, now=1299), "invalid_processing_time")
        self.assert_unknown(assess(self.s, self.n, self.w, max_age=1000), "frame_expired")
        self.assertTrue(all(d.status == CANDIDATE for d in assess(self.s, self.n, self.w, max_age=1001)))

    def test_original_witness_deadline_is_not_renewed_by_reassessment(self):
        self.assertTrue(all(d.status == CANDIDATE for d in assess(self.s, self.n, self.w, now=2999)))
        self.assert_unknown(assess(self.s, self.n, self.w, now=3000), "evidence_expired")
        self.assert_unknown(assess(self.s, self.n, self.w, now=3100), "evidence_expired")

    def test_occluded_unknown_visibility_unknown_phase_and_future_witness_abstain(self):
        for w, reason in ((replace(self.w, visibility="occluded"), "occluded"),
                          (replace(self.w, visibility="unknown"), "visibility_unknown"),
                          (replace(self.w, scene=None), "battle_scene_unproven"),
                          (replace(self.w, phase=None), "phase_unproven_or_unsupported"),
                          (replace(self.w, phase="selection"), "phase_unproven_or_unsupported"),
                          (replace(self.w, completed_monotonic_ns=2100), "witness_from_future")):
            with self.subTest(reason=reason):
                self.assert_unknown(assess(self.s, self.n, w), reason)

    def test_phase_disagreement_or_conflict_is_not_settled_by_external_claim(self):
        s = scene(layout=scene_layout(phase=True), texts=("3-2", "BUY XP", "REFRESH", None, "COMBAT"))
        self.assert_unknown(assess(s, self.n, self.w), "phase_disagreement")
        s = scene(layout=scene_layout(phase=True), texts=("3-2", "BUY XP", "REFRESH", "PREP", "COMBAT"))
        self.assert_unknown(assess(s, self.n, self.w), "phase_cue_conflict")

    def test_opponent_observed_or_unknown_identity_is_not_self_hp(self):
        for role in ("opponent_hp", "observed_hp", "unknown", "self_gold"):
            fields = tuple(replace(f, role=role) if f.field_name == "hp" else f for f in self.w.fields)
            decisions = assess(self.s, self.n, replace(self.w, fields=fields))
            self.assertEqual((decisions[1].status, decisions[1].candidate_value, decisions[1].reason),
                             ("unknown", None, "field_subject_unproven"))
            self.assertEqual(decisions[2].status, CANDIDATE)

    def test_field_identity_visibility_and_complete_input_checked_independently(self):
        for change, reason in ((dict(visibility="occluded"), "field_occluded"),
                               (dict(visibility="unknown"), "field_visibility_unknown"),
                               (dict(regions=(PixelROI("only-text", SIZE, (21, 22, 30, 31)),)),
                                "field_input_coverage_incomplete")):
            fields = tuple(replace(f, **change) if f.field_name == "gold" else f for f in self.w.fields)
            decisions = assess(self.s, self.n, replace(self.w, fields=fields))
            self.assertEqual(decisions[2].reason, reason)
            self.assertIsNone(decisions[2].candidate_value)
            self.assertEqual(decisions[1].status, CANDIDATE)
        decisions = assess(self.s, self.n, replace(self.w, fields=self.w.fields[:-1]))
        self.assertEqual(decisions[-1].reason, "field_identity_missing")

    def test_complete_scene_input_union_allows_touching_rectangles_but_not_gap(self):
        coverage = (PixelROI("left", SIZE, (0, 0, 60, 80)), PixelROI("right", SIZE, (60, 0, 120, 80)))
        self.assertTrue(all(d.status == CANDIDATE for d in assess(self.s, self.n, replace(self.w, coverage_regions=coverage))))
        gap = (PixelROI("left", SIZE, (0, 0, 24, 80)), PixelROI("right", SIZE, (25, 0, 120, 80)))
        self.assert_unknown(assess(self.s, self.n, replace(self.w, coverage_regions=gap)), "scene_input_coverage_incomplete")

    def test_complete_field_input_union_rejects_one_pixel_gap(self):
        for second_left, expected in ((40, CANDIDATE), (41, "unknown")):
            regions = (PixelROI("a", SIZE, (20, 20, 40, 50)), PixelROI("b", SIZE, (second_left, 20, 60, 50)))
            fields = tuple(replace(f, regions=regions) if f.field_name == "gold" else f for f in self.w.fields)
            d = assess(self.s, self.n, replace(self.w, fields=fields))[2]
            self.assertEqual(d.status, expected)

    def test_external_whole_frame_look_is_not_shrunk_to_local_text_box(self):
        for d in assess(self.s, self.n, self.w):
            self.assertIn(FULL.box, d.input_regions)
            self.assertIn(VALUE, d.input_regions)
            self.assertIn((0, 0, 1, 1), d.normalized_regions)
            self.assertIn((20 / 120, 20 / 80, 60 / 120, 50 / 80), d.normalized_regions)

    def test_numeric_syntax_failure_is_not_filled_from_context(self):
        n = numbers(texts=("3-2", "100", "42/50", "7"))
        decisions = assess(self.s, n, self.w)
        self.assertEqual((decisions[2].status, decisions[2].candidate_value), ("unknown", None))
        self.assertEqual(decisions[1].status, CANDIDATE)

    def test_external_clear_claim_cannot_override_positive_blocker(self):
        s = scene(layout=scene_layout(blocker=True), texts=("3-2", "BUY XP", "REFRESH", "SETTINGS"))
        self.assert_unknown(assess(s, self.n, self.w), "battle_scene_unproven")

    def test_rebinding_existing_numeric_experiment_to_wrong_source_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(self.n, experiment=replace(self.n.experiment, source=replace(self.n.binding.source, sequence=2)))
        with self.assertRaises(ValueError):
            replace(self.n, experiment=replace(self.n.experiment, probes=self.n.experiment.probes[::-1]))

    def test_related_variants_disagree_without_selecting_largest_score(self):
        n = numbers(texts=("3-2", "100", "42", "7", "3-2", "100", "43", "7"),
                    layouts=(number_layout(scale=1), number_layout(scale=2)))
        decisions = assess(self.s, n, self.w)
        self.assertEqual((decisions[2].status, decisions[2].candidate_value, decisions[2].reason),
                         ("unknown", None, "variant_disagreement"))
        self.assertEqual(n.experiment.variant_count, 2)

    def test_private_values_references_hashes_and_raw_strings_not_in_summaries(self):
        s = scene(texts=("3-2", "a private player message", "REFRESH"))
        output = json.dumps({"scene": s.summary(), "fields": summarize_fields(assess(self.s, self.n, self.w))})
        for private in ("synthetic-stream", CLOCK, self.s.binding.image_sha256, "a private player message",
                        "synthetic-test-only-no-human", "candidate_value", "input_regions"):
            self.assertNotIn(private, output)
        self.assertNotIn("candidate_value=", repr(assess(self.s, self.n, self.w)))
        self.assertIsNone(s.summary()["accuracy"])

    def test_explicit_policy_and_invalid_configuration_fail_with_fixed_errors(self):
        for max_age in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                assess(self.s, self.n, self.w, max_age=max_age)
        with self.assertRaises(ValueError):
            SceneLayout(KEY, (scene_layout().cues[0], scene_layout().cues[0]))
        with self.assertRaises(ValueError):
            replace(self.w, fields=(self.w.fields[0], self.w.fields[0]))
        with self.assertRaises(ValueError):
            replace(self.w, expires_monotonic_ns=1500)
        with self.assertRaises(ValueError):
            numbers(key=replace(KEY, mode_key="different"), layouts=(replace(number_layout(), layout_id="wrong"),))
        with self.assertRaises(ValueError):
            huge_key = replace(KEY, geometry=ImageGeometry((2000, 2000)))
            SceneLayout(huge_key, (CueSpec("round_header", PixelROI("huge", (2000, 2000), (0, 0, 2000, 2000)), scale=4),))


if __name__ == "__main__":
    unittest.main()
