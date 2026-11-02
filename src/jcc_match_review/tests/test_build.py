"""Private end-to-end generation and dependency/write boundaries on fake data."""

import json
from pathlib import Path
import tempfile
import unittest

from jcc_match_review.core import THREADS, run_review
from jcc_match_review.private import ReviewError, digest, write_new
from jcc_match_review.tests.test_boundaries import generated_image


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.output = self.base / "out"
        self.inputs = {}
        self.pin("video", b"generated fixture, not a real video")
        snapshot = {"polls": [{"thread": {"id": thread, "status": {"type": "idle"}},
                                 "latestTurn": {"status": "completed"}} for thread in THREADS.values()]}
        self.pin("idle", json.dumps(snapshot).encode())
        frames = []
        for index, color in enumerate(("red", "blue", "green")):
            key = f"frame-{index}"
            self.pin(key, generated_image(color))
            frames.append({"frame_id": key, "image_pin": key, "title": "Generated fixture", "period": "synthetic",
                           "note": "synthetic only", "capacity_sources": [], "agent_visual": [],
                           "recording_time": {"basis_kind": "documented_ffmpeg_request", "basis": "generated",
                                              "requested_seconds": index, "decoded_pts_seconds": None}})
        self.manifest = {"schema_version": "match-review-v1", "purpose": "private_match_review_only",
                         "source_kind": "recovered_recording_development", "source_root": str(self.base),
                         "output_root": str(self.output), "pins": self.inputs, "source_video_pin": "video",
                         "idle_snapshot_pin": "idle", "frames": frames, "cases": [{"title": "generated",
                             "frame_ids": [f["frame_id"] for f in frames], "limitation": "not real"}]}
        self.manifest_path = self.base / "manifest.json"

    def pin(self, key, raw):
        path = self.base / f"{key}.private"
        write_new(path, raw)
        self.inputs[key] = {"path": str(path), "sha256": digest(raw)}

    def save(self):
        write_new(self.manifest_path, json.dumps(self.manifest).encode())

    def test_complete_output_private_and_sources_unchanged(self):
        self.save()
        before = {item["path"]: Path(item["path"]).read_bytes() for item in self.inputs.values()}
        page, summary = run_review(self.manifest_path)
        self.assertEqual(summary["frames"], 3)
        self.assertTrue(page.exists())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        completed = json.loads((self.output / "COMPLETED.json").read_bytes())
        for path in self.output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for name, expected in completed["files"].items():
            self.assertEqual(digest((self.output / name).read_bytes()), expected)
        self.assertEqual(before, {path: Path(path).read_bytes() for path in before})
        with self.assertRaisesRegex(ReviewError, "output_exists"):
            run_review(self.manifest_path)

    def test_active_dependency_produces_no_output(self):
        path = Path(self.inputs["idle"]["path"])
        doc = json.loads(path.read_bytes())
        doc["polls"][0]["thread"]["status"]["type"] = "active"
        path.write_bytes(json.dumps(doc).encode())
        self.inputs["idle"]["sha256"] = digest(path.read_bytes())
        self.save()
        with self.assertRaisesRegex(ReviewError, "dependency_not_idle"):
            run_review(self.manifest_path)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
