"""Generated videos and OCR replies only; no real screenshots or game values."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch

from PIL import Image

from jcc_video_economy import AdapterError, analyze_index
from jcc_device.vision_ocr import VisionOCR
from jcc_video_economy.adapter import (
    APPLE_VISION_SOURCE, APPLE_VISION_WORKER, INDEX_SCHEMA, PROJECT, SEEK_METHOD, _BoundOCR, _decode_frame,
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeOCR:
    def __init__(self, replies=("31", "31"), callback=None):
        self.replies = list(replies)
        self.callback = callback
        self.requests = 0

    def recognize(self, data):
        self.requests += 1
        if self.callback:
            self.callback(self.requests)
        reply = self.replies.pop(0)
        if reply is None:
            return []
        if reply == "raise":
            raise RuntimeError("private raw OCR exception must not escape")
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
        return [{"text": reply, "confidence": .95, "x": 2, "y": 2,
                 "width": min(10, width - 4), "height": min(10, height - 4)}]


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is needed for the generated video integration")
class VideoEconomyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.common = tempfile.TemporaryDirectory(prefix="synthetic-video-economy-")
        root = Path(cls.common.name)
        Image.new("RGB", (64, 48), (20, 20, 20)).save(root / "0.png")
        Image.new("RGB", (64, 48), (180, 180, 180)).save(root / "1.png")
        cls.video = root / "synthetic.mp4"
        result = subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-framerate", "1", "-i", str(root / "%d.png"),
            "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(cls.video),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode:
            cls.common.cleanup()
            raise unittest.SkipTest("FFmpeg cannot encode the generated test video")
        with cls.video.open("rb") as video:
            cls.frame_data = _decode_frame(video.fileno(), 0)
        cls.video_hash = sha(cls.video.read_bytes())

    @classmethod
    def tearDownClass(cls):
        cls.common.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-video-index-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video_copy = self.root / "synthetic.mp4"
        shutil.copyfile(self.video, self.video_copy)
        self.frame = self.root / "frame.png"
        self.frame.write_bytes(self.frame_data)
        self.index_path = self.root / "index.json"
        self.index = {
            "schema_version": INDEX_SCHEMA, "source_kind": "replay", "capture_kind": "video_frame",
            "video": {"path": str(self.video_copy), "sha256": self.video_hash},
            "frame": {"path": str(self.frame), "sha256": sha(self.frame_data), "seek_target_ms": 0,
                      "seek_method": SEEK_METHOD, "image_size": [64, 48]},
            "roi": {"field": "gold_numeric_candidate", "box": [8, 8, 48, 32]},
            "agent_visual": {"observer": "agent_visual_unreviewed", "status": "candidate_clear", "candidate": 31},
        }

    def write_index(self):
        data = json.dumps(self.index, sort_keys=True, separators=(",", ":")).encode()
        self.index_path.write_bytes(data)
        return sha(data)

    def analyze(self, *, price=2, ocr=None):
        return analyze_index(self.index_path, self.write_index(), hypothetical_spend=price, ocr=ocr or FakeOCR())

    def assert_blocked_arithmetic(self, result):
        self.assertEqual(result["status"], "blocked")
        row = result["economy"]["comparisons"][0]
        self.assertIsNone(row["gold_after_hypothetical_purchase"])
        self.assertIsNone(row["interest_before"])
        self.assertIsNone(row["interest_after"])
        self.assertIsNone(result["human_ground_truth"])
        self.assertIsNone(result["action"])
        self.assertFalse(result["production_eligible"])
        self.assertFalse(result["execution_eligible"])

    def test_generated_video_frame_to_cash_only_comparison(self):
        result = self.analyze()
        self.assertEqual(result["status"], "cash_only")
        self.assertEqual(result["capture_kind"], "video_frame")
        self.assertTrue(result["binding"]["decoded_pixels_match"])
        self.assertEqual(result["binding"]["seek_target_ms"], 0)
        self.assertIsNone(result["binding"]["actual_frame_pts"])
        self.assertEqual(result["binding"]["frame_sha256"], sha(self.frame_data))
        self.assertEqual(result["agent_visual"], {"status": "unreviewed_candidate", "value": 31})
        self.assertEqual(result["machine_ocr"]["value"], 31)
        self.assertFalse(result["machine_ocr"]["worker_fingerprint_checked"])
        self.assertNotIn(APPLE_VISION_WORKER, result["dependency_sha256"])
        self.assertNotIn(APPLE_VISION_SOURCE, result["dependency_sha256"])
        self.assertFalse(result["machine_ocr"]["agreement_is_accuracy"])
        self.assertEqual(len(result["machine_ocr"]["request_response_hashes"]), 2)
        self.assertEqual([item["scale"] for item in result["machine_ocr"]["request_response_hashes"]], [1, 2])
        for item in result["machine_ocr"]["request_response_hashes"]:
            encoded = json.dumps(item["response"], ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode("utf-8")
            self.assertEqual(item["response_sha256"], sha(encoded))
        row = result["economy"]["comparisons"][0]
        self.assertEqual(row["gold_after_hypothetical_purchase"], 29)
        self.assertIsNone(row["interest_before"])
        self.assertIsNone(row["interest_after"])
        self.assertFalse(result["real_rules_verified"])
        self.assertFalse(result["execution_eligible"])
        self.assertIsNone(result["action"])
        self.assertIsNone(result["human_ground_truth"])

    def test_index_hash_must_match_exact_bytes(self):
        self.write_index()
        with self.assertRaisesRegex(AdapterError, "index_hash_mismatch"):
            analyze_index(self.index_path, "0" * 64, hypothetical_spend=2, ocr=FakeOCR())

    def test_source_type_and_hash_binding_fail_closed(self):
        faults = (("capture", "index_source_mismatch"), ("frame", "frame_sha256_mismatch"),
                  ("video", "video_sha256_mismatch"), ("method", "frame_binding_invalid"))
        original = deepcopy(self.index)
        for fault, expected in faults:
            with self.subTest(fault=fault):
                self.index = deepcopy(original)
                if fault == "capture": self.index["capture_kind"] = "screenshot"
                if fault == "frame": self.index["frame"]["sha256"] = "0" * 64
                if fault == "video": self.index["video"]["sha256"] = "0" * 64
                if fault == "method": self.index["frame"]["seek_method"] = "unknown"
                with self.assertRaisesRegex(AdapterError, expected): self.analyze()

    def test_wrong_seek_target_is_not_treated_as_the_indexed_frame(self):
        self.index["frame"]["seek_target_ms"] = 1000
        with self.assertRaisesRegex(AdapterError, "video_seek_pixels_mismatch"): self.analyze()

    def test_bad_seek_geometry_roi_and_visibility_are_refused(self):
        original = deepcopy(self.index)
        changes = (
            ("negative_time", "frame_binding_invalid"), ("bool_time", "frame_binding_invalid"),
            ("fractional_time", "frame_binding_invalid"), ("bad_geometry", "frame_geometry_or_format_mismatch"),
            ("outside_roi", "roi_invalid"), ("unknown_visual", "agent_observation_unavailable"),
            ("human_truth_claim", "agent_observation_unavailable"),
        )
        for fault, expected in changes:
            with self.subTest(fault=fault):
                self.index = deepcopy(original)
                if fault == "negative_time": self.index["frame"]["seek_target_ms"] = -1
                if fault == "bool_time": self.index["frame"]["seek_target_ms"] = True
                if fault == "fractional_time": self.index["frame"]["seek_target_ms"] = 0.5
                if fault == "bad_geometry": self.index["frame"]["image_size"] = [65, 48]
                if fault == "outside_roi": self.index["roi"]["box"] = [8, 8, 65, 32]
                if fault == "unknown_visual": self.index["agent_visual"]["status"] = "unknown"
                if fault == "human_truth_claim": self.index["agent_visual"]["observer"] = "human_verified"
                with self.assertRaisesRegex(AdapterError, expected): self.analyze()

    def test_incomplete_disagreeing_and_failed_ocr_do_not_get_cash(self):
        for replies in (("31", None), ("31", "32"), ("31", "raise")):
            with self.subTest(replies=replies):
                result = self.analyze(ocr=FakeOCR(replies))
                self.assert_blocked_arithmetic(result)
                self.assertEqual([item["scale"] for item in result["machine_ocr"]["request_response_hashes"]], [1, 2])
                if replies[1] == "raise":
                    self.assertEqual(result["machine_ocr"]["request_response_hashes"][1]["status"], "failed")

    def test_agent_and_machine_disagreement_does_not_get_cash(self):
        self.index["agent_visual"]["candidate"] = 32
        result = self.analyze()
        self.assertEqual(result["reason"], "agent_ocr_disagreement")
        self.assert_blocked_arithmetic(result)

    def test_ocr_subclass_cannot_supply_a_different_value_from_its_record(self):
        def row(value):
            return {"text": value, "confidence": .95, "x": 2, "y": 2, "width": 10, "height": 10}

        class DifferentIndex(list):
            def __init__(self):
                super().__init__([row("31")])

            def __getitem__(self, index):
                return row("32") if index == 0 else super().__getitem__(index)

        class ConflictingBackend:
            def recognize(self, data):
                return DifferentIndex()

        self.index["agent_visual"]["candidate"] = 32
        result = self.analyze(ocr=ConflictingBackend())
        self.assertEqual(result["machine_ocr"]["value"], 31)
        self.assertEqual([x["response"][0]["text"] for x in result["machine_ocr"]["request_response_hashes"]],
                         ["31", "31"])
        self.assertEqual(result["reason"], "agent_ocr_disagreement")
        self.assert_blocked_arithmetic(result)

    def test_recorded_ocr_value_is_detached_from_backend_mutations(self):
        original = [{"text": "31"}]
        class MutableBackend:
            def recognize(self, data):
                return original
        wrapped = _BoundOCR(MutableBackend())
        snapshot = wrapped.recognize(b"synthetic crop")
        original[0]["text"] = "32"
        self.assertIs(snapshot, wrapped.records[0]["response"])
        self.assertEqual(snapshot[0]["text"], "31")

    def test_nonfinite_ocr_response_never_yields_cash(self):
        class NonfiniteBackend:
            def recognize(self, data):
                return [{"text": "31", "confidence": float("nan"), "x": 2, "y": 2, "width": 10, "height": 10}]
        result = self.analyze(ocr=NonfiniteBackend())
        self.assert_blocked_arithmetic(result)
        self.assertEqual([item["status"] for item in result["machine_ocr"]["request_response_hashes"]],
                         ["failed", "failed"])

    def test_ambiguous_six_nine_glyph_is_vetoed_even_if_ocr_agrees(self):
        self.index["agent_visual"]["candidate"] = 9
        result = self.analyze(ocr=FakeOCR(("9", "9")))
        self.assertEqual(result["reason"], "gold_glyph_inconclusive")
        self.assert_blocked_arithmetic(result)

    def test_over_budget_is_not_reported_as_cash_success(self):
        result = self.analyze(price=32)
        self.assertEqual(result["status"], "insufficient_candidate_gold")
        self.assertIsNone(result["economy"]["comparisons"][0]["gold_after_hypothetical_purchase"])
        self.assertIsNone(result["economy"]["comparisons"][0]["interest_delta"])

    def test_frame_index_or_video_mutation_during_ocr_is_refused(self):
        for target, expected in (("frame", "frame_changed_during_ocr"), ("index", "index_changed_during_ocr"),
                                 ("video", "video_changed_during_ocr")):
            with self.subTest(target=target):
                self.frame.write_bytes(self.frame_data)
                def mutate(number):
                    if number == 1:
                        path = {"frame": self.frame, "index": self.index_path, "video": self.video_copy}[target]
                        with path.open("ab") as file: file.write(b" ")
                with self.assertRaisesRegex(AdapterError, expected): self.analyze(ocr=FakeOCR(callback=mutate))

    def test_no_input_may_resolve_inside_public_project(self):
        project_file = Path(__file__).resolve().parents[1] / "adapter.py"
        self.index["frame"]["path"] = str(project_file)
        with self.assertRaisesRegex(AdapterError, "private_path_inside_project"): self.analyze()

    def test_price_and_extra_claims_are_not_silently_fixed(self):
        with self.assertRaisesRegex(AdapterError, "hypothetical_spend_invalid"): self.analyze(price=True)
        self.index["verified"] = True
        with self.assertRaisesRegex(AdapterError, "index_shape_invalid"): self.analyze()

    def test_dependency_change_during_processing_invalidates_result(self):
        with patch("jcc_video_economy.adapter._dependency_hashes", side_effect=[{"module": "a"}, {"module": "b"}]):
            with self.assertRaisesRegex(AdapterError, "dependency_changed_during_ocr"): self.analyze()

    def test_caller_backend_does_not_need_the_apple_worker_or_swift_source(self):
        original = Path.read_bytes
        def unavailable_apple_files(path):
            if path in (PROJECT / APPLE_VISION_SOURCE, PROJECT / APPLE_VISION_WORKER):
                raise FileNotFoundError("Apple Vision build input is unavailable")
            return original(path)
        with patch.object(Path, "read_bytes", unavailable_apple_files):
            result = self.analyze()
        self.assertEqual(result["status"], "cash_only")
        self.assertEqual(result["machine_ocr"]["backend"], "caller_supplied_local_backend_unverified")
        self.assertFalse(result["machine_ocr"]["worker_fingerprint_checked"])

    def test_builtin_vision_backend_rejects_a_missing_worker(self):
        original = Path.is_file
        def no_worker(path):
            return False if path == PROJECT / APPLE_VISION_WORKER else original(path)
        with patch.object(Path, "is_file", no_worker):
            with self.assertRaisesRegex(AdapterError, "apple_vision_worker_unavailable"):
                self.analyze(ocr=VisionOCR())

    def test_builtin_vision_uses_worker_policy_and_refuses_dependency_change(self):
        builtin = VisionOCR()
        fake = FakeOCR()
        with patch.object(builtin, "recognize", side_effect=fake.recognize), \
                patch("jcc_video_economy.adapter._dependency_hashes",
                      side_effect=[{"worker": "before"}, {"worker": "after"}]) as fingerprint:
            with self.assertRaisesRegex(AdapterError, "dependency_changed_during_ocr"):
                self.analyze(ocr=builtin)
        self.assertEqual(fingerprint.call_args_list,
                         [call(apple_vision_worker=True), call(apple_vision_worker=True)])


if __name__ == "__main__":
    unittest.main()
