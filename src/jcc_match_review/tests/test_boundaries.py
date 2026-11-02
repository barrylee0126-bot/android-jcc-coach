"""Generated pixels only: no real recordings, OCR, network or device access."""

import copy
from html.parser import HTMLParser
import io
import os
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from jcc_match_review.core import THREADS, require_idle
from jcc_match_review.private import PinnedInputs, ReviewError, digest, json_document, safe_path, write_new
from jcc_match_review.render import render_page


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def generated_image(color):
    with Image.new("RGB", (120, 80), color) as image, io.BytesIO() as out:
        image.save(out, format="PNG")
        return out.getvalue()


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir(mode=0o700)
        self.raw = generated_image("#324458")
        self.source_hash = digest(self.raw)
        self.path = self.source / "generated.png"
        write_new(self.path, self.raw)
        self.pins = {"image": {"path": str(self.path), "sha256": self.source_hash}}
        self.frame = {"frame_id": "frame-1", "title": "生成测试帧", "period": "测试", "note": "未知保留。",
                      "image_size": [120, 80], "source_sha256": self.source_hash, "video_sha256": "b" * 64,
                      "recording_time": {"requested_seconds": 12.5, "decoded_pts_seconds": None, "basis": "请求时间"},
                      "provenance": [], "regions": [{"label": "开发框", "roi": [1, 2, 70, 40],
                          "selection": "测试手调", "source_sha256": self.source_hash}],
                      "observations": [{"label": "容量", "authority": "machine_candidate", "status": "unknown",
                          "candidate": None, "reason_codes": ["occluded"], "explanation": "区域受遮挡，保持未知。",
                          "source_sha256": self.source_hash}]}
        self.report = {"frames": [self.frame], "cases": [{"title": "测试", "frame_ids": ["frame-1"], "limitation": "合成"}],
                       "video_sha256": "b" * 64,
                       "frame_titles": {"frame-1": "生成测试帧"}, "generated_utc": "test", "human_ground_truth_count": 0,
                       "independent_review_count": 0, "accuracy": None, "automatic_action": None, "advice_eligible": False}

    def reader(self):
        return PinnedInputs(self.source, self.pins, self.base / "output")

    def test_fresh_bytes_override_previous_success(self):
        reader = self.reader()
        self.assertEqual(reader.read("image"), self.raw)
        self.path.write_bytes(generated_image("red"))
        with self.assertRaisesRegex(ReviewError, "source_changed_during_review"):
            reader.verify_unchanged()

    def test_pin_mismatch_rejected(self):
        self.pins["image"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ReviewError, "pinned_source_mismatch"):
            self.reader().read("image")

    def test_symlink_and_hardlink_rejected(self):
        link = self.source / "link.png"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ReviewError, "symlink_forbidden"):
            safe_path(link)
        link.unlink()
        os.link(self.path, link)
        with self.assertRaisesRegex(ReviewError, "unsafe_file_permissions"):
            self.reader().read("image")

    def test_public_file_and_cloud_git_locations_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ReviewError, "unsafe_file_permissions"):
            self.reader().read("image")
        for component, code in (("CloudStorage", "cloud_location_forbidden"), ("git-repo", "private_data_in_git_forbidden")):
            folder = self.base / component
            folder.mkdir(mode=0o700)
            if component == "git-repo":
                (folder / ".git").mkdir()
            with self.subTest(component=component), self.assertRaisesRegex(ReviewError, code):
                safe_path(folder / "private.html")

    def test_private_writes_no_overwrite(self):
        output = self.base / "private.html"
        write_new(output, b"original")
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            write_new(output, b"changed")
        self.assertEqual(output.read_bytes(), b"original")

    def test_output_must_not_be_input(self):
        with self.assertRaisesRegex(ReviewError, "input_output_boundary"):
            PinnedInputs(self.source, self.pins, self.source)

    def test_duplicate_json_and_nan_rejected(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}'):
            with self.assertRaises(ReviewError):
                json_document(raw)

    def test_active_or_missing_dependency_rejected(self):
        polls = [{"thread": {"id": key, "status": {"type": "idle"}}, "latestTurn": {"status": "completed"}}
                 for key in THREADS.values()]
        require_idle({"polls": polls})
        polls[0]["thread"]["status"]["type"] = "active"
        with self.assertRaisesRegex(ReviewError, "dependency_not_idle"):
            require_idle({"polls": polls})
        with self.assertRaisesRegex(ReviewError, "dependency_not_idle"):
            require_idle({"polls": []})

    def test_wrong_frame_and_cross_frame_observation_rejected(self):
        with self.assertRaisesRegex(ReviewError, "render_source_binding_mismatch"):
            render_page(self.report, {"frame-1": generated_image("yellow")})
        self.frame["observations"][0]["source_sha256"] = "e" * 64
        with self.assertRaisesRegex(ReviewError, "render_evidence_binding_mismatch"):
            render_page(self.report, {"frame-1": self.raw})

    def test_video_binding_mismatch_rejected(self):
        self.report["video_sha256"] = "d" * 64
        with self.assertRaisesRegex(ReviewError, "render_video_binding_mismatch"):
            render_page(self.report, {"frame-1": self.raw})

    def test_crop_from_wrong_frame_and_unknown_with_value_rejected(self):
        self.frame["regions"][0]["source_sha256"] = "e" * 64
        with self.assertRaisesRegex(ReviewError, "render_evidence_binding_mismatch"):
            render_page(self.report, {"frame-1": self.raw})
        self.frame["regions"][0]["source_sha256"] = self.source_hash
        self.frame["observations"][0]["candidate"] = 0
        with self.assertRaisesRegex(ReviewError, "unknown_must_not_have_value"):
            render_page(self.report, {"frame-1": self.raw})

    def test_occlusion_keeps_unknown_and_privacy_policy(self):
        page = render_page(self.report, {"frame-1": self.raw})
        self.assertIn("区域受遮挡，保持未知", page)
        self.assertIn("非人类真值", page)
        self.assertIn("时延不是实战成绩", page)
        parser = Tags()
        parser.feed(page)
        for tag, attrs in parser.elements:
            self.assertNotIn(tag, ("script", "iframe", "link", "video", "audio", "form", "object"))
            if "src" in attrs:
                self.assertTrue(attrs["src"].startswith("data:image/"))
            if "href" in attrs:
                self.assertTrue(attrs["href"].startswith("#"))
            self.assertFalse(any(key.startswith("on") for key in attrs))
        self.assertIn("connect-src &#x27;none&#x27;", page)

    def test_untrusted_report_text_is_escaped(self):
        self.frame["note"] = '<script src="https://invalid.example/private"></script>'
        page = render_page(self.report, {"frame-1": self.raw})
        self.assertIn("&lt;script", page)
        parser = Tags()
        parser.feed(page)
        self.assertFalse(any(tag == "script" for tag, _ in parser.elements))

    def test_human_truth_or_action_upgrade_rejected(self):
        for key, value in (("human_ground_truth_count", 1), ("accuracy", 1.0), ("automatic_action", "tap"), ("advice_eligible", True)):
            report = copy.deepcopy(self.report)
            report[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ReviewError, "render_authority_invalid"):
                render_page(report, {"frame-1": self.raw})


if __name__ == "__main__":
    unittest.main()
