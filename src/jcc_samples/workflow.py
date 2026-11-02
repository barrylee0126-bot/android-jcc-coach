"""Private image imports and explicit annotation/review/split transitions.

Only workspace.json is mutable authoritative state. Images are installed before
its atomic replacement, and removed if that commit fails. Manifest exports are
immutable JSON arrays understood by game_state_contract.validate_annotations.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
from typing import Any, Iterator
import uuid
import warnings

from PIL import Image, UnidentifiedImageError

from src.jcc_device.game_state_contract import (
    CORE_FIELDS,
    ContractViolation,
    validate_annotations,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_JSON_BYTES = 32 * 1024 * 1024
TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
SAMPLE_TOKEN = re.compile(r"sample-[0-9a-f]{32}\Z")
ASSET_TOKEN = re.compile(r"asset-[0-9a-f]{32}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SPLITS = ("quarantine", "development", "acceptance")


class WorkflowError(ValueError):
    """A stable error code and a message that never includes source content."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


def _token(value: Any, field: str) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        _fail("invalid_identifier", field + " must be a non-personal lowercase ASCII code (1..64 characters).")
    return value


def _json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        _fail("invalid_json", "Input must contain finite JSON values and valid text.")


def _read_file(path: Path, limit: int, *, private: bool = False) -> bytes:
    """Read one regular file, without following its final symlink or unbounded input."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                _fail("invalid_file", "Input must be a regular file, not a link or special file.")
            if private and (info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1):
                _fail("unsafe_permissions", "Private files must be owned by this user, have no group/other access, and no hard links.")
            if info.st_size > limit:
                _fail("input_too_large", "Input exceeds the documented local size limit.")
            data = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
            if len(data) > limit:
                _fail("input_too_large", "Input exceeds the documented local size limit.")
            if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                _fail("input_changed", "Input changed while it was being read; retry with a stable local file.")
            return data
    except OSError:
        _fail("file_unreadable", "A required local file is missing, unreadable, or a symbolic link.")


def _parse_json(data: bytes) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("invalid_json", "JSON contains duplicate object keys.")
            result[key] = value
        return result

    def nonfinite(_):
        _fail("invalid_json", "JSON must not contain NaN or Infinity.")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            nonfinite(value)
        return number

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=nonfinite, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, WorkflowError):
            raise
        _fail("invalid_json", "Local input is not valid UTF-8 JSON.")


def _check_annotations(records: Any) -> None:
    try:
        validate_annotations(records)
    except ContractViolation as error:
        # Contract messages contain schema constraints, indices and fixed field
        # names, not values. Classify them without echoing any supplied content.
        text = str(error)
        if "cannot span dataset splits" in text:
            _fail("split_isolation", "One recording session or dedupe group spans dataset splits.")
        if "one representative per dedupe group" in text:
            _fail("acceptance_duplicate", "Acceptance requires one representative per dedupe group.")
        if "independent review" in text:
            _fail("independent_review_required", "Review must use a different non-personal reviewer code.")
        _fail("invalid_annotations", "Sample annotations violate sample-annotation-v1. " + text.splitlines()[0])
    except RuntimeError:
        _fail("missing_dependency", "The project's existing jsonschema dependency is required; no dependency was installed.")


def read_annotation_manifest(path: Path | str) -> list[dict]:
    """Read and contract-check an explicit local JSON array without printing it.

    This standalone function does not open images. SampleStore.validate also
    verifies asset bindings, bytes, dimensions, and decoded-pixel duplicates.
    """
    records = _parse_json(_read_file(Path(path), MAX_JSON_BYTES))
    _check_annotations(records)
    return records


def _private_dir(path: Path | str, *, must_exist: bool) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute() or ".." in raw.parts:
        _fail("unsafe_private_dir", "Specify an absolute private directory without parent traversal.")
    # Resolve for containment checks as well as rejecting symlink spellings.
    try:
        resolved = raw.resolve()
        for part in (raw, *raw.parents):
            if part.is_symlink():
                _fail("unsafe_private_dir", "Private directory paths must not contain symbolic links; use a canonical local path.")
        if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
            _fail("source_output_forbidden", "Sample output must be outside the project and public source directories.")
        for part in (resolved, *resolved.parents):
            if (part / ".git").exists() or (part / ".git").is_symlink():
                _fail("source_output_forbidden", "Sample output must be outside all Git checkouts.")
        for part in resolved.parts:
            name = part.casefold()
            if (name in {"cloudstorage", "mobile documents", "dropbox", "box", "synologydrive", "synology drive"}
                    or name.startswith(("onedrive", "googledrive", "google drive", "icloud", "dropbox "))):
                _fail("cloud_output_forbidden", "Choose a local private directory outside cloud-sync locations.")
        if must_exist:
            info = resolved.stat()
            if not stat.S_ISDIR(info.st_mode):
                _fail("unsafe_private_dir", "Private output must be a directory.")
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                _fail("unsafe_permissions", "Private output must be owned by this user with mode 0700.")
        return resolved
    except OSError:
        _fail("unsafe_private_dir", "The private directory or its parent is unavailable.")


def _image_info(data: bytes) -> dict:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ("PNG", "JPEG"):
                    _fail("unsupported_image", "Only single-frame PNG and JPEG are supported.")
                if getattr(image, "n_frames", 1) != 1:
                    _fail("unsupported_image", "Animated or multi-frame images are not supported.")
                width, height = image.size
                if width * height > MAX_PIXELS:
                    _fail("image_too_large", "Image exceeds the 16-million-pixel limit.")
                image_format = image.format
                image.verify()
            # verify() alone does not decode the image payload.
            with Image.open(io.BytesIO(data)) as image:
                if image.getexif().get(274, 1) != 1:
                    _fail("image_orientation", "Apply EXIF orientation to a local copy before import; the importer never rotates or rewrites image bytes.")
                image.load()
                pixels = hashlib.sha256(f"{width}x{height}:RGBA:".encode("ascii"))
                pixels.update(image.convert("RGBA").tobytes())
        return {"sha256": hashlib.sha256(data).hexdigest(), "pixel_sha256": pixels.hexdigest(),
                "width_px": width, "height_px": height, "format": image_format}
    except WorkflowError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        _fail("image_too_large", "Image exceeds the safe decoding limit.")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError, OverflowError, RuntimeError):
        _fail("image_unreadable", "Image bytes could not be fully decoded and verified.")


def _write_new(path: Path, data: bytes) -> None:
    """Exclusive creation: never overwrite or follow a destination link."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        _fail("output_exists", "The destination already exists; use a new output name.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _atomic_save(root: Path, workspace: dict) -> None:
    data = _json_bytes(workspace)
    if len(data) > MAX_JSON_BYTES:
        _fail("workspace_too_large", "Workspace exceeds 32 MiB; start a separate dataset revision.")
    temporary = root / (".workspace-" + uuid.uuid4().hex + ".tmp")
    created = False
    try:
        _write_new(temporary, data)
        created = True
        os.replace(temporary, root / "workspace.json")
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def _unknown() -> dict:
    return {"applicability": "undetermined", "truth_status": "unknown", "value": None,
            "reason": "not_annotated", "region": None}


