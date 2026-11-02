"""Synthetic regressions for private files, immutable images and -O checks."""

import base64
import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from jcc_shop_probe.private_cli import _new_output, _viewer, _write_new, run_private
from jcc_shop_probe.tests.test_probe import image, layout


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fixture(root):
    samples = root / "synthetic-samples"
    samples.mkdir(mode=0o700)
    pixels = image()
    png = samples / "synthetic.png"
    png.write_bytes(pixels)
    annotation = {"sample_id": "fictional-sample", "asset": {"sha256": sha(pixels)},
                  "frame": {"frame_id": "fictional-frame"},
                  "stream_id": "fictional-stream", "host_clock_id": "fictional-clock"}
    workspace = {"revision": 4, "annotations": [annotation], "assets": {"fictional-sample": {
        "path": "synthetic.png", "sha256": sha(pixels), "asset_ref": "fictional-asset",
    }}}
    files = {"workspace": samples / "workspace.json", "manifest": samples / "frozen.json",
             "vocabulary": root / "synthetic-vocabulary.json", "image": png}
    for key, value in (("workspace", workspace), ("manifest", [annotation]),
                       ("vocabulary", [{"name": "虚构甲", "cost": 3}])):
        files[key].write_text(json.dumps(value, ensure_ascii=False))
    original = layout()
    cfg = {"purpose": "exposed_development_frames_only", "sample_root": str(samples),
           "workspace_sha256": sha(files["workspace"].read_bytes()), "manifest_path": "frozen.json",
           "manifest_sha256": sha(files["manifest"].read_bytes()),
           "vocabulary_path": str(files["vocabulary"]), "vocabulary_sha256": sha(files["vocabulary"].read_bytes()),
           "allowed_sample_ids": ["fictional-sample"],
           "layout": {"layout_id": original.layout_id, "image_size": original.image_size,
                      "slots": [{"slot": s.slot, **{key: getattr(s, key).box for key in (
                          "card", "name_zone", "price", "interior")}} for s in original.slots]}}
    config = root / "synthetic-config.json"
    config.write_text(json.dumps(cfg))
    return config, files, pixels


