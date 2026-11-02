"""Bind one private decoded video frame to OCR and a cash-only comparison.

Nothing here publishes a game state, recommendation, action, or human label.
The input index is a private caller declaration; the original video, seek
target and exact frame pixels are checked before either OCR or calculation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

from PIL import Image, UnidentifiedImageError

from jcc_device.latest import Frame, FrameRef
from jcc_device.recognition import PixelROI
from jcc_device.roi_recognition import (
    CANDIDATE, MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, FieldROI, ImageGeometry,
    ROILayout, probe_frame, reconcile_variants,
)
from jcc_economy_lab import GoldCandidate, HypotheticalPrice, MAX_EXACT_INTEGER, analyze_purchases
from jcc_replay_demo.gold import check_gold_glyph


PROJECT = Path(__file__).resolve().parents[2]
INDEX_SCHEMA = "video-economy-private-index-v1"
RESULT_SCHEMA = "video-economy-candidate-v1"
MAX_INDEX_BYTES = 128 * 1024
MAX_VIDEO_BYTES = 64 * 1024 * 1024 * 1024
SEEK_METHOD = "ffmpeg_input_seek_first_frame"
DEPENDENCIES = (
    "src/jcc_video_economy/adapter.py", "src/jcc_video_economy/__init__.py",
    "src/jcc_video_economy/__main__.py", "src/jcc_device/latest.py",
    "src/jcc_device/recognition.py", "src/jcc_device/roi_recognition.py",
    "src/jcc_device/vision_ocr.py", "src/jcc_replay_demo/gold.py",
    "src/jcc_economy_lab/__init__.py", "src/jcc_economy_lab/assumptions.py",
    "src/jcc_economy_lab/core.py",
)
APPLE_VISION_SOURCE = "src/jcc_device/vision_ocr.swift"
APPLE_VISION_WORKER = "tools/vision_ocr"


class AdapterError(ValueError):
    """A fixed reason code; never include local paths or raw OCR contents."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str):
    raise AdapterError(code)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _valid_hash(value) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _integer(value, maximum=MAX_EXACT_INTEGER) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _dependency_hashes(*, apple_vision_worker: bool = False) -> dict:
    result = {}
    try:
        for name in DEPENDENCIES:
            path = PROJECT / name
            # A missing dependency cannot serve as evidence of a stable run.
            result[name] = _sha(path.read_bytes())
        if apple_vision_worker:
            worker = PROJECT / APPLE_VISION_WORKER
            if not worker.is_file() or not os.access(worker, os.X_OK):
                _fail("apple_vision_worker_unavailable")
            try:
                result[APPLE_VISION_WORKER] = _sha(worker.read_bytes())
            except OSError:
                _fail("apple_vision_worker_unavailable")
            # Record the public build input too; the pair of hashes is not
            # proof that this executable was actually built from this source.
            result[APPLE_VISION_SOURCE] = _sha((PROJECT / APPLE_VISION_SOURCE).read_bytes())
        executable = shutil.which("ffmpeg")
        if not executable:
            _fail("ffmpeg_unavailable")
        with open(executable, "rb") as file:
            result["ffmpeg_binary_sha256"] = hashlib.file_digest(file, "sha256").hexdigest()
    except AdapterError:
        raise
    except (OSError, ValueError):
        _fail("dependency_unavailable")
    return result


def _private_file(path) -> tuple[object, os.stat_result]:
    """Open an external, regular, non-symlink leaf without exposing its name."""
    if not isinstance(path, (str, Path)):
        _fail("private_path_invalid")
    try:
        path = Path(path)
        if not path.is_absolute():
            _fail("private_path_invalid")
        resolved = path.resolve(strict=True)
        if resolved == PROJECT or resolved.is_relative_to(PROJECT):
            _fail("private_path_inside_project")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            _fail("private_file_not_regular")
        return os.fdopen(fd, "rb", buffering=0), info
    except AdapterError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError):
        _fail("private_file_unavailable")


def _stable(file, before: os.stat_result) -> bool:
    after = os.fstat(file.fileno())
    return ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns))


