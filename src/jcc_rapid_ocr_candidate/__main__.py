"""Opt-in offline CLI. Help and synthetic checks need only Python's stdlib."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

from .contract import (CandidateError, CropRequest, FixedROI, MAX_IMAGE_BYTES,
                       blank_png, normalize_raw, reconcile)
from .runtime import RapidOCRCandidate, RuntimeConfig


def synthetic_check():
    request = CropRequest(FixedROI("synthetic.example", 96, 48), blank_png())

    def fake(text, score=0.9):
        return normalize_raw(request, {"result_type": "TextRecOutput", "txts": [text],
                             "scores": [score], "boxes": None, "inference_ms": 0.0}, 0.5)

    normal, different, blank, low = fake("42"), fake("24"), fake(""), fake("42", 0.1)
    conflict, incomplete = reconcile([normal, different]), reconcile([normal, low])
    checks = [normal.status == "development_candidate", blank.status == "unknown",
              low.transcription is None, conflict.reason == "transcription_conflict",
              incomplete.reason == "comparison_incomplete",
              all(x.to_dict()["geometry_evidence"] is False for x in (normal, blank, conflict)),
              all(x.to_dict()["eligible_for_execution"] is False for x in (normal, blank, conflict))]
    return {"status": "passed" if all(checks) else "error", "checks": len(checks),
            "synthetic_only": True, "real_model_loaded": False,
            "evidence_kind": "transcription_only", "geometry_evidence": False,
            "human_annotation": False, "live_use_authorized": False,
            "eligible_for_policy": False, "eligible_for_execution": False}


def timing_summary(values):
    ordered = sorted(values)
    return {"n": len(values), "min": ordered[0], "p50": statistics.median(values),
            "p95_nearest_rank": ordered[math.ceil(0.95 * len(values)) - 1], "max": ordered[-1]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-check", action="store_true", help="fake outputs only; no optional imports")
    parser.add_argument("--environment", type=Path, help="existing audited Python 3.11 venv, absolute path")
    parser.add_argument("--wheelhouse", type=Path, help="existing 24 official 18A wheels, absolute path")
    parser.add_argument("--rec-model", type=Path, help="explicit existing PP-OCRv6_rec_small.onnx, absolute path")
    parser.add_argument("--warmup-only", action="store_true")
    parser.add_argument("--crop", type=Path, help="already cropped local PNG, absolute path")
    parser.add_argument("--input-sha256", help="expected complete PNG digest; required with --crop")
    parser.add_argument("--roi-id")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--minimum-raw-score", type=float, default=0.5)
    parser.add_argument("--include-transcription", action="store_true", help="include private machine text on stdout")
    args = parser.parse_args(argv)
    if args.synthetic_check:
        if any(v is not None for v in (args.environment, args.wheelhouse, args.rec_model, args.crop)) or args.warmup_only:
            parser.error("--synthetic-check cannot be combined with runtime/image options")
        checked = synthetic_check()
        print(json.dumps(checked, ensure_ascii=False))
        return 0 if checked["status"] == "passed" else 2
    if any(v is None for v in (args.environment, args.wheelhouse, args.rec_model)):
        parser.error("--environment, --wheelhouse and --rec-model are required")
    if not 1 <= args.repeat <= 100 or args.warmup_only == (args.crop is not None):
        parser.error("choose --warmup-only or --crop, with --repeat in 1..100")
    try:
        config = RuntimeConfig(args.environment, args.wheelhouse, args.rec_model,
                               args.timeout_seconds, args.minimum_raw_score)
        request = None
        if args.crop is not None:
            if not args.crop.is_absolute() or not args.crop.is_file() or args.crop.stat().st_size > MAX_IMAGE_BYTES:
                raise CandidateError("invalid_image_bytes")
            data = args.crop.read_bytes()
            if hashlib.sha256(data).hexdigest() != args.input_sha256:
                raise CandidateError("input_hash_mismatch")
            request = CropRequest(FixedROI(args.roi_id, args.width, args.height), data)
        with RapidOCRCandidate(config) as backend:
            warmup = backend.warmup()
            results = []
            for _ in range(args.repeat if request is not None else 0):
                result = backend.transcribe(request)
                results.append(result)
                if result.status == "error":
                    break
            output = {"startup": backend.startup, "warmup": warmup,
                      "evidence_kind": "transcription_only", "geometry_evidence": False,
                      "human_annotation": False, "live_use_authorized": False,
                      "eligible_for_policy": False, "eligible_for_execution": False}
            if results:
                comparison = reconcile(results) if len(results) > 1 else results[0]
                output["result"] = comparison.to_dict()
                if not args.include_transcription:
                    output["result"].pop("transcription")
                    output["result"].pop("raw_transcription")
                    output["transcription_omitted"] = True
                output["request_wall_ms"] = timing_summary([r.request_wall_ms for r in results if r.request_wall_ms is not None])
                output["comparison"] = {"status": comparison.status, "reason": comparison.reason}
                output["status"] = comparison.status
            else:
                output["status"] = "warmed"
            print(json.dumps(output, ensure_ascii=False, allow_nan=False))
            if any(r.status == "error" for r in results):
                return 2
            return 3 if output["status"] == "unknown" else 0
    except (CandidateError, OSError) as exc:
        reason = exc.code if isinstance(exc, CandidateError) else "local_file_read_failed"
        print(json.dumps({"status": "error", "reason": reason,
                          "evidence_kind": "transcription_only", "geometry_evidence": False,
                          "human_annotation": False, "live_use_authorized": False,
                          "eligible_for_policy": False, "eligible_for_execution": False}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