class BlankOCR:
    def __init__(self, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def recognize(self, data): return []


class Sources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images.append(dict(attrs)["src"])


class PrivateCLITests(unittest.TestCase):
    def setUp(self):
        # A trusted user-owned ancestor: /tmp may itself be shared or symlinked.
        self.temp = tempfile.TemporaryDirectory(prefix=".shop-probe-test-", dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def run_fixture(self):
        config, files, pixels = fixture(self.root)
        output = self.root / "new-output"
        with patch("jcc_device.vision_ocr.VisionOCR", BlankOCR), contextlib.redirect_stdout(io.StringIO()):
            run_private(config, output)
        return output, files, pixels

    def test_existing_directory_is_not_chmodded_or_overwritten(self):
        output = self.root / "existing"
        output.mkdir(mode=0o750)
        existing = output / "summary.json"
        existing.write_bytes(b"keep existing summary")
        mode = stat.S_IMODE(output.stat().st_mode)
        with self.assertRaises(FileExistsError):
            run_private(self.root / "unused-config", output)
        self.assertEqual(existing.read_bytes(), b"keep existing summary")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), mode)

    def test_output_directory_symlink_is_rejected(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        output = self.root / "link"
        output.symlink_to(target, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            run_private(self.root / "unused-config", output)
        self.assertEqual(list(target.iterdir()), [])

    def test_symlink_ancestor_is_not_resolved_away(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        link = self.root / "ancestor-link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            run_private(self.root / "unused-config", link / "new")
        self.assertFalse((target / "new").exists())

    def test_writable_ancestor_is_rejected_without_changing_it(self):
        unsafe = self.root / "shared"
        unsafe.mkdir(mode=0o700)
        unsafe.chmod(0o777)
        try:
            with self.assertRaises(ValueError):
                run_private(self.root / "unused-config", unsafe / "new")
            self.assertEqual(stat.S_IMODE(unsafe.stat().st_mode), 0o777)
            self.assertFalse((unsafe / "new").exists())
        finally:
            unsafe.chmod(0o700)

    def test_exclusive_writer_never_follows_or_overwrites_a_file(self):
        target = self.root / "outside.txt"
        target.write_bytes(b"outside remains")
        output = self.root / "new-output"
        with _new_output(output) as directory:
            (output / "page.html").symlink_to(target)
            with self.assertRaises(FileExistsError):
                _write_new(directory, "page.html", b"overwrite attempt")
            _write_new(directory, "summary.json", b"first write")
            with self.assertRaises(FileExistsError):
                _write_new(directory, "summary.json", b"second write")
            with self.assertRaises(ValueError):
                _write_new(directory, "../outside.txt", b"path escape")
        self.assertEqual(target.read_bytes(), b"outside remains")
        self.assertEqual((output / "summary.json").read_bytes(), b"first write")

    def test_bundle_is_private_complete_and_revisioned(self):
        output, _, pixels = self.run_fixture()
        marker = json.loads((output / "COMPLETED.json").read_text())
        result = json.loads((output / "results.private.json").read_text())
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["summary"]["source_workspace_revision"], 4)
        self.assertEqual(result["frames"][0]["source_sha256"], sha(pixels))
        self.assertEqual(marker["run_id"], result["summary"]["run_id"])
        for name, digest in marker["files_sha256"].items():
            self.assertEqual(sha((output / name).read_bytes()), digest)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in output.iterdir()))

    def test_explicit_price_processing_is_recorded_and_missing_gold_stays_unknown(self):
        config, _, _ = fixture(self.root)
        cfg = json.loads(config.read_text())
        cfg["experimental_price_processing"] = {"left_padding_px": 5, "suppress_gold": True}
        config.write_text(json.dumps(cfg))
        output = self.root / "experimental-output"
        with patch("jcc_device.vision_ocr.VisionOCR", BlankOCR), contextlib.redirect_stdout(io.StringIO()):
            run_private(config, output)
        result = json.loads((output / "results.private.json").read_text())
        self.assertTrue(result["summary"]["experimental_price_processing"])
        self.assertTrue(result["frames"][0]["experimental_price_processing"])
        for slot in result["frames"][0]["slots"]:
            self.assertEqual(slot["price"]["status"], "unknown")
            self.assertIn("price_mask_no_gold_pixels", slot["price"]["reasons"])
        self.assertIn("试验性价格区域处理：已启用", (output / "查看四帧五格.html").read_text())

    def test_viewer_embeds_verified_bytes_despite_source_replacement_and_symlink(self):
        output, files, pixels = self.run_fixture()
        page = output / "查看四帧五格.html"
        before = page.read_bytes()
        other = self.root / "replacement.png"
        other.write_bytes(image((1, 2, 3)))
        files["image"].write_bytes(other.read_bytes())
        files["image"].unlink()
        files["image"].symlink_to(other)
        self.assertEqual(page.read_bytes(), before)
        parser = Sources()
        parser.feed(before.decode())
        self.assertEqual(len(parser.images), 1)
        self.assertNotIn("file://", before.decode())
        decoded = base64.b64decode(parser.images[0].split(",", 1)[1], validate=True)
        self.assertEqual(decoded, pixels)
        self.assertNotEqual(sha(decoded), sha(files["image"].read_bytes()))

    def test_viewer_rejects_bytes_not_matching_frame_digest(self):
        with self.assertRaises(ValueError):
            _viewer([{"source_sha256": "a" * 64, "slots": []}], {}, {"a" * 64: image()})

    def test_optimized_python_still_rechecks_every_pinned_input(self):
        code = r'''
import sys
from pathlib import Path
from unittest.mock import patch
from jcc_shop_probe.private_cli import run_private
class MutatingOCR:
    def __init__(self, **kwargs): self.mutated = False
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def recognize(self, data):
        if not self.mutated:
            target = Path(sys.argv[3])
            target.write_bytes(target.read_bytes() + b"changed during synthetic OCR")
            self.mutated = True
        return []
with patch("jcc_device.vision_ocr.VisionOCR", MutatingOCR):
    try:
        run_private(Path(sys.argv[1]), Path(sys.argv[2]))
    except ValueError:
        sys.exit(27)
sys.exit(0)
'''
        for kind in ("workspace", "manifest", "vocabulary", "image"):
            with self.subTest(input_kind=kind):
                case = self.root / kind
                case.mkdir(mode=0o700)
                config, files, _ = fixture(case)
                output = case / "must-not-succeed"
                env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                       "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
                child = subprocess.run([sys.executable, "-O", "-c", code,
                                        str(config), str(output), str(files[kind])],
                                       capture_output=True, text=True, env=env, timeout=20)
                self.assertEqual(child.returncode, 27, child.stderr + child.stdout)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
