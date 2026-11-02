from __future__ import annotations

import json
import unittest

from jcc_rapid_ocr_geometry import GeometryError, normalize_full_result


class GeometryContractTests(unittest.TestCase):
    def raw(self, *, text="31", score=0.99, box=None, **updates):
        if box is None:
            box = [[2, 3], [27, 3], [27, 16], [2, 16]]
        return {"result_type": "RapidOCROutput", "txts": [text], "scores": [score], "boxes": [box], **updates}

    def candidate(self, **updates):
        return normalize_full_result(crop_width=30, crop_height=20, raw=self.raw(**updates))

    def test_interior_box_is_shadow_candidate_only(self):
        output = self.candidate().to_dict()
        self.assertEqual((output["status"], output["reason"]),
                         ("development_candidate", "interior_detector_box_available"))
        self.assertEqual(output["text_box"], [[2.0, 3.0], [27.0, 3.0], [27.0, 16.0], [2.0, 16.0]])
        self.assertTrue(output["geometry_evidence"])
        self.assertEqual(output["geometry_scope"], "declared_crop_only")
        self.assertEqual(output["backend_identity"], "caller_declared_RapidOCROutput")
        self.assertIs(output["backend_identity_authenticated"], False)
        self.assertIs(output["input_frame_binding_authenticated"], False)
        self.assertEqual(output["visibility"], "unknown")
        for key in ("human_annotation", "eligible_for_geometry_gate", "eligible_for_policy", "eligible_for_execution"):
            self.assertIs(output[key], False)
        self.assertIsNone(output["accuracy"])
        json.dumps(output, allow_nan=False)

    def test_boundary_box_is_preserved_but_cannot_be_candidate(self):
        result = self.candidate(box=[[0, 2], [28, 2], [28, 17], [0, 17]])
        self.assertEqual((result.status, result.reason), ("unknown", "text_box_touches_crop_boundary"))
        self.assertEqual(result.text, "31")
        self.assertIsNotNone(result.text_box)
        self.assertTrue(result.to_dict()["geometry_evidence"])

    def test_subpixel_boundary_proximity_is_not_an_interior_box(self):
        for box in (
            [[0.1, 3], [27, 3], [27, 16], [2, 16]],
            [[2, 3], [28.9, 3], [27, 16], [2, 16]],
            [[2, 0.1], [27, 3], [27, 16], [2, 16]],
            [[2, 3], [27, 3], [27, 18.9], [2, 16]],
        ):
            with self.subTest(box=box):
                self.assertEqual(self.candidate(box=box).reason, "text_box_touches_crop_boundary")

    def test_does_not_select_a_line_from_multiple_detections(self):
        result = normalize_full_result(crop_width=30, crop_height=20, raw={
            "result_type": "RapidOCROutput", "txts": ["31", "2"], "scores": [0.2, 0.99],
            "boxes": [[[2, 2], [15, 2], [15, 12], [2, 12]], [[16, 2], [27, 2], [27, 12], [16, 12]]],
        })
        self.assertEqual((result.status, result.reason, result.raw_detection_count),
                         ("unknown", "ambiguous_detection_count", 2))

    def test_malformed_or_low_confidence_results_fail_closed(self):
        cases = [
            {"result_type": "TextRecOutput", "txts": ["31"], "scores": [0.99], "boxes": None},
            self.raw(txts=["31"], scores=[0.99], boxes=[]),
            self.raw(score=float("nan")),
            self.raw(box=[[2, 3], [27, 3], [27, 16], [27, 16]]),
            self.raw(box=[[2, 3], [31, 3], [27, 16], [2, 16]]),
            self.raw(box=[[2, 2], [26, 16], [4, 12], [28, 2]]),
            self.raw(box=[[10**400, 3], [27, 3], [27, 16], [2, 16]]),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                result = normalize_full_result(crop_width=30, crop_height=20, raw=raw)
                self.assertEqual(result.status, "unknown")
        self.assertEqual(self.candidate(score=0.49).reason, "low_raw_score")

    def test_adjacent_collinear_vertices_are_not_a_four_corner_extent(self):
        # Each example has non-zero total area, so the adjacent-vertex guard
        # rather than only a shoelace-area check must reject it. The final two
        # cases exercise triples spanning the final/first boundary.
        for box in (
            [[2, 2], [10, 2], [20, 2], [20, 15]],
            [[2, 2], [20, 2], [20, 10], [20, 15]],
            [[2, 2], [20, 2], [20, 15], [10, 2]],
            [[2, 2], [20, 2], [10, 2], [2, 15]],
        ):
            with self.subTest(box=box):
                self.assertEqual(self.candidate(box=box).reason, "invalid_text_box")

    def test_invalid_caller_contract_raises(self):
        with self.assertRaisesRegex(GeometryError, "invalid_crop_geometry"):
            normalize_full_result(crop_width=0, crop_height=20, raw=self.raw())
        with self.assertRaisesRegex(GeometryError, "invalid_minimum_score"):
            normalize_full_result(crop_width=30, crop_height=20, raw=self.raw(), minimum_score=float("nan"))


if __name__ == "__main__":
    unittest.main()