def _new_annotation(workspace: dict, info: dict, *, sample_kind: str, sample_id: str,
                    asset_ref: str, session: str, dedupe_group: str, state_group: str,
                    annotator: str, redaction: str) -> dict:
    width, height = info["width_px"], info["height_px"]
    return {
        "schema_version": "sample-annotation-v1", "sample_kind": sample_kind,
        "sample_id": sample_id, "dataset_id": workspace["dataset_id"],
        "recording_session_id": session, "stream_id": "import-stream-" + uuid.uuid4().hex,
        "host_clock_id": "import-clock-" + uuid.uuid4().hex,
        "split": "quarantine", "dedupe_group_id": dedupe_group, "state_group_id": state_group,
        "asset": None if sample_kind == "synthetic" else {
            "asset_ref": asset_ref, "sha256": info["sha256"], "access": "local_only", "redaction": redaction},
        "frame": {"frame_id": "import-frame-" + uuid.uuid4().hex, "sequence": 0, "context_epoch": 0,
                  "host_received_monotonic_ms": time.monotonic_ns() / 1_000_000,
                  "device_capture_time": None,
                  "image": {"width_px": width, "height_px": height,
                            "orientation": "landscape" if width > height else "portrait" if width < height else "square",
                            "rotation_applied_degrees": 0, "coordinate_space": "decoded_frame_normalized_top_left",
                            "game_viewport": None, "calibration_id": None}},
        "annotation_method": "synthetic" if sample_kind == "synthetic" else "human",
        "annotator_id": annotator, "reviewer_id": None, "review_status": "draft",
        "conditions": ["local_import_unannotated"],
        "labels": {**{field: _unknown() for field in CORE_FIELDS},
                   "shop": [{"slot": slot, "content": _unknown()} for slot in range(1, 6)]},
    }


