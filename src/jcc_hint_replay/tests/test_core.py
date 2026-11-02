from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from jcc_hint_replay.core import HintReplayError, build_replay


PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000c49444154789c6360f8cf00000004000101f5cdb50000000049454e44ae426082")


class HintReplayTests(unittest.TestCase):
    def setUp(self):
        self.private = tempfile.TemporaryDirectory()
        self.base = Path(self.private.name)
        self.frames = []
        for name in ("first", "middle", "last"):
            path = self.base / f"{name}.png"
            path.write_bytes(PNG)
            self.frames.append(path)

    def tearDown(self):
        self.private.cleanup()

    def manifest(self):
        def frame(path, seconds, status, deployed=None, cap=None, empty=None):
            return {"id": path.stem, "requested_time_s": seconds, "decoded_pts_s": seconds + .05,
                    "path": str(path), "sha256": hashlib.sha256(PNG).hexdigest(), "capacity_status": status,
                    "displayed_deployed": deployed, "capacity": cap, "empty_slots": empty}
        return {"schema_version": "hint-replay-v1", "purpose": "private_replay_lifecycle_only",
                "source_kind": "recovered_recording_development", "frames": [
                    frame(self.frames[0], 1, "development_candidate", 11, 12, 1),
                    frame(self.frames[1], 2, "unknown"),
                    frame(self.frames[2], 3, "development_candidate", 12, 12, 0),
                ]}

    def write_manifest(self, value):
        path = self.base / "manifest.json"
        path.write_text(json.dumps(value))
        return path

    def test_private_three_frame_lifecycle_clears_previous_difference(self):
        output = self.base / "out"
        report = build_replay(self.write_manifest(self.manifest()), output)
        self.assertEqual([item["state"] for item in report["frames"]], ["conditional_prompt", "withdrawn_unknown", "withdrawn_full"])
        self.assertEqual(report["frames"][1]["capacity_text"], "未知")
        self.assertNotIn("差 1", (output / "report.html").read_text().split("第 2 帧", 1)[1].split("第 3 帧", 1)[0])
        self.assertFalse(report["advice_eligible"])

    def test_hash_mismatch_refuses_before_output(self):
        manifest = self.manifest()
        manifest["frames"][0]["sha256"] = "0" * 64
        output = self.base / "out"
        with self.assertRaisesRegex(HintReplayError, "frame_sha256_mismatch"):
            build_replay(self.write_manifest(manifest), output)
        self.assertFalse(output.exists())

    def test_unknown_with_carried_value_is_refused(self):
        manifest = self.manifest()
        manifest["frames"][1]["empty_slots"] = 1
        with self.assertRaisesRegex(HintReplayError, "unknown_frame_contains_capacity"):
            build_replay(self.write_manifest(manifest), self.base / "out")

    def test_other_order_cannot_claim_lifecycle(self):
        manifest = self.manifest()
        manifest["frames"][1]["capacity_status"] = "development_candidate"
        manifest["frames"][1].update(displayed_deployed=11, capacity=12, empty_slots=1)
        with self.assertRaisesRegex(HintReplayError, "lifecycle_sequence_not_demonstrated"):
            build_replay(self.write_manifest(manifest), self.base / "out")


if __name__ == "__main__":
    unittest.main()
