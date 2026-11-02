"""Explicit private-file CLI. No discovery, sample updates, devices or actions."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from .probe import METHOD, IMPLEMENTATION_REVISION, FrameEvidence, ProbeLayout, probe_capacity


def _private_json(path: Path, value: dict, *, inputs: set[Path]) -> None:
    target = path.expanduser().resolve()
    project = Path(__file__).resolve().parents[2]
    if target == project or project in target.parents or target in inputs or target.suffix != ".json":
        raise ValueError("private_report_outside_checkout_required")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.parent.stat().st_mode & 0o077:
        raise ValueError("report_directory_must_be_private_0700")
    fd, temporary = tempfile.mkstemp(prefix="capacity-report-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_config(config_path: Path, *, report_path: Path | None = None) -> dict:
    """Read only an explicit, hash-pinned image allowlist; no workspace scanning.

    The private config is an experiment declaration, not sample annotations.
    No OCR text or source filename is put in results. File/worker startup and
    per-frame processing are timed separately; no historical/game latency is
    inferred. All files are preflighted before starting the worker.
    """
    from jcc_device.vision_ocr import VisionOCR

    started = time.perf_counter()
    config_path = config_path.expanduser().resolve()
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    if config.get("schema_version") != METHOD or not isinstance(config.get("frames"), list):
        raise ValueError("invalid_config")
    if not 1 <= len(config["frames"]) <= 128:
        raise ValueError("explicit_nonempty_frame_allowlist_required")
    layout = ProbeLayout(**config["layout"])
    frames, inputs, seen = [], {config_path}, set()
    read_started = time.perf_counter()
    for row in config["frames"]:
        path = Path(row["image_path"]).expanduser().resolve()
        if not path.is_file() or path.stat().st_size > 25 * 1024 * 1024:
            raise ValueError("input_file_unavailable_or_too_large")
        sha = row["expected_sha256"]
        if sha in seen:
            raise ValueError("duplicate_source_frame")
        seen.add(sha)
        evidence = FrameEvidence(**row["evidence"])
        payload = path.read_bytes()
        frames.append((payload, sha, evidence))
        inputs.add(path)
    read_seconds = time.perf_counter() - read_started
    results = []
    worker_started = time.perf_counter()
    with VisionOCR() as ocr:
        startup_seconds = time.perf_counter() - worker_started
        for payload, sha, evidence in frames:
            result = probe_capacity(payload, source_kind=layout.source_kind, layout=layout,
                                    evidence=evidence, expected_sha256=sha, ocr=ocr)
            results.append(result.to_dict())
    report = {
        "schema_version": METHOD, "implementation_revision": IMPLEMENTATION_REVISION,
        "experiment_role": "development_only_not_human_ground_truth",
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "frame_count": len(results),
        "candidate_count": sum(r["status"] == "development_candidate" for r in results),
        "unknown_count": sum(r["status"] == "unknown" for r in results),
        "human_ground_truth_count": 0, "independent_review_count": 0, "accuracy": None,
        "timing": {
            "image_file_reads_seconds": read_seconds,
            "worker_process_start_seconds": startup_seconds,
            "total_seconds_before_report_write": time.perf_counter() - started,
            "scope": "offline_mac_only_not_capture_to_hint_or_game_latency",
        },
        "results": results,
    }
    if report_path is not None:
        _private_json(report_path, report, inputs=inputs)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline, opt-in capacity display candidate probe; never accesses a device.")
    parser.add_argument("--config", type=Path, required=True, help="Private JSON with exact layout and hash-pinned image paths")
    parser.add_argument("--report", type=Path, required=True, help="Private JSON output outside this checkout, in a 0700 directory")
    args = parser.parse_args(argv)
    try:
        report = run_config(args.config, report_path=args.report)
    except Exception:
        print(json.dumps({"schema_version": METHOD, "status": "unknown", "reason_codes": ["offline_run_failed"],
                          "accuracy": None, "automatic_action": None}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
