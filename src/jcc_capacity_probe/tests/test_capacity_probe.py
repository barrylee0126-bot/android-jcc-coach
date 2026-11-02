from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from jcc_capacity_probe import FrameEvidence, ProbeLayout, parse_capacity_ratio, probe_capacity
from jcc_capacity_probe.__main__ import _private_json, run_config


def png(size=(200, 100), *, exif_orientation=None):
    with Image.new("RGB", size, (17, 32, 51)) as im, io.BytesIO() as stream:
        args = {}
        if exif_orientation is not None:
            exif = Image.Exif()
            exif[274] = exif_orientation
            args["exif"] = exif
        im.save(stream, format="PNG", **args)
        return stream.getvalue()


class ScriptedOCR:
    def __init__(self, texts=("2/3", "2/3"), *, scores=(0.9, 0.9), override=None, error=None):
        self.texts = texts
        self.scores = scores
        self.override = override
        self.error = error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def recognize(self, payload):
        with Image.open(io.BytesIO(payload)) as image:
            self.calls.append((image.size, image.getpixel((0, 0))))
        i = len(self.calls) - 1
        if self.error:
            raise self.error
        if self.override is not None:
            return self.override
        scale = i + 1
        return [{"text": self.texts[i], "confidence": self.scores[i],
                 "x": 10 * scale, "y": 8 * scale, "width": 35 * scale, "height": 20 * scale}]


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.payload = png()
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.layout = ProbeLayout("synthetic-layout", (200, 100), (20, 10, 160, 80), "synthetic")
        self.evidence = FrameEvidence(self.sha, self.layout.layout_id, "synthetic", (200, 100), 0,
                                      self.layout.roi, True, "clear_candidate", "not_observed_candidate", "synthetic")

    def probe(self, *, layout=None, evidence=None, ocr=None, payload=None, **kwargs):
        return probe_capacity(self.payload if payload is None else payload, source_kind="synthetic",
                              layout=self.layout if layout is None else layout,
                              evidence=self.evidence if evidence is None else evidence,
                              ocr=ScriptedOCR() if ocr is None else ocr, **kwargs)

    def assert_unknown(self, result, reason):
        self.assertEqual(result.status, "unknown")
        self.assertIn(reason, result.reasons)
        self.assertEqual((result.displayed_deployed, result.capacity, result.empty_slots), (None, None, None))
        output = result.to_dict()
        self.assertIsNone(output["automatic_action"])
        self.assertIsNone(output["accuracy"])

    def test_strict_ratio_and_resource_limits(self):
        for text, expected in (("2/3", (2, 3)), ("0/3", (0, 3)), ("3/3", (3, 3)),
                               ("10/11", (10, 11)), ("999998/999999", (999998, 999999)),
                               (" \t2/3\n", (2, 3))):
            with self.subTest(text=text):
                self.assertEqual(parse_capacity_ratio(text), expected)

    def test_missing_limit_infinity_invalid_numbers_and_extra_text(self):
        invalid = ("2", "2/", "/3", "2/∞", "2/无上限", "2/-1", "2/0", "4/3", "03/4", "3/04",
                   "-1/3", "2.0/3", "２/３", "2 /3", "2/ 3", "x2/3", "2/3 HP", "2/3/4",
                   "2/1000000", "O/3", "l/3", "", "\u00a02/3", None)
        for text in invalid:
            with self.subTest(text=text):
                self.assertIsNone(parse_capacity_ratio(text))

    def test_positive_vacancy_is_only_a_conditional_candidate(self):
        result = self.probe()
        self.assertEqual(result.status, "development_candidate")
        self.assertEqual((result.displayed_deployed, result.capacity, result.empty_slots), (2, 3, 1))
        self.assertIn("若为己方备战且备战席有可用棋子", result.hint)
        output = result.to_dict()
        self.assertTrue(all(output["context"][k] == "unknown" for k in ("phase", "subject", "deployable_bench_unit")))
        self.assertIsNone(output["automatic_action"])
        self.assertIsNone(output["confidence"]["calibrated_probability"])
        self.assertNotIn("2/3", json.dumps(output))

    def test_explicit_full_and_zero_used_are_valid_candidates(self):
        for text, expected in (("3/3", (3, 3, 0)), ("0/3", (0, 3, 3))):
            result = self.probe(ocr=ScriptedOCR((text, text)))
            self.assertEqual((result.displayed_deployed, result.capacity, result.empty_slots), expected)

    def test_crop_pixels_sizes_and_inverse_mapping(self):
        ocr = ScriptedOCR()
        result = self.probe(ocr=ocr)
        self.assertEqual(ocr.calls, [((140, 70), (17, 32, 51)), ((280, 140), (17, 32, 51))])
        self.assertEqual(result.input_roi, (20, 10, 160, 80))
        self.assertEqual(result.variants[0].text_roi, (30, 18, 65, 38))
        self.assertEqual(result.variants[0].text_roi, result.variants[1].text_roi)
        self.assertEqual(result.to_dict()["source_frame"]["anonymous_ref"], "sha256:" + self.sha)

    def test_missing_evidence_abstains_without_ocr(self):
        ocr = ScriptedOCR()
        result = probe_capacity(self.payload, source_kind="synthetic", layout=self.layout, ocr=ocr)
        self.assert_unknown(result, "frame_evidence_missing")
        self.assertEqual(ocr.calls, [])

    def test_unconfigured_layout_abstains(self):
        self.assert_unknown(probe_capacity(self.payload, source_kind="synthetic"), "layout_unconfigured")

    def test_occlusion_and_animation_override_even_perfect_ocr(self):
        for changes, reason in (({"visibility": "occluded"}, "occluded"),
                                ({"animation": "observed"}, "animation_observed"),
                                ({"visibility": "unknown"}, "visibility_unknown"),
                                ({"animation": "unknown"}, "animation_unknown"),
                                ({"layout_matches": False}, "layout_mismatch"),
                                ({"layout_matches": None}, "layout_identity_unknown")):
            with self.subTest(changes=changes):
                ocr = ScriptedOCR(scores=(1, 1))
                result = self.probe(evidence=replace(self.evidence, **changes), ocr=ocr)
                self.assert_unknown(result, reason)
                self.assertEqual(ocr.calls, [])

    def test_frame_layout_rotation_and_geometry_evidence_are_bound(self):
        variants = ({"encoded_sha256": "0" * 64}, {"layout_id": "other"},
                    {"source_kind": "replay"}, {"image_size": (199, 100)},
                    {"rotation_applied_degrees": 90})
        for changes in variants:
            with self.subTest(changes=changes):
                self.assert_unknown(self.probe(evidence=replace(self.evidence, **changes)), "frame_evidence_mismatch")

    def test_same_frame_same_layout_id_changed_roi_requires_new_evidence(self):
        # Same frame, layout name, dimensions and rotation; only the complete
        # OCR input ROI changes. A's visibility assertion must not authorize B.
        for roi in ((40, 10, 180, 80), (20, 10, 150, 80), (20, 15, 160, 85)):
            with self.subTest(roi=roi):
                ocr = ScriptedOCR(scores=(1, 1))
                result = self.probe(layout=replace(self.layout, roi=roi), ocr=ocr)
                self.assert_unknown(result, "frame_evidence_roi_mismatch")
                self.assertEqual(ocr.calls, [])

    def test_exact_evidence_roi_required_and_validated(self):
        from dataclasses import asdict

        legacy = asdict(self.evidence)
        legacy.pop("roi")
        with self.assertRaises(TypeError):
            FrameEvidence(**legacy)
        for roi in ((0, 0, 201, 100), (20, 10, 20, 80), (True, 10, 160, 80), (20, 10, 160)):
            with self.subTest(roi=roi), self.assertRaises(ValueError):
                replace(self.evidence, roi=roi)
        from_json = replace(self.evidence, roi=list(self.layout.roi))
        self.assertEqual(from_json.roi, self.layout.roi)
        self.assertEqual(self.probe(evidence=from_json).status, "development_candidate")

    def test_synthetic_evidence_cannot_authorize_replay_pixels(self):
        layout = replace(self.layout, source_kind="replay")
        evidence = replace(self.evidence, source_kind="replay")
        result = probe_capacity(self.payload, source_kind="replay", layout=layout, evidence=evidence, ocr=ScriptedOCR())
        self.assert_unknown(result, "evidence_authority_mismatch")

    def test_replay_evidence_stays_unreviewed(self):
        layout = replace(self.layout, source_kind="replay")
        evidence = replace(self.evidence, source_kind="replay", assessment_method="agent_development_observation")
        result = probe_capacity(self.payload, source_kind="replay", layout=layout, evidence=evidence, ocr=ScriptedOCR())
        self.assertEqual(result.status, "development_candidate")
        self.assertEqual(result.to_dict()["context"]["review_status"], "unreviewed_machine_agent_candidate")

    def test_layout_source_and_actual_dimensions_mismatch(self):
        self.assert_unknown(self.probe(layout=replace(self.layout, source_kind="replay")), "layout_mismatch")
        self.assert_unknown(self.probe(payload=png((201, 100))), "image_geometry_mismatch")

    def test_hash_pin_failure_does_not_use_new_pixels(self):
        ocr = ScriptedOCR()
        result = self.probe(expected_sha256="0" * 64, ocr=ocr)
        self.assert_unknown(result, "source_hash_mismatch")
        self.assertEqual(ocr.calls, [])

    def test_unapplied_exif_and_corrupt_image_abstain(self):
        self.assert_unknown(self.probe(payload=png(exif_orientation=6)), "unapplied_exif_rotation")
        self.assert_unknown(self.probe(payload=b"not an image"), "image_unreadable")
        self.assert_unknown(self.probe(payload=b""), "image_unreadable_or_too_large")

    def test_no_text_never_means_full_or_zero(self):
        self.assert_unknown(self.probe(ocr=ScriptedOCR(override=[])), "no_text")

    def test_missing_denominator_and_invalid_ratio_never_repaired(self):
        for text in ("2/", "3", "2/∞", "2/0", "4/3", "2/3 HP"):
            with self.subTest(text=text):
                self.assert_unknown(self.probe(ocr=ScriptedOCR((text, text))), "incomplete_or_invalid_ratio")

    def test_one_failed_scale_cannot_be_filled_from_the_other(self):
        self.assert_unknown(self.probe(ocr=ScriptedOCR(("2/3", "2/"))), "variant_incomplete")

    def test_conflicting_scales_abstain_without_exported_alternatives(self):
        result = self.probe(ocr=ScriptedOCR(("2/3", "3/3")))
        self.assert_unknown(result, "variant_disagreement")
        self.assertNotIn("pair", json.dumps(result.to_dict()))
        self.assertTrue(all("displayed_deployed" not in v for v in result.to_dict()["confidence"]["variants"]))

    def test_unknown_public_object_and_repr_never_expose_variant_pairs(self):
        from dataclasses import asdict

        for texts in (("2/3", "3/3"), ("2/3", "2/")):
            with self.subTest(texts=texts):
                result = self.probe(ocr=ScriptedOCR(texts))
                self.assertEqual(result.status, "unknown")
                self.assertTrue(all(not hasattr(v, "pair") for v in result.variants))
                self.assertTrue(all(v.status == "unknown" for v in result.variants))
                self.assertNotIn("pair", json.dumps(asdict(result)))
                self.assertNotIn("pair=", repr(result))
                self.assertNotIn("(2, 3)", repr(result))
                self.assertNotIn("(3, 3)", repr(result))

    def test_raw_scores_are_filters_not_probabilities(self):
        self.assert_unknown(self.probe(ocr=ScriptedOCR(scores=(0.49, 1))), "low_raw_score")
        self.assertEqual(self.probe(ocr=ScriptedOCR(scores=(0.5, 0.5))).status, "development_candidate")

    def test_multiple_boxes_are_not_joined_or_selected(self):
        one = {"text": "2/3", "confidence": 1, "x": 1, "y": 1, "width": 40, "height": 20}
        self.assert_unknown(self.probe(ocr=ScriptedOCR(override=[one, one])), "multiple_text_boxes")

    def test_ocr_malformed_scores_and_boxes(self):
        template = {"text": "2/3", "confidence": 1, "x": 10, "y": 10, "width": 35, "height": 20}
        for changes, reason in (({"confidence": float("nan")}, "invalid_ocr_response"),
                                ({"confidence": True}, "invalid_ocr_response"),
                                ({"confidence": 2}, "invalid_ocr_response"),
                                ({"width": -1}, "invalid_ocr_box"),
                                ({"width": 5000}, "invalid_ocr_box"),
                                ({"x": 0}, "text_touches_roi_edge")):
            with self.subTest(changes=changes):
                self.assert_unknown(self.probe(ocr=ScriptedOCR(override=[dict(template, **changes)])), reason)
        for response in ({"results": []}, [None], [{}]):
            self.assert_unknown(self.probe(ocr=ScriptedOCR(override=response)), "invalid_ocr_response")

    def test_backend_error_text_is_not_exposed(self):
        result = self.probe(ocr=ScriptedOCR(error=RuntimeError("PRIVATE_OCR_OR_PATH")))
        self.assert_unknown(result, "ocr_failed")
        self.assertNotIn("PRIVATE", json.dumps(result.to_dict()))

    def test_no_backend_is_unknown(self):
        result = probe_capacity(self.payload, source_kind="synthetic", layout=self.layout, evidence=self.evidence)
        self.assert_unknown(result, "ocr_unavailable")

    def test_bad_configuration_is_rejected(self):
        for changes in ({"roi": (0, 0, 201, 100)}, {"image_size": (True, 100)},
                        {"scales": (1,)}, {"scales": (1, 1)}, {"scales": (True, 2)},
                        {"minimum_raw_score": float("nan")}, {"rotation_applied_degrees": 45}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.layout, **changes)

    def test_same_bytes_match_and_new_frame_does_not_reuse_result(self):
        first = self.probe()
        second = self.probe(payload=png((199, 100)))
        self.assertNotEqual(first.encoded_sha256, second.encoded_sha256)
        self.assert_unknown(second, "image_geometry_mismatch")

    def test_explicit_offline_source_kind_only(self):
        with self.assertRaises(ValueError):
            probe_capacity(self.payload, source_kind="live")


