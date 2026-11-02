"""Synthetic parser, crop-provenance and abstention tests; no device or OCR model."""

from dataclasses import replace
import io
import json
import unittest

from PIL import Image

from jcc_device.latest import Frame, FrameRef
from jcc_device.recognition import PixelROI
from jcc_device.roi_recognition import (CANDIDATE, FieldROI, FrameSceneEvidence,
    ImageGeometry, ROILayout, interpret, parse_numeric, probe_frame, reconcile_variants)


SIZE = (120, 80)
GEOMETRY = ImageGeometry(SIZE)


def encoded_frame(*, size=SIZE, rotation=None, sequence=1):
    with Image.new("RGB", size, (3, 7, 11)) as image, io.BytesIO() as output:
        image.putpixel((21, 21), (200, 201, 202))
        kwargs = {}
        if rotation:
            exif = Image.Exif()
            exif[274] = rotation
            kwargs["exif"] = exif
        image.save(output, format="PNG", **kwargs)
        return Frame(FrameRef("synthetic-stream", sequence, 1000, "synthetic-utc"), output.getvalue())


def item(text="42", *, x=2, y=3, width=9, height=8, confidence=0.9):
    return dict(text=text, x=x, y=y, width=width, height=height, confidence=confidence)


class FakeOCR:
    def __init__(self, results=None, error=None):
        self.results = [item()] if results is None else results
        self.error = error
        self.calls = []

    def recognize(self, data):
        with Image.open(io.BytesIO(data)) as image:
            self.calls.append((image.format, image.size, image.getpixel((1, 1))))
        if self.error:
            raise self.error
        return self.results


def layout(*, name="gold", scale=1, kind="synthetic", geometry=GEOMETRY):
    config = FieldROI(name, PixelROI("synthetic-value", geometry.image_size, (20, 20, 60, 50)), scale)
    return ROILayout("synthetic-layout", geometry, kind, (config,))


class ParserTests(unittest.TestCase):
    def test_strict_values_follow_contract_without_guessed_game_limits(self):
        for name, text, expected in (("hp", "100", 100), ("hp", "-5", -5),
                ("hp", "1000", 1000), ("gold", "0", 0), ("gold", "-3", -3),
                ("gold", " 42\n", 42), ("level", "12", 12),
                ("round", "3-2", {"stage": 3, "step": 2})):
            with self.subTest(name=name, text=text):
                self.assertEqual(parse_numeric(name, text), expected)

    def test_counterexamples_do_not_repair_join_extract_or_guess(self):
        for name in ("hp", "gold", "level"):
            for text in ("O8", "l0", "42/50", "1.5", "50%", "1 2", "3级", "Lv.3",
                         "００", "08", "-0", "+5", "5 gold", "", "3\n4", "1" * 13):
                with self.subTest(name=name, text=text):
                    self.assertIsNone(parse_numeric(name, text))
        for text in ("0", "-1"):
            self.assertIsNone(parse_numeric("level", text))
        for text in ("3-0", "0-1", "3/2", "3—2", "3 - 2", "3-2-1", "32"):
            self.assertIsNone(parse_numeric("round", text))
        self.assertIsNone(parse_numeric("hp", True))

    def test_only_explicit_scalar_bounds_restrict_candidates(self):
        self.assertIsNone(parse_numeric("hp", "101", (0, 100)))
        self.assertEqual(parse_numeric("hp", "100", (0, 100)), 100)
        self.assertEqual(parse_numeric("hp", "101"), 101)

    def test_level_label_grammar_requires_explicit_field_configuration(self):
        self.assertIsNone(parse_numeric("level", "3级"))
        self.assertEqual(parse_numeric("level", "3级", syntax="level_label"), 3)
        for text in ("3", "3 级", "0级", "3级 4/6", "3级\n4级"):
            self.assertIsNone(parse_numeric("level", text, syntax="level_label"))
        with self.assertRaises(ValueError):
            parse_numeric("hp", "3级", syntax="level_label")