def _same_file_state(a: os.stat_result, b: os.stat_result) -> bool:
    return ((a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns)
            == (b.st_dev, b.st_ino, b.st_size, b.st_mtime_ns))


def _read_private(path, maximum: int) -> bytes:
    file, before = _private_file(path)
    with file:
        if not 0 < before.st_size <= maximum:
            _fail("private_file_size_invalid")
        data = file.read(maximum + 1)
        if len(data) != before.st_size or not _stable(file, before):
            _fail("private_file_changed")
        return data


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("index_duplicate_key")
        result[key] = value
    return result


def _bad_constant(_value):
    _fail("index_nonfinite_number")


def _exact_keys(value, names) -> bool:
    return type(value) is dict and set(value) == set(names)


def _load_index(path, expected_hash: str) -> tuple[dict, str]:
    if not _valid_hash(expected_hash):
        _fail("index_hash_required")
    raw = _read_private(path, MAX_INDEX_BYTES)
    index_hash = _sha(raw)
    if index_hash != expected_hash:
        _fail("index_hash_mismatch")
    try:
        index = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_bad_constant)
    except AdapterError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _fail("index_invalid_json")
    if not _exact_keys(index, ("schema_version", "source_kind", "capture_kind", "video", "frame", "roi", "agent_visual")):
        _fail("index_shape_invalid")
    if (index["schema_version"] != INDEX_SCHEMA or index["source_kind"] != "replay"
            or index["capture_kind"] != "video_frame"):
        _fail("index_source_mismatch")
    video, frame, roi, visual = (index[key] for key in ("video", "frame", "roi", "agent_visual"))
    if not _exact_keys(video, ("path", "sha256")) or not _valid_hash(video["sha256"]):
        _fail("video_binding_invalid")
    if not _exact_keys(frame, ("path", "sha256", "seek_target_ms", "seek_method", "image_size")):
        _fail("frame_binding_invalid")
    if (not _valid_hash(frame["sha256"]) or not _integer(frame["seek_target_ms"])
            or frame["seek_method"] != SEEK_METHOD):
        _fail("frame_binding_invalid")
    size = frame["image_size"]
    if (type(size) is not list or len(size) != 2
            or any(type(n) is not int or n <= 0 for n in size)
            or size[0] * size[1] > MAX_IMAGE_PIXELS):
        _fail("frame_geometry_invalid")
    if not _exact_keys(roi, ("field", "box")) or roi["field"] != "gold_numeric_candidate":
        _fail("roi_invalid")
    box = roi["box"]
    if (type(box) is not list or len(box) != 4 or any(type(n) is not int for n in box)
            or not 0 <= box[0] < box[2] <= size[0] or not 0 <= box[1] < box[3] <= size[1]):
        _fail("roi_invalid")
    if not _exact_keys(visual, ("observer", "status", "candidate")):
        _fail("agent_observation_invalid")
    if (visual["observer"] != "agent_visual_unreviewed" or visual["status"] != "candidate_clear"
            or not _integer(visual["candidate"])):
        _fail("agent_observation_unavailable")
    return index, index_hash


def _pixels(data: bytes, size: list[int]) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if (image.format != "PNG" or getattr(image, "n_frames", 1) != 1
                    or image.size != tuple(size) or image.getexif().get(274, 1) != 1
                    or image.width * image.height > MAX_IMAGE_PIXELS):
                _fail("frame_geometry_or_format_mismatch")
            image.load()
            with image.convert("RGB") as rgb:
                return rgb.tobytes()
    except AdapterError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        _fail("frame_image_unreadable")


def _decode_frame(video_fd: int, target_ms: int) -> bytes:
    executable = shutil.which("ffmpeg")
    if not executable:
        _fail("ffmpeg_unavailable")
    # The inherited, already-hashed regular-file descriptor prevents a path
    # substitution between hashing and decoding. This is a seek target, not a
    # claim that the physical capture timestamp or actual frame PTS is known.
    os.lseek(video_fd, 0, os.SEEK_SET)
    seconds = f"{target_ms // 1000}.{target_ms % 1000:03d}"
    command = [executable, "-nostdin", "-v", "error", "-ss", seconds, "-i", f"/dev/fd/{video_fd}",
               "-map", "0:v:0", "-frames:v", "1", "-f", "image2pipe",
               "-vcodec", "png", "pipe:1"]
    try:
        result = subprocess.run(command, pass_fds=(video_fd,), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired):
        _fail("video_decode_failed")
    if result.returncode != 0 or not 0 < len(result.stdout) <= MAX_IMAGE_BYTES:
        _fail("video_decode_failed")
    return result.stdout


