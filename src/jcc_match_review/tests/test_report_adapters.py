"""Final-report mismatches stay unknown/rejected even with self-consistent hashes."""

import json
from pathlib import Path
import unittest

from jcc_match_review.private import ReviewError, digest
from jcc_match_review.reports import read_20b_gold, read_20b_window


class MemoryPins:
    """Hashes are recomputed after a semantic mutation, not an integrity test."""
    root = Path("/private/generated-match-review")

    def __init__(self, docs, paths):
        self.docs, self.paths = docs, paths
        self.pins = {key: {"path": str(self.root / paths[key]), "sha256": digest(json.dumps(value).encode())}
                     for key, value in docs.items()}

    def path(self, key):
        return self.root / self.paths[key]

    def json(self, key):
        return self.docs[key]


class FinalAdapterTests(unittest.TestCase):
    sha = "a" * 64
    video = "b" * 64

    def inputs(self, *, gold=False, state="development_candidate", occluded=False):
        roi = [1, 2, 100, 60]
        source = {"encoded_sha256": self.sha, "source_kind": "replay", "image_size": [120, 80], "rotation_applied_degrees": 0}
        evidence = {**source, "roi": roi, "layout_id": "fictional", "assessment_method": "agent_development_observation",
                    "layout_matches": True, "visibility": "occluded" if occluded else "clear_candidate", "animation": "not_observed_candidate"}
        diag = [{"scale": s, "status": state, "reason": None if state == "development_candidate" else "low_raw_score",
                 "raw_ocr_score": 0.9 if state == "development_candidate" else 0.3, "text_roi": None, "processing_seconds": 0.1} for s in (1, 2)]
        trial = {"source_frame": source, "layout_id": "fictional", "input_roi": roi, "status": state,
                 "reason_codes": [] if state == "development_candidate" else ["low_raw_score"],
                 "implementation_revision": "roi-bound-sanitized-variants-v2",
                 "confidence": {"calibrated_probability": None, "basis": "same_apple_vision_backend_scale_comparison", "variants": diag}}
        frame = {"source_frame": source, "capacity": {"status": state, "trials": [trial], "reader_revision": "roi-bound-sanitized-variants-v2",
                  "displayed_deployed": 2 if state == "development_candidate" else None,
                  "capacity": 3 if state == "development_candidate" else None,
                  "empty_slots": 1 if state == "development_candidate" else None,
                  "reason_codes": trial["reason_codes"]},
                 "context": {key: "unknown" for key in ("subject", "phase", "rules", "deployable_bench_unit")},
                 "advice_eligible": False, "automatic_action": None, "accuracy": None}
        paths = {"image": "frames/test.png", "source": "source.json", "frames": "frames.json", "report": "run/results.private.json",
                 "config": "config.json", "index": "index.json", "completed": "run/COMPLETED.json", "artifact": "artifact.json"}
        docs = {"image": "synthetic bytes placeholder", "source": {"sha256": self.video},
                "frames": [{"sha256": self.sha, "file": paths["image"], "requested_time_s": 1, "decoded_pts_s": 1.01}],
                "config": {"frames": [{"expected_sha256": self.sha, "image_path": str(MemoryPins.root / paths["image"]),
                    "image_size": [120, 80], "capacity_trials": [{"layout": {"layout_id": "fictional", "roi": roi}, "evidence": evidence}]}]},
                "index": {"frames": [{"encoded_sha256": self.sha, "asset_path": str(MemoryPins.root / paths["image"]), "decoded_pts_s": 1.01}]}}
        doc_sha = lambda key: digest(json.dumps(docs[key]).encode())
        docs["report"] = {"schema_version": "open-slot-probe-v1", "implementation_revision": "offline-bound-appearance-v1",
                          "config_sha256": doc_sha("config"), "source_index_sha256": doc_sha("index"), "results": [frame]}
        if gold:
            docs["report"] = {"input_roi": roi, "results": [{"frame": {"source_sha256": self.sha, "requested_time_s": 1,
                "decoded_pts_s": 1.01, "capture_kind": "screenshot", "fields": [{"field": "gold", "candidate": 31}]},
                "cash_only": {"gold_after_hypothetical_purchase": 29}}]}
        docs["completed"] = {"status": "completed", "input_hashes_unchanged": True,
                             "file_sha256": {"results.private.json": doc_sha("report")},
                             "config_sha256": doc_sha("config"), "source_index_sha256": doc_sha("index")}
        docs["artifact"] = {"files": [{"path": paths[key], "sha256": doc_sha(key)} for key in docs]}
        reader = MemoryPins(docs, paths)
        spec = {"image_pin": "image", "source": {"source_pin": "source", "frame_index_pin": "frames", "report_pin": "report",
                "artifact_pin": "artifact"}}
        if not gold:
            spec["source"].update(config_pin="config", index_pin="index", completed_pin="completed")
        return reader, spec

    def test_gold_screenshot_misdeclaration_never_releases_numeric_or_economy(self):
        inputs, spec = self.inputs(gold=True)
        rows, _, _, _, _ = read_20b_gold(spec, inputs, self.sha, [120, 80], self.video)
        self.assertTrue(all(row["candidate"] is None for row in rows))
        self.assertIn("upstream_capture_kind_mismatch", rows[0]["reason_codes"])
        self.assertNotIn("29", json.dumps(rows, ensure_ascii=False))
        self.assertNotIn("31", json.dumps(rows, ensure_ascii=False))
        self.assertTrue(all("cash_only" not in row and "gold_after_hypothetical_purchase" not in row for row in rows))

    def test_three_observations_do_not_carry_capacity_through_unknown(self):
        results = []
        for state in ("development_candidate", "unknown", "development_candidate"):
            inputs, spec = self.inputs(state=state)
            rows, _, _, _, _ = read_20b_window(spec, inputs, self.sha, [120, 80], self.video)
            results.append(rows[0]["candidate"])
        self.assertEqual(results, ["2/3（显示差值 1）", None, "2/3（显示差值 1）"])

    def test_rehashed_success_with_occlusion_is_rejected(self):
        inputs, spec = self.inputs(occluded=True)
        with self.assertRaisesRegex(ReviewError, "20b_candidate_overrides_blocker"):
            read_20b_window(spec, inputs, self.sha, [120, 80], self.video)

    def test_result_for_another_frame_is_rejected(self):
        inputs, spec = self.inputs(gold=True)
        with self.assertRaisesRegex(ReviewError, "20b_frame_binding_mismatch"):
            read_20b_gold(spec, inputs, "e" * 64, [120, 80], self.video)


if __name__ == "__main__":
    unittest.main()
