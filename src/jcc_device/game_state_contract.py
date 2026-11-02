"""Offline game-state-v1 checks; importing this module never accesses a device.

JSON Schema is the structural authority. The standard-library semantic checks
cover references, one clock domain, expiry and consecutive snapshot updates.
The optional jsonschema package is loaded only by make_validator/validate_state.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterator


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas/game-state-v1.schema.json"
CORE_FIELDS = ("scene", "phase", "mode", "game_version", "round", "hp", "gold", "level")


class ContractViolation(ValueError):
    """A snapshot failed structural or semantic validation."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def iter_observations(state: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Enumerate atomic fields, including namespaced extension observations."""
    for key in CORE_FIELDS:
        yield "/" + key, state[key]
    for index, slot in enumerate(state["shop"]):
        yield f"/shop/{index}/content", slot["content"]
        for key, field in slot.get("extensions", {}).items():
            yield f"/shop/{index}/extensions/{key}", field
    for container in ("affordances", "extensions"):
        for key, field in state.get(container, {}).items():
            yield f"/{container}/{key}", field


def _same_value(left: Any, right: Any) -> bool:
    # JSON numbers 1 and 1.0 are equal; the JSON boolean true is distinct.
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_value(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return left == right


def semantic_errors(state: dict[str, Any], previous: dict[str, Any] | None = None) -> list[str]:
    """Check a structurally valid snapshot; previous must also be structurally valid.

    This validates claimed provenance and transitions, not actual pixels, TTL
    calibration, model accuracy or the correctness of the producer's epochs.
    """
    errors: list[str] = []
    now = state["assembled_host_monotonic_ms"]

    def add(path: str, message: str) -> None:
        errors.append(f"{path}: {message}")

    def finite(value: Any, path: str = "") -> None:
        if isinstance(value, float) and not math.isfinite(value):
            add(path or "/", "non-finite numbers are not JSON values")
        elif isinstance(value, dict):
            for key, child in value.items():
                finite(child, path + "/" + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                finite(child, path + "/" + str(index))

    def box(value: dict[str, float] | None, path: str) -> None:
        if value is not None and (value["x_min"] >= value["x_max"] or value["y_min"] >= value["y_max"]):
            add(path, "normalized box must have positive area")

    finite(state)
    frames = state["frames"]
    by_id = {frame["frame_id"]: frame for frame in frames}
    if len(by_id) != len(frames):
        add("/frames", "frame_id must be unique")
    if len({frame["sequence"] for frame in frames}) != len(frames):
        add("/frames", "sequence must be unique within a stream")
    anchor = by_id.get(state["frame_id"])
    if anchor is None:
        add("/frame_id", "anchor frame is absent from frames")
    elif anchor["sequence"] != max(frame["sequence"] for frame in frames):
        add("/frame_id", "anchor must be the newest registered frame")
    if anchor is not None and anchor["context_epoch"] != state["context_epoch"]:
        add("/context_epoch", "must match the anchor frame")

    ordered = sorted(frames, key=lambda frame: frame["sequence"])
    for index, frame in enumerate(ordered):
        path = f"/frames(sequence={frame['sequence']})"
        if frame["host_received_monotonic_ms"] > now:
            add(path, "frame reception cannot occur after snapshot assembly")
        if frame["context_epoch"] > state["context_epoch"]:
            add(path, "frame epoch cannot exceed the snapshot epoch")
        image = frame["image"]
        width, height = image["width_px"], image["height_px"]
        expected = "landscape" if width > height else "portrait" if width < height else "square"
        if image["orientation"] != expected:
            add(path + "/image/orientation", "does not match decoded-frame dimensions")
        box(image["game_viewport"], path + "/image/game_viewport")
        capture = frame["device_capture_time"]
        if capture is not None and capture["clock_id"] == state["host_clock_id"]:
            add(path + "/device_capture_time", "device timestamps require a distinct clock domain")
        if index:
            earlier = ordered[index - 1]
            if frame["host_received_monotonic_ms"] < earlier["host_received_monotonic_ms"]:
                add(path, "reception time regressed as sequence increased")
            if frame["context_epoch"] < earlier["context_epoch"]:
                add(path, "context epoch regressed as sequence increased")
            if frame["context_epoch"] == earlier["context_epoch"] and image != earlier["image"]:
                add(path + "/image", "geometry or calibration changes require a new context epoch")

    def provenance(record: dict[str, Any], path: str, *, active: bool, scope: str) -> dict | None:
        source = by_id.get(record["source_frame_id"])
        if source is None:
            add(path + "/source_frame_id", "source frame is absent from frames")
            return None
        observed = record["observed_host_monotonic_ms"]
        completed = record["completed_host_monotonic_ms"]
        if observed != source["host_received_monotonic_ms"]:
            add(path + "/observed_host_monotonic_ms", "must equal the source frame's host reception time")
        if not observed <= completed <= now:
            add(path + "/completed_host_monotonic_ms", "must lie between source reception and assembly")
        expires = record["expires_host_monotonic_ms"]
        if expires is not None and expires <= observed:
            add(path + "/expires_host_monotonic_ms", "must be later than source reception")
        if anchor is not None and source["sequence"] > anchor["sequence"]:
            add(path + "/source_frame_id", "source is newer than the snapshot anchor")
        if active and scope == "context" and source["context_epoch"] != state["context_epoch"]:
            add(path, "active context-scoped field cites an obsolete context")
        box(record["region"], path + "/region")
        return source

    for path, field in iter_observations(state):
        status = field["status"]
        source = provenance(field, path, active=status != "stale", scope=field["scope"])
        expires = field["expires_host_monotonic_ms"]
        if status == "recognized" and expires <= now:
            add(path, "recognized value is expired; use stale with value=null")
        if status == "stale" and field["reason"] == "expired" and expires > now:
            add(path, "expired reason precedes the deadline")
        if status == "stale" and field["reason"] == "context_changed" and source is not None:
            if field["scope"] != "context" or source["context_epoch"] >= state["context_epoch"]:
                add(path, "context_changed must refer to an older context-scoped observation")
        if status == "conflict":
            candidates = field["candidates"]
            if all(_same_value(candidates[0]["value"], item["value"]) for item in candidates[1:]):
                add(path + "/candidates", "conflict needs at least two distinct values")
            candidate_frames = []
            for index, candidate in enumerate(candidates):
                candidate_path = f"{path}/candidates/{index}"
                candidate_source = provenance(candidate, candidate_path, active=True, scope=field["scope"])
                if candidate_source is not None:
                    candidate_frames.append(candidate_source)
                if candidate["expires_host_monotonic_ms"] <= now:
                    add(candidate_path, "expired candidate cannot support a current conflict")
                if candidate["completed_host_monotonic_ms"] > field["completed_host_monotonic_ms"]:
                    add(candidate_path, "candidate completion is later than conflict assembly")
            if candidate_frames and source is not None:
                if source["sequence"] != max(frame["sequence"] for frame in candidate_frames):
                    add(path + "/source_frame_id", "conflict must cite the newest candidate frame")

    if previous is not None:
        errors.extend(_transition_errors(state, previous))
    return errors


def _transition_errors(state: dict, previous: dict) -> list[str]:
    errors: list[str] = []
    if any(state[key] != previous[key] for key in ("stream_id", "host_clock_id", "sample_kind")):
        return ["/: previous belongs to a different stream, clock or sample kind; reset the merge context"]
    if state["snapshot_revision"] <= previous["snapshot_revision"]:
        errors.append("/snapshot_revision: must increase for each accepted snapshot")
    if state["assembled_host_monotonic_ms"] < previous["assembled_host_monotonic_ms"]:
        errors.append("/assembled_host_monotonic_ms: cannot regress")
    if state["context_epoch"] < previous["context_epoch"]:
        errors.append("/context_epoch: cannot regress")
    if state["freshness_policy_id"] != previous["freshness_policy_id"]:
        errors.append("/freshness_policy_id: policy is immutable within a stream; start a new merge context")
    old_frames = {frame["frame_id"]: frame for frame in previous["frames"]}
    frames = {frame["frame_id"]: frame for frame in state["frames"]}
    if previous["frame_id"] in old_frames and state["frame_id"] in frames:
        old_anchor = old_frames[previous["frame_id"]]
        anchor = frames[state["frame_id"]]
        if anchor["sequence"] < old_anchor["sequence"]:
            errors.append("/frame_id: anchor sequence cannot regress")
        if anchor["host_received_monotonic_ms"] < old_anchor["host_received_monotonic_ms"]:
            errors.append("/frame_id: anchor reception time cannot regress")
        if state["context_epoch"] == previous["context_epoch"] and anchor["image"] != old_anchor["image"]:
            errors.append("/context_epoch: geometry or calibration change requires a new epoch")
    old_sequences = {frame["sequence"]: frame["frame_id"] for frame in previous["frames"]}
    for frame_id, frame in frames.items():
        if frame_id in old_frames and frame != old_frames[frame_id]:
            errors.append("/frames: previously published frame metadata is immutable")
        if frame["sequence"] in old_sequences and old_sequences[frame["sequence"]] != frame_id:
            errors.append("/frames: a sequence cannot be reassigned to a different frame")
    old_fields = dict(iter_observations(previous))
    fields = dict(iter_observations(state))
    for path, old in old_fields.items():
        if path not in fields:
            errors.append(path + ": field disappeared; emit unknown instead to preserve its watermark")
            continue
        field = fields[path]
        old_source = old_frames.get(old["source_frame_id"])
        source = frames.get(field["source_frame_id"])
        if old_source is None or source is None:
            continue  # The standalone reference checks report this.
        if source["sequence"] < old_source["sequence"]:
            errors.append(path + ": late result would overwrite a newer field watermark")
        elif source["sequence"] == old_source["sequence"]:
            if field["completed_host_monotonic_ms"] < old["completed_host_monotonic_ms"]:
                errors.append(path + ": same-frame completion time cannot regress")
            if field["status"] == "stale" and old["status"] in ("recognized", "stale"):
                old_value = old["value"] if old["status"] == "recognized" else old["last_known_value"]
                if not _same_value(field["last_known_value"], old_value):
                    errors.append(path + ": stale history must preserve the previously observed value")
                for key in ("observed_host_monotonic_ms", "completed_host_monotonic_ms",
                            "expires_host_monotonic_ms", "method", "region", "confidence", "scope"):
                    if not _same_value(field.get(key), old.get(key)):
                        errors.append(path + ": stale history must preserve its original provenance")
                        break
            if field["status"] == old["status"] == "recognized" and not _same_value(field["value"], old["value"]):
                errors.append(path + ": disagreeing same-frame recognized values require a conflict")
            old_expiry, expiry = old["expires_host_monotonic_ms"], field["expires_host_monotonic_ms"]
            if old_expiry is not None and expiry is not None and expiry > old_expiry:
                errors.append(path + ": same-frame results cannot renew the existing deadline")
    return errors


def make_validator(schema_path: Path = SCHEMA_PATH, *, definition: str | None = None):
    """Create a Draft 2020-12 validator without remote references or installs."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError as error:
        raise RuntimeError("Validation requires the optional jsonschema package; no dependency was installed.") from error
    schema = load_json(schema_path)
    Draft202012Validator.check_schema(schema)
    if definition is not None:
        if definition not in schema["$defs"]:
            raise ValueError("Unknown schema definition: " + definition)
        schema = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/" + definition}
    return Draft202012Validator(schema)


def validate_state(state: dict, previous: dict | None = None, *, validator=None) -> None:
    """Validate a snapshot and, optionally, its transition from the last accepted one."""
    if validator is None:
        validator = make_validator()
    errors = []
    candidates = [("", state)] if previous is None else [("/previous", previous), ("", state)]
    for prefix, candidate in candidates:
        structural = list(validator.iter_errors(candidate))
        for error in structural:
            path = "/".join(str(part) for part in error.absolute_path)
            errors.append(f"{prefix}/{path}: schema constraint {error.validator!r} failed")
        if not structural:
            errors.extend(prefix + error for error in semantic_errors(candidate))
    if not errors and previous is not None:
        errors.extend(_transition_errors(state, previous))
    if errors:
        raise ContractViolation(errors)


def validate_annotations(records: list[dict], *, validator=None) -> None:
    """Check local sample metadata and dataset split isolation, without opening assets."""
    if not isinstance(records, list):
        raise ContractViolation(["/: sample manifest must be a list of records"])
    if validator is None:
        validator = make_validator(definition="sample_annotation")
    errors: list[str] = []
    sample_ids: set[str] = set()
    session_splits: dict[str, str] = {}
    dedupe_splits: dict[str, str] = {}
    accepted_groups: set[str] = set()
    asset_groups: dict[str, str] = {}
    for index, record in enumerate(records):
        prefix = f"/{index}"
        structural = list(validator.iter_errors(record))
        if structural:
            errors.extend(f"{prefix}: sample schema constraint {error.validator!r} failed" for error in structural)
            continue
        try:
            json.dumps(record, allow_nan=False)
        except ValueError:
            errors.append(prefix + ": non-finite numbers are not JSON values")
        sample_id = record["sample_id"]
        if sample_id in sample_ids:
            errors.append(prefix + "/sample_id: duplicate sample")
        sample_ids.add(sample_id)
        split, group = record["split"], record["dedupe_group_id"]
        for key, mapping in (("recording_session_id", session_splits), ("dedupe_group_id", dedupe_splits)):
            if mapping.setdefault(record[key], split) != split:
                errors.append(prefix + "/" + key + ": group cannot span dataset splits")
        if split == "acceptance":
            if group in accepted_groups:
                errors.append(prefix + "/dedupe_group_id: acceptance needs one representative per dedupe group")
            accepted_groups.add(group)
        if record["asset"] is not None:
            digest = record["asset"]["sha256"]
            if asset_groups.setdefault(digest, group) != group:
                errors.append(prefix + "/asset/sha256: identical images require the same dedupe group")
        if record["review_status"] != "draft":
            if record["reviewer_id"] is None or record["reviewer_id"] == record["annotator_id"]:
                errors.append(prefix + "/reviewer_id: independent review requires a different reviewer")
        frame = record["frame"]
        image = frame["image"]
        expected = ("landscape" if image["width_px"] > image["height_px"]
                    else "portrait" if image["width_px"] < image["height_px"] else "square")
        if image["orientation"] != expected:
            errors.append(prefix + "/frame/image/orientation: dimensions disagree")
        if frame["device_capture_time"] is not None and frame["device_capture_time"]["clock_id"] == record["host_clock_id"]:
            errors.append(prefix + "/frame/device_capture_time: device timestamps require a distinct clock domain")
        boxes = [("/frame/image/game_viewport", image["game_viewport"])]
        for name in CORE_FIELDS:
            boxes.append(("/labels/" + name + "/region", record["labels"][name]["region"]))
        for slot in record["labels"]["shop"]:
            boxes.append((f"/labels/shop/{slot['slot'] - 1}/content/region", slot["content"]["region"]))
        for path, region in boxes:
            if region is not None and (region["x_min"] >= region["x_max"] or region["y_min"] >= region["y_max"]):
                errors.append(prefix + path + ": normalized box must have positive area")
    if errors:
        raise ContractViolation(errors)


def load_json(path: Path) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError("Non-JSON numeric constant: " + value)

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def main() -> int:
    parser = argparse.ArgumentParser(description="离线校验 game-state-v1；不连接平板、不安装依赖")
    parser.add_argument("state", type=Path)
    parser.add_argument("--previous", type=Path, help="同一流的上一份已接纳快照")
    parser.add_argument("--annotations", action="store_true", help="校验样本标注对象或数组")
    args = parser.parse_args()
    if args.annotations and args.previous:
        parser.error("--annotations cannot be combined with --previous")
    try:
        data = load_json(args.state)
        if args.annotations:
            validate_annotations(data if isinstance(data, list) else [data])
        else:
            previous = load_json(args.previous) if args.previous else None
            validate_state(data, previous)
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error))
        return 1
    print("Contract schema and semantic checks passed (offline; no recognition score implied)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