class CLITests(unittest.TestCase):
    def test_hash_pinned_allowlist_and_private_output_preserve_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            payload = png()
            sha = hashlib.sha256(payload).hexdigest()
            image = root / "synthetic.png"
            image.write_bytes(payload)
            config = {
                "schema_version": "capacity-probe-v1",
                "layout": {"layout_id": "synthetic-cli", "image_size": [200, 100],
                           "roi": [20, 10, 160, 80], "source_kind": "synthetic"},
                "frames": [{"image_path": str(image), "expected_sha256": sha,
                            "evidence": {"encoded_sha256": sha, "layout_id": "synthetic-cli",
                                         "source_kind": "synthetic", "image_size": [200, 100],
                                         "rotation_applied_degrees": 0, "roi": [20, 10, 160, 80], "layout_matches": True,
                                         "visibility": "clear_candidate", "animation": "not_observed_candidate",
                                         "assessment_method": "synthetic"}}],
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            before = config_path.read_bytes()
            output = root / "result.json"
            with patch("jcc_device.vision_ocr.VisionOCR", return_value=ScriptedOCR()):
                report = run_config(config_path, report_path=output)
            self.assertEqual(report["candidate_count"], 1)
            self.assertEqual(report["implementation_revision"], "roi-bound-sanitized-variants-v2")
            self.assertEqual(json.loads(output.read_text())["results"][0]["empty_slots"], 1)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config_path.read_bytes(), before)
            self.assertEqual(image.read_bytes(), payload)
            self.assertNotIn(str(image), json.dumps(report))
            self.assertEqual(report["human_ground_truth_count"], 0)

    def test_report_cannot_overwrite_input_or_enter_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "config.json"
            target.write_text("original")
            with self.assertRaises(ValueError):
                _private_json(target, {}, inputs={target.resolve()})
            self.assertEqual(target.read_text(), "original")
        with self.assertRaises(ValueError):
            _private_json(Path(__file__).resolve().parents[1] / "must-not-write.json", {}, inputs=set())

    def test_report_parent_must_be_private(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            os.chmod(root, 0o755)
            with self.assertRaises(ValueError):
                _private_json(root / "report.json", {}, inputs=set())


if __name__ == "__main__":
    unittest.main()
