"""Read pinned local images and expose candidate-only 10G results privately.

There is no device, network, annotation, strategy, or overlay entry point here.
Real layouts and sample identities belong in an external private profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from jcc_device.game_state_contract import validate_annotations
from jcc_device.latest import Frame, FrameRef
from jcc_device.recognition import PixelROI
from jcc_device.roi_recognition import (
    CANDIDATE, NUMERIC_FIELDS, FieldROI, ImageGeometry, ROILayout,
    reconcile_variants,
)
from jcc_device.scene_evidence import (
    BoundNumbers, ClockReading, CueSpec, LayoutKey, SceneLayout,
    assess_fields, probe_numbers, probe_scene,
)

from . import VERSION


PROJECT = Path(__file__).resolve().parents[2]
JSON_LIMIT = 4 * 1024 * 1024
IMAGE_LIMIT = 25 * 1024 * 1024
DEPENDENCIES = (
    "src/jcc_device/roi_recognition.py", "src/jcc_device/scene_evidence.py",
    "src/jcc_device/vision_ocr.py", "src/jcc_device/vision_ocr.swift",
    "tools/vision_ocr",
    "src/jcc_economy_lab/__init__.py", "src/jcc_economy_lab/core.py",
    "src/jcc_economy_lab/assumptions.py",
)


class InspectorError(ValueError):
    """A fixed error code, never a path, OCR string, or backend exception."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def default_profile() -> Path:
    return Path.home() / "Library/Application Support/jcc-replay-inspector/profile.json"


