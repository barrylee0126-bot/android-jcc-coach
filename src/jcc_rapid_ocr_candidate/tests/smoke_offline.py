"""Optional local-only smoke. Emits aggregate timings, never images or OCR text.

Default inputs are synthetic. Historical mode requires all four explicit paths/
digests and four unique gold 1x PNG byte strings bound to four distinct declared
source frames. Caller-supplied hashes bind files, not provenance or independent
games. Source images are never opened. This is output consistency, not accuracy
or live-use authorization.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct

from jcc_rapid_ocr_candidate import CandidateError, CropRequest, FixedROI, RapidOCRCandidate, RuntimeConfig
from jcc_rapid_ocr_candidate.contract import blank_png
from jcc_rapid_ocr_candidate.__main__ import timing_summary


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def historical(args):
    manifest, baseline_path = args.historical_manifest, args.baseline
    manifest_data, baseline_data = manifest.read_bytes(), baseline_path.read_bytes()
    if (hashlib.sha256(manifest_data).hexdigest() != args.expected_manifest_sha256
            or hashlib.sha256(baseline_data).hexdigest() != args.expected_baseline_sha256):
        raise CandidateError("historical_evidence_hash_mismatch")
    spec, baseline = json.loads(manifest_data), json.loads(baseline_data)
    if not isinstance(spec, dict) or not isinstance(baseline, dict):
        raise CandidateError("historical_scope_mismatch")
    if baseline.get("backend") != "rapid_rec" or baseline.get("input_manifest_sha256") != args.expected_manifest_sha256:
        raise CandidateError("historical_evidence_binding_mismatch")
    inputs, frames, records = spec.get("inputs"), spec.get("frames"), baseline.get("records")
    if (not isinstance(inputs, list) or any(not isinstance(v, dict) for v in inputs)
            or not isinstance(frames, list) or any(not isinstance(v, dict) for v in frames)
            or not isinstance(records, list) or any(not isinstance(v, dict) for v in records)):
        raise CandidateError("historical_scope_mismatch")
    entries = [v for v in inputs if v.get("kind") == "gold" and type(v.get("scale")) is int and v["scale"] == 1]
    if len(entries) != 4:
        raise CandidateError("historical_scope_mismatch")
    ids = [v.get("id") for v in entries]
    if any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != 4:
        raise CandidateError("historical_input_id_not_unique")
    orders = [v.get("frame_order") for v in entries]
    sources = [v.get("source_sha256") for v in entries]
    if (any(type(v) is not int or v < 1 for v in orders)
            or any(not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v)
                   for v in sources)):
        raise CandidateError("historical_source_binding_mismatch")
    if len(set(orders)) != 4 or len(set(sources)) != 4:
        raise CandidateError("historical_source_frame_not_unique")
    selected_frames = {}
    for frame in frames:
        order = frame.get("order")
        if type(order) is int and order in orders:
            if order in selected_frames:
                raise CandidateError("historical_source_binding_mismatch")
            selected_frames[order] = frame
    if set(selected_frames) != set(orders):
        raise CandidateError("historical_source_binding_mismatch")
    for entry in entries:
        if selected_frames[entry["frame_order"]].get("source_sha256") != entry["source_sha256"]:
            raise CandidateError("historical_source_binding_mismatch")
    sample_ids = [v.get("sample_id") for v in selected_frames.values()]
    if any(not isinstance(v, str) or not v for v in sample_ids):
        raise CandidateError("historical_source_binding_mismatch")
    if len(set(sample_ids)) != 4:
        raise CandidateError("historical_source_frame_not_unique")
    # These checks establish only consistency of the caller's source claims.
    # Never open frame.source_path or infer recording/session independence.
    expected = {}
    for record in records:
        if (record.get("phase") == "hot_gold" and type(record.get("cycle")) is int
                and record["cycle"] == 1 and record.get("id") in ids):
            if record["id"] in expected:
                raise CandidateError("historical_baseline_invalid")
            expected[record["id"]] = record
    if set(expected) != set(ids):
        raise CandidateError("historical_baseline_invalid")
    crops, frozen = [], {manifest: args.expected_manifest_sha256, baseline_path: args.expected_baseline_sha256}
    seen_pngs = set()
    input_root = manifest.parent / "inputs"
    for entry in entries:
        relative = Path(entry["path"])
        path = manifest.parent / relative
        if (not relative.parts or relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "inputs"
                or path.suffix != ".png" or path.is_symlink() or input_root.is_symlink()
                or not path.resolve().is_relative_to(input_root.resolve())):
            raise CandidateError("historical_input_path_invalid")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        reference = expected[entry["id"]]
        if digest != entry["png_sha256"] or digest != reference["input_sha256"]:
            raise CandidateError("historical_input_hash_mismatch")
        if digest in seen_pngs:
            raise CandidateError("historical_input_not_unique")
        seen_pngs.add(digest)
        if (reference.get("kind") != "gold" or type(reference.get("scale")) is not int
                or reference["scale"] != 1 or type(reference.get("frame_order")) is not int
                or reference["frame_order"] != entry["frame_order"]
                or entry.get("slot") is not None or reference.get("slot") is not None):
            raise CandidateError("historical_evidence_binding_mismatch")
        texts = reference["raw"]["txts"]
        if not isinstance(texts, list) or len(texts) != 1 or not isinstance(texts[0], str):
            raise CandidateError("historical_baseline_invalid")
        if len(data) < 33 or not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
            raise CandidateError("historical_input_format_invalid")
        width, height = struct.unpack(">II", data[16:24])
        if entry.get("size") != [width, height]:
            raise CandidateError("historical_crop_size_mismatch")
        roi = FixedROI("historical.fixed-crop", width, height)
        if crops and roi != crops[0][0].roi:
            raise CandidateError("historical_crop_size_mismatch")
        # Dimensions bind the input protocol; they do not certify full-frame
        # origin, field identity, calibration or detected text extents.
        crops.append((CropRequest(roi, data), texts[0]))
        frozen[path] = digest
    return crops, frozen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--rec-model", type=Path, required=True)
    parser.add_argument("--historical-manifest", type=Path)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--expected-baseline-sha256")
    args = parser.parse_args()
    optional = (args.historical_manifest, args.expected_manifest_sha256,
                args.baseline, args.expected_baseline_sha256)
    if any(v is not None for v in optional) and not all(v is not None for v in optional):
        parser.error("historical mode requires both existing files and both expected SHA-256 values")
    try:
        config = RuntimeConfig(args.environment, args.wheelhouse, args.rec_model)
        if all(v is not None for v in optional):
            crops, frozen = historical(args)
            mode = "four_caller_bound_historical_crops_not_accuracy"
        else:
            crops = [(CropRequest(FixedROI("synthetic.blank", 96, 48), blank_png()), "")]
            frozen, mode = {}, "synthetic_only"
        records, startups, warmups = [], [], []
        for _ in range(3):
            with RapidOCRCandidate(config) as engine:
                startups.append(engine.startup)
                warmups.append(engine.warmup()["warmup_wall_ms"])
                for _ in range(6):
                    for crop, expected_text in crops:
                        result = engine.transcribe(crop)
                        expected_status = "development_candidate" if expected_text.strip() else "unknown"
                        consistent = (result.status == expected_status
                                      and result.raw_transcription == expected_text
                                      and result.to_dict()["geometry_evidence"] is False
                                      and result.to_dict()["eligible_for_execution"] is False)
                        records.append({"status": result.status, "consistent": consistent,
                                        "wall_ms": result.request_wall_ms, "inference_ms": result.inference_ms})
                        if result.status == "error":
                            raise CandidateError(result.reason)
        intact = all(sha(path) == expected for path, expected in frozen.items())
        summary = {
            "schema_version": "rapid-candidate-aggregate-smoke-v1", "mode": mode,
            "status": "passed" if intact and all(r["consistent"] for r in records) else "failed",
            "original_input_count": len(crops), "fresh_process_count": len(startups),
            "original_input_count_scope": "unique_encoded_crop_bytes",
            "unique_crop_png_count": len({crop.sha256 for crop, _ in crops}),
            "declared_unique_source_frame_count": len(crops) if frozen else 0,
            "manifest_provenance_authenticated": False,
            "source_frames_independently_verified": False, "independent_game_count": None,
            "repetitions_per_input_per_process": 6, "request_count": len(records),
            "consistent_request_count": sum(r["consistent"] for r in records),
            "inputs_and_baseline_unchanged": intact,
            "startup_ms": timing_summary([s["process_start_to_ready_ms"] for s in startups]),
            "startup_components_ms": {key: timing_summary([s["timing_ms"][key] for s in startups])
                                      for key in startups[0]["timing_ms"]},
            "warmup_ms": timing_summary(warmups),
            "request_wall_ms": timing_summary([r["wall_ms"] for r in records]),
            "inference_ms": timing_summary([r["inference_ms"] for r in records]),
            "network_denied_all_processes": all(s["network_denied"] for s in startups),
            "providers": ["CPUExecutionProvider"], "human_label_count": 0,
            "accuracy": None, "evidence_kind": "transcription_only",
            "geometry_evidence": False, "live_use_authorized": False,
            "eligible_for_geometry_gate": False, "eligible_for_policy": False, "eligible_for_execution": False,
        }
        print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
        return 0 if summary["status"] == "passed" else 1
    except (CandidateError, OSError, KeyError, ValueError, TypeError, IndexError) as exc:
        reason = exc.code if isinstance(exc, CandidateError) else "smoke_evidence_invalid"
        print(json.dumps({"status": "error", "reason": reason, "live_use_authorized": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
