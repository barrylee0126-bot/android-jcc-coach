"""Caller-supplied evidence: uniqueness and binding, never provenance approval."""

from __future__ import annotations

from argparse import Namespace
import contextlib
import copy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib

from jcc_rapid_ocr_candidate import CandidateError
from jcc_rapid_ocr_candidate.contract import blank_png, normalize_raw
from jcc_rapid_ocr_candidate.tests import smoke_offline


def digest(data):
    return hashlib.sha256(data).hexdigest()


def synthetic_png(index):
    # Four visibly different synthetic RGB images of equal dimensions.
    base = blank_png()
    rows = (b"\x00" + bytes([index, 100, 200]) * 96) * 48
    payload = zlib.compress(rows)
    body = b"IDAT" + payload
    return base[:33] + struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body)) + base[-12:]


class HistoricalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jcc-18d-synthetic-history-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "inputs").mkdir()
        self.spec = {"inputs": [], "frames": []}
        self.baseline = {"backend": "rapid_rec", "records": []}
        for index in range(1, 5):
            data = synthetic_png(index)
            relative = f"inputs/synthetic-{index}.png"
            (self.root / relative).write_bytes(data)
            source = digest(f"synthetic source frame {index}".encode())
            self.spec["frames"].append({"order": index, "sample_id": f"synthetic-frame-{index}",
                                        "source_sha256": source,
                                        "source_path": "/not-opened/synthetic-source-frame.png"})
            self.spec["inputs"].append({"id": f"synthetic-crop-{index}", "kind": "gold", "scale": 1,
                                       "frame_order": index, "source_sha256": source, "slot": None,
                                       "path": relative, "png_sha256": digest(data), "size": [96, 48]})
            self.baseline["records"].append({"id": f"synthetic-crop-{index}", "phase": "hot_gold", "cycle": 1,
                                              "kind": "gold", "scale": 1, "frame_order": index,
                                              "slot": None, "input_sha256": digest(data),
                                              "raw": {"txts": ["synthetic"]}})

    def args(self):
        manifest, baseline = self.root / "manifest.json", self.root / "baseline.json"
        manifest.write_text(json.dumps(self.spec))
        manifest_hash = digest(manifest.read_bytes())
        self.baseline["input_manifest_sha256"] = manifest_hash
        baseline.write_text(json.dumps(self.baseline))
        return Namespace(historical_manifest=manifest, expected_manifest_sha256=manifest_hash,
                         baseline=baseline, expected_baseline_sha256=digest(baseline.read_bytes()))

    def rejected(self, reason):
        with self.assertRaisesRegex(CandidateError, f"^{reason}$"):
            smoke_offline.historical(self.args())

    def test_four_distinct_crops_with_distinct_declared_sources_are_accepted(self):
        crops, frozen = smoke_offline.historical(self.args())
        self.assertEqual(len(crops), 4)
        self.assertEqual(len({crop.sha256 for crop, _ in crops}), 4)
        self.assertEqual(len(frozen), 6)
        self.assertTrue(all(text == "synthetic" for _, text in crops))
        # All source_path values are the same nonexistent path: no source read
        # or recording/session-independence claim is required or established.

    def test_duplicate_crop_ids_do_not_get_overwritten_in_the_baseline_map(self):
        self.spec["inputs"][1]["id"] = self.spec["inputs"][0]["id"]
        self.rejected("historical_input_id_not_unique")

    def test_same_synthetic_crop_cannot_count_as_four_original_inputs(self):
        first = self.spec["inputs"][0]
        for entry, record in zip(self.spec["inputs"][1:], self.baseline["records"][1:]):
            entry.update(path=first["path"], png_sha256=first["png_sha256"])
            record["input_sha256"] = first["png_sha256"]
        self.rejected("historical_input_not_unique")

    def test_copied_image_bytes_under_distinct_names_are_still_duplicates(self):
        first, second = self.spec["inputs"][:2]
        (self.root / second["path"]).write_bytes((self.root / first["path"]).read_bytes())
        second["png_sha256"] = self.baseline["records"][1]["input_sha256"] = first["png_sha256"]
        self.rejected("historical_input_not_unique")

    def test_one_source_digest_cannot_be_renamed_as_four_source_frames(self):
        first = self.spec["frames"][0]["source_sha256"]
        for entry, frame in zip(self.spec["inputs"][1:], self.spec["frames"][1:]):
            entry["source_sha256"] = frame["source_sha256"] = first
        self.rejected("historical_source_frame_not_unique")

    def test_repeated_frame_order_or_sample_id_is_not_a_distinct_source(self):
        original = copy.deepcopy(self.spec)
        self.spec["inputs"][1]["frame_order"] = 1
        self.rejected("historical_source_frame_not_unique")
        self.spec = original
        self.spec["frames"][1]["sample_id"] = self.spec["frames"][0]["sample_id"]
        self.rejected("historical_source_frame_not_unique")

    def test_crop_source_digest_must_match_its_declared_frame(self):
        self.spec["inputs"][1]["source_sha256"] = "0" * 64
        self.rejected("historical_source_binding_mismatch")

    def test_missing_or_duplicate_frame_declaration_is_not_a_binding(self):
        original = copy.deepcopy(self.spec)
        self.spec["frames"].pop()
        self.rejected("historical_source_binding_mismatch")
        self.spec = original
        self.spec["frames"].append(copy.deepcopy(self.spec["frames"][0]))
        self.rejected("historical_source_binding_mismatch")

    def test_baseline_record_must_bind_the_same_frame_kind_and_scale(self):
        original = copy.deepcopy(self.baseline)
        for changes in ({"frame_order": 2}, {"kind": "name"}, {"scale": 2}, {"scale": True}):
            self.baseline = copy.deepcopy(original)
            self.baseline["records"][0].update(changes)
            self.rejected("historical_evidence_binding_mismatch")

    def test_repeated_baseline_record_is_rejected_before_map_overwrite(self):
        self.baseline["records"].append(copy.deepcopy(self.baseline["records"][0]))
        self.rejected("historical_baseline_invalid")

    def test_bool_scale_or_frame_order_does_not_mean_integer_one(self):
        original = copy.deepcopy(self.spec)
        self.spec["inputs"][0]["scale"] = True
        self.rejected("historical_scope_mismatch")
        self.spec = original
        self.spec["inputs"][0]["frame_order"] = True
        self.rejected("historical_source_binding_mismatch")

    def test_wrong_bytes_or_expected_manifest_digest_is_rejected(self):
        args = self.args()
        args.expected_manifest_sha256 = "0" * 64
        with self.assertRaisesRegex(CandidateError, "historical_evidence_hash_mismatch"):
            smoke_offline.historical(args)
        (self.root / self.spec["inputs"][0]["path"]).write_bytes(blank_png())
        self.rejected("historical_input_hash_mismatch")

    def test_paths_cannot_escape_the_explicit_crop_directory(self):
        self.spec["inputs"][0]["path"] = "../not-opened.png"
        self.rejected("historical_input_path_invalid")

    def test_caller_supplied_hashes_are_not_reported_as_provenance_or_game_independence(self):
        args = self.args()
        engine = Mock()
        engine.startup = {"process_start_to_ready_ms": 1.0, "network_denied": True,
                          "timing_ms": {"synthetic_startup": 1.0}}
        engine.warmup.return_value = {"warmup_wall_ms": 1.0}
        def transcribe(crop):
            return replace(normalize_raw(crop, {"result_type": "TextRecOutput", "txts": ["synthetic"],
                                               "scores": [0.9], "boxes": None, "inference_ms": 1.0}, 0.5),
                           request_wall_ms=1.0)
        engine.transcribe.side_effect = transcribe
        context = MagicContext(engine)
        argv = ["smoke_offline", "--environment", str(self.root), "--wheelhouse", str(self.root),
                "--rec-model", str(self.root), "--historical-manifest", str(args.historical_manifest),
                "--expected-manifest-sha256", args.expected_manifest_sha256, "--baseline", str(args.baseline),
                "--expected-baseline-sha256", args.expected_baseline_sha256]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(smoke_offline, "RuntimeConfig"), \
                patch.object(smoke_offline, "RapidOCRCandidate", return_value=context), \
                contextlib.redirect_stdout(output):
            code = smoke_offline.main()
        summary = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(summary["original_input_count"], 4)
        self.assertEqual(summary["unique_crop_png_count"], 4)
        self.assertEqual(summary["declared_unique_source_frame_count"], 4)
        self.assertEqual(summary["request_count"], 72)
        self.assertIs(summary["manifest_provenance_authenticated"], False)
        self.assertIs(summary["source_frames_independently_verified"], False)
        self.assertIsNone(summary["independent_game_count"])


class MagicContext:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        return self.engine

    def __exit__(self, *args):
        return None


if __name__ == "__main__":
    unittest.main()
