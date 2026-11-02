"""Fabricated, quarantine-only fixtures. No images or real account/device data.

These helpers test the evaluator and may never be cited as real measurements.
"""

from __future__ import annotations

from copy import deepcopy


VALUES = {"scene": "battle", "phase": "preparation", "round": {"stage": 3, "step": 2},
          "hp": 72, "gold": 50, "level": 7}
REGION = {"x_min": 0.1, "y_min": 0.7, "x_max": 0.25, "y_max": 0.95}


def label(value=None, *, applicability="applicable", reason="synthetic_unreadable") -> dict:
    return {"applicability": applicability, "truth_status": "known" if value is not None else "unknown",
            "value": deepcopy(value), "reason": None if value is not None else "not_applicable" if applicability == "not_applicable" else reason,
            "region": deepcopy(REGION)}


def sample(number=1) -> dict:
    """One fully known, entirely invented sample; mode and version are unknown."""
    return {
        "schema_version": "sample-annotation-v1", "sample_kind": "synthetic",
        "sample_id": f"synthetic-sample-{number}", "dataset_id": "synthetic-evaluation-v1-fixtures",
        "recording_session_id": f"synthetic-session-{number}", "stream_id": f"synthetic-stream-{number}",
        "host_clock_id": f"synthetic-clock-{number}", "split": "quarantine",
        "dedupe_group_id": f"synthetic-dedupe-{number}", "state_group_id": f"synthetic-state-{number}",
        "asset": None,
        "frame": {
            "frame_id": f"synthetic-frame-{number}", "sequence": 1, "context_epoch": 0,
            "host_received_monotonic_ms": 1000, "device_capture_time": None,
            "image": {"width_px": 1280, "height_px": 800, "orientation": "landscape", "rotation_applied_degrees": 0,
                      "coordinate_space": "decoded_frame_normalized_top_left", "game_viewport": {"x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1},
                      "calibration_id": "synthetic-layout-v1"},
        },
        "annotation_method": "synthetic", "annotator_id": "synthetic-annotator", "reviewer_id": None, "review_status": "draft",
        "conditions": ["synthetic_no_real_image"],
        "labels": {
            **{key: label(value) for key, value in VALUES.items()},
            "mode": label(reason="synthetic_mode_unverified"), "game_version": label(reason="synthetic_patch_unverified"),
            "shop": [{"slot": slot, "content": label({"kind": "empty"} if slot % 2 else
                      {"kind": "unit", "display_name": f"合成棋子{slot}", "catalog_ref": None})} for slot in range(1, 6)],
        },
    }


def observation(record: dict, truth: dict) -> dict:
    known = truth["truth_status"] == "known"
    return {
        "status": "recognized" if known else "unknown", "value": deepcopy(truth["value"]),
        "applicability": truth["applicability"], "scope": "context", "source_frame_id": record["frame"]["frame_id"],
        "observed_host_monotonic_ms": 1000, "completed_host_monotonic_ms": 1100,
        "expires_host_monotonic_ms": 2000 if known else None,
        "method": {"kind": "metadata", "name": "synthetic-fixture", "version": "fixture-v1"},
        "region": deepcopy(truth["region"]), "reason": None if known else truth["reason"],
    }


def prediction(record: dict) -> dict:
    fields = {key: observation(record, record["labels"][key]) for key in (*VALUES, "mode", "game_version")}
    return {
        "schema_version": "game-state-v1", "sample_kind": "synthetic", "stream_id": record["stream_id"],
        "host_clock_id": record["host_clock_id"], "snapshot_revision": 0, "context_epoch": 0,
        "frame_id": record["frame"]["frame_id"], "assembled_host_monotonic_ms": 1200,
        "freshness_policy_id": "synthetic-policy-not-a-recommendation", "frames": [deepcopy(record["frame"])],
        **fields,
        "shop": [{"slot": item["slot"], "content": observation(record, item["content"])} for item in record["labels"]["shop"]],
    }


def binding(record: dict, state=None) -> dict:
    return {"sample_id": record["sample_id"], "state": prediction(record) if state is None else state}


def set_status(state: dict, field: str, status: str):
    """Change one fabricated field to a valid abstention/stale/conflict envelope."""
    item = state[field] if not field.startswith("shop.") else state["shop"][int(field.split(".")[1]) - 1]["content"]
    old = deepcopy(item)
    item.update(status=status, value=None)
    item.pop("last_known_value", None)
    item.pop("candidates", None)
    if status == "unknown":
        item.update(reason="synthetic_unreadable", expires_host_monotonic_ms=None)
    elif status == "stale":
        item.update(reason="invalidated", last_known_value=old["value"])
    elif status == "conflict":
        value = old["value"]
        if isinstance(value, int):
            alternate = value + 1
        elif field == "round":
            alternate = {**value, "step": value["step"] + 1}
        elif field == "scene":
            alternate = "other" if value != "other" else "battle"
        elif field == "phase":
            alternate = "combat" if value != "combat" else "preparation"
        else:
            alternate = {"kind": "empty"} if value["kind"] == "unit" else {"kind": "unit", "display_name": "合成其他棋子", "catalog_ref": None}
        values = [value, alternate]
        item.update(reason="synthetic_disagreement", candidates=[{
            key: deepcopy(old[key]) for key in ("source_frame_id", "observed_host_monotonic_ms", "completed_host_monotonic_ms",
                                               "expires_host_monotonic_ms", "method", "region")
        } for _ in values])
        for candidate, value in zip(item["candidates"], values):
            candidate["value"] = value
        item["expires_host_monotonic_ms"] = None
    else:
        raise ValueError("Fixture status must be unknown, stale or conflict.")


def demo_inputs() -> tuple[list[dict], list[dict], dict]:
    """Seven fictional frames partition gold into C/E/U/stale/conflict/V/missing."""
    annotations = [sample(number) for number in range(1, 8)]
    predictions = [binding(record) for record in annotations[:6]]
    predictions[1]["state"]["gold"]["value"] = 49
    for index, status in ((2, "unknown"), (3, "stale"), (4, "conflict")):
        set_status(predictions[index]["state"], "gold", status)
    annotations[5]["labels"]["gold"] = label(reason="synthetic_unreadable")
    metadata = {key: "synthetic-fixture-v1" for key in (
        "annotation_revision", "split_revision", "dedupe_revision", "review_evidence_ref",
        "recognizer_revision", "model_revision", "config_revision",
    )}
    return annotations, predictions, metadata
