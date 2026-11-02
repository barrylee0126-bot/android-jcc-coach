"""Offline, read-only visual candidate review for explicit frozen sample stores.

No image, OCR, source identifier, path or descriptor is included in a report.
Similarity is only a review lead: it neither establishes state equivalence nor
certifies independence, and this module never edits samples or assigns splits.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict, dataclass
import hashlib
import io
from importlib.metadata import version as package_version
import json
import math
import os
from pathlib import Path
import platform
import re
import stat
import sys
from typing import Sequence
import uuid
import warnings

import numpy as np
from PIL import Image, __version__ as PILLOW_VERSION

from . import workflow as wf


METHOD_VERSION = "jcc-near-duplicate-review-v1.0.0"
REPORT_SCHEMA = "near-duplicate-review-v1"
THUMBNAIL_SIZE = 32
TILE_GRID = 4
HASH_SIZE = 8
MAX_SOURCES = 8
NOTICE_CODES = [
    "uncalibrated_thresholds_manual_review_only",
    "similar_pixels_do_not_establish_same_game_state",
    "non_candidates_do_not_establish_independence_or_truth",
    "declared_groups_and_provenance_not_authenticated",
    "only_explicit_full_snapshots_are_in_scope",
    "no_effective_acceptance_sample_count_or_m1_verdict",
    "local_report_keep_private_no_images_or_descriptors",
]


class NearDuplicateError(ValueError):
    """A fixed error code and content-free message; no partial report exists."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _fail(code: str, message: str) -> None:
    raise NearDuplicateError(code, message)


@dataclass(frozen=True)
class FrozenSource:
    """A caller-selected private store and its full current immutable export."""

    private_dir: Path | str
    manifest: str


