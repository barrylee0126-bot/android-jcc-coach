"""Opt-in real-runtime negative checks; all pictures and bad models are synthetic.

Uses the same explicitly supplied audited environment. Temporary files stay in a
private temporary directory and are removed. Does not modify the environment or model.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib

from jcc_rapid_ocr_candidate import CandidateError, CropRequest, FixedROI, RapidOCRCandidate, RuntimeConfig
from jcc_rapid_ocr_candidate.contract import MODEL_BYTES, blank_png
from jcc_rapid_ocr_candidate.runtime import reviewed_interpreter


def expect_start_failure(config, expected):
    engine = RapidOCRCandidate(config)
    try:
        engine.start()
    except CandidateError as exc:
        if exc.code != expected or engine._worker is not None:
            raise CandidateError("negative_smoke_unexpected_failure") from None
    else:
        engine.close()
        raise CandidateError("negative_smoke_unexpected_success")


def with_orientation(data):
    tiff = (b"II" + struct.pack("<HIH", 42, 8, 1)
            + struct.pack("<HHIHHI", 274, 3, 1, 6, 0, 0))
    kind = b"eXIf"
    chunk = struct.pack(">I", len(tiff)) + kind + tiff + struct.pack(">I", zlib.crc32(kind + tiff))
    return data[:33] + chunk + data[33:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--rec-model", type=Path, required=True)
    args = parser.parse_args()
    checks = []
    try:
        config = RuntimeConfig(args.environment, args.wheelhouse, args.rec_model)
        worker = Path(__file__).resolve().parents[1] / "worker.py"
        # Direct invocation outside the OS policy must refuse before optional
        # imports or image/model reads; this process performs no network probe.
        done = subprocess.run([str(reviewed_interpreter(config.environment)), "-I", "-B", "-S",
                               "-X", "pycache_prefix=/dev/null/jcc-rapid-no-bytecode", str(worker)],
                              env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=10)
        size = struct.unpack(">I", done.stdout[:4])[0]
        message = json.loads(done.stdout[4:4 + size])
        if done.returncode != 1 or message != {"status": "error", "reason": "network_isolation_failed"}:
            raise CandidateError("negative_smoke_unguarded_worker")
        checks.append("unguarded_worker_refuses_without_connecting")
        with tempfile.TemporaryDirectory(prefix="jcc-rapid-negative-smoke-") as directory:
            root = Path(directory)
            try:
                replace(config, rec_model=root / "missing.onnx")
            except CandidateError as exc:
                if exc.code != "local_runtime_path_missing":
                    raise
            else:
                raise CandidateError("negative_smoke_missing_model_accepted")
            checks.append("missing_model_no_packaged_fallback")
            bad_model = root / "synthetic.onnx"
            bad_model.write_bytes(b"not_a_model")
            expect_start_failure(replace(config, rec_model=bad_model), "invalid_local_model")
            checks.append("invalid_model_no_download")
            bad_model.write_bytes(b"\x00" * MODEL_BYTES)
            expect_start_failure(replace(config, rec_model=bad_model), "model_hash_mismatch")
            checks.append("same_size_wrong_model_rejected")
            empty_wheels = root / "empty_wheels"
            empty_wheels.mkdir()
            expect_start_failure(replace(config, wheelhouse=empty_wheels), "dependency_wheel_set_mismatch")
            checks.append("missing_wheels_no_install")
        valid_png = blank_png()
        good = CropRequest(FixedROI("synthetic.negative", 96, 48), valid_png)
        examples = [
            (CropRequest(good.roi, valid_png[:-10]), "invalid_image_bytes"),
            (CropRequest(FixedROI("synthetic.negative", 95, 48), valid_png), "invalid_image_geometry"),
            (CropRequest(good.roi, with_orientation(valid_png)), "invalid_image_geometry"),
        ]
        for label, (request, expected) in zip(("corrupt_png", "size_mismatch", "unapplied_exif"), examples):
            with RapidOCRCandidate(config) as engine:
                engine.warmup()
                result = engine.transcribe(request)
                if result.status != "error" or result.reason != expected or result.transcription is not None:
                    raise CandidateError("negative_smoke_crop_accepted")
                if engine.transcribe(good).reason != "backend_not_started":
                    raise CandidateError("negative_smoke_automatic_retry")
            checks.append(label + "_closes_worker_without_retry")
        print(json.dumps({"status": "passed", "checks": checks, "check_count": len(checks),
                          "synthetic_only": True, "evidence_kind": "transcription_only",
                          "geometry_evidence": False, "human_annotation": False,
                          "live_use_authorized": False, "eligible_for_execution": False}))
        return 0
    except (CandidateError, OSError, ValueError, KeyError, struct.error, subprocess.TimeoutExpired) as exc:
        code = exc.code if isinstance(exc, CandidateError) else "negative_smoke_failed"
        print(json.dumps({"status": "error", "reason": code, "completed_checks": checks,
                          "synthetic_only": True, "live_use_authorized": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