class CropTests(unittest.TestCase):
    def setUp(self):
        self.frame = encoded_frame()
        self.ocr = FakeOCR()

    def probe(self, config=None, **kwargs):
        return probe_frame(self.frame, geometry=GEOMETRY, sample_kind="synthetic",
                           layout=layout() if config is None else config,
                           ocr=self.ocr, **kwargs)

    def test_lossless_crop_uses_exact_pixels_and_maps_scaled_boxes_back(self):
        result = self.probe(layout(scale=2))
        probe = result.probes[2]
        self.assertEqual(self.ocr.calls, [("PNG", (80, 60), (3, 7, 11))])
        self.assertEqual(probe.status, CANDIDATE)
        self.assertEqual(probe.candidate_value, 42)
        self.assertEqual(probe.input_region, (20, 20, 60, 50))
        self.assertEqual(probe.text_region, (21, 21.5, 25.5, 25.5))
        self.assertEqual([p.reason for p in result.probes if p.field_name != "gold"],
                         ["field_unconfigured"] * 3)

    def test_nontext_input_pixel_is_in_full_dependency_region(self):
        result = self.probe()
        context = evidence(result)
        observation = interpret(result, context)[3]
        self.assertEqual(observation.input_regions[0], (20, 20, 60, 50))
        self.assertEqual(observation.normalized_regions[0], (20 / 120, 20 / 80, 60 / 120, 50 / 80))
        self.assertIn((0, 0, *SIZE), observation.input_regions)
        self.assertNotEqual(observation.input_regions[0], result.probes[2].text_region)

    def test_exact_dimensions_rotation_and_real_synthetic_layout_cannot_mix(self):
        for geometry, kind in ((ImageGeometry((80, 120)), "synthetic"),
                              (ImageGeometry(SIZE, 180), "synthetic"),
                              (GEOMETRY, "replay")):
            result = probe_frame(self.frame, geometry=geometry, sample_kind=kind,
                                 layout=layout(), ocr=self.ocr)
            self.assertEqual(result.failure_reason, "layout_mismatch")
            self.assertTrue(all(p.status == "unknown" for p in result.probes))
        self.assertEqual(self.ocr.calls, [])

    def test_wrong_pixels_corruption_and_unapplied_exif_do_not_call_ocr(self):
        for frame, reason in ((encoded_frame(size=(121, 80)), "image_geometry_mismatch"),
                              (encoded_frame(rotation=6), "unapplied_exif_rotation"),
                              (replace(self.frame, jpeg=b"not-image"), "image_unreadable")):
            result = probe_frame(frame, geometry=GEOMETRY, sample_kind="synthetic",
                                 layout=layout(), ocr=self.ocr)
            self.assertEqual(result.failure_reason, reason)
            self.assertTrue(all(p.status == "unknown" for p in interpret(result, evidence(result))))
        self.assertEqual(self.ocr.calls, [])

    def test_absent_configuration_or_backend_stays_unknown(self):
        result = probe_frame(self.frame, geometry=GEOMETRY, sample_kind="synthetic", layout=None, ocr=self.ocr)
        self.assertEqual(result.failure_reason, "layout_unconfigured")
        result = probe_frame(self.frame, geometry=GEOMETRY, sample_kind="synthetic", layout=layout(), ocr=None)
        self.assertEqual(result.probes[2].reason, "ocr_unavailable")
        self.assertEqual(self.ocr.calls, [])

    def test_ambiguous_invalid_low_score_and_clipped_results_abstain(self):
        cases = (([], "no_text"), ([item(), item("50")], "ambiguous_ocr_items"),
                 ([item("42/50")], "invalid_numeric_syntax_or_bounds"),
                 ([item(confidence=0.49)], "low_raw_score"),
                 ([item(x=0)], "text_touches_crop_edge"),
                 ([item(x=31)], "text_touches_crop_edge"),
                 ([item(x=-1)], "invalid_ocr_response"),
                 ([item(confidence=float("nan"))], "invalid_ocr_response"),
                 ([item(width=True)], "invalid_ocr_response"),
                 ([{"text": "42"}], "invalid_ocr_response"),
                 ([None], "invalid_ocr_response"), ({"results": []}, "invalid_ocr_response"))
        for results, reason in cases:
            with self.subTest(reason=reason, results=repr(results)):
                self.ocr.results = results
                probe = self.probe().probes[2]
                self.assertEqual((probe.status, probe.candidate_value, probe.reason), ("unknown", None, reason))

    def test_errors_raw_text_and_values_do_not_leak_in_sanitized_output(self):
        secret = "private OCR or account content"
        self.ocr.error = RuntimeError(secret)
        result = self.probe()
        self.assertEqual(result.probes[2].reason, "ocr_failed")
        self.assertNotIn(secret, json.dumps(result.summary()))
        self.ocr.error = None
        result = self.probe()
        summary = json.dumps(result.summary())
        self.assertNotIn("candidate_value", summary)
        self.assertNotIn("synthetic-stream", summary)
        self.assertNotIn("text_region", summary)
        self.assertIsNone(result.summary()["accuracy"])
        self.assertNotIn("candidate_value", repr(result))

    def test_all_fields_are_independent_and_processing_times_nonnegative(self):
        configs = tuple(FieldROI(name, PixelROI(name, SIZE, (20, 20, 60, 50)))
                        for name in ("round", "hp", "gold", "level"))
        config = ROILayout("synthetic-layout", GEOMETRY, "synthetic", configs)
        result = self.probe(config)
        self.assertEqual(result.probes[0].status, "unknown")
        self.assertEqual([p.status for p in result.probes[1:]], [CANDIDATE] * 3)
        self.assertEqual(len(self.ocr.calls), 4)
        self.assertGreaterEqual(result.processing_seconds, sum(p.processing_seconds for p in result.probes))


