from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from PIL import Image, PngImagePlugin

from jcc_replay_coach.core import ReplayCoachError, build_replay_coach


class ReplayCoachTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.frames = []
        for index, name in enumerate(("known", "unknown")):
            path = self.root / f"{name}.png"
            Image.new("RGB", (1, 1), (index, 0, 0)).save(path, format="PNG")
            os.chmod(path, 0o600)
            self.frames.append(path)

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self):
        def row(path, time, candidate):
            return {"id": path.stem, "requested_time_s": time, "decoded_pts_s": time + .01,
                    "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "candidate": candidate}
        first = row(self.frames[0], 1.0, {"status": "machine_candidate", "source": "video-economy-candidate-v1",
                                          "value": 31, "source_frame_sha256": hashlib.sha256(self.frames[0].read_bytes()).hexdigest(),
                                          "hypothetical_spend": 2})
        second = row(self.frames[1], 1.5, {"status": "unknown", "reason": "new_frame_without_reliable_candidate",
                                            "basis": "no_fresh_candidate_supplied"})
        return {"schema_version": "replay-coach-v1", "purpose": "private_replay_condition_demo",
                "source_kind": "recovered_recording_development", "frames": [first, second]}

    def write_manifest(self, value):
        path = self.root / "manifest.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def test_candidate_is_visible_then_new_unknown_withdraws(self):
        output = self.root / "out"
        report = build_replay_coach(self.write_manifest(self.manifest()), output)
        self.assertEqual([frame["state"] for frame in report["frames"]],
                         ["conditional_explanation_visible", "withdrawn_unknown"])
        self.assertEqual(report["frames"][0]["balance_after"], 29)
        self.assertIsNone(report["frames"][1]["candidate"])
        self.assertEqual(report["frames"][1]["basis"], "no_fresh_candidate_supplied")
        page = (output / "report.html").read_text(encoding="utf-8")
        unknown = page.split("data-sequence='2'", 1)[1]
        self.assertNotIn("31 − 2 = 29", unknown)
        self.assertIn("本模块不运行 OCR，本帧未提供新的可消费候选。", unknown)
        self.assertIn("调用方声明的私有抽取静帧", page)
        self.assertNotIn("同一局恢复录像的连续私有帧", page)
        self.assertFalse(report["advice_eligible"])

    def test_candidate_must_bind_to_its_own_frame(self):
        value = self.manifest()
        value["frames"][0]["candidate"]["source_frame_sha256"] = "0" * 64
        with self.assertRaisesRegex(ReplayCoachError, "machine_candidate_invalid"):
            build_replay_coach(self.write_manifest(value), self.root / "out")

    def test_discontinuous_or_adjacent_identical_encoded_frame_is_refused(self):
        value = self.manifest()
        value["frames"][1]["requested_time_s"] = 9
        with self.assertRaisesRegex(ReplayCoachError, "frame_time_order_or_gap_invalid"):
            build_replay_coach(self.write_manifest(value), self.root / "out")
        value = self.manifest()
        value["frames"][1]["path"] = str(self.frames[0])
        value["frames"][1]["sha256"] = hashlib.sha256(self.frames[0].read_bytes()).hexdigest()
        with self.assertRaisesRegex(ReplayCoachError, "repeated_frame_invalid"):
            build_replay_coach(self.write_manifest(value), self.root / "out")

    def test_differently_encoded_same_pixels_are_not_a_continuity_proof(self):
        alternate = self.root / "same-pixels-different-bytes.png"
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("encoding-note", "different metadata only")
        Image.new("RGB", (1, 1), (0, 0, 0)).save(alternate, format="PNG", pnginfo=metadata)
        os.chmod(alternate, 0o600)
        value = self.manifest()
        value["frames"][1]["path"] = str(alternate)
        value["frames"][1]["sha256"] = hashlib.sha256(alternate.read_bytes()).hexdigest()
        report = build_replay_coach(self.write_manifest(value), self.root / "out")
        self.assertEqual(report["frames"][1]["state"], "withdrawn_unknown")

    def test_newest_frame_must_withdraw_or_output_refuses(self):
        value = self.manifest()
        value["frames"][1]["candidate"] = {"status": "machine_candidate", "source": "video-economy-candidate-v1",
                                               "value": 30, "source_frame_sha256": value["frames"][1]["sha256"],
                                               "hypothetical_spend": 2}
        with self.assertRaisesRegex(ReplayCoachError, "candidate_then_withdrawal_required"):
            build_replay_coach(self.write_manifest(value), self.root / "out")

    def test_known_withdrawal_reasons_are_required(self):
        value = self.manifest()
        value["frames"][1]["candidate"]["reason"] = "untrusted_text"
        with self.assertRaisesRegex(ReplayCoachError, "unknown_candidate_invalid"):
            build_replay_coach(self.write_manifest(value), self.root / "out")

    def test_disconnect_uses_the_same_withdrawn_state(self):
        value = self.manifest()
        value["frames"][1]["candidate"]["reason"] = "source_disconnected"
        report = build_replay_coach(self.write_manifest(value), self.root / "out")
        self.assertEqual(report["frames"][1]["state"], "withdrawn_unknown")
        self.assertIsNone(report["frames"][1]["balance_after"])

    def test_nonfinite_or_huge_time_returns_fixed_error(self):
        value = self.manifest()
        value["frames"][0]["requested_time_s"] = 10 ** 400
        with self.assertRaisesRegex(ReplayCoachError, "frame_time_invalid"):
            build_replay_coach(self.write_manifest(value), self.root / "out")


if __name__ == "__main__":
    unittest.main()
