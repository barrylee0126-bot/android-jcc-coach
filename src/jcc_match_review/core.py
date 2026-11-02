"""Build a private view of fixed evidence, without running recognition again."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import re

from PIL import Image

from . import VERSION
from .private import IMAGE_LIMIT, PinnedInputs, ReviewError, digest, json_document, private_dir, read_private, safe_path, write_new
from .render import render_page


THREADS = {"20a": "01a19c32-65b4-7e03-9559-ca7fbd052179", "20b": "01a19c32-67b7-7de2-bcb5-e78669dd1364"}


def require_idle(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("errors"):
        raise ReviewError("dependency_idle_evidence_missing")
    polls = snapshot.get("polls", [])
    for thread_id in THREADS.values():
        entries = [item for item in polls if item.get("thread", {}).get("id") == thread_id]
        if (len(entries) != 1 or entries[0]["thread"].get("status", {}).get("type") != "idle"
                or entries[0].get("latestTurn", {}).get("status") != "completed"):
            raise ReviewError("dependency_not_idle")


def read_image(inputs, pin):
    raw = inputs.read(pin, limit=IMAGE_LIMIT)
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if (image.format not in ("PNG", "JPEG") or getattr(image, "n_frames", 1) != 1
                    or image.width * image.height > 16_000_000 or image.getexif().get(274, 1) != 1):
                raise ReviewError("image_geometry_invalid")
            image.load()
            size = [image.width, image.height]
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ReviewError("image_invalid") from None
    return raw, size


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def run_review(manifest_path):
    from .adapters import build_frame

    manifest_path = safe_path(manifest_path)
    raw_manifest = read_private(manifest_path)
    manifest = json_document(raw_manifest)
    if (manifest.get("schema_version") != VERSION or manifest.get("purpose") != "private_match_review_only"
            or manifest.get("source_kind") != "recovered_recording_development"):
        raise ReviewError("manifest_scope_invalid")
    output = safe_path(manifest["output_root"])
    if output.exists():
        raise ReviewError("output_exists")
    private_dir(output.parent)
    inputs = PinnedInputs(manifest["source_root"], manifest["pins"], output)
    require_idle(inputs.json(manifest["idle_snapshot_pin"]))
    inputs.fingerprint(manifest["source_video_pin"])
    video_hash = inputs.pins[manifest["source_video_pin"]]["sha256"]
    specs = manifest.get("frames")
    if not isinstance(specs, list) or not 3 <= len(specs) <= 18:
        raise ReviewError("frame_allowlist_invalid")
    frames, images, ids = [], {}, set()
    for spec in specs:
        key = spec.get("frame_id")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", key) or key in ids:
            raise ReviewError("frame_identity_invalid")
        raw, size = read_image(inputs, spec["image_pin"])
        frame = build_frame(spec, inputs, source_hash=digest(raw), size=size, video_hash=video_hash, raw=raw)
        frames.append(frame)
        images[key] = raw
        ids.add(key)
    cases = manifest.get("cases")
    if (not isinstance(cases, list) or not cases or any(not isinstance(case, dict)
            or not isinstance(case.get("frame_ids"), list) or not case["frame_ids"]
            or any(key not in ids for key in case["frame_ids"]) for case in cases)):
        raise ReviewError("case_frame_binding_invalid")
    if set(key for case in cases for key in case["frame_ids"]) != ids:
        raise ReviewError("unreferenced_frame")
    report = {"schema_version": VERSION, "generated_utc": datetime.now(timezone.utc).isoformat(),
              "source_kind": "historical_development", "video_sha256": video_hash,
              "manifest_sha256": digest(raw_manifest), "frames": frames, "cases": cases,
              "frame_titles": {frame["frame_id"]: frame["title"] for frame in frames},
              "human_ground_truth_count": 0, "independent_review_count": 0, "accuracy": None,
              "advice_eligible": False, "automatic_action": None}
    page = render_page(report, images)
    inputs.verify_unchanged()
    if digest(read_private(manifest_path)) != digest(raw_manifest):
        raise ReviewError("manifest_changed_during_review")
    report["read_only_verification"] = {"status": "unchanged", "pin_count": len(inputs.used),
                                         "source_files_modified": False}
    # A fresh private output directory is the only write destination.
    private_dir(output, create=True)
    write_new(output / "report.html", page.encode("utf-8"))
    write_new(output / "review.private.json", encode(report))
    write_new(output / "sources.private.json", encode({key: inputs.pins[key] for key in sorted(inputs.used)}))
    summary = {"schema_version": VERSION, "status": "private_report_created", "frames": len(frames),
               "cases": len(cases), "human_ground_truth_count": 0, "accuracy": None,
               "advice_eligible": False, "automatic_action": None, "source_files_unchanged": True}
    write_new(output / "summary.json", encode(summary))
    write_new(output / "COMPLETED.json", encode({"schema_version": VERSION, "status": "completed",
              "files": {"report.html": digest(page.encode("utf-8")), "review.private.json": digest(encode(report)),
                        "summary.json": digest(encode(summary))}}))
    return output / "report.html", summary
