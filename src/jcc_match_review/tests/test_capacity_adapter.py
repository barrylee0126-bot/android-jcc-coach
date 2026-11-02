"""Consumer checks beyond hashes, using fictional cached capacity evidence."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from jcc_match_review.adapters import capacity_evidence
from jcc_match_review.private import PinnedInputs, ReviewError, digest, write_new
from jcc_match_review.tests.test_boundaries import generated_image


class CapacityAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.raw = generated_image("green")
        self.sha = digest(self.raw)
        self.roi = [10, 10, 110, 60]
        self.image = self.root / "frame.png"
        write_new(self.image, self.raw)
        self.config = {"layout": {"layout_id": "generated-only", "image_size": [120, 80], "roi": self.roi, "scales": [1, 2]},
                       "frames": [{"expected_sha256": self.sha, "image_path": str(self.image), "evidence": {
                           "encoded_sha256": self.sha, "roi": self.roi, "image_size": [120, 80], "layout_id": "generated-only",
                           "layout_matches": True, "visibility": "clear_candidate", "animation": "not_observed_candidate",
                           "assessment_method": "agent_development_observation"}}]}
        self.record = {"schema_version": "capacity-probe-v1", "implementation_revision": "roi-bound-sanitized-variants-v2",
                       "source_frame": {"encoded_sha256": self.sha, "source_kind": "replay", "image_size": [120, 80], "rotation_applied_degrees": 0},
                       "input_roi": self.roi, "layout_id": "generated-only", "status": "development_candidate",
                       "displayed_deployed": 2, "capacity": 3, "empty_slots": 1, "reason_codes": [], "accuracy": None,
                       "automatic_action": None, "processing_seconds": 0.1,
                       "context": {"phase": "unknown", "subject": "unknown", "deployable_bench_unit": "unknown",
                                   "temporal_stability": "unverified_single_frame", "review_status": "unreviewed_machine_agent_candidate"},
                       "confidence": {"calibrated_probability": None, "basis": "same_apple_vision_backend_scale_comparison",
                                      "variants": [{"scale": scale, "status": "development_candidate", "reason": None,
                                          "raw_ocr_score": 0.9, "text_roi": [20, 20, 90, 40], "processing_seconds": 0.04} for scale in (1, 2)]}}
        self.spec = {"config_pin": "config", "report_pin": "report", "label": "容量测试", "selection": "虚构开发框"}

    def consume(self):
        config_bytes = json.dumps(self.config).encode()
        document = {"schema_version": "capacity-probe-v1", "implementation_revision": "roi-bound-sanitized-variants-v2",
                    "config_sha256": digest(config_bytes), "frame_count": 1, "results": [self.record]}
        report_bytes = json.dumps(document).encode()
        write_new(self.root / "config.json", config_bytes)
        write_new(self.root / "report.json", report_bytes)
        pins = {"image": {"path": str(self.image), "sha256": self.sha},
                "config": {"path": str(self.root / "config.json"), "sha256": digest(config_bytes)},
                "report": {"path": str(self.root / "report.json"), "sha256": digest(report_bytes)}}
        reader = PinnedInputs(self.root, pins, self.root / "out")
        return capacity_evidence(self.spec, reader, source_hash=self.sha, size=[120, 80], image_pin="image")

    def test_valid_candidate_does_not_export_hint_or_action(self):
        rows, regions, _ = self.consume()
        self.assertEqual(rows[0]["candidate"], "2/3（显示差值 1）")
        self.assertNotIn("reminder_zh", rows[0])
        self.assertNotIn("automatic_action", rows[0])
        self.assertEqual(regions[0]["roi"], self.roi)

    def test_tampered_success_cannot_override_occlusion(self):
        self.config["frames"][0]["evidence"]["visibility"] = "occluded"
        with self.assertRaisesRegex(ReviewError, "capacity_candidate_overrides_blocker"):
            self.consume()

    def test_wrong_source_or_roi_never_releases_candidate(self):
        self.record["source_frame"]["encoded_sha256"] = "b" * 64
        rows, _, _ = self.consume()
        self.assertIsNone(rows[0]["candidate"])
        self.assertIn("capacity_source_not_found", rows[0]["reason_codes"])

    def test_roi_declaration_mismatch_rejected(self):
        self.config["frames"][0]["evidence"]["roi"] = [11, 10, 110, 60]
        with self.assertRaisesRegex(ReviewError, "capacity_declaration_binding_mismatch"):
            self.consume()

    def test_unknown_stays_unknown_even_with_successful_variant(self):
        self.record.update(status="unknown", displayed_deployed=None, capacity=None, empty_slots=None, reason_codes=["occluded"])
        self.config["frames"][0]["evidence"]["visibility"] = "occluded"
        for variant in self.record["confidence"]["variants"]:
            variant.update(status="unknown", reason="discarded_due_to_unknown_result")
        rows, _, _ = self.consume()
        self.assertIsNone(rows[0]["candidate"])
        self.assertEqual(rows[0]["reason_codes"], ["occluded"])
        self.assertEqual(rows[1]["candidate"], "区域受遮挡")


if __name__ == "__main__":
    unittest.main()