class SampleStore:
    """Explicit local workspace with no network, device, or public output API."""

    def __init__(self, private_dir: Path | str):
        self.root = _private_dir(private_dir, must_exist=True)

    @classmethod
    def create(cls, private_dir: Path | str, *, dataset_id: str) -> "SampleStore":
        _token(dataset_id, "dataset_id")
        root = _private_dir(private_dir, must_exist=False)
        try:
            root.mkdir(mode=0o700)  # Parent must already exist; never adopt an existing directory.
        except FileExistsError:
            _fail("output_exists", "Private output already exists; open it or choose a new directory.")
        except OSError:
            _fail("private_dir_unavailable", "Create the local parent directory before initializing the workspace.")
        created = []
        try:
            for name in ("assets", "edits", "manifests"):
                child = root / name
                child.mkdir(mode=0o700)
                created.append(child)
            _write_new(root / ".lock", b"")
            _atomic_save(root, {"schema_version": "sample-workspace-v1", "dataset_id": dataset_id,
                                "revision": 0, "annotations": [], "assets": {}, "events": []})
            return cls(root)
        except BaseException:
            for name in ("workspace.json", ".lock"):
                (root / name).unlink(missing_ok=True)
            for child in reversed(created):
                child.rmdir()
            root.rmdir()
            raise

    def _path(self, relative: str, *, directory: bool = False) -> Path:
        if (not isinstance(relative, str) or not relative or "\\" in relative
                or relative.startswith("/") or any(part in ("", ".", "..") for part in relative.split("/"))):
            _fail("path_escape", "Use a relative private path without traversal or empty components.")
        parts = PurePosixPath(relative).parts
        current = self.root
        for index, part in enumerate(parts):
            current /= part
            if current.is_symlink():
                _fail("path_escape", "Private paths must not contain symbolic links.")
            if current.exists() and (directory or index < len(parts) - 1):
                info = current.stat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    _fail("unsafe_permissions", "Private path components must be user-owned directories with mode 0700.")
        return current

    @contextmanager
    def _locked(self) -> Iterator[None]:
        _private_dir(self.root, must_exist=True)
        try:
            fd = os.open(self._path(".lock"), os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            _fail("workspace_unreadable", "Workspace lock is missing or unsafe; initialization is required.")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                _fail("unsafe_permissions", "Workspace lock must be a private regular file.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _fail("workspace_busy", "Another process is using this workspace; retry after it finishes.")
            yield
        finally:
            os.close(fd)

    def _load(self) -> dict:
        data = _parse_json(_read_file(self._path("workspace.json"), MAX_JSON_BYTES, private=True))
        if (not isinstance(data, dict) or set(data) != {"schema_version", "dataset_id", "revision", "annotations", "assets", "events"}
                or data["schema_version"] != "sample-workspace-v1" or type(data["revision"]) is not int
                or data["revision"] < 0 or not isinstance(data["assets"], dict) or not isinstance(data["events"], list)):
            _fail("invalid_workspace", "Workspace envelope is invalid; restore a known local copy.")
        _token(data["dataset_id"], "dataset_id")
        self._verify(data, data["annotations"])
        return data

    def _verify(self, workspace: dict, records: list[dict]) -> None:
        _check_annotations(records)
        ids = {record["sample_id"] for record in workspace["annotations"]}
        if set(workspace["assets"]) != ids:
            _fail("asset_mapping", "Each stored sample needs exactly one private asset-index entry.")
        paths, digests, pixels = set(), set(), set()
        for record in records:
            sample_id = record["sample_id"]
            if SAMPLE_TOKEN.fullmatch(sample_id) is None or sample_id not in workspace["assets"]:
                _fail("asset_mapping", "Manifest sample is not present in this private workspace.")
            for key in ("recording_session_id", "dedupe_group_id", "state_group_id", "annotator_id"):
                _token(record[key], key)
            if record["reviewer_id"] is not None:
                _token(record["reviewer_id"], "reviewer_id")
            if record["dataset_id"] != workspace["dataset_id"]:
                _fail("dataset_mismatch", "Manifest and workspace dataset revisions disagree.")
            asset = workspace["assets"][sample_id]
            expected_keys = {"asset_ref", "path", "sample_kind", "sha256", "pixel_sha256", "width_px", "height_px", "format"}
            if not isinstance(asset, dict) or set(asset) != expected_keys:
                _fail("asset_mapping", "Private asset metadata is invalid.")
            ref = asset["asset_ref"]
            if not isinstance(ref, str) or ASSET_TOKEN.fullmatch(ref) is None or asset["format"] not in ("PNG", "JPEG"):
                _fail("asset_mapping", "Private asset reference or format is invalid.")
            suffix = ".png" if asset["format"] == "PNG" else ".jpg"
            if asset["path"] != "assets/" + ref + suffix or asset["sample_kind"] != record["sample_kind"]:
                _fail("asset_mapping", "Asset path or sample-kind binding disagrees.")
            if any(not isinstance(asset[key], str) or SHA256.fullmatch(asset[key]) is None for key in ("sha256", "pixel_sha256")):
                _fail("asset_mapping", "Private asset hashes are invalid.")
            info = _image_info(_read_file(self._path(asset["path"]), MAX_IMAGE_BYTES, private=True))
            if any(info[key] != asset[key] for key in info):
                _fail("asset_integrity", "Stored image bytes, decoded pixels, dimensions, or format changed.")
            image = record["frame"]["image"]
            if any(image[key] != info[key] for key in ("width_px", "height_px")) or image["rotation_applied_degrees"] != 0:
                _fail("asset_geometry", "Annotation dimensions or rotation disagree with the exact stored image.")
            if record["asset"] is not None and (record["asset"]["asset_ref"] != ref or record["asset"]["sha256"] != info["sha256"]):
                _fail("asset_binding", "Annotation asset reference or SHA-256 disagrees with the private index.")
            if asset["path"] in paths or info["sha256"] in digests or info["pixel_sha256"] in pixels:
                _fail("duplicate_image", "Manifest contains repeated image bytes or identical decoded pixels.")
            paths.add(asset["path"])
            digests.add(info["sha256"])
            pixels.add(info["pixel_sha256"])

    @staticmethod
    def _summary(workspace: dict, records: list[dict]) -> dict:
        return {"status": "valid", "revision": workspace["revision"], "samples": len(records),
                "real_samples": sum(record["sample_kind"] == "real" for record in records),
                "synthetic_samples": sum(record["sample_kind"] == "synthetic" for record in records),
                "splits": {split: sum(record["split"] == split for record in records) for split in SPLITS},
                "reviewed_samples": sum(record["review_status"] != "draft" for record in records),
                "accuracy": None}

    def validate(self, manifest: str | None = None) -> dict:
        with self._locked():
            workspace = self._load()
            records = workspace["annotations"]
            if manifest is not None:
                records = _parse_json(_read_file(self._path(manifest), MAX_JSON_BYTES, private=True))
                self._verify(workspace, records)
            return self._summary(workspace, records)

    def _commit(self, workspace: dict, action: str, affected: list[str], **metadata) -> dict:
        _check_annotations(workspace["annotations"])
        workspace["revision"] += 1
        workspace["events"].append({"revision": workspace["revision"], "action": action,
                                     "sample_ids": affected, **metadata})
        _atomic_save(self.root, workspace)
        return {**self._summary(workspace, workspace["annotations"]), "affected_samples": len(affected)}

    def import_image(self, image: Path | str, *, sample_kind: str, recording_session_id: str,
                     dedupe_group_id: str, state_group_id: str, annotator_id: str,
                     redaction: str = "none") -> dict:
        if sample_kind not in ("real", "synthetic") or redaction not in ("none", "redacted"):
            _fail("invalid_import", "Specify sample_kind as real or synthetic, and a supported redaction claim.")
        for key, value in (("recording_session_id", recording_session_id), ("dedupe_group_id", dedupe_group_id),
                           ("state_group_id", state_group_id), ("annotator_id", annotator_id)):
            _token(value, key)
        with self._locked():
            workspace = self._load()
            data = _read_file(Path(image), MAX_IMAGE_BYTES)
            info = _image_info(data)
            if any(asset["sha256"] == info["sha256"] or asset["pixel_sha256"] == info["pixel_sha256"]
                   for asset in workspace["assets"].values()):
                _fail("duplicate_image", "Image bytes or decoded pixels already exist; renaming or re-encoding does not create a new sample.")
            sample_id, asset_ref = "sample-" + uuid.uuid4().hex, "asset-" + uuid.uuid4().hex
            relative = "assets/" + asset_ref + (".png" if info["format"] == "PNG" else ".jpg")
            record = _new_annotation(workspace, info, sample_kind=sample_kind, sample_id=sample_id,
                                     asset_ref=asset_ref, session=recording_session_id, dedupe_group=dedupe_group_id,
                                     state_group=state_group_id, annotator=annotator_id, redaction=redaction)
            workspace["annotations"].append(record)
            _check_annotations(workspace["annotations"])
            workspace["assets"][sample_id] = {**info, "asset_ref": asset_ref, "path": relative, "sample_kind": sample_kind}
            destination = self._path(relative)
            installed = False
            try:
                _write_new(destination, data)
                installed = True
                result = self._commit(workspace, "import", [sample_id], time_basis="host_local_import_decode")
            except BaseException:
                if installed:
                    destination.unlink(missing_ok=True)
                raise
            return {**result, "sample_id": sample_id, "width_px": info["width_px"], "height_px": info["height_px"]}

    @staticmethod
    def _revision(workspace: dict, expected_revision: int) -> None:
        if type(expected_revision) is not int or expected_revision != workspace["revision"]:
            _fail("revision_conflict", "Workspace changed; read the current revision and review the current sample before retrying.")

    @staticmethod
    def _record(workspace: dict, sample_id: str) -> dict:
        for record in workspace["annotations"]:
            if record["sample_id"] == sample_id:
                return record
        _fail("sample_missing", "The requested sample is absent from this workspace.")

    def _new_document(self, relative: str, value: Any, directory: str) -> str:
        if not isinstance(relative, str) or not relative.startswith(directory + "/") or len(PurePosixPath(relative).parts) != 2:
            _fail("invalid_output", "Write a new JSON file directly in the designated private output directory.")
        if not relative.endswith(".json"):
            _fail("invalid_output", "Output must use a .json suffix.")
        data = _json_bytes(value)
        _write_new(self._path(relative), data)
        return hashlib.sha256(data).hexdigest()

    def export_manifest(self, output: str) -> dict:
        with self._locked():
            workspace = self._load()
            digest = self._new_document(output, workspace["annotations"], "manifests")
            return {**self._summary(workspace, workspace["annotations"]), "manifest_sha256": digest}

    def edit_template(self, sample_id: str, output: str) -> dict:
        with self._locked():
            workspace = self._load()
            record = self._record(workspace, sample_id)
            self._new_document(output, {"sample_id": sample_id, "expected_revision": workspace["revision"],
                                        "labels": record["labels"], "conditions": record["conditions"]}, "edits")
            return {"status": "template_written", "revision": workspace["revision"]}

    def annotate(self, edit_file: str, *, annotator_id: str) -> dict:
        _token(annotator_id, "annotator_id")
        with self._locked():
            workspace = self._load()
            edit = _parse_json(_read_file(self._path(edit_file), MAX_JSON_BYTES, private=True))
            if not isinstance(edit, dict) or set(edit) != {"sample_id", "expected_revision", "labels", "conditions"}:
                _fail("invalid_edit", "Edit input must be a template containing sample_id, expected_revision, labels and conditions only.")
            self._revision(workspace, edit["expected_revision"])
            record = self._record(workspace, edit["sample_id"])
            if record["split"] == "acceptance":
                _fail("acceptance_frozen", "Return the entire recording cohort to quarantine before changing acceptance annotations.")
            record.update(labels=deepcopy(edit["labels"]), conditions=deepcopy(edit["conditions"]),
                          annotator_id=annotator_id, review_status="draft", reviewer_id=None)
            return self._commit(workspace, "annotate", [record["sample_id"]], annotator_id=annotator_id)

    def review(self, sample_id: str, *, reviewer_id: str, expected_revision: int,
               evidence_ref: str, status: str = "independently_reviewed") -> dict:
        _token(reviewer_id, "reviewer_id")
        _token(evidence_ref, "evidence_ref")
        if status not in ("independently_reviewed", "adjudicated"):
            _fail("invalid_review", "Review status must be independently_reviewed or adjudicated.")
        with self._locked():
            workspace = self._load()
            self._revision(workspace, expected_revision)
            record = self._record(workspace, sample_id)
            if reviewer_id == record["annotator_id"]:
                _fail("independent_review_required", "A different person must supply a different reviewer code.")
            record.update(reviewer_id=reviewer_id, review_status=status)
            return self._commit(workspace, "review", [sample_id], reviewer_id=reviewer_id, evidence_ref=evidence_ref,
                                labels_sha256=hashlib.sha256(_json_bytes(record["labels"])).hexdigest())

    def assign_split(self, recording_session_ids: list[str], *, split: str, expected_revision: int) -> dict:
        if split not in SPLITS or not recording_session_ids:
            _fail("invalid_split", "Choose a split and at least one recording session.")
        sessions = {_token(value, "recording_session_id") for value in recording_session_ids}
        with self._locked():
            workspace = self._load()
            self._revision(workspace, expected_revision)
            records = workspace["annotations"]
            if not sessions <= {record["recording_session_id"] for record in records}:
                _fail("session_missing", "A requested recording session is absent.")
            # Whole sessions connected through dedupe groups form one cohort.
            while True:
                groups = {record["dedupe_group_id"] for record in records if record["recording_session_id"] in sessions}
                expanded = sessions | {record["recording_session_id"] for record in records if record["dedupe_group_id"] in groups}
                if expanded == sessions:
                    break
                sessions = expanded
            affected = [record for record in records if record["recording_session_id"] in sessions]
            if split != "quarantine" and any(record["sample_kind"] == "synthetic" for record in affected):
                _fail("synthetic_split_forbidden", "Synthetic samples must remain in quarantine, including all dedupe-connected recording sessions.")
            if split == "acceptance":
                if any(record["sample_kind"] != "real" or record["review_status"] == "draft" for record in affected):
                    _fail("acceptance_review_required", "Acceptance requires real-image records with completed independent review; synthetic samples remain quarantined.")
            for record in affected:
                record["split"] = split
            result = self._commit(workspace, "split", [record["sample_id"] for record in affected], target_split=split)
            return {**result, "affected_sessions": len(sessions)}
