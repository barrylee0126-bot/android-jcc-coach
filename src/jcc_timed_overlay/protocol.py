"""Strict anonymous event/report boundaries; no images or arbitrary text."""

from __future__ import annotations

import math
import re

EVENT_SCHEMA = "timed-overlay-event-v1"
VIEW_SCHEMA = "timed-overlay-view-v1"
SOURCE_KINDS = {"synthetic", "replay", "device_readonly"}
TERMINAL_STATUSES = {"no_result", "timeout", "unknown"}
SAMPLE_STATUSES = {"pending", "ready"} | TERMINAL_STATUSES
CONTROL_STATUSES = {"heartbeat", "invalidated", "disconnected"}
EVENT_KEYS = {
    "schema_version", "sample_id", "source_kind", "stream_id", "sequence",
    "clock_domain_id", "frame_received_monotonic_ns", "emitted_monotonic_ns",
    "expires_at_monotonic_ns", "status",
}
REPORT_KEYS = {
    "session_id", "client_id", "client_event_sequence", "revision", "render_token",
    "sample_id", "stage", "reason", "visibility", "browser_monotonic_ms",
    "since_fetch_start_ms", "dom_to_raf_ms",
}
WITHDRAW_REASONS = {
    "expired", "lease_elapsed", "transport_failure", "hidden", "pagehide",
    "clock_unreliable", "superseded", "server_withdrawal", "resume", "unsafe_view",
}


def anonymous_id(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", value) is not None


def integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= 2**63 - 1


def duration(value: object, maximum: float = 86_400_000) -> bool:
    return type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value)


def validate_event(raw: object) -> str | None:
    if type(raw) is not dict or set(raw) != EVENT_KEYS:
        return "event_shape"
    if raw["schema_version"] != EVENT_SCHEMA:
        return "event_schema"
    if not all(anonymous_id(raw[key]) for key in ("sample_id", "stream_id", "clock_domain_id")):
        return "anonymous_identifier"
    if type(raw["source_kind"]) is not str or raw["source_kind"] not in SOURCE_KINDS:
        return "source_kind"
    if type(raw["status"]) is not str or raw["status"] not in SAMPLE_STATUSES | CONTROL_STATUSES:
        return "status"
    if not integer(raw["sequence"], 1):
        return "sequence"
    keys = ("frame_received_monotonic_ns", "emitted_monotonic_ns", "expires_at_monotonic_ns")
    if not all(integer(raw[key], 1) for key in keys):
        return "time_type"
    frame, emitted, expires = (raw[key] for key in keys)
    if not frame <= emitted or not frame < expires or expires - frame > 30_000_000_000:
        return "time_order_or_window"
    # A timeout records an outcome; it may be emitted when the display lease
    # has already elapsed. Every other status must still be emitted in-window.
    if raw["status"] != "timeout" and emitted >= expires:
        return "time_order_or_window"
    return None


def validate_report(raw: object) -> str | None:
    if type(raw) is not dict or set(raw) != REPORT_KEYS:
        return "report_shape"
    if not all(anonymous_id(raw[key]) for key in ("session_id", "client_id", "render_token", "sample_id")):
        return "report_identifier"
    if not integer(raw["revision"]) or not integer(raw["client_event_sequence"], 1):
        return "report_sequence"
    if raw["stage"] not in ("dom_updated", "paint_opportunity", "withdrawn"):
        return "report_stage"
    if raw["visibility"] not in ("visible", "hidden"):
        return "report_visibility"
    if type(raw["reason"]) is not str or raw["reason"] not in WITHDRAW_REASONS | {"none"}:
        return "report_reason"
    if raw["stage"] != "withdrawn" and raw["reason"] != "none":
        return "report_reason"
    if raw["stage"] == "withdrawn" and raw["reason"] == "none":
        return "report_reason"
    if not duration(raw["browser_monotonic_ms"], 10**12) or not duration(raw["since_fetch_start_ms"]):
        return "report_time"
    if raw["dom_to_raf_ms"] is not None and not duration(raw["dom_to_raf_ms"]):
        return "report_time"
    if raw["stage"] == "paint_opportunity" and raw["dom_to_raf_ms"] is None:
        return "report_time"
    return None
