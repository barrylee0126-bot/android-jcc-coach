"""Run one private offline video-frame candidate through local Apple Vision."""

from __future__ import annotations

import argparse
import json

from .adapter import APPLE_VISION_WORKER, PROJECT, AdapterError, _dependency_hashes, analyze_index


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Private offline video frame; no real game advice or action.")
    parser.add_argument("--index", required=True, help="Absolute path to a private index outside the project")
    parser.add_argument("--index-sha256", required=True, help="Pinned SHA-256 of the exact private index bytes")
    parser.add_argument("--hypothetical-spend", required=True, type=int, help="One explicitly hypothetical nonnegative integer")
    args = parser.parse_args(argv)
    try:
        from jcc_device.vision_ocr import VisionOCR, WORKER

        if WORKER != PROJECT / APPLE_VISION_WORKER:
            raise AdapterError("apple_vision_worker_binding_invalid")
        # Fingerprint before the worker starts, as well as inside the run.
        dependencies_before_start = _dependency_hashes(apple_vision_worker=True)
        with VisionOCR() as ocr:
            result = analyze_index(args.index, args.index_sha256,
                                   hypothetical_spend=args.hypothetical_spend, ocr=ocr)
        if (result["dependency_sha256"] != dependencies_before_start
                or _dependency_hashes(apple_vision_worker=True) != dependencies_before_start):
            raise AdapterError("dependency_changed_during_ocr")
    except AdapterError as exc:
        print(json.dumps({"status": "blocked", "reason": exc.code, "action": None,
                          "production_eligible": False, "execution_eligible": False}))
        return 1
    except Exception:
        # Paths, OCR content and backend errors can contain private information.
        print(json.dumps({"status": "blocked", "reason": "local_backend_unavailable",
                          "action": None, "production_eligible": False, "execution_eligible": False}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["status"] in ("cash_only", "insufficient_candidate_gold") else 1


if __name__ == "__main__":
    raise SystemExit(main())
