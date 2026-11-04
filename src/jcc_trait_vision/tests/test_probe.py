from __future__ import annotations

import hashlib
import io
import unittest

from PIL import Image

from jcc_capacity_probe import FrameEvidence, ProbeLayout
from jcc_trait_vision import (CANDIDATE, FieldEvidence, FrameRecord, PopulationInput,
                              TraitRowInput, TraitRowLayout, parse_trait_ratio, probe_trait_frame,
                              GrayDualRatioInput, GrayDualRatioLayout, probe_gray_dual_ratio)


class ScriptedOCR:
    def __init__(self, replies): self.replies = list(replies)
    def recognize(self, _payload):
        if not self.replies: raise AssertionError("unexpected OCR request")
        return self.replies.pop(0)


def item(text, score=0.9, x=2, y=2, width=10, height=8):
    return [{"text": text, "confidence": score, "x": x, "y": y, "width": width, "height": height}]


class TraitVisionTests(unittest.TestCase):
    def setUp(self):
        out = io.BytesIO(); Image.new("RGB", (200, 100), "white").save(out, format="PNG")
        self.image = out.getvalue(); self.sha = hashlib.sha256(self.image).hexdigest()
        self.frame = FrameRecord("case-25b", "frame-001", 7.5, "a" * 64)
        self.layout = TraitRowLayout("forest-row", (200, 100), (10, 10, 120, 80),
                                     (20, 15, 110, 40), (20, 45, 80, 70), "synthetic")

    def evidence(self, field_id, roi, **changes):
        value = dict(encoded_sha256=self.sha, field_id=field_id, roi=roi, image_size=(200, 100),
                     source_kind="synthetic", layout_matches=True, visibility="clear_candidate",
                     animation="not_observed_candidate", assessment_method="synthetic")
        value.update(changes); return FieldEvidence(**value)

    def row(self, **changes):
        value = dict(layout=self.layout,
            row_evidence=self.evidence("forest-row", self.layout.row_roi),
            label_evidence=self.evidence("forest-row-label", self.layout.label_roi),
            ratio_evidence=self.evidence("forest-row-ratio", self.layout.ratio_roi))
        value.update(changes); return TraitRowInput(**value)

    def probe(self, ocr, rows=None, population=None, expected=None):
        return probe_trait_frame(self.image, source_kind="synthetic", frame=self.frame,
            population=population, trait_rows=tuple(rows or [self.row()]), ocr=ocr,
            expected_sha256=expected or self.sha)

    def test_strict_ratio_parser(self):
        self.assertEqual(parse_trait_ratio("2/3"), (2, (3,)))
        self.assertEqual(parse_trait_ratio("2/4/6"), (2, (4, 6)))
        for text in ("2 / 3", "3/2", "2/6/4", "02/3", "x/3", "2/3/", "2/3/4/5"):
            self.assertIsNone(parse_trait_ratio(text), text)

    def test_bound_display_label_and_ratio_candidate(self):
        result = self.probe(ScriptedOCR([item("永恒之森"), item("永恒之森"), item("2/3"), item("2/3")]))
        row = result["trait_rows"][0]
        self.assertEqual(row["status"], CANDIDATE); self.assertEqual(row["display_label"], "永恒之森")
        self.assertEqual(row["observed_count"], 2); self.assertEqual(row["displayed_thresholds"], [3])
        self.assertEqual(row["trait_identity"], "unbound")
        self.assertEqual(row["source_frame"]["context_authority"], "caller_declared_unverified")
        self.assertIsNone(row["automatic_action"])

    def test_full_frame_scope_binds_only_interior_source_boxes(self):
        layout = TraitRowLayout("full-row", (200, 100), (10, 10, 120, 80),
            (20, 15, 110, 40), (20, 45, 80, 70), "synthetic", ocr_scope="full_frame")
        row = TraitRowInput(layout, self.evidence("full-row", layout.row_roi),
            self.evidence("full-row-label", layout.label_roi), self.evidence("full-row-ratio", layout.ratio_roi))
        replies = [
            [dict(item("永恒之森")[0], x=30, y=20)],
            [dict(item("永恒之森")[0], x=60, y=40)],
            [dict(item("2/3")[0], x=30, y=50)],
            [dict(item("2/3")[0], x=60, y=100)],
        ]
        result = self.probe(ScriptedOCR(replies), rows=[row])
        self.assertEqual(result["trait_rows"][0]["status"], CANDIDATE)

    def test_disagreement_and_edge_box_fail_closed(self):
        result = self.probe(ScriptedOCR([item("永恒之森"), item("永恒之林"), item("2/3"), item("2/3")]))
        self.assertEqual(result["trait_rows"][0]["status"], "unknown")
        self.assertIn("variant_disagreement", result["trait_rows"][0]["reason_codes"])
        result = self.probe(ScriptedOCR([item("永恒之森", x=0), item("永恒之森", x=0), item("2/3"), item("2/3")]))
        self.assertEqual(result["trait_rows"][0]["status"], "unknown")
        self.assertIn("variant_incomplete", result["trait_rows"][0]["reason_codes"])

    def test_source_and_occlusion_fail_closed(self):
        row = self.row(label_evidence=self.evidence("forest-row-label", self.layout.label_roi, visibility="occluded"))
        result = self.probe(ScriptedOCR([]), rows=[row]); self.assertIn("occluded", result["trait_rows"][0]["reason_codes"])
        result = self.probe(ScriptedOCR([]), expected="b" * 64)
        self.assertEqual(result["reason_codes"], ["source_hash_mismatch"])

    def test_population_reuses_capacity_probe(self):
        layout = ProbeLayout("population", (200, 100), (130, 10, 190, 40), "synthetic")
        evidence = FrameEvidence(self.sha, "population", "synthetic", (200, 100), 0, layout.roi,
                                 True, "clear_candidate", "not_observed_candidate", "synthetic")
        population = PopulationInput(layout, evidence, self.evidence("population", layout.roi))
        replies = [item("4/5"), item("4/5"), item("永恒之森"), item("永恒之森"), item("2/3"), item("2/3")]
        result = self.probe(ScriptedOCR(replies), population=population)
        self.assertEqual(result["population"]["status"], CANDIDATE); self.assertEqual(result["population"]["empty_slots"], 1)

    def test_invalid_geometry_and_nonfinite_box_do_not_pass(self):
        with self.assertRaises(ValueError):
            TraitRowLayout("bad-row", (200, 100), (10, 10, 120, 80), (5, 15, 110, 45), (20, 40, 80, 70), "synthetic")
        result = self.probe(ScriptedOCR([item("永恒之森", x=10**1000), item("永恒之森"), item("2/3"), item("2/3")]))
        self.assertEqual(result["trait_rows"][0]["status"], "unknown")
        self.assertIn("variant_incomplete", result["trait_rows"][0]["reason_codes"])

    def gray_input(self, **changes):
        layout = GrayDualRatioLayout("gray-ratio", (200, 100), (20, 45, 80, 70), (20, 47, 80, 72), "synthetic")
        value = dict(layout=layout,
            up_1px_evidence=self.evidence("gray-ratio-up-1px", layout.up_1px_roi),
            down_1px_evidence=self.evidence("gray-ratio-down-1px", layout.down_1px_roi))
        value.update(changes); return GrayDualRatioInput(**value)

    def gray_probe(self, replies, candidate=None):
        return probe_gray_dual_ratio(self.image, source_kind="synthetic", frame=self.frame,
            candidate=candidate or self.gray_input(), ocr=ScriptedOCR(replies), expected_sha256=self.sha)

    def test_gray_dual_roi_accepts_only_same_strict_ratio_and_overlapping_boxes(self):
        replies = [item("2/3", score=.3, x=10, y=10, width=60, height=20),
                   item("2/3", score=.3, x=10, y=6, width=60, height=20)]
        result = self.gray_probe(replies)
        self.assertEqual(result["status"], CANDIDATE)
        self.assertEqual(result["observed_count"], 2)
        self.assertEqual(result["displayed_thresholds"], [3])
        self.assertEqual(result["inverse_box_iou"], 1.0)
        self.assertEqual(result["evidence_status"], "machine_candidate")

    def test_gray_dual_roi_rejects_malformed_split_and_low_overlap(self):
        result = self.gray_probe([item("2./3", score=.3), item("2/3", score=.3)])
        self.assertEqual(result["status"], "unknown")
        self.assertIn("invalid_display_text", result["reason_codes"])
        result = self.gray_probe([[item("2", score=1)[0], item("3", score=1, x=30)[0]], item("2/3", score=.3)])
        self.assertEqual(result["status"], "unknown")
        self.assertIn("no_single_text_box", result["reason_codes"])
        result = self.gray_probe([item("2/3", score=.3, x=10, y=10, width=60, height=20),
                                  item("1/2", score=.3, x=10, y=6, width=60, height=20)])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason_codes"], ["view_disagreement"])
        result = self.gray_probe([item("2/3", score=.3, x=10, y=10, width=60, height=20),
                                  item("2/3", score=.3, x=50, y=6, width=40, height=20)])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason_codes"], ["inverse_box_iou_below_threshold"])

    def test_gray_dual_layout_requires_the_fixed_two_pixel_offset(self):
        with self.assertRaises(ValueError):
            GrayDualRatioLayout("bad-gray", (200, 100), (20, 45, 80, 70), (20, 46, 80, 71), "synthetic")