def evidence(result, **changes):
    return replace(FrameSceneEvidence(result.source, result.geometry, result.sample_kind,
        "synthetic-layout", "battle", True, "synthetic", "synthetic-verifier-only",
        (PixelROI("all-context-input", SIZE, (0, 0, *SIZE)),),
        ("round", "hp", "gold", "level")), **changes)


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.frame = encoded_frame()
        self.result = probe_frame(self.frame, geometry=GEOMETRY, sample_kind="synthetic",
                                  layout=layout(), ocr=FakeOCR())

    def assert_unknown(self, context, reason):
        fields = interpret(self.result, context)
        self.assertTrue(all(item.status == "unknown" and item.value is None for item in fields))
        self.assertEqual({item.reason for item in fields}, {reason})

    def test_missing_scene_hidden_or_unknown_visibility_stays_unknown(self):
        self.assert_unknown(None, "scene_evidence_missing")
        for changes, reason in (({"scene": None}, "scene_unknown"),
                                ({"unobscured": False}, "occluded"),
                                ({"unobscured": None}, "visibility_unknown")):
            self.assert_unknown(evidence(self.result, **changes), reason)

    def test_foreign_source_geometry_layout_sample_type_or_review_cannot_authorize(self):
        changes = ({"source": encoded_frame(sequence=2).source},
                   {"geometry": ImageGeometry(SIZE, 180)}, {"layout_id": "other"},
                   {"sample_kind": "replay"})
        for change in changes:
            self.assert_unknown(evidence(self.result, **change), "scene_evidence_mismatch")
        self.assert_unknown(evidence(self.result, review_status="independently_reviewed"), "scene_evidence_unreviewed")

    def test_real_scene_guesses_and_synthetic_evidence_never_authorize_real_fields(self):
        result = probe_frame(self.frame, geometry=GEOMETRY, sample_kind="replay",
                              layout=layout(kind="replay"), ocr=FakeOCR())
        for status in ("synthetic", CANDIDATE):
            fields = interpret(result, evidence(result, review_status=status))
            self.assertEqual({item.reason for item in fields}, {"scene_evidence_unreviewed"})
            self.assertTrue(all(item.value is None for item in fields))
        # Even an asserted independently reviewed context does not review OCR.
        fields = interpret(result, evidence(result, review_status="independently_reviewed"))
        self.assertEqual(fields[3].status, CANDIDATE)
        self.assertNotIn("recognized", [item.status for item in fields])

    def test_history_and_dialog_do_not_turn_visible_digits_into_battle_fields(self):
        for scene in ("history", "options_dialog", "other"):
            fields = interpret(self.result, evidence(self.result, scene=scene))
            self.assertEqual(fields[0].status, CANDIDATE)
            self.assertTrue(all(item.reason == "battle_scene_required" and item.value is None for item in fields[1:]))

    def test_battle_scene_alone_does_not_identify_player_gold_or_health(self):
        fields = interpret(self.result, evidence(self.result, verified_fields=()))
        self.assertEqual(fields[0].status, CANDIDATE)
        self.assertTrue(all(item.reason == "field_identity_unverified" and item.value is None for item in fields[1:]))