@dataclass(frozen=True)
class ReviewConfig:
    """Engineering defaults only, not thresholds calibrated on game samples."""

    aspect_tolerance: float = 0.02
    global_mae: float = 20.0
    dhash_hamming: int = 12
    tile_mae: float = 10.0
    min_matching_tiles: int = 12
    local_global_mae: float = 45.0
    max_entries: int = 400
    max_pairs: int = 80_000
    max_candidates: int = 10_000

    def validate(self) -> None:
        for name, maximum in (("aspect_tolerance", 0.1), ("global_mae", 255),
                              ("tile_mae", 255), ("local_global_mae", 255)):
            value = getattr(self, name)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not 0 <= value <= maximum):
                _fail("invalid_config", "Thresholds must be finite numbers within documented bounds.")
        for name, minimum, maximum in (("dhash_hamming", 0, 64), ("min_matching_tiles", 1, 16),
                                       ("max_entries", 1, 2000), ("max_pairs", 1, 2_000_000),
                                       ("max_candidates", 1, 100_000)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                _fail("invalid_config", "Integer settings must be within documented bounds.")


@dataclass
class _Feature:
    rgb: np.ndarray
    dhash: int
    low_information: bool
    width: int
    height: int
    byte_sha256: str
    pixel_sha256: str


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _feature(data: bytes, asset: dict) -> _Feature:
    # _load() already decoded and verified this exact byte digest. Read the same
    # bytes again so a producer ignoring the advisory lock cannot substitute an
    # unchecked image between the store check and feature extraction.
    if _sha(data) != asset["sha256"]:
        _fail("input_changed", "A checked asset changed before feature extraction.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            rgba = image.convert("RGBA")
            if rgba.getchannel("A").getextrema() != (255, 255):
                _fail("unsupported_transparency", "Non-opaque images require an explicit private preprocessing step.")
            rgb_image = rgba.convert("RGB")
            rgb = np.array(rgb_image.resize((THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.Resampling.LANCZOS),
                           dtype=np.int16)
            gray = rgb_image.convert("L")
            hash_pixels = np.array(gray.resize((HASH_SIZE + 1, HASH_SIZE), Image.Resampling.LANCZOS))
            dhash = 0
            for bit in (hash_pixels[:, 1:] > hash_pixels[:, :-1]).flat:
                dhash = (dhash << 1) | int(bit)
            gray_small = np.array(gray.resize((THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.Resampling.LANCZOS))
            low_information = float(gray_small.std()) < 4.0
    except NearDuplicateError:
        raise
    except (OSError, ValueError, SyntaxError, EOFError):
        _fail("image_unreadable", "A checked image could not be decoded for candidate review.")
    return _Feature(rgb, dhash, low_information, asset["width_px"], asset["height_px"],
                    asset["sha256"], asset["pixel_sha256"])


def _compare(left: _Feature, right: _Feature, config: ReviewConfig) -> dict | None:
    cross_left = left.width * right.height
    cross_right = right.width * left.height
    aspect_delta = abs(cross_left - cross_right) / max(cross_left, cross_right)
    if aspect_delta > config.aspect_tolerance:
        return None  # Geometry filtering is reported; it is not evidence of independence.
    difference = np.abs(left.rgb - right.rgb)
    global_mae = float(difference.mean())
    tile_errors = difference.reshape(TILE_GRID, THUMBNAIL_SIZE // TILE_GRID,
                                     TILE_GRID, THUMBNAIL_SIZE // TILE_GRID, 3).mean(axis=(1, 3, 4))
    matching_tiles = int(np.count_nonzero(tile_errors <= config.tile_mae))
    hamming = (left.dhash ^ right.dhash).bit_count()
    reasons = []
    if left.byte_sha256 == right.byte_sha256:
        reasons.append("identical_image_bytes")
    if left.pixel_sha256 == right.pixel_sha256:
        reasons.append("identical_decoded_pixels")
    if global_mae <= config.global_mae and hamming <= config.dhash_hamming:
        reasons.append("whole_thumbnail_similarity")
    if matching_tiles >= config.min_matching_tiles and global_mae <= config.local_global_mae:
        reasons.append("mostly_matching_tiles")
    return {
        "reasons": reasons,
        "scores": {"rgb_thumbnail_mae_0_255": round(global_mae, 6),
                   "dhash_hamming_0_64": hamming, "matching_tiles_of_16": matching_tiles,
                   "aspect_relative_delta": round(aspect_delta, 8)},
        "low_information_warning": left.low_information or right.low_information,
    }


def _manifest_path(store: wf.SampleStore, relative: str) -> Path:
    # Freeze exports only, never arbitrary caller-selected file traversal.
    if (not isinstance(relative, str) or len(relative.split("/")) != 2
            or relative.split("/")[0] != "manifests" or not relative.endswith(".json")):
        _fail("invalid_manifest", "Select a JSON export directly inside the private manifests directory.")
    return store._path(relative)


def _alias(mapping: dict, value: str, prefix: str) -> str:
    if value not in mapping:
        mapping[value] = f"{prefix}-{len(mapping) + 1:06d}"
    return mapping[value]


def _boundary(left: dict, right: dict, raw_left: dict, raw_right: dict) -> dict:
    splits = {left["split"], right["split"]}
    cross_source = left["source"] != right["source"]
    return {
        "cross_source": cross_source,
        "cross_split": len(splits) > 1,
        "cross_development_acceptance": splits == {"development", "acceptance"},
        # Across stores, equal caller codes may be a collision. Do not silently
        # merge namespaces or infer independent recordings from different codes.
        "declared_code_equal": {key: raw_left[key] == raw_right[key]
                                for key in ("recording_session_id", "dedupe_group_id", "state_group_id")},
        "cross_source_identity": "unverified" if cross_source else "within_declared_source",
    }


def _review(sources: Sequence[FrozenSource], config: ReviewConfig) -> dict:
    if not isinstance(config, ReviewConfig):
        _fail("invalid_config", "Use a ReviewConfig object.")
    config.validate()
    if (not isinstance(sources, (list, tuple)) or not 1 <= len(sources) <= MAX_SOURCES
            or any(not isinstance(source, FrozenSource) for source in sources)):
        _fail("invalid_sources", "Provide one to eight explicit frozen sources.")
    stores = [wf.SampleStore(source.private_dir) for source in sources]
    if len({store.root for store in stores}) != len(stores):
        _fail("repeated_source", "One workspace may appear only once per review.")
    entries, raw_records, features, provenance, stable_files = [], [], [], [], []
    byte_bindings = []
    with ExitStack() as stack:
        # Existing nonblocking locks do not create files or alter source bytes.
        for store in sorted(stores, key=lambda item: str(item.root)):
            stack.enter_context(store._locked())
        for source_index, (source, store) in enumerate(zip(sources, stores), 1):
            manifest_path = _manifest_path(store, source.manifest)
            manifest_bytes = wf._read_file(manifest_path, wf.MAX_JSON_BYTES, private=True)
            records = wf._parse_json(manifest_bytes)
            if not isinstance(records, list):
                _fail("invalid_manifest", "The frozen export must be an annotation array.")
            size = len(entries) + len(records)
            if size > config.max_entries or size * (size - 1) // 2 > config.max_pairs:
                _fail("scan_limit_exceeded", "The corpus exceeds the configured entry budget; no partial review was returned.")
            # Reuse the authoritative store validator, not a weakened copy of
            # its annotation, split, image integrity and private-path checks.
            workspace_bytes = wf._read_file(store._path("workspace.json"), wf.MAX_JSON_BYTES, private=True)
            preview = wf._parse_json(workspace_bytes)
            if (isinstance(preview, dict) and isinstance(preview.get("annotations"), list)
                    and len(preview["annotations"]) + len(entries) > config.max_entries):
                _fail("scan_limit_exceeded", "The source workspace exceeds the configured entry budget.")
            workspace = store._load()
            wf._check_annotations(records)
            # Python equality considers True == 1, which is unsuitable for
            # frozen JSON bindings. Validate both sides and bind exact types.
            if _canonical(records) != _canonical(workspace["annotations"]):
                _fail("manifest_snapshot_mismatch", "Use the full current frozen export; subsets or stale snapshots cannot support a split review.")
            source_token = f"source-{source_index:04d}"
            aliases = {key: {} for key in ("recording_session_id", "dedupe_group_id", "state_group_id")}
            for ordinal, record in enumerate(records, 1):
                entry = {"entry": f"entry-{len(entries) + 1:06d}", "source": source_token,
                         "manifest_ordinal_1based": ordinal, "split": record["split"],
                         "declared_sample_kind": record["sample_kind"]}
                for key, prefix in (("recording_session_id", "session"), ("dedupe_group_id", "dedupe"),
                                    ("state_group_id", "state")):
                    entry[key.replace("_id", "_alias")] = source_token + ":" + _alias(aliases[key], record[key], prefix)
                asset = workspace["assets"][record["sample_id"]]
                path = store._path(asset["path"])
                data = wf._read_file(path, wf.MAX_IMAGE_BYTES, private=True)
                features.append(_feature(data, asset))
                entries.append(entry)
                raw_records.append(record)
                stable_files.append((path, wf.MAX_IMAGE_BYTES, asset["sha256"]))
                byte_bindings.append({"entry": entry["entry"], "image_sha256": asset["sha256"],
                                      "pixel_sha256": asset["pixel_sha256"]})
            provenance.append({"source": source_token, "workspace_schema": workspace["schema_version"],
                               "workspace_revision": workspace["revision"], "input_entries": len(records),
                               "workspace_bytes_sha256": _sha(workspace_bytes),
                               "manifest_bytes_sha256": _sha(manifest_bytes),
                               "manifest_canonical_sha256": _sha(_canonical(records))})
            stable_files.extend(((manifest_path, wf.MAX_JSON_BYTES, _sha(manifest_bytes)),
                                 (store._path("workspace.json"), wf.MAX_JSON_BYTES, _sha(workspace_bytes))))
        if not entries:
            _fail("empty_corpus", "An empty corpus cannot produce a candidate review.")
        total_pairs = len(entries) * (len(entries) - 1) // 2
        if total_pairs > config.max_pairs:
            _fail("scan_limit_exceeded", "All pairs exceed the configured budget; no partial review was returned.")
        candidates, declared_boundary_alerts = [], []
        geometry_skipped = 0
        for i, left in enumerate(entries):
            for j in range(i + 1, len(entries)):
                right = entries[j]
                boundary = _boundary(left, right, raw_records[i], raw_records[j])
                pair = {"left": left["entry"], "right": right["entry"]}
                if (boundary["cross_source"] and boundary["cross_split"]
                        and any(boundary["declared_code_equal"][key] for key in ("recording_session_id", "dedupe_group_id"))):
                    if len(declared_boundary_alerts) >= config.max_candidates:
                        _fail("candidate_limit_exceeded", "Declared boundary alerts exceed the report budget; no partial review was returned.")
                    declared_boundary_alerts.append({**pair, "boundaries": boundary,
                                                     "reason": "cross_source_declared_code_overlap_requires_identity_review"})
                comparison = _compare(features[i], features[j], config)
                if comparison is None:
                    geometry_skipped += 1
                    continue
                if not comparison["reasons"]:
                    continue
                if len(candidates) >= config.max_candidates:
                    _fail("candidate_limit_exceeded", "Visual candidates exceed the report budget; no partial review was returned.")
                priority = ("cross_development_acceptance" if boundary["cross_development_acceptance"]
                            else "cross_split" if boundary["cross_split"] else "within_split")
                candidates.append({**pair, "priority": priority, "boundaries": boundary,
                                   **comparison, "review_status": "pending", "decision": None})
        # The advisory lock covers cooperative writers; these final raw-byte
        # reads also catch source edits which ignored it during the scan.
        for path, limit, digest in stable_files:
            if _sha(wf._read_file(path, limit, private=True)) != digest:
                _fail("input_changed", "A frozen source changed during review; discard this run.")
    order = {"cross_development_acceptance": 0, "cross_split": 1, "within_split": 2}
    candidates.sort(key=lambda item: (order[item["priority"]], item["left"], item["right"]))
    for ordinal, candidate in enumerate(candidates, 1):
        candidate["review_item"] = f"review-{ordinal:06d}"
    return {
        "schema_version": REPORT_SCHEMA, "status": "complete_candidate_scan",
        "manual_review_status": "pending", "notice_codes": list(NOTICE_CODES),
        "method": {"version": METHOD_VERSION, "threshold_validation": "synthetic_tests_only_uncalibrated",
                   "module_sha256": _sha(Path(__file__).read_bytes()),
                   "workflow_module_sha256": _sha(Path(wf.__file__).read_bytes()),
                   "contract_module_sha256": _sha(Path(sys.modules[wf.validate_annotations.__module__].__file__).read_bytes()),
                   "game_state_schema_sha256": _sha((wf.PROJECT_ROOT / "schemas/game-state-v1.schema.json").read_bytes()),
                   "python": platform.python_version(), "pillow": PILLOW_VERSION, "numpy": np.__version__,
                   "jsonschema": package_version("jsonschema"),
                   "thumbnail_rgb": [THUMBNAIL_SIZE, THUMBNAIL_SIZE], "tile_grid": [TILE_GRID, TILE_GRID],
                   "dhash_bits": HASH_SIZE ** 2, "resize": "Pillow_LANCZOS", "config": asdict(config)},
        "provenance": {"source_order_matters": True, "sources": provenance,
                       "ordered_asset_binding_sha256": _sha(_canonical(byte_bindings)),
                       "caller_scope_completeness": "not_verified"},
        "coverage": {"input_entries": len(entries), "input_sources": len(sources),
                     "unordered_pairs_considered": total_pairs,
                     "pairs_scored": total_pairs - geometry_skipped,
                     "pairs_skipped_by_aspect_filter": geometry_skipped,
                     "visual_candidate_pairs": len(candidates),
                     "cross_development_acceptance_candidate_pairs": sum(
                         item["boundaries"]["cross_development_acceptance"] for item in candidates),
                     "declared_boundary_alert_pairs": len(declared_boundary_alerts),
                     "effective_acceptance_samples": None, "independent_recordings": None,
                     "distinct_game_states": None, "accuracy": None, "full_m1_acceptance": "not_assessed"},
        "entries": entries, "candidates": candidates, "declared_boundary_alerts": declared_boundary_alerts,
    }


def review_frozen_sources(sources: Sequence[FrozenSource], *, config: ReviewConfig | None = None) -> dict:
    """Return complete review leads, or raise without returning a partial result.

    Private store helpers are reused read-only under their existing locks. A
    full current export of each explicitly selected store is required; absent
    stores cannot be discovered or certified. Report ordinals resolve only
    against these exact hashed exports. Reports must remain private as well.
    """
    try:
        with warnings.catch_warnings():
            # A decoder warning may include metadata or filenames. Stop and
            # sanitize it rather than printing it or accepting a weak decode.
            warnings.simplefilter("error")
            return _review(sources, ReviewConfig() if config is None else config)
    except wf.WorkflowError as error:
        raise NearDuplicateError(error.code, "A frozen source failed the existing private sample checks; no partial review was returned.") from None
    except NearDuplicateError:
        raise
    except OSError:
        raise NearDuplicateError("local_io_failed", "A required local operation failed; no partial review was returned.") from None
    except MemoryError:
        raise NearDuplicateError("local_resource_failed", "Local memory was insufficient; no partial review was returned.") from None
    except Warning:
        raise NearDuplicateError("decoder_warning", "A source decoder or validator emitted a warning; no partial review was returned.") from None
    except (TypeError, KeyError, ValueError, RecursionError, OverflowError):
        raise NearDuplicateError("invalid_snapshot", "A source or configuration contains an unsupported structure; no partial review was returned.") from None


def _output_path(directory: Path | str, name: str, sources: Sequence[FrozenSource]) -> Path:
    root = wf._private_dir(directory, must_exist=True)
    if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}\.json", name) is None:
        _fail("invalid_output", "Use a short anonymous lowercase JSON report name.")
    for source in sources:
        source_root = wf._private_dir(source.private_dir, must_exist=True)
        if root == source_root or source_root in root.parents:
            _fail("source_write_forbidden", "Choose a private report directory outside every source workspace.")
    destination = root / name
    if destination.exists() or destination.is_symlink():
        _fail("output_exists", "The report destination already exists; choose a new anonymous name.")
    return destination


def _write_report(output: Path, data: bytes) -> None:
    """Stage a complete file, then publish using an atomic exclusive hard link.

    Final names never refer to a partly written JSON report. A process kill can
    leave the anonymous staging file (or a complete final file); no recovery
    material is automatically adopted or deleted on a subsequent run.
    """
    directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".near-review-" + uuid.uuid4().hex + ".tmp"
    created = False
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            _fail("unsafe_permissions", "The report directory is no longer private.")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        created = True
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, output.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        except FileExistsError:
            _fail("output_exists", "The report destination appeared during review; it was not overwritten.")
    finally:
        try:
            if created:
                os.unlink(temporary, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, '{"status":"no_report","error":{"code":"invalid_arguments","message":"Invalid or missing arguments; use --help."}}\n')


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="本机离线近似画面候选复核；阈值未实样本校准，不改样本或分集。")
    parser.add_argument("--source", nargs=2, action="append", required=True, metavar=("PRIVATE_DIR", "MANIFEST"),
                        help="重复指定私有工作区及其 manifests/ 内的完整当前冻结清单。")
    parser.add_argument("--output-dir", required=True, type=Path, help="已有 0700 本地私有目录，须在所有输入工作区之外。")
    parser.add_argument("--output-name", default="near-duplicate-review.json")
    defaults = ReviewConfig()
    for name in asdict(defaults):
        parser.add_argument("--" + name.replace("_", "-"), default=getattr(defaults, name),
                            type=int if type(getattr(defaults, name)) is int else float)
    args = parser.parse_args(argv)
    try:
        sources = [FrozenSource(root, manifest) for root, manifest in args.source]
        config = ReviewConfig(**{name: getattr(args, name) for name in asdict(defaults)})
        config.validate()
        output = _output_path(args.output_dir, args.output_name, sources)
        report = review_frozen_sources(sources, config=config)
        # Recheck output privacy and source separation after the potentially
        # longer scan. The anchored directory fd also rejects a replaced link.
        output = _output_path(args.output_dir, args.output_name, sources)
        _write_report(output, wf._json_bytes(report))
        print(json.dumps({"status": report["status"], "manual_review_status": "pending",
                          "coverage": report["coverage"]}, allow_nan=False))
        return 0
    except (NearDuplicateError, wf.WorkflowError) as error:
        code = error.code
    except (OSError, MemoryError):
        code = "local_resource_failed"
    print(json.dumps({"status": "no_report", "error": {"code": code,
                     "message": "Review was not completed; no partial report was created. Source content was not printed."}}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
