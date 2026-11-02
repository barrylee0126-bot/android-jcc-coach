from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from jcc_open_slot_probe import METHOD, probe_frame
from jcc_open_slot_probe.__main__ import _card, _output_path, run_config


class FakeOCR:
    def __init__(self, texts=None):
        self.texts = iter(texts) if texts is not None else None
        self.calls = 0

    def recognize(self, payload):
        self.calls += 1
        text = next(self.texts) if self.texts else "2/3"
        if isinstance(text, Exception):
            raise text
        if text is None:
            return []
        with Image.open(io.BytesIO(payload)) as im:
            w, h = im.size
        return [{"text": text, "confidence": 0.95, "x": w / 8,
                 "y": h / 8, "width": w / 2, "height": h / 2}]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def fixture(*, model=True, resource=True):
    im = Image.new("RGB", (400, 240), (185, 185, 175))
    d = ImageDraw.Draw(im)
    if model:
        d.rectangle((62, 144, 98, 195), fill=(20, 60, 40), outline="black", width=3)
        d.polygon([(65, 150), (94, 166), (70, 188), (92, 193)], fill=(230, 70, 35))
        d.line((65, 147, 92, 190), fill=(70, 230, 60), width=4)
    if resource:
        d.polygon([(225, 147), (244, 170), (225, 194), (206, 170)], fill=(25, 70, 200), outline="black", width=3)
        d.line((225, 150, 220, 174, 227, 190), fill=(30, 220, 250), width=3)
    b = io.BytesIO()
    im.save(b, format="PNG")
    data = b.getvalue()
    sha = hashlib.sha256(data).hexdigest()

    def evidence(roi, layout_id):
        return {"encoded_sha256": sha, "layout_id": layout_id, "source_kind": "synthetic",
                "image_size": [400, 240], "rotation_applied_degrees": 0, "roi": roi,
                "layout_matches": True, "visibility": "clear_candidate",
                "animation": "not_observed_candidate", "assessment_method": "synthetic"}

    cap_roi = [30, 20, 190, 90]
    frame = {"alias": "SYN_A", "viewing_group": "SYN_GROUP", "collection_activity": "SYN_COLLECTION",
             "source_kind": "synthetic", "expected_sha256": sha, "image_size": [400, 240],
             "capacity_trials": [{"layout": {"layout_id": "synthetic-capacity", "image_size": [400, 240],
                                             "roi": cap_roi, "source_kind": "synthetic"},
                                  "evidence": evidence(cap_roi, "synthetic-capacity")}],
             "bench": {"layout_id": "synthetic-bench", "roi": [20, 110, 380, 230],
                       "evidence": evidence([20, 110, 380, 230], "synthetic-bench"),
                       "object_regions": [[30, 130, 150, 220], [150, 130, 270, 220], [270, 130, 370, 220]],
                       "parameters": {"template_min_score": 0.75, "foreground_dark_delta": 35,
                                      "foreground_min_fraction": 0.04, "edge_min_fraction": 0.02}}}
    refs = []
    for name, cls, roi in [("synthetic-model", "model_appearance", [60, 140, 100, 200]),
                            ("synthetic-resource", "resource_appearance", [204, 142, 246, 200])]:
        refs.append({"crop": im.crop(roi), "metadata": {
            "reference_id": name, "class": cls, "encoded_sha256": sha, "roi": roi,
            "viewing_group": "SYN_GROUP", "collection_activity": "SYN_COLLECTION",
            "source_kind": "synthetic", "image_size": [400, 240],
            "layout_id": "synthetic-reference", "assessment_method": "synthetic"}})
    im.close()
    return data, frame, refs


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.payload, self.frame, self.refs = fixture()

    def tearDown(self):
        for ref in self.refs:
            ref["crop"].close()

    def probe(self, ocr=None, frame=None, payload=None):
        return probe_frame(payload or self.payload, frame or self.frame, self.refs, ocr or FakeOCR())

    def test_synthetic_positive_is_review_only(self):
        r = self.probe()
        self.assertEqual(r["capacity"]["empty_slots"], 1)
        self.assertEqual(r["bench"]["model_like_presence"], "appearance_candidate")
        self.assertEqual(r["bench"]["resource_like_presence"], "appearance_candidate")
        self.assertEqual(r["status"], "review_only_conditional_clue")
        self.assertFalse(r["advice_eligible"])
        self.assertIsNone(r["automatic_action"])
        self.assertEqual(r["context"]["phase"], "unknown")
        self.assertEqual(r["context"]["subject"], "unknown")
        self.assertEqual(r["bench"]["deployable_unit"], "unknown")

    def test_synthetic_full_does_not_make_empty_clue(self):
        r = self.probe(FakeOCR(["3/3", "3/3"]))
        self.assertEqual(r["capacity"]["empty_slots"], 0)
        self.assertEqual(r["status"], "review_only_observation")

    def test_capacity_occlusion_rejects_perfect_ocr_without_call(self):
        self.frame["capacity_trials"][0]["evidence"]["visibility"] = "occluded"
        ocr = FakeOCR()
        r = self.probe(ocr)
        self.assertEqual(ocr.calls, 0)
        self.assertIsNone(r["capacity"]["empty_slots"])
        self.assertIn("occluded", r["capacity"]["reason_codes"])

    def test_animation_and_missing_identity_remain_unknown(self):
        for field, val in [("animation", "observed"), ("animation", "unknown"), ("layout_matches", None)]:
            with self.subTest(field=field, val=val):
                frame = copy.deepcopy(self.frame)
                frame["capacity_trials"][0]["evidence"][field] = val
                self.assertIsNone(self.probe(frame=frame)["capacity"]["capacity"])

    def test_shifted_roi_cannot_reuse_evidence(self):
        self.frame["capacity_trials"][0]["layout"]["roi"] = [31, 20, 191, 90]
        ocr = FakeOCR()
        r = self.probe(ocr)
        self.assertIn("frame_evidence_roi_mismatch", r["capacity"]["reason_codes"])
        self.assertEqual(ocr.calls, 0)

    def test_wrong_frame_capacity_evidence_is_not_merged(self):
        self.frame["capacity_trials"][0]["evidence"]["encoded_sha256"] = "0" * 64
        self.assertIsNone(self.probe()["capacity"]["displayed_deployed"])

    def test_scale_disagreement_discards_all_numbers(self):
        r = self.probe(FakeOCR(["2/3", "3/3"]))
        self.assertIsNone(r["capacity"]["capacity"])
        self.assertNotIn('"pair"', json.dumps(r))
        self.assertTrue(all(v["status"] == "unknown" for v in r["capacity"]["trials"][0]["confidence"]["variants"]))

    def test_disagreeing_strict_regions_no_picking(self):
        self.frame["capacity_trials"].append(copy.deepcopy(self.frame["capacity_trials"][0]))
        r = self.probe(FakeOCR(["2/3", "2/3", "3/3", "3/3"]))
        self.assertEqual(r["capacity"]["reason_codes"], ["strict_trial_disagreement"])
        self.assertIsNone(r["capacity"]["capacity"])
        self.assertTrue(all(x["status"] == "unknown" for x in r["capacity"]["trials"]))

    def test_one_strict_region_fails_all_are_unknown(self):
        self.frame["capacity_trials"].append(copy.deepcopy(self.frame["capacity_trials"][0]))
        r = self.probe(FakeOCR(["2/3", "2/3", None, None]))
        self.assertIsNone(r["capacity"]["empty_slots"])
        self.assertTrue(all(x["status"] == "unknown" for x in r["capacity"]["trials"]))

    def test_ocr_exception_is_sanitized(self):
        r = self.probe(FakeOCR([RuntimeError("secret OCR text"), RuntimeError("private file")]))
        self.assertNotIn("secret", json.dumps(r))
        self.assertNotIn("private file", json.dumps(r))
        self.assertIsNone(r["capacity"]["capacity"])

    def test_diagnostics_cannot_resurrect_blocked_capacity(self):
        self.frame["capacity_trials"][0]["evidence"]["visibility"] = "occluded"
        self.frame["diagnostic_trials"] = [{"trial_id": "synthetic-diag", "roi": [30, 20, 190, 90]}]
        r = self.probe()
        self.assertTrue(all(d["same_complete_pattern_across_scales"] for d in r["diagnostics"]))
        self.assertIsNone(r["capacity"]["empty_slots"])
        self.assertNotIn("2/3", json.dumps(r))
        self.assertNotIn("pair", json.dumps(r))

    def test_bench_same_frame_wrong_roi_rejected(self):
        self.frame["bench"]["roi"] = [22, 110, 380, 230]
        r = self.probe()
        self.assertEqual(r["bench"]["reason_codes"], ["frame_evidence_roi_mismatch"])
        self.assertEqual(r["bench"]["matches"], [])

    def test_bench_wrong_hash_and_popup_reject(self):
        for change in [{"encoded_sha256": "0" * 64}, {"visibility": "occluded"}, {"layout_matches": False}]:
            frame = copy.deepcopy(self.frame)
            frame["bench"]["evidence"].update(change)
            self.assertEqual(self.probe(frame=frame)["bench"]["model_like_presence"], "unknown")

    def test_synthetic_resource_is_not_model_or_legal_unit(self):
        payload, frame, _ = fixture(model=False)
        r = self.probe(frame=frame, payload=payload)
        self.assertEqual(r["bench"]["object_presence"], "appearance_candidate")
        self.assertEqual(r["bench"]["resource_like_presence"], "appearance_candidate")
        self.assertEqual(r["bench"]["model_like_presence"], "unknown")
        self.assertEqual(r["bench"]["deployable_unit"], "unknown")

    def test_no_match_is_unknown_not_absence(self):
        payload, frame, _ = fixture(model=False, resource=False)
        r = self.probe(frame=frame, payload=payload)
        self.assertEqual(r["bench"]["object_presence"], "unknown")
        self.assertNotIn("absent", json.dumps(r))

    def test_source_mismatch_no_processing(self):
        ocr = FakeOCR()
        self.frame["expected_sha256"] = "0" * 64
        r = self.probe(ocr)
        self.assertEqual(r["reason_codes"], ["source_hash_mismatch"])
        self.assertIsNone(r["bench"])
        self.assertEqual(ocr.calls, 0)

    def test_bad_image_and_wrong_dimensions_fail(self):
        self.frame["image_size"] = [401, 240]
        self.assertEqual(self.probe()["reason_codes"], ["image_geometry_mismatch"])
        data = b"broken image"
        self.frame["expected_sha256"] = hashlib.sha256(data).hexdigest()
        self.assertEqual(self.probe(payload=data)["reason_codes"], ["image_unreadable"])

    def test_no_carry_forward_after_good_frame(self):
        self.assertEqual(self.probe()["capacity"]["empty_slots"], 1)
        self.frame["capacity_trials"] = []
        self.frame["bench"] = None
        r = self.probe()
        self.assertIsNone(r["capacity"]["empty_slots"])
        self.assertEqual(r["bench"]["object_presence"], "unknown")

    def test_live_source_rejected(self):
        self.frame["source_kind"] = "live"
        with self.assertRaises(ValueError):
            self.probe()

    def test_unsupported_reader_revision_rejected_before_ocr(self):
        ocr = FakeOCR()
        with patch("jcc_open_slot_probe.probe.CAPACITY_REVISION", "unverified-old-reader"):
            with self.assertRaises(ValueError):
                self.probe(ocr)
        self.assertEqual(ocr.calls, 0)

    def test_capacity_rotation_cannot_disagree_with_original_frame(self):
        self.frame["capacity_trials"][0]["layout"]["rotation_applied_degrees"] = 90
        self.frame["capacity_trials"][0]["evidence"]["rotation_applied_degrees"] = 90
        ocr = FakeOCR()
        with self.assertRaises(ValueError):
            self.probe(ocr)
        self.assertEqual(ocr.calls, 0)

    def test_viewer_crops_bound_and_renderer_does_not_change_results(self):
        r = self.probe()
        reasons = copy.deepcopy(r["capacity"]["reason_codes"])
        page = _card(r, self.payload)
        self.assertEqual(r["capacity"]["reason_codes"], reasons)
        for which, embedded in zip(["capacity", "bench"], re.findall(r'data:image/png;base64,([^\"]+)', page)):
            data = base64.b64decode(embedded)
            binding = r["previews"][which]
            self.assertEqual(hashlib.sha256(data).hexdigest(), binding["derived_png_sha256"])
            self.assertEqual(binding["source_encoded_sha256"], hashlib.sha256(self.payload).hexdigest())
            with Image.open(io.BytesIO(self.payload)) as im:
                expected = im.convert("RGB").crop(binding["roi"]).tobytes()
            with Image.open(io.BytesIO(data)) as im:
                self.assertEqual(im.tobytes(), expected)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        os.chmod(self.root, 0o700)
        self.payload, self.frame, refs = fixture()
        for ref in refs:
            ref["crop"].close()
        self.image = self.root / "synthetic.png"
        self.image.write_bytes(self.payload)
        self.frame["image_path"] = str(self.image)
        self.config = self.root / "config.json"
        self.write_config()
        self.output = self.root / "run"

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, frames=None):
        self.config.write_text(json.dumps({"schema_version": METHOD, "frames": frames or [self.frame], "references": []}))

    def run_probe(self, **kwargs):
        return run_config(self.config, output=self.output, ocr_factory=kwargs.get("factory", FakeOCR))

    def test_runner_completed_hashes_permissions_and_inputs_unchanged(self):
        before = {p: p.read_bytes() for p in [self.config, self.image]}
        report = self.run_probe()
        marker = json.loads((self.output / "COMPLETED.json").read_bytes())
        for name, sha in marker["file_sha256"].items():
            path = self.output / name
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.read_bytes() == b for p, b in before.items()))
        self.assertEqual(report["human_labels_added"], 0)
        self.assertNotIn("file://", (self.output / "查看线索.html").read_text())

    def test_existing_output_never_overwritten(self):
        self.output.mkdir(mode=0o700)
        sentinel = self.output / "keep"
        sentinel.write_text("keep")
        with self.assertRaises(ValueError):
            self.run_probe()
        self.assertEqual(sentinel.read_text(), "keep")

    def test_output_inside_project_rejected(self):
        with self.assertRaises(ValueError):
            _output_path(Path(__file__).resolve().parent / "forbidden-run", set())

    def test_symlink_output_and_input_rejected(self):
        self.output.symlink_to(self.root / "elsewhere")
        with self.assertRaises(ValueError):
            self.run_probe()
        self.output.unlink()
        linked = self.root / "linked.png"
        linked.symlink_to(self.image)
        self.frame["image_path"] = str(linked)
        self.write_config()
        with self.assertRaises(ValueError):
            self.run_probe()

    def test_bad_input_hash_no_output(self):
        self.image.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.run_probe()
        self.assertFalse(self.output.exists())

    def test_duplicate_input_not_a_new_sample(self):
        self.write_config([self.frame, self.frame])
        with self.assertRaises(ValueError):
            self.run_probe()
        self.assertFalse(self.output.exists())

    def test_wrong_viewing_group_cannot_reuse_source_index(self):
        index = self.root / "source-index.json"
        index.write_text(json.dumps({"frames": [{"alias": self.frame["alias"],
            "source": "different-group", "asset_path": str(self.image),
            "encoded_sha256": self.frame["expected_sha256"]}]}))
        config = json.loads(self.config.read_bytes())
        config["source_index"] = {"path": str(index), "expected_sha256": hashlib.sha256(index.read_bytes()).hexdigest()}
        self.config.write_text(json.dumps(config))
        with self.assertRaises(ValueError):
            self.run_probe()
        self.assertFalse(self.output.exists())

    def test_replay_requires_hash_pinned_source_index(self):
        self.frame["source_kind"] = "replay"
        self.write_config()
        with self.assertRaises((ValueError, KeyError)):
            self.run_probe()
        self.assertFalse(self.output.exists())

    def test_inputs_changed_during_recognition_cannot_complete(self):
        target = self.image

        class MutatingOCR(FakeOCR):
            def recognize(self, payload):
                target.write_bytes(b"changed during recognition")
                return super().recognize(payload)

        with self.assertRaises(ValueError):
            self.run_probe(factory=MutatingOCR)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