class VariantTests(unittest.TestCase):
    def setUp(self):
        self.frame = encoded_frame()
        self.first = self.probe(layout(), [item("42")])

    def probe(self, config, items):
        return probe_frame(self.frame, geometry=GEOMETRY, sample_kind="synthetic",
                           layout=config, ocr=FakeOCR(items))

    def test_agreement_stays_candidate_and_does_not_review_context(self):
        result = reconcile_variants((self.first, self.probe(layout(scale=2), [item("42")])))
        self.assertEqual(result.variant_count, 2)
        self.assertEqual((result.probes[2].status, result.probes[2].candidate_value), (CANDIDATE, 42))
        self.assertTrue(all(item.status == "unknown" for item in interpret(result)))

    def test_different_values_or_missing_variant_do_not_choose_highest_score(self):
        for items, reason in (([item("43", confidence=1)], "variant_disagreement"),
                              ([], "variant_incomplete")):
            result = reconcile_variants((self.first, self.probe(layout(scale=2), items)))
            self.assertEqual((result.probes[2].status, result.probes[2].candidate_value,
                              result.probes[2].reason), ("unknown", None, reason))

    def test_frame_or_crop_substitution_is_not_a_comparable_variant(self):
        for other, reason in ((replace(self.first, source=encoded_frame(sequence=2).source), "variant_source_mismatch"),
                              (replace(self.first, layout=replace(layout(), layout_id="other")), "variant_layout_mismatch"),
                              (replace(self.first, failure_reason="image_unreadable"), "variant_unusable")):
            result = reconcile_variants((self.first, other))
            self.assertEqual(result.failure_reason, reason)
            self.assertTrue(all(p.status == "unknown" and p.candidate_value is None for p in result.probes))


class ConfigurationTests(unittest.TestCase):
    def test_invalid_scales_scores_bounds_duplicate_fields_and_rois_rejected(self):
        roi = PixelROI("synthetic", SIZE, (20, 20, 60, 50))
        for changes in ({"scale": 0}, {"scale": 1.5}, {"scale": True}, {"scale": 5},
                        {"minimum_raw_score": float("nan")}, {"minimum_raw_score": True},
                        {"bounds": (100, 0)}, {"bounds": (0, True)}, {"field_name": "scene"},
                        {"field_name": "round", "bounds": (0, 10)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                FieldROI(**{"field_name": "gold", "roi": roi, **changes})
        config = FieldROI("gold", roi)
        with self.assertRaises(ValueError):
            ROILayout("synthetic", GEOMETRY, "synthetic", (config, config))
        with self.assertRaises(ValueError):
            FieldROI("gold", PixelROI("outside", SIZE, (20, 20, 121, 50)))

    def test_nested_input_coordinates_are_copied(self):
        box = [20, 20, 60, 50]
        config = FieldROI("gold", PixelROI("synthetic", list(SIZE), box))
        box[2] = 119
        self.assertEqual(config.roi.box, (20, 20, 60, 50))


if __name__ == "__main__":
    unittest.main()
