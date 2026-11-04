"""Fail-closed adapters for one caller-declared offline frame.

This module has no image, OCR, browser, device, or game-data dependency.  It
only translates the small published shapes of the vision, partial-planner and
player-coach contracts.  A caller retains media privately and supplies a
separate, hash-bound address for every frame.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from typing import Any


PARTIAL_FORMAT = "jcc-partial-trait-hint-request/v1"
COACH_SCHEMA = "human-coach-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
_PTS = re.compile(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,12})?\Z")
_MAX_PTS_SECONDS = Decimal("10000000")
# 25B's frozen legacy result serializes seconds at six decimal places.  The
# outer integer-tick comparison therefore allows at most one microsecond,
# enough for serialization (and the observed tick-to-decimal difference), not
# enough to identify a frame.  Source records produced by the *same* 25B
# result must agree exactly after Decimal(str(float)).
_MAX_MANIFEST_PTS_SERIALIZATION_DELTA = Decimal("0.000001")
_PLANNER_PTS = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,9})?\Z")


class PipelineContractError(ValueError):
    """A stable, non-sensitive reason for a rejected adapter input."""


def _is_int(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _id(value: object) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _text(value: object, maximum: int = 64) -> bool:
    return (type(value) is str and 0 < len(value) <= maximum and value == value.strip()
            and not any(ord(char) < 32 for char in value))


def _sha(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _require_exact(value: object, fields: set[str], code: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise PipelineContractError(code)
    return value


def _raw_pts(value: object) -> Decimal:
    if type(value) is not str or _PTS.fullmatch(value) is None:
        raise PipelineContractError("raw_pts_invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise PipelineContractError("raw_pts_invalid") from exc
    if parsed > _MAX_PTS_SECONDS:
        raise PipelineContractError("raw_pts_out_of_range")
    return parsed


def _finite_pts(value: object) -> Decimal:
    if type(value) is int:
        if value < 0 or value > int(_MAX_PTS_SECONDS):
            raise PipelineContractError("vision_pts_invalid")
        return Decimal(value)
    if type(value) is not float or not math.isfinite(value) or value < 0 or value > float(_MAX_PTS_SECONDS):
        raise PipelineContractError("vision_pts_invalid")
    return Decimal(str(value))


def _pts_from_manifest(value: object) -> tuple[dict[str, Any], str | None]:
    """Keep an externally supplied lossless PTS representation intact.

    The adapter accepts either integer ticks plus a time base, or a caller's
    raw seconds string.  It never reconstructs either form from a float.
    A seconds string that already fits 25D's narrow public contract can be
    relayed there. Integer ticks are *not* decimal-rounded merely to satisfy
    that contract: their 25D PTS remains unknown while the original object
    stays in the private binding digest.
    """
    if type(value) is not dict or type(value.get("kind")) is not str:
        raise PipelineContractError("manifest_pts_shape")
    if value["kind"] == "seconds_text":
        pts = _require_exact(value, {"kind", "value"}, "manifest_pts_shape")
        parsed = _raw_pts(pts["value"])
        raw_seconds = format(parsed, "f")
        return pts, raw_seconds if _PLANNER_PTS.fullmatch(raw_seconds) is not None else None
    if value["kind"] == "integer_timebase":
        pts = _require_exact(value, {"kind", "ticks", "time_base_num", "time_base_den"}, "manifest_pts_shape")
        if (not _is_int(pts["ticks"], 0, 2**63 - 1) or not _is_int(pts["time_base_num"], 1, 2**31 - 1)
                or not _is_int(pts["time_base_den"], 1, 2**31 - 1)):
            raise PipelineContractError("manifest_pts_invalid")
        if Decimal(pts["ticks"]) * Decimal(pts["time_base_num"]) > _MAX_PTS_SECONDS * Decimal(pts["time_base_den"]):
            raise PipelineContractError("raw_pts_out_of_range")
        return pts, None
    raise PipelineContractError("manifest_pts_kind")


def _parse_manifest(value: object, source_kind: object) -> dict[str, Any]:
    manifest = _require_exact(value, {
        "case_id", "frame_id", "source_video_sha256", "encoded_sha256", "decode_sequence", "pts",
    }, "frame_manifest_shape")
    if type(source_kind) is not str or source_kind not in {"replay", "synthetic"}:
        raise PipelineContractError("source_kind_invalid")
    if (not _id(manifest["case_id"]) or not _id(manifest["frame_id"])
            or not _sha(manifest["source_video_sha256"]) or not _sha(manifest["encoded_sha256"])
            or not _is_int(manifest["decode_sequence"], 1, 2**63 - 1)):
        raise PipelineContractError("frame_manifest_invalid")
    pts, pts_seconds = _pts_from_manifest(manifest["pts"])
    return {**manifest, "pts": pts, "pts_seconds": pts_seconds, "source_kind": source_kind}


def _source_matches(source: object, manifest: dict[str, Any]) -> bool:
    if type(source) is not dict:
        return False
    try:
        return (
            source.get("case_id") == manifest["case_id"]
            and source.get("frame_id") == manifest["frame_id"]
            and source.get("source_video_sha256") == manifest["source_video_sha256"]
            and source.get("encoded_sha256") == manifest["encoded_sha256"]
            and _finite_pts(source.get("decoded_pts_s")) >= 0
        )
    except PipelineContractError:
        return False


def _manifest_pts_diagnostic(manifest: dict[str, Any]) -> Decimal:
    """Return a comparison-only value, never an asserted recovered PTS."""
    pts = manifest["pts"]
    if pts["kind"] == "seconds_text":
        return _raw_pts(pts["value"])
    return Decimal(pts["ticks"]) * Decimal(pts["time_base_num"]) / Decimal(pts["time_base_den"])


def _same_visual_context(source: object, manifest: dict[str, Any], root_pts: Decimal | None = None) -> bool:
    try:
        value = _finite_pts(source["decoded_pts_s"])
        if root_pts is not None:
            return value == root_pts
        # This only rejects an impossible tick/float serialization pair.  It
        # neither turns a float into exact provenance nor rounds it into PTS.
        return abs(value - _manifest_pts_diagnostic(manifest)) <= _MAX_MANIFEST_PTS_SERIALIZATION_DELTA
    except (KeyError, PipelineContractError, InvalidOperation, OverflowError):
        return False


def _vision_root(vision: object, address: dict[str, Any]) -> dict[str, Any]:
    root = _require_exact(vision, {
        "schema_version", "implementation_revision", "source_frame", "population", "trait_rows",
        "accuracy", "automatic_action", "status", "reason_codes", "processing_seconds",
    }, "vision_shape")
    if (root["schema_version"] != "trait-vision-v1" or root.get("status") != "development_candidate"
            or not _source_matches(root["source_frame"], address)
            or not _same_visual_context(root["source_frame"], address)
            or root["source_frame"].get("context_authority") != "caller_declared_unverified"
            or type(root["trait_rows"]) is not list or len(root["trait_rows"]) > 12):
        raise PipelineContractError("vision_source_binding_invalid")
    return root


def _visible_capacity(population: object, address: dict[str, Any], root_pts: Decimal) -> dict[str, Any]:
    if (type(population) is not dict or not _source_matches(population.get("source_frame"), address)
            or not _same_visual_context(population.get("source_frame"), address, root_pts)):
        raise PipelineContractError("vision_population_binding_invalid")
    status = population.get("status")
    deployed, capacity = population.get("displayed_deployed"), population.get("capacity")
    if type(status) is not str:
        raise PipelineContractError("vision_population_status_invalid")
    if status == "unknown":
        if deployed is not None or capacity is not None:
            raise PipelineContractError("vision_population_unknown_residue")
        return {"status": "unknown", "current": None, "maximum": None}
    if status != "development_candidate":
        raise PipelineContractError("vision_population_status_invalid")
    empty_slots = population.get("empty_slots")
    if (not _is_int(deployed, 0, 10) or not _is_int(capacity, 1, 10) or deployed > capacity
            or (empty_slots is not None and empty_slots != capacity - deployed)):
        raise PipelineContractError("vision_population_candidate_invalid")
    return {"status": "known", "current": deployed, "maximum": capacity}


def _visible_traits(rows: object, address: dict[str, Any], root_pts: Decimal) -> list[dict[str, Any]]:
    if type(rows) is not list:
        raise PipelineContractError("vision_trait_rows_invalid")
    traits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if (type(row) is not dict or not _source_matches(row.get("source_frame"), address)
                or not _same_visual_context(row.get("source_frame"), address, root_pts)):
            raise PipelineContractError("vision_trait_binding_invalid")
        status, label_status, ratio_status = row.get("status"), row.get("label_status"), row.get("ratio_status")
        if (type(status) is not str or type(label_status) is not str or type(ratio_status) is not str
                or status not in {"development_candidate", "unknown"}
                or label_status not in {"development_candidate", "unknown"}
                or ratio_status not in {"development_candidate", "unknown"}):
            raise PipelineContractError("vision_trait_status_invalid")
        row_id, label = row.get("row_id"), row.get("display_label")
        observed, thresholds = row.get("observed_count"), row.get("displayed_thresholds")
        if status == "unknown":
            # A partial label is diagnostic only and must not create a trait gap.
            if ratio_status == "unknown" and (observed is not None or thresholds is not None):
                raise PipelineContractError("vision_trait_unknown_residue")
            continue
        if label_status != "development_candidate" or ratio_status != "development_candidate":
            raise PipelineContractError("vision_trait_status_conflict")
        if (not _id(row_id) or len("display-row-" + row_id) > 64 or not _text(label, 32) or not _is_int(observed, 0, 10)
                or type(thresholds) is not list or not thresholds
                or not all(_is_int(item, 1, 10) for item in thresholds)
                or observed > thresholds[0]
                or any(left >= right for left, right in zip(thresholds, thresholds[1:]))):
            raise PipelineContractError("vision_trait_candidate_invalid")
        trait_id = "display-row-" + row_id
        if trait_id in seen:
            raise PipelineContractError("vision_trait_duplicate_row")
        seen.add(trait_id)
        traits.append({
            "trait_id": trait_id, "display_name": label, "count": observed,
            "next_threshold": next((item for item in thresholds if item > observed), None),
        })
    return traits


def build_partial_trait_request(vision_result: object, *, source_kind: str, frame_manifest: object) -> dict[str, Any]:
    """Prepare a manifest-bound 25D request from one caller-declared vision result.

    The returned envelope retains both source-video and encoded-frame hashes.
    A display-row key is not a game catalog identity. ``source_kind`` stays a
    caller declaration and is explicitly marked unauthenticated.
    """
    address = _parse_manifest(frame_manifest, source_kind)
    root = _vision_root(vision_result, address)
    root_pts = _finite_pts(root["source_frame"]["decoded_pts_s"])
    partial = {
        "format_version": PARTIAL_FORMAT,
        "observation": {
            "source_kind": source_kind, "evidence_status": "machine_candidate",
            "case_id": address["case_id"], "frame_ref": address["frame_id"],
            "source_sha256": address["source_video_sha256"],
            "pts_seconds": ({"status": "known", "value": address["pts_seconds"]}
                            if address["pts_seconds"] is not None else {"status": "unknown", "value": None}),
            "freshness": "stale", "same_frame": True,
        },
        "rules_context": {"status": "unknown", "binding": None, "refs": []},
        "phase": {"status": "unknown", "value": None},
        "field_capacity": _visible_capacity(root["population"], address, root_pts),
        "gold": {"status": "unknown", "value": None},
        "active_traits": _visible_traits(root["trait_rows"], address, root_pts),
        "trait_observation_complete": False, "candidate": {"status": "unknown"},
    }
    partial_digest = _partial_digest(partial)
    return {
        "partial_request": partial,
        "replay_binding": {
            **address, "source_kind_declared": source_kind,
            "pts_evidence": "external_manifest_caller_declared",
            "provenance_authenticated": False, "vision_accuracy": None,
            "strategy_verified": False, "execution_eligible": False,
            "partial_request_sha256": partial_digest,
        },
    }


def _partial_digest(partial: object) -> str:
    try:
        canonical = json.dumps(partial, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise PipelineContractError("partial_request_invalid") from exc
    return hashlib.sha256(canonical).hexdigest()


def _binding_digest(envelope: dict[str, Any]) -> str:
    binding = envelope["replay_binding"]
    canonical = json.dumps({key: binding[key] for key in (
        "case_id", "frame_id", "source_video_sha256", "encoded_sha256", "decode_sequence", "pts", "source_kind",
        "partial_request_sha256",
    )}, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _expected_gaps(partial: dict[str, Any]) -> list[dict[str, Any]]:
    """Revalidate untrusted envelope content at the 25E consumption edge.

    The digest is an integrity link in a normal pipeline, not a secret.  A
    caller can recompute it, so this function must never assume it makes a
    structurally malformed partial request safe to consume.
    """
    rows = partial.get("active_traits") if type(partial) is dict else None
    if type(rows) is not list or len(rows) > 12:
        raise PipelineContractError("partial_active_traits_invalid")
    gaps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if (type(row) is not dict or set(row) != {"trait_id", "display_name", "count", "next_threshold"}
                or not _id(row.get("trait_id")) or len(row["trait_id"]) > 64
                or not _text(row.get("display_name"), 32) or not _is_int(row.get("count"), 0, 10)):
            raise PipelineContractError("partial_active_traits_invalid")
        trait_id, count, next_threshold = row["trait_id"], row["count"], row["next_threshold"]
        if trait_id in seen or (next_threshold is not None and (not _is_int(next_threshold, 1, 10) or next_threshold <= count)):
            raise PipelineContractError("partial_active_traits_invalid")
        seen.add(trait_id)
        gaps.append({
            "trait_id": trait_id, "display_name": row["display_name"],
            "current_count": count, "next_threshold": next_threshold,
            "needed_for_next_threshold": None if next_threshold is None else next_threshold - count,
        })
    return gaps


def _facts_zh(result: dict[str, Any]) -> str:
    capacity = result["field_capacity"]
    segments: list[str] = []
    if capacity["status"] == "known":
        segments.append(f"场上 {capacity['current']}/{capacity['maximum']}")
    for row in result["observed_trait_gaps"]:
        if row["next_threshold"] is not None:
            segments.append(f"{row['display_name']} {row['current_count']}/{row['next_threshold']}，距下一档 {row['needed_for_next_threshold']}")
        else:
            segments.append(f"{row['display_name']} {row['current_count']}")
    return "；".join(segments) if segments else "未取得可展示的局面字段"


def _event_input_valid(stream_id: object, sequence: object,
                       host_received_ms: object, emitted_ms: object, expires_ms: object) -> bool:
    return (
        _id(stream_id) and _is_int(sequence, 1, 2**63 - 1)
        and _is_int(host_received_ms, 0, 2**63 - 1) and _is_int(emitted_ms, 0, 2**63 - 1)
        and _is_int(expires_ms, 0, 2**63 - 1)
        and host_received_ms <= emitted_ms < expires_ms <= emitted_ms + 10_000
    )


def _coach_base(envelope: object, *, stream_id: str, sequence: int,
                host_received_ms: int, emitted_ms: int, expires_ms: int) -> tuple[dict[str, Any], dict[str, Any], str]:
    if type(envelope) is not dict or set(envelope) != {"partial_request", "replay_binding"}:
        raise PipelineContractError("pipeline_envelope_invalid")
    partial, binding = envelope["partial_request"], envelope["replay_binding"]
    if type(partial) is not dict or type(binding) is not dict or not _event_input_valid(
            stream_id, sequence, host_received_ms, emitted_ms, expires_ms):
        raise PipelineContractError("coach_event_input_invalid")
    observation = partial.get("observation")
    expected_binding_keys = {
        "case_id", "frame_id", "source_video_sha256", "encoded_sha256", "decode_sequence", "pts", "pts_seconds",
        "source_kind", "source_kind_declared", "pts_evidence", "provenance_authenticated", "vision_accuracy",
        "strategy_verified", "execution_eligible", "partial_request_sha256",
    }
    if (type(observation) is not dict or set(binding) != expected_binding_keys
            or observation.get("case_id") != binding["case_id"]
            or observation.get("frame_ref") != binding["frame_id"]
            or observation.get("source_sha256") != binding["source_video_sha256"]
            or observation.get("pts_seconds") != ({"status": "known", "value": binding["pts_seconds"]}
                                                  if binding["pts_seconds"] is not None else {"status": "unknown", "value": None})
            or observation.get("source_kind") != binding["source_kind"]
            or binding["source_kind_declared"] != binding["source_kind"]
            or binding["pts_evidence"] != "external_manifest_caller_declared"
            or binding["provenance_authenticated"] is not False
            or binding["vision_accuracy"] is not None
            or binding["strategy_verified"] is not False
            or binding["execution_eligible"] is not False
            or type(binding["source_kind"]) is not str or binding["source_kind"] not in {"replay", "synthetic"}
            or not _sha(binding["encoded_sha256"]) or not _sha(binding["source_video_sha256"])
            or not _id(binding["case_id"]) or not _id(binding["frame_id"])
            or not _is_int(binding["decode_sequence"], 1, 2**63 - 1)
            or not _sha(binding["partial_request_sha256"])):
        raise PipelineContractError("pipeline_binding_invalid")
    try:
        if _pts_from_manifest(binding["pts"])[1] != binding["pts_seconds"]:
            raise PipelineContractError("pipeline_binding_invalid")
        if _partial_digest(partial) != binding["partial_request_sha256"]:
            raise PipelineContractError("pipeline_binding_invalid")
    except PipelineContractError:
        raise PipelineContractError("pipeline_binding_invalid") from None
    digest = _binding_digest(envelope)
    return partial, binding, digest


def run_partial_planner(envelope: object) -> dict[str, Any]:
    """Call 25D in the same function that owns the full frame binding.

    The planner's public input cannot carry an encoded-frame digest.  This
    receipt therefore records the digest of the complete private envelope,
    preventing an A-frame result from being supplied as a B-frame result in a
    normal pipeline call. It is still caller-controlled and never authenticates
    image provenance.
    """
    try:
        # Reuse the event-side binding validation before giving untrusted
        # envelope data to 25D.  Timing is irrelevant here but still bounded.
        _coach_base(envelope, stream_id="pipeline-guard", sequence=1,
                    host_received_ms=0, emitted_ms=1, expires_ms=2)
        from jcc_team_planner.core import plan_partial_trait_hint
        result = plan_partial_trait_hint(envelope["partial_request"])
        digest = _binding_digest(envelope)
    except PipelineContractError:
        raise
    except Exception as exc:
        raise PipelineContractError("partial_planner_failed") from exc
    return {"binding_digest": digest, "partial_result": result}


def build_coach_observation_event(envelope: object, planner_receipt: object, *, stream_id: str, sequence: int,
                                  host_received_ms: int, emitted_ms: int,
                                  expires_ms: int) -> dict[str, Any]:
    """Make a 25E observation card only for a matching 25D result."""
    partial, binding, digest = _coach_base(envelope, stream_id=stream_id, sequence=sequence,
                                           host_received_ms=host_received_ms,
                                           emitted_ms=emitted_ms, expires_ms=expires_ms)
    if (type(planner_receipt) is not dict or set(planner_receipt) != {"binding_digest", "partial_result"}
            or planner_receipt["binding_digest"] != digest or type(planner_receipt["partial_result"]) is not dict):
        raise PipelineContractError("planner_receipt_binding_mismatch")
    partial_result = planner_receipt["partial_result"]
    required_match = ("observation", "rules_context", "phase", "field_capacity", "gold", "trait_observation_complete")
    if (partial_result.get("status") != "evaluated_partial_observation"
            or partial_result.get("hint_state") != "observation_only"
            or partial_result.get("withdraw_previous_action") is not True
            or partial_result.get("candidate_projection") is not None
            or partial_result.get("execution_eligible") is not False
            or partial_result.get("provenance_authenticated") is not False
            or any(partial_result.get(key) != partial.get(key) for key in required_match)
            or partial_result.get("observed_trait_gaps") != _expected_gaps(partial)):
        raise PipelineContractError("partial_result_binding_mismatch")
    facts = _facts_zh(partial_result)
    if len("机器候选：" + facts) > 96:
        raise PipelineContractError("observation_text_too_long")
    source_label = "回放" if partial["observation"]["source_kind"] == "replay" else "合成"
    title = source_label + "机器候选 · " + facts.split("；", 1)[0]
    if len(title) > 48:
        title = source_label + "机器候选"
    event_kind = "replay_candidate" if partial["observation"]["source_kind"] == "replay" else "synthetic"
    return {"coach_event": {
        "schema_version": COACH_SCHEMA, "source_kind": event_kind, "stream_id": stream_id,
        "frame_id": partial["observation"]["frame_ref"], "sequence": sequence,
        "source_sequence": binding["decode_sequence"], "frame_received_ms": host_received_ms,
        "emitted_ms": emitted_ms, "expires_ms": expires_ms, "status": "candidate_visible",
        "reason": "candidate", "context_version": partial["observation"]["source_kind"] + "-bind-" + digest[:20],
        "evidence_id": partial["observation"]["source_kind"] + "-" + digest[:24], "evidence_status": "machine_candidate",
        "advice": None,
        "observation": {
            "observation_id": "observe-" + digest[:20], "title_zh": title,
            "facts_zh": "机器候选：" + facts,
            "unknown_zh": "本人、阶段、备战棋子和规则版本未确认；不提供建议。",
        },
    }, "replay_binding": {
            **binding, "withdraw_previous_action": True,
            "timing_note": "decoded PTS is offline addressing only, not a host receive time or player-visible latency.",
        },
    }