def _safe_path(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise InspectorError("unsafe_private_path")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise InspectorError("symlink_forbidden")
    path = path.resolve()
    if path == PROJECT or PROJECT in path.parents:
        raise InspectorError("private_data_in_project_forbidden")
    for parent in (path, *path.parents):
        if (parent / ".git").exists() or (parent / ".git").is_symlink():
            raise InspectorError("private_data_in_git_forbidden")
    for component in path.parts:
        name = component.casefold()
        if (name in {"cloudstorage", "mobile documents", "dropbox", "box",
                     "synologydrive", "synology drive"}
                or name.startswith(("onedrive", "googledrive", "google drive", "icloud", "dropbox "))):
            raise InspectorError("cloud_location_forbidden")
    return path


def _private_dir(path: Path, *, create: bool = False) -> Path:
    path = _safe_path(path)
    if create and not path.exists():
        missing = []
        cursor = path
        while not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for part in reversed(missing):
            part.mkdir(mode=0o700)
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise InspectorError("unsafe_directory_permissions")
    return path


def _relative(root: Path, name: str) -> Path:
    if (not isinstance(name, str) or not name or Path(name).is_absolute()
            or ".." in Path(name).parts):
        raise InspectorError("unsafe_relative_path")
    result = _safe_path(root / name)
    if not result.is_relative_to(root):
        raise InspectorError("unsafe_relative_path")
    for parent in result.parents:
        if parent == root:
            break
        _private_dir(parent)
    return result


def _read(path: Path, limit: int) -> bytes:
    path = _safe_path(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise InspectorError("unsafe_file_permissions")
        if not 0 <= info.st_size <= limit:
            raise InspectorError("input_size_limit")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise InspectorError("input_size_limit")
    return data


def _json(data: bytes):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise InspectorError("duplicate_json_key")
            result[name] = value
        return result

    def constant(_):
        raise InspectorError("nonfinite_json")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def _hash(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


@dataclass(frozen=True)
class Profile:
    source_root: Path = field(repr=False)
    output_root: Path = field(repr=False)
    manifest_path: str = field(repr=False)
    workspace_sha256: str = field(repr=False)
    manifest_sha256: str = field(repr=False)
    sample_ids: tuple[str, ...] = field(repr=False)
    key: LayoutKey
    scene_layout: SceneLayout
    number_layouts: tuple[ROILayout, ...]
    dependency_sha256: dict = field(repr=False)
    reference_key: bytes = field(repr=False)
    capacity_config: dict | None = field(default=None, repr=False)
    shop_config: dict | None = field(default=None, repr=False)
    gate_age_ns: int = 1_000_000_000


def load_profile(path: Path) -> Profile:
    path = _safe_path(path)
    _private_dir(path.parent)
    config = _json(_read(path, JSON_LIMIT))
    if (config.get("schema_version") != "replay-inspector-profile-v1"
            or config.get("purpose") != "private_development_history_only"
            or config.get("sample_kind") != "replay"
            or config.get("capture_kind") != "screenshot"
            or config.get("mode_key") is not None
            or config.get("applied_rotation_degrees") != 0):
        raise InspectorError("unsupported_profile_scope")
    source = _private_dir(Path(config["source_root"]))
    output = _safe_path(Path(config["output_root"]))
    if (output == source or output.is_relative_to(source) or source.is_relative_to(output)
            or path.is_relative_to(source) or path.is_relative_to(output)):
        raise InspectorError("source_output_overlap")
    if not all(_hash(config.get(k)) for k in ("workspace_sha256", "manifest_sha256")):
        raise InspectorError("pinned_metadata_required")
    manifest = config["manifest_path"]
    if (not isinstance(manifest, str) or Path(manifest).parts[:1] != ("manifests",)
            or Path(manifest).suffix != ".json"):
        raise InspectorError("invalid_manifest_path")
    _relative(source, manifest)
    ids = config["sample_ids"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 4
            or any(not isinstance(s, str) or not s for s in ids) or len(set(ids)) != len(ids)):
        raise InspectorError("explicit_sample_allowlist_required")
    geometry = ImageGeometry(tuple(config["image_size"]), 0)
    if geometry.image_size[0] * geometry.image_size[1] > 16_000_000:
        raise InspectorError("image_size_limit")
    key = LayoutKey(config["layout_id"], geometry, "replay", "screenshot", None)
    cues = tuple(CueSpec(c["cue_id"], PixelROI(c["cue_id"], geometry.image_size, tuple(c["box"])),
                         tuple(c.get("exact_texts", ())), c.get("scale", 1))
                 for c in config["cues"])
    scene = SceneLayout(key, cues)
    if (config["numeric_scales"] != [1, 2] or set(config["fields"]) != set(NUMERIC_FIELDS)):
        raise InspectorError("explicit_two_scale_fields_required")
    numbers = tuple(ROILayout(key.layout_id, geometry, "replay", tuple(
        FieldROI(name, PixelROI(name, geometry.image_size, tuple(config["fields"][name]["box"])),
                 scale=scale, syntax=config["fields"][name].get("syntax", "bare"))
        for name in NUMERIC_FIELDS)) for scale in config["numeric_scales"])
    hashes = config["dependency_sha256"]
    if set(hashes) != set(DEPENDENCIES) or not all(_hash(h) for h in hashes.values()):
        raise InspectorError("pinned_dependencies_required")
    if not _hash(config.get("private_reference_key")):
        raise InspectorError("private_reference_key_required")
    profile = Profile(source, output, manifest, config["workspace_sha256"], config["manifest_sha256"],
                      tuple(ids), key, scene, numbers, dict(hashes), bytes.fromhex(config["private_reference_key"]),
                      capacity_config=config.get("capacity_results"), shop_config=config.get("shop_results"))
    check_dependencies(profile)
    return profile


def check_dependencies(profile: Profile) -> None:
    if any(digest((PROJECT / name).read_bytes()) != expected
           for name, expected in profile.dependency_sha256.items()):
        raise InspectorError("dependency_changed")


@dataclass(frozen=True)
class SourceImage:
    order: int
    sample_id: str = field(repr=False)
    data: bytes = field(repr=False)
    image_format: str
    asset_read_ms: float
    image_validation_ms: float


@dataclass(frozen=True)
class SourceBatch:
    images: tuple[SourceImage, ...] = field(repr=False)
    snapshot: dict[Path, str] = field(repr=False)
    label_count: int
    metadata_validation_ms: float


def load_source(profile: Profile) -> SourceBatch:
    """Read exact pinned metadata and allowlisted assets; never opens a lock."""
    from PIL import Image

    started = time.perf_counter()
    workspace_path = _relative(profile.source_root, "workspace.json")
    manifest_path = _relative(profile.source_root, profile.manifest_path)
    before = {workspace_path: _read(workspace_path, JSON_LIMIT),
              manifest_path: _read(manifest_path, JSON_LIMIT)}
    if (digest(before[workspace_path]) != profile.workspace_sha256
            or digest(before[manifest_path]) != profile.manifest_sha256):
        raise InspectorError("pinned_metadata_changed")
    workspace, records = _json(before[workspace_path]), _json(before[manifest_path])
    if (workspace.get("schema_version") != "sample-workspace-v1"
            or not isinstance(records, list) or workspace.get("annotations") != records
            or [r.get("sample_id") for r in records] != list(profile.sample_ids)
            or set(workspace.get("assets", {})) != set(profile.sample_ids)):
        raise InspectorError("sample_allowlist_mismatch")
    try:
        validate_annotations(records)
    except Exception:
        raise InspectorError("annotation_contract_invalid") from None
    label_count = 0
    for record in records:
        if (record["sample_kind"] != "real" or record["split"] != "quarantine"
                or record["review_status"] != "draft" or record["reviewer_id"] is not None):
            raise InspectorError("development_quarantine_only")
        labels = record["labels"]
        values = [labels[k] for k in labels if k != "shop"] + [v["content"] for v in labels["shop"]]
        if any(v["truth_status"] != "unknown" or v["value"] is not None for v in values):
            raise InspectorError("unannotated_inputs_only")
        label_count += len(values)
    metadata_ms = (time.perf_counter() - started) * 1000
    snapshot = {p: digest(b) for p, b in before.items()}
    images, pixel_hashes = [], set()
    for order, record in enumerate(records, 1):
        asset = workspace["assets"][record["sample_id"]]
        if (not isinstance(asset.get("asset_ref"), str)
                or asset.get("format") not in ("PNG", "JPEG")):
            raise InspectorError("invalid_asset")
        suffix = ".png" if asset["format"] == "PNG" else ".jpg"
        if (asset.get("path") != "assets/" + asset["asset_ref"] + suffix
                or asset.get("sample_kind") != "real"
                or record["asset"]["asset_ref"] != asset["asset_ref"]
                or record["asset"]["sha256"] != asset.get("sha256")):
            raise InspectorError("asset_binding_mismatch")
        started = time.perf_counter()
        image_path = _relative(profile.source_root, asset["path"])
        data = _read(image_path, IMAGE_LIMIT)
        read_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        if digest(data) != asset["sha256"]:
            raise InspectorError("asset_bytes_changed")
        with Image.open(io.BytesIO(data)) as original:
            if (original.format != asset["format"] or getattr(original, "n_frames", 1) != 1
                    or original.getexif().get(274, 1) != 1
                    or original.size != profile.key.geometry.image_size
                    or original.size != (asset["width_px"], asset["height_px"])):
                raise InspectorError("image_geometry_or_format_mismatch")
            frame_image = record["frame"]["image"]
            if (original.size != (frame_image["width_px"], frame_image["height_px"])
                    or frame_image["rotation_applied_degrees"] != 0):
                raise InspectorError("image_geometry_or_format_mismatch")
            with original.convert("RGBA") as rgba:
                # Match the existing sample workflow's geometry + RGBA digest.
                pixel_digest = hashlib.sha256(f"{original.width}x{original.height}:RGBA:".encode("ascii"))
                pixel_digest.update(rgba.tobytes())
                pixels = pixel_digest.hexdigest()
        if pixels != asset["pixel_sha256"] or pixels in pixel_hashes:
            raise InspectorError("asset_pixels_changed_or_duplicate")
        pixel_hashes.add(pixels)
        snapshot[image_path] = digest(data)
        images.append(SourceImage(order, record["sample_id"], data, asset["format"], read_ms,
                                  (time.perf_counter() - started) * 1000))
    return SourceBatch(tuple(images), snapshot, label_count, metadata_ms)


def verify_unchanged(batch: SourceBatch, profile: Profile) -> None:
    for path, expected in batch.snapshot.items():
        if digest(_read(path, IMAGE_LIMIT)) != expected:
            raise InspectorError("source_changed_during_run")
    check_dependencies(profile)


class RecordingOCR:
    """Raw results stay in a private return object. Never logs backend errors."""

    def __init__(self, backend):
        self.backend = backend
        self.group = "scene"
        self.records = []

    def recognize(self, image_bytes: bytes):
        started = time.perf_counter()
        record = {"group": self.group, "request": len(self.records) + 1}
        try:
            result = self.backend.recognize(image_bytes)
            try:
                record["items"] = json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
            except (TypeError, ValueError):
                record["items"] = None
                record["error"] = "invalid_ocr_response"
            return result
        except Exception:
            record["items"] = None
            record["error"] = "ocr_failed"
            raise RuntimeError("ocr_failed") from None
        finally:
            record["processing_ms"] = (time.perf_counter() - started) * 1000
            self.records.append(record)


def _probe_dict(probe) -> dict:
    return {"field": probe.field_name, "status": probe.status,
            "candidate": probe.candidate_value if probe.status == CANDIDATE else None,
            "reason": probe.reason, "input_region": probe.input_region,
            "raw_score": probe.raw_score, "ocr_items_count": probe.ocr_items_count}


def inspect_image(image: SourceImage, profile: Profile, backend) -> dict:
    from .integrations import economy_comparison, unknown_capacity
    from .shop import unknown_shop

    started = time.perf_counter()
    clock = "offline-replay-inspection-process"
    frame = Frame(FrameRef("offline-development-replay", image.order, time.monotonic_ns(),
                          datetime.now(timezone.utc).isoformat(),
                          host_received_time_source="mac_offline_probe_start_not_capture"), image.data)
    ocr = RecordingOCR(backend)
    scene = probe_scene(frame, key=profile.key, host_clock_id=clock, context_epoch=0,
                        layout=profile.scene_layout, ocr=ocr)
    variants = []
    for layout in profile.number_layouts:
        ocr.group = f"numeric_{layout.fields[0].scale}x"
        variants.append(probe_numbers(frame, key=profile.key, host_clock_id=clock, context_epoch=0,
                                      layouts=(layout,), ocr=ocr))
    if any(v.binding != scene.binding for v in variants):
        raise InspectorError("internal_binding_mismatch")
    reconcile_start = time.perf_counter()
    combined = reconcile_variants(tuple(v.experiment for v in variants))
    numbers = BoundNumbers(variants[0].binding, combined, variants[0].started_monotonic_ns,
                           variants[-1].completed_monotonic_ns)
    reconcile_ms = (time.perf_counter() - reconcile_start) * 1000
    gate_start = time.perf_counter()
    decisions = assess_fields(scene, numbers, current=scene.binding,
                             now=ClockReading(clock, time.monotonic_ns()),
                             max_frame_age_ns=profile.gate_age_ns, witness=None)
    gate_ms = (time.perf_counter() - gate_start) * 1000
    result = {
        "order": image.order,
        "frame_ref": "replay-" + hmac.new(profile.reference_key, image.data, hashlib.sha256).hexdigest()[:24],
        "source_sha256": digest(image.data),
        "sample_kind": "replay", "capture_kind": "screenshot",
        "evidence": "development_candidate_only_not_human_reviewed",
        "source_time": "historical_capture_time_unknown",
        "subject": "unknown", "visibility": "unknown", "mode": "unknown", "game_version": "unknown",
        "scene": scene.summary(), "fields": [_probe_dict(p) for p in combined.probes],
        "variants": [{"scale": v.experiment.layout.fields[0].scale,
                      "fields": [_probe_dict(p) for p in v.experiment.probes],
                      "processing_ms": (v.completed_monotonic_ns - v.started_monotonic_ns) / 1e6}
                     for v in variants],
        "semantic_gate": [{"field": d.field_name, "status": d.status,
                           "candidate": None, "reason": d.reason} for d in decisions],
        "raw_ocr": ocr.records,
        "timing_ms": {"asset_read": image.asset_read_ms, "image_validation": image.image_validation_ms,
                      "scene": (scene.completed_monotonic_ns - scene.started_monotonic_ns) / 1e6,
                      "numeric_variants": [(v.completed_monotonic_ns - v.started_monotonic_ns) / 1e6
                                           for v in variants],
                      "reconcile": reconcile_ms, "semantic_gate": gate_ms,
                      "offline_recognition_wall": (time.perf_counter() - started) * 1000},
    }
    comparison_start = time.perf_counter()
    result["economy"] = economy_comparison(result)
    result["capacity"] = unknown_capacity(result, "capacity_results_not_loaded")
    result["shop"] = unknown_shop(result, "shop_results_not_loaded")
    result["timing_ms"]["economy_adapter"] = (time.perf_counter() - comparison_start) * 1000
    result["timing_ms"]["offline_frame_wall"] = (time.perf_counter() - started) * 1000
    return result


def structured_frame(frame: dict) -> dict:
    """Candidate-only integration shape; never carries pixels, raw OCR or paths.

    The reference is stable only within the private profile, and is neither a
    source-session identity nor a proof of freshness, subject, or independence.
    """
    fields = {}
    for probe in frame["fields"]:
        name = probe["field"]
        fields[name] = {k: probe[k] for k in ("status", "candidate", "reason")}
        fields[name]["conflict"] = probe["reason"] == "variant_disagreement"
        fields[name]["variants"] = [
            {"scale": v["scale"], **{k: next(p for p in v["fields"] if p["field"] == name)[k]
                                      for k in ("status", "candidate", "reason")}}
            for v in frame["variants"]]
    return {"schema_version": "replay-inspector-frame-v1", "frame_ref": frame["frame_ref"],
            "source_sha256": frame["source_sha256"],
            "order": frame["order"], "reference_scope": "private_profile_only",
            "source": {"kind": "historical_replay", "capture_kind": "screenshot",
                       "capture_time": None, "freshness": "not_live", "usage": "development_only"},
            "scene": dict(frame["scene"]["scene"]), "phase": dict(frame["scene"]["phase"]),
            "unverified": {k: frame[k] for k in ("subject", "visibility", "mode", "game_version")},
            "fields": fields,
            "semantic_gate": {p["field"]: {k: p[k] for k in ("status", "candidate", "reason")}
                              for p in frame["semantic_gate"]},
            "timing_ms": dict(frame["timing_ms"]),
            "economy": frame["economy"], "capacity": frame["capacity"], "shop": frame["shop"],
            "independently_reviewed": False, "recognized_count": 0,
            "advice_eligible": False, "accuracy": None}


def safe_summary(frames: list[dict]) -> dict:
    """An explicit allowlist for console output. No raw text, values or paths."""
    fields = [p for f in frames for p in f["fields"]]
    return {"method": VERSION, "source": "historical_replay_development_only",
            "frames": len(frames), "ocr_requests": sum(len(f["raw_ocr"]) for f in frames),
            "scene_candidates": sum(f["scene"]["scene"]["status"] == CANDIDATE for f in frames),
            "phase_unknown": sum(f["scene"]["phase"]["status"] == "unknown" for f in frames),
            "numeric_candidates": sum(p["status"] == CANDIDATE for p in fields),
            "numeric_unknown": sum(p["status"] == "unknown" for p in fields),
            "variant_conflicts": sum(p["reason"] == "variant_disagreement" for p in fields),
            "semantic_unknown": sum(p["status"] == "unknown" for f in frames for p in f["semantic_gate"]),
            "recognized_count": 0, "real_acceptance_samples": 0, "accuracy": None,
            "device_actions": 0, "advice_count": 0,
            "economy_conditional_frames": sum(f["economy"]["result"]["status"] == "conditional" for f in frames),
            "economy_blocked_frames": sum(f["economy"]["result"]["status"] == "blocked" for f in frames),
            "economy_partial_frames": sum(f["economy"]["result"]["status"] == "partial" for f in frames),
            "economy_frames_with_comparison": sum(any(r["status"] == "conditional" for r in f["economy"]["result"]["comparisons"])
                                                   for f in frames),
            "hypothetical_comparisons_calculated": sum(r["status"] == "conditional" for f in frames
                                                       for r in f["economy"]["result"]["comparisons"]),
            "capacity_unknown_frames": sum(f["capacity"]["status"] == "unknown" for f in frames),
            "capacity_candidate_frames": sum(f["capacity"]["status"] == CANDIDATE for f in frames),
            "capacity_exact_source_matches": sum(f["capacity"].get("matched_source_sha256") == f["source_sha256"] for f in frames),
            "shop_exact_source_matches": sum(f["shop"].get("matched_source_sha256") == f["source_sha256"] for f in frames),
            "shop_name_candidate_slots": sum(s["name_status"] == "candidate" for f in frames for s in f["shop"]["slots"]),
            "shop_unknown_price_slots": sum(s["price_status"] == "unknown" for f in frames for s in f["shop"]["slots"]),
            "shop_empty_candidate_slots": sum(s["status"] == "empty_candidate" for f in frames for s in f["shop"]["slots"]),
            "shop_occluded_candidate_slots": sum(s["status"] == "occluded_candidate" for f in frames for s in f["shop"]["slots"])}


def attach_capacity(frames: list[dict], profile: Profile) -> float:
    """Read one explicit pinned private report; any failure keeps capacity unknown."""
    from .integrations import match_capacity, unknown_capacity

    started = time.perf_counter()
    config = profile.capacity_config
    document, failure = None, None
    if config is not None:
        try:
            if not isinstance(config, dict) or not _hash(config.get("report_sha256")) or not _hash(config.get("config_sha256")):
                raise InspectorError("capacity_private_config_invalid")
            path = _safe_path(Path(config["path"]))
            _private_dir(path.parent)
            payload = _read(path, JSON_LIMIT)
            if digest(payload) != config["report_sha256"]:
                raise InspectorError("capacity_report_bytes_changed")
            document = _json(payload)
        except InspectorError as exc:
            failure = str(exc) if str(exc).startswith("capacity_") else "capacity_private_report_unavailable"
        except Exception:
            failure = "capacity_private_report_unavailable"
    for frame in frames:
        frame["capacity"] = (unknown_capacity(frame, failure) if failure else
                             match_capacity(frame, document, config, profile.key.geometry.image_size))
    return (time.perf_counter() - started) * 1000


def attach_shop(frames: list[dict], profile: Profile) -> float:
    """Verify all completed run files before consuming a narrow private subset."""
    from .shop import match_shop, unknown_shop

    started = time.perf_counter()
    config, document, failure = profile.shop_config, None, "shop_results_not_configured"
    if config is not None:
        failure = "shop_private_run_unavailable"
        try:
            root = _private_dir(Path(config["directory"]))
            completed_bytes = _read(_relative(root, "COMPLETED.json"), JSON_LIMIT)
            if not _hash(config.get("completed_sha256")) or digest(completed_bytes) != config["completed_sha256"]:
                raise InspectorError("shop_completed_marker_changed")
            completed = _json(completed_bytes)
            if (type(completed.get("revision")) is not int or completed["revision"] != 1
                    or not isinstance(config.get("run_id"), str) or not config["run_id"].strip()
                    or completed.get("run_id") != config["run_id"]):
                raise InspectorError("shop_completed_revision_or_run_mismatch")
            checksums = completed.get("files_sha256")
            if (not isinstance(checksums, dict) or len(checksums) != 3
                    or not {"results.private.json", "summary.json"} <= set(checksums)
                    or len([name for name in checksums if name.endswith(".html")]) != 1
                    or any(Path(name).name != name or not _hash(value) for name, value in checksums.items())):
                raise InspectorError("shop_completed_files_invalid")
            payloads = {}
            for name, expected in checksums.items():
                data = _read(_relative(root, name), 64 * 1024 * 1024 if name.endswith(".html") else JSON_LIMIT)
                if digest(data) != expected:
                    raise InspectorError("shop_completed_file_hash_mismatch")
                if not name.endswith(".html"):
                    payloads[name] = data
            document = _json(payloads["results.private.json"])
            if document.get("summary") != _json(payloads["summary.json"]):
                raise InspectorError("shop_completed_summary_mismatch")
            failure = None
        except InspectorError as exc:
            failure = str(exc) if str(exc).startswith("shop_") else "shop_private_run_unavailable"
        except Exception:
            failure = "shop_private_run_unavailable"
    current_hashes = [frame["source_sha256"] for frame in frames]
    for frame in frames:
        frame["shop"] = (unknown_shop(frame, failure) if failure else
                         match_shop(frame, document, config, profile.key.geometry.image_size,
                                    current_hashes, completion_verified=True))
    return (time.perf_counter() - started) * 1000


def _write_new(path: Path, data: bytes) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(data)


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def run_inspection(profile_path: Path, backend_factory=None) -> tuple[Path, dict]:
    """Create only new private run artifacts, and close the OCR worker on exit."""
    from .render import render_page
    from jcc_device.vision_ocr import VisionOCR

    started = time.perf_counter()
    profile = load_profile(profile_path)
    profile_ms = (time.perf_counter() - started) * 1000
    source_start = time.perf_counter()
    batch = load_source(profile)
    source_ms = (time.perf_counter() - source_start) * 1000
    worker_start = time.perf_counter()
    with (backend_factory or VisionOCR)() as backend:
        worker_ms = (time.perf_counter() - worker_start) * 1000
        frames = [inspect_image(image, profile, backend) for image in batch.images]
    capacity_attach_ms = attach_capacity(frames, profile)
    shop_attach_ms = attach_shop(frames, profile)
    check_start = time.perf_counter()
    verify_unchanged(batch, profile)
    check_ms = (time.perf_counter() - check_start) * 1000
    summary = safe_summary(frames)
    summary.update({"source_bytes_unchanged": True, "unknown_source_labels": batch.label_count})
    report = {"method": VERSION, "generated_utc": datetime.now(timezone.utc).isoformat(),
              "summary": dict(summary), "frames": frames,
              "timing_ms": {"profile_validation": profile_ms, "source_validation": source_ms,
                            "metadata_validation": batch.metadata_validation_ms,
                            "worker_process_start": worker_ms, "source_recheck": check_ms,
                            "capacity_report_read_and_join": capacity_attach_ms,
                            "shop_completed_files_read_and_join": shop_attach_ms},
              "timing_scope": "offline_image_processing_only_not_capture_to_advice",
              "notes": ["Previously used for development; no independent acceptance truth.",
                        "Read time is not capture time; no live freshness or real-play latency.",
                        "Correlated scales may agree and still both be wrong."]}
    render_start = time.perf_counter()
    page = render_page(report, batch.images, profile).encode("utf-8")
    render_ms = (time.perf_counter() - render_start) * 1000
    output_root = _private_dir(profile.output_root, create=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=output_root))
    write_start = time.perf_counter()
    try:
        _write_new(run_dir / "report.html", page)
        _write_new(run_dir / "private-results.json", _json_bytes(report))
        _write_new(run_dir / "candidates.json", _json_bytes({"schema_version": VERSION,
                   "frames": [structured_frame(f) for f in frames]}))
        write_ms = (time.perf_counter() - write_start) * 1000
        summary["timing_ms"] = {**report["timing_ms"], "page_render": render_ms,
                                "artifacts_write": write_ms,
                                "offline_run_wall": (time.perf_counter() - started) * 1000,
                                "frame_recognition_wall": [f["timing_ms"]["offline_recognition_wall"] for f in frames]}
        _write_new(run_dir / "summary.json", _json_bytes(summary))
    except BaseException:
        for name in ("report.html", "private-results.json", "candidates.json", "summary.json"):
            (run_dir / name).unlink(missing_ok=True)
        run_dir.rmdir()
        raise
    return run_dir / "report.html", summary