class _BoundOCR:
    """Record crop digests and normalized recognize() return values.

    Worker wire bytes and any outer protocol envelope are not retained.
    """

    def __init__(self, backend):
        self.backend = backend
        self.scale = None
        self.records = []

    def recognize(self, data: bytes):
        record = {"scale": self.scale, "crop_sha256": _sha(data),
                  "response_sha256": None, "response": None, "status": "failed"}
        try:
            result = self.backend.recognize(data)
            encoded = json.dumps(result, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode("utf-8")
            snapshot = json.loads(encoded)
        except Exception:
            self.records.append(record)
            _fail("ocr_failed_or_response_invalid")
        # Preserve the normalized JSON value returned by recognize(), not the
        # worker's wire bytes. The detached copy avoids later backend mutation.
        record.update(response_sha256=_sha(encoded), response=snapshot, status="returned")
        self.records.append(record)
        return snapshot


def _probe_summary(probe, scale: int) -> dict:
    return {"scale": scale, "status": probe.status,
            "candidate": probe.candidate_value if probe.status == CANDIDATE else None,
            "reason": probe.reason, "raw_score": probe.raw_score,
            "ocr_items_count": probe.ocr_items_count}


def analyze_index(index_path, expected_index_sha256: str, *, hypothetical_spend: int, ocr) -> dict:
    """Use one pinned, private index with a caller-supplied local OCR backend.

    This function checks the actual video digest and re-decodes the declared
    seek target. It compares lossless RGB pixels before OCR, then verifies the
    same indexed frame bytes are used by two explicit variants and glyph veto.
    The caller controls the OCR backend and the agent observation; neither is
    independently authenticated or a human truth label.
    """
    if not _integer(hypothetical_spend):
        _fail("hypothetical_spend_invalid")
    if ocr is None or not callable(getattr(ocr, "recognize", None)):
        _fail("ocr_unavailable")
    from jcc_device.vision_ocr import VisionOCR, WORKER

    builtin_vision = type(ocr) is VisionOCR
    if builtin_vision and WORKER != PROJECT / APPLE_VISION_WORKER:
        _fail("apple_vision_worker_binding_invalid")
    dependencies = _dependency_hashes(apple_vision_worker=builtin_vision)
    index, index_hash = _load_index(index_path, expected_index_sha256)
    video, frame, roi, visual = (index[key] for key in ("video", "frame", "roi", "agent_visual"))
    frame_data = _read_private(frame["path"], MAX_IMAGE_BYTES)
    if _sha(frame_data) != frame["sha256"]:
        _fail("frame_sha256_mismatch")
    expected_pixels = _pixels(frame_data, frame["image_size"])
    video_file, video_stat = _private_file(video["path"])
    with video_file:
        if not 0 < video_stat.st_size <= MAX_VIDEO_BYTES:
            _fail("video_size_invalid")
        video_hash = hashlib.file_digest(video_file, "sha256").hexdigest()
        if video_hash != video["sha256"] or not _stable(video_file, video_stat):
            _fail("video_sha256_mismatch")
        decoded = _decode_frame(video_file.fileno(), frame["seek_target_ms"])
        if not _stable(video_file, video_stat):
            _fail("video_changed_during_decode")
    if _pixels(decoded, frame["image_size"]) != expected_pixels:
        _fail("video_seek_pixels_mismatch")
    geometry = ImageGeometry(tuple(frame["image_size"]), 0)
    frame_ref = "video-frame-" + frame["sha256"][:24]
    source = FrameRef(frame_ref, 0, time.monotonic_ns(), datetime.now(timezone.utc).isoformat(),
                      host_received_time_source="mac_offline_video_probe_not_capture")
    image = Frame(source, frame_data)
    bound_ocr = _BoundOCR(ocr)
    experiments = []
    for scale in (1, 2):
        bound_ocr.scale = scale
        region = PixelROI("gold-numeric", geometry.image_size, tuple(roi["box"]))
        layout = ROILayout("private-video-gold-roi-v1", geometry, "replay",
                           (FieldROI("gold", region, scale=scale),))
        experiments.append(probe_frame(image, geometry=geometry, sample_kind="replay",
                                       layout=layout, ocr=bound_ocr))
    combined = reconcile_variants(experiments)
    gold = next(probe for probe in combined.probes if probe.field_name == "gold")
    variants = [next(probe for probe in e.probes if probe.field_name == "gold") for e in experiments]
    value = gold.candidate_value if gold.status == CANDIDATE and _integer(gold.candidate_value) else None
    glyph = check_gold_glyph(frame_data, roi=roi["box"], frame_ref=frame_ref, proposed_value=value)
    reason = None
    if value is None or len(bound_ocr.records) != 2:
        reason = gold.reason or "ocr_candidate_unavailable"
    elif glyph["status"] not in ("not_required", "consistent"):
        reason = glyph["reason"] or "glyph_check_inconclusive"
    elif visual["candidate"] != value:
        reason = "agent_ocr_disagreement"
    # Check that indexed frame and index still name the same bytes after OCR.
    if _sha(_read_private(frame["path"], MAX_IMAGE_BYTES)) != frame["sha256"]:
        _fail("frame_changed_during_ocr")
    if _sha(_read_private(index_path, MAX_INDEX_BYTES)) != index_hash:
        _fail("index_changed_during_ocr")
    video_check, video_after = _private_file(video["path"])
    with video_check:
        if not _same_file_state(video_stat, video_after):
            _fail("video_changed_during_ocr")
    if _dependency_hashes(apple_vision_worker=builtin_vision) != dependencies:
        _fail("dependency_changed_during_ocr")
    candidate = GoldCandidate(value if reason is None else None, "replay_candidate",
                              frame_ref + "/gold", status="candidate" if reason is None else "unknown")
    comparison = analyze_purchases(candidate, [HypotheticalPrice(hypothetical_spend,
                                   label="显式单笔假设支出")], rule=None)
    row_status = comparison["comparisons"][0]["status"]
    output_status = row_status if reason is None and row_status in (
        "cash_only", "insufficient_candidate_gold") else "blocked"
    return {
        "schema_version": RESULT_SCHEMA, "status": output_status,
        "reason": reason, "source_kind": "replay", "capture_kind": "video_frame",
        "binding": {"index_sha256": index_hash, "video_sha256": video_hash,
                    "frame_sha256": frame["sha256"], "frame_ref": frame_ref,
                    "seek_target_ms": frame["seek_target_ms"], "seek_method": SEEK_METHOD,
                    "actual_frame_pts": None, "decoded_pixels_match": True,
                    "image_size": frame["image_size"], "roi": roi["box"]},
        "dependency_sha256": dependencies,
        "agent_visual": {"status": "unreviewed_candidate", "value": visual["candidate"]},
        "machine_ocr": {"backend": ("local_apple_vision_worker_unverified" if builtin_vision
                                    else "caller_supplied_local_backend_unverified"),
                        "worker_fingerprint_checked": builtin_vision,
                        "status": "candidate" if value is not None else "unknown", "value": value,
                        "variants": [_probe_summary(item, scale) for item, scale in zip(variants, (1, 2))],
                        "request_response_hashes": bound_ocr.records,
                        "agreement_is_accuracy": False, "glyph_status": glyph["status"]},
        "human_ground_truth": None, "independently_reviewed": False,
        "scene": "unknown", "self_subject": "unverified", "mode": "unknown", "game_version": "unknown",
        "hypothetical_spend": hypothetical_spend, "economy": comparison,
        "real_rules_verified": False, "interest_inferred": False,
        "production_eligible": False, "execution_eligible": False,
        "strategy_recommendation": None, "action": None,
        "label_zh": "离线录像帧：Agent目视与机器OCR均为未核验候选；仅比较显式假设支出的余额。",
    }
