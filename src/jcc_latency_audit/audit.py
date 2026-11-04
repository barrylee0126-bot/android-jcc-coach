"""Auditable timing summaries without silently dropping unsuccessful attempts."""

from collections import Counter
import json
import math
from pathlib import Path
import re
import statistics


ATTEMPT_SCHEMA = "latency-attempt-v1"
VISIBILITY_SCHEMA = "latency-visibility-attempt-v1"
SOURCE_KINDS = frozenset(("synthetic", "replay", "live"))
VISIBILITY_SOURCE_KINDS = SOURCE_KINDS | frozenset(("replay_candidate", "live_candidate"))
EXECUTION_STATES = frozenset(("none", "simulated", "attempted"))
TERMINAL_OUTCOMES = frozenset(("advice_displayed", "action_confirmed", "unknown", "timeout", "withdrawn", "failed"))
VISIBILITY_OUTCOMES = frozenset(("browser_raf_reported", "unknown", "occluded", "disconnected", "timeout", "expired", "superseded", "withdrawn", "failed"))
CASE_PARTITIONS = frozenset(("development", "holdout"))
TIMESTAMPS = (
    "observed_monotonic_ns",
    "capture_received_ns",
    "recognition_completed_ns",
    "decision_completed_ns",
    "action_sent_ns",
    "feedback_received_ns",
    "display_reported_ns",
)
OPTIONAL_TIMESTAMPS = frozenset(TIMESTAMPS[1:])
REQUIRED_KEYS = frozenset(("schema_version", "opportunity_id", "source_kind", "execution", "outcome", *TIMESTAMPS))
SEGMENTS = (
    ("observed_to_capture", "observed_monotonic_ns", "capture_received_ns"),
    ("capture_to_recognition", "capture_received_ns", "recognition_completed_ns"),
    ("recognition_to_decision", "recognition_completed_ns", "decision_completed_ns"),
    ("decision_to_action", "decision_completed_ns", "action_sent_ns"),
    ("action_to_feedback", "action_sent_ns", "feedback_received_ns"),
    ("decision_to_display", "decision_completed_ns", "display_reported_ns"),
    ("capture_to_display", "capture_received_ns", "display_reported_ns"),
)
VISIBILITY_HOST_TIMESTAMPS = (
    "host_frame_received_ns",
    "recognition_completed_ns",
    "decision_completed_ns",
    "overlay_ingested_ns",
    "browser_dom_report_received_ns",
    "browser_raf_report_received_ns",
    "browser_withdrawn_report_received_ns",
)
VISIBILITY_OPTIONAL_HOST_TIMESTAMPS = frozenset(VISIBILITY_HOST_TIMESTAMPS[1:])
VISIBILITY_REQUIRED_KEYS = frozenset((
    "schema_version", "opportunity_id", "source_kind", "source_stream_id", "source_sequence", "execution", "outcome", "host_clock_domain_id",
    "record_kind", "host_stream_started_ns",
    "case_id", "case_partition", "source_video_digest", "source_frame_digest", "source_pts_ns",
    "source_capture_clock_domain_id", "source_capture_ns", "replay_input_read_started_host_ns", *VISIBILITY_HOST_TIMESTAMPS,
    "presentation_session_id", "presentation_revision", "render_token", "target_client_id",
    "dom_report_session_id", "dom_report_client_id", "dom_report_client_event_sequence", "dom_report_revision", "dom_report_render_token",
    "dom_report_opportunity_id", "dom_report_stage", "dom_report_visibility", "dom_report_browser_monotonic_ms",
    "raf_report_session_id", "raf_report_client_id", "raf_report_client_event_sequence", "raf_report_revision", "raf_report_render_token",
    "raf_report_opportunity_id", "raf_report_stage", "raf_report_visibility", "raf_report_browser_monotonic_ms",
    "withdrawn_report_session_id", "withdrawn_report_client_id", "withdrawn_report_client_event_sequence", "withdrawn_report_revision", "withdrawn_report_render_token",
    "withdrawn_report_opportunity_id", "withdrawn_report_stage", "withdrawn_report_visibility", "withdrawn_report_browser_monotonic_ms",
    "browser_dom_to_raf_ms", "physical_visible_clock_domain_id", "physical_visible_ns",
    "user_acknowledged_clock_domain_id", "user_acknowledged_ns",
))
OPAQUE_CLOCK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
VISIBILITY_RECORD_KINDS = frozenset(("frame_opportunity", "stream_start_failure"))


def _valid_ns(value):
    return type(value) is int and 0 <= value <= 9_223_372_036_854_775_807


def _clock_id(value):
    return type(value) is str and OPAQUE_CLOCK_ID.fullmatch(value) is not None


def _optional_clock_pair(raw, domain_key, value_key):
    domain, value = raw[domain_key], raw[value_key]
    if (domain is None) != (value is None):
        raise ValueError("clock_pair_" + value_key)
    if domain is not None and (not _clock_id(domain) or not _valid_ns(value)):
        raise ValueError("clock_pair_" + value_key)


def _valid_browser_ms(value, *, maximum=None):
    return (type(value) in (int, float) and not isinstance(value, bool)
            and (type(value) is not float or math.isfinite(value))
            and 0 <= value and (maximum is None or value <= maximum))


def _report_keys(prefix):
    return (
        prefix + "_report_session_id", prefix + "_report_client_id",
        prefix + "_report_client_event_sequence", prefix + "_report_revision",
        prefix + "_report_render_token", prefix + "_report_opportunity_id",
        prefix + "_report_stage", prefix + "_report_visibility",
        prefix + "_report_browser_monotonic_ms",
    )


def _report_present(raw, prefix):
    return any(raw[key] is not None for key in _report_keys(prefix))


def _validate_browser_report(raw, prefix, *, required_stage, allow_hidden):
    """Validate one independently received 25E browser POST.

    This only compares caller-supplied fields.  It deliberately does not
    authenticate a browser or infer physical display visibility.
    """
    keys = _report_keys(prefix)
    present = _report_present(raw, prefix)
    received = raw["browser_" + prefix + "_report_received_ns"]
    if (received is None) != (not present):
        raise ValueError(prefix + "_report_receipt_binding")
    if not present:
        return False
    if any(raw[key] is None for key in keys):
        raise ValueError(prefix + "_report_requires_complete_binding")
    expected = (raw["presentation_session_id"], raw["presentation_revision"],
                raw["render_token"], raw["target_client_id"], raw["opportunity_id"])
    reported = (raw[prefix + "_report_session_id"], raw[prefix + "_report_revision"],
                raw[prefix + "_report_render_token"], raw[prefix + "_report_client_id"],
                raw[prefix + "_report_opportunity_id"])
    if any(item is None for item in expected):
        raise ValueError("browser_report_requires_expected_binding")
    if expected != reported:
        raise ValueError(prefix + "_report_binding_mismatch")
    if raw[prefix + "_report_stage"] != required_stage:
        raise ValueError(prefix + "_report_stage")
    if raw[prefix + "_report_visibility"] not in ({"visible", "hidden"} if allow_hidden else {"visible"}):
        raise ValueError(prefix + "_report_visibility")
    if type(raw[prefix + "_report_client_event_sequence"]) is not int or raw[prefix + "_report_client_event_sequence"] < 1:
        raise ValueError(prefix + "_report_client_event_sequence")
    for key in (prefix + "_report_session_id", prefix + "_report_client_id", prefix + "_report_render_token", prefix + "_report_opportunity_id"):
        if not _clock_id(raw[key]):
            raise ValueError(prefix + "_report_binding")
    if type(raw[prefix + "_report_revision"]) is not int or raw[prefix + "_report_revision"] < 1:
        raise ValueError(prefix + "_report_binding")
    if not _valid_browser_ms(raw[prefix + "_report_browser_monotonic_ms"]):
        raise ValueError(prefix + "_report_browser_monotonic_ms")
    if raw["overlay_ingested_ns"] is None:
        raise ValueError("browser_report_requires_overlay_ingest")
    if received < raw["overlay_ingested_ns"]:
        raise ValueError("browser_report_before_overlay_ingest")
    return True


def _distribution(values):
    ordered = sorted(values)
    count = len(ordered)
    return {
        "n": count,
        "p50_ms": statistics.median(ordered) / 1e6 if count else None,
        "p95_ms": ordered[math.ceil(count * .95) - 1] / 1e6 if count >= 20 else None,
        "min_ms": ordered[0] / 1e6 if count else None,
        "max_ms": ordered[-1] / 1e6 if count else None,
    }


def validate_attempt(raw):
    """Return a normalized attempt or reject ambiguous timing input.

    A row is one *opportunity*, including ones that never produce an OCR
    candidate or an action.  Therefore an absent optional timestamp never
    removes the row from the terminal-outcome denominator.
    """
    if type(raw) is not dict or frozenset(raw) != REQUIRED_KEYS:
        raise ValueError("attempt_shape")
    if raw["schema_version"] != ATTEMPT_SCHEMA:
        raise ValueError("attempt_schema")
    if type(raw["opportunity_id"]) is not str or not raw["opportunity_id"].strip() or len(raw["opportunity_id"]) > 128:
        raise ValueError("opportunity_id")
    if type(raw["source_kind"]) is not str or raw["source_kind"] not in SOURCE_KINDS:
        raise ValueError("source_kind")
    if type(raw["execution"]) is not str or raw["execution"] not in EXECUTION_STATES:
        raise ValueError("execution")
    if type(raw["outcome"]) is not str or raw["outcome"] not in TERMINAL_OUTCOMES:
        raise ValueError("outcome")
    for key in TIMESTAMPS:
        value = raw[key]
        if key in OPTIONAL_TIMESTAMPS and value is None:
            continue
        if not _valid_ns(value):
            raise ValueError("timestamp_" + key)
    # Stage timestamps are intentionally sparse: an early live run may only
    # measure observed→action→feedback or observed→display.  Do not require
    # unmeasured intermediate stages.  When both endpoints of a dependency
    # exist, their order must still be honest.
    for key in OPTIONAL_TIMESTAMPS:
        if raw[key] is not None and raw[key] < raw["observed_monotonic_ns"]:
            raise ValueError("timestamp_before_observed_" + key)
    # Skip missing instrumentation but do not skip its already observed
    # predecessor: each branch remains ordered after nulls are removed.
    for name, chain in (
        ("display_branch", ("observed_monotonic_ns", "capture_received_ns", "recognition_completed_ns",
                            "decision_completed_ns", "display_reported_ns")),
        ("action_branch", ("observed_monotonic_ns", "capture_received_ns", "recognition_completed_ns",
                           "decision_completed_ns", "action_sent_ns", "feedback_received_ns")),
    ):
        recorded = [raw[key] for key in chain if raw[key] is not None]
        if any(later < earlier for earlier, later in zip(recorded, recorded[1:])):
            raise ValueError("timestamp_order_" + name)
    if raw["action_sent_ns"] is not None and raw["execution"] != "attempted":
        raise ValueError("action_requires_attempted_execution")
    if raw["feedback_received_ns"] is not None:
        if raw["execution"] != "attempted":
            raise ValueError("feedback_requires_attempted_execution")
    if raw["outcome"] == "advice_displayed" and raw["display_reported_ns"] is None:
        raise ValueError("displayed_outcome_requires_display_report")
    if raw["outcome"] == "action_confirmed":
        if raw["execution"] != "attempted" or raw["feedback_received_ns"] is None:
            raise ValueError("confirmed_action_requires_feedback")
    return dict(raw)


def validate_visibility_attempt(raw):
    """Validate a full-denominator host/render timing opportunity.

    Browser reports are received by the host after DOM/RAF execution.  Their
    host receipt timestamp is therefore a measurable bridge milestone, not a
    claim that the tablet's pixels were lit or a person saw the card.
    """
    if type(raw) is not dict or frozenset(raw) != VISIBILITY_REQUIRED_KEYS:
        raise ValueError("visibility_attempt_shape")
    if raw["schema_version"] != VISIBILITY_SCHEMA:
        raise ValueError("visibility_attempt_schema")
    if type(raw["opportunity_id"]) is not str or not raw["opportunity_id"].strip() or len(raw["opportunity_id"]) > 128:
        raise ValueError("opportunity_id")
    if type(raw["source_kind"]) is not str or raw["source_kind"] not in VISIBILITY_SOURCE_KINDS:
        raise ValueError("source_kind")
    if not _clock_id(raw["source_stream_id"]):
        raise ValueError("source_identity")
    if type(raw["execution"]) is not str or raw["execution"] not in EXECUTION_STATES:
        raise ValueError("execution")
    if type(raw["outcome"]) is not str or raw["outcome"] not in VISIBILITY_OUTCOMES:
        raise ValueError("visibility_outcome")
    if type(raw["record_kind"]) is not str or raw["record_kind"] not in VISIBILITY_RECORD_KINDS:
        raise ValueError("record_kind")
    if not _clock_id(raw["host_clock_domain_id"]):
        raise ValueError("host_clock_domain")
    if raw["host_stream_started_ns"] is not None and not _valid_ns(raw["host_stream_started_ns"]):
        raise ValueError("host_stream_started_ns")
    is_start_failure = raw["record_kind"] == "stream_start_failure"
    if is_start_failure:
        empty = ("source_sequence", "host_frame_received_ns", *VISIBILITY_OPTIONAL_HOST_TIMESTAMPS,
                 "case_id", "case_partition", "source_video_digest", "source_frame_digest", "source_pts_ns",
                 "source_capture_clock_domain_id", "source_capture_ns", "replay_input_read_started_host_ns",
                 "presentation_session_id", "presentation_revision", "render_token", "target_client_id",
                 *_report_keys("dom"), *_report_keys("raf"), *_report_keys("withdrawn"),
                 "browser_dom_to_raf_ms", "physical_visible_clock_domain_id", "physical_visible_ns",
                 "user_acknowledged_clock_domain_id", "user_acknowledged_ns")
        if (raw["outcome"] not in {"disconnected", "timeout", "failed"} or raw["execution"] != "none"
                or not _valid_ns(raw["host_stream_started_ns"]) or any(raw[key] is not None for key in empty)):
            raise ValueError("stream_start_failure")
        return dict(raw)
    if type(raw["source_sequence"]) is not int or raw["source_sequence"] < 1:
        raise ValueError("source_identity")
    for key in VISIBILITY_HOST_TIMESTAMPS:
        value = raw[key]
        if key in VISIBILITY_OPTIONAL_HOST_TIMESTAMPS and value is None:
            continue
        if not _valid_ns(value):
            raise ValueError("timestamp_" + key)
    if raw["host_stream_started_ns"] is not None and raw["host_stream_started_ns"] > raw["host_frame_received_ns"]:
        raise ValueError("host_stream_started_after_frame")
    case_fields = ("case_id", "case_partition", "source_video_digest", "source_frame_digest", "source_pts_ns")
    if raw["case_id"] is None:
        if any(raw[key] is not None for key in case_fields[1:]):
            raise ValueError("case_binding")
    elif (not _clock_id(raw["case_id"]) or type(raw["case_partition"]) is not str or raw["case_partition"] not in CASE_PARTITIONS
          or type(raw["source_video_digest"]) is not str or SHA256.fullmatch(raw["source_video_digest"]) is None
          or type(raw["source_frame_digest"]) is not str or SHA256.fullmatch(raw["source_frame_digest"]) is None
          or not _valid_ns(raw["source_pts_ns"])):
        raise ValueError("case_binding")
    replay = raw["source_kind"] in {"replay", "replay_candidate"}
    if replay and raw["case_id"] is None:
        raise ValueError("replay_requires_case_binding")
    if not replay and raw["case_id"] is not None:
        raise ValueError("nonreplay_forbids_case_binding")
    _optional_clock_pair(raw, "source_capture_clock_domain_id", "source_capture_ns")
    if replay and raw["source_capture_ns"] is not None:
        raise ValueError("replay_capture_clock_forbidden")
    if raw["replay_input_read_started_host_ns"] is not None:
        if not replay or not _valid_ns(raw["replay_input_read_started_host_ns"]) or raw["replay_input_read_started_host_ns"] > raw["host_frame_received_ns"]:
            raise ValueError("replay_input_read_started_host_ns")
    _optional_clock_pair(raw, "physical_visible_clock_domain_id", "physical_visible_ns")
    _optional_clock_pair(raw, "user_acknowledged_clock_domain_id", "user_acknowledged_ns")
    for key in ("presentation_session_id", "render_token", "target_client_id"):
        if raw[key] is not None and not _clock_id(raw[key]):
            raise ValueError("presentation_binding")
    if raw["presentation_revision"] is not None and (type(raw["presentation_revision"]) is not int or raw["presentation_revision"] < 1):
        raise ValueError("presentation_binding")
    expected_binding = (raw["presentation_session_id"], raw["presentation_revision"],
                        raw["render_token"], raw["target_client_id"])
    if any(item is None for item in expected_binding) and any(item is not None for item in expected_binding):
        raise ValueError("presentation_binding")
    for prefix in ("dom", "raf", "withdrawn"):
        for key in (prefix + "_report_session_id", prefix + "_report_client_id", prefix + "_report_render_token",
                    prefix + "_report_opportunity_id"):
            if raw[key] is not None and not _clock_id(raw[key]):
                raise ValueError(prefix + "_report_binding")
        for key in (prefix + "_report_revision", prefix + "_report_client_event_sequence"):
            if raw[key] is not None and (type(raw[key]) is not int or raw[key] < 1):
                raise ValueError(prefix + "_report_binding")
        if raw[prefix + "_report_stage"] is not None and type(raw[prefix + "_report_stage"]) is not str:
            raise ValueError(prefix + "_report_stage")
        if raw[prefix + "_report_visibility"] is not None and type(raw[prefix + "_report_visibility"]) is not str:
            raise ValueError(prefix + "_report_visibility")
        if raw[prefix + "_report_browser_monotonic_ms"] is not None and not _valid_browser_ms(raw[prefix + "_report_browser_monotonic_ms"]):
            raise ValueError(prefix + "_report_browser_monotonic_ms")
    recorded = [raw[key] for key in ("host_frame_received_ns", "recognition_completed_ns",
                                     "decision_completed_ns", "overlay_ingested_ns") if raw[key] is not None]
    if any(later < earlier for earlier, later in zip(recorded, recorded[1:])):
        raise ValueError("visibility_host_timestamp_order")
    dom_reported = _validate_browser_report(raw, "dom", required_stage="dom_updated", allow_hidden=True)
    raf_reported = _validate_browser_report(raw, "raf", required_stage="paint_opportunity", allow_hidden=False)
    _validate_browser_report(raw, "withdrawn", required_stage="withdrawn", allow_hidden=True)
    # DOM and RAF POST delivery can race at the host, but when both are from
    # this browser event stream their own sequence and local monotonic stamps
    # must still describe DOM before its paired paint opportunity.
    if dom_reported and raf_reported:
        if raw["dom_report_client_event_sequence"] >= raw["raf_report_client_event_sequence"]:
            raise ValueError("browser_local_dom_raf_sequence_order")
        if raw["dom_report_browser_monotonic_ms"] > raw["raf_report_browser_monotonic_ms"]:
            raise ValueError("browser_local_dom_raf_clock_order")
    browser_interval = raw["browser_dom_to_raf_ms"]
    if browser_interval is not None:
        if not raf_reported or not _valid_browser_ms(browser_interval, maximum=10_000):
            raise ValueError("browser_dom_to_raf_ms")
    if raf_reported and browser_interval is None:
        raise ValueError("raf_report_requires_browser_interval")
    host_frame = raw["host_frame_received_ns"]
    if raw["source_capture_clock_domain_id"] == raw["host_clock_domain_id"] and raw["source_capture_ns"] > host_frame:
        raise ValueError("same_clock_source_capture_after_host_frame")
    if raw["physical_visible_clock_domain_id"] == raw["host_clock_domain_id"] and raw["physical_visible_ns"] < host_frame:
        raise ValueError("same_clock_physical_visible_before_host_frame")
    if raw["user_acknowledged_clock_domain_id"] == raw["host_clock_domain_id"] and raw["user_acknowledged_ns"] < host_frame:
        raise ValueError("same_clock_user_acknowledged_before_host_frame")
    if raw["outcome"] == "browser_raf_reported":
        if not raf_reported:
            raise ValueError("raf_outcome_requires_matched_report")
        if raw["raf_report_visibility"] != "visible":
            raise ValueError("raf_outcome_requires_visible_document")
    return dict(raw)


def _group_report(rows):
    """Timing stats for one source/execution group only."""
    outcome_counts = Counter(row["outcome"] for row in rows)
    coverage = {}
    for key in TIMESTAMPS:
        observed = sum(row[key] is not None for row in rows)
        coverage[key] = {"observed": observed, "missing": len(rows) - observed,
                         "denominator": len(rows)}
    segments = {}
    for name, start, end in SEGMENTS:
        values = [row[end] - row[start] for row in rows
                  if row[start] is not None and row[end] is not None]
        segments[name] = _distribution(values)
        segments[name]["denominator"] = len(rows)
        segments[name]["unmeasured"] = len(rows) - len(values)
    return {
        "opportunities": len(rows),
        "outcomes": {outcome: outcome_counts[outcome] for outcome in sorted(TERMINAL_OUTCOMES)},
        "coverage": coverage,
        "segments": segments,
        "all_supplied_rows_accounted": bool(rows) and sum(outcome_counts.values()) == len(rows),
    }


def audit_attempts(attempts):
    """Summarize every supplied opportunity and only the observed segments.

    `p95_ms` is intentionally null under 20 observed values.  Segment timing
    samples are conditional on both endpoints being recorded; the coverage
    section always retains their full terminal denominator.
    """
    normalized = [validate_attempt(raw) for raw in attempts]
    ids = [row["opportunity_id"] for row in normalized]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate_opportunity_id")
    outcome_counts = Counter(row["outcome"] for row in normalized)
    source_counts = Counter(row["source_kind"] for row in normalized)
    execution_counts = Counter(row["execution"] for row in normalized)
    groups = {}
    for source in sorted(SOURCE_KINDS):
        for execution in sorted(EXECUTION_STATES):
            rows = [row for row in normalized
                    if row["source_kind"] == source and row["execution"] == execution]
            if rows:
                groups[source + "/" + execution] = _group_report(rows)
    return {
        "schema_version": "latency-audit-report-v1",
        "opportunities": len(normalized),
        "outcomes": {outcome: outcome_counts[outcome] for outcome in sorted(TERMINAL_OUTCOMES)},
        "source_kinds": {source: source_counts[source] for source in sorted(SOURCE_KINDS)},
        "execution": {state: execution_counts[state] for state in sorted(EXECUTION_STATES)},
        "groups": groups,
        "all_supplied_rows_accounted": bool(normalized) and sum(outcome_counts.values()) == len(normalized),
        "input_opportunity_completeness": "not_verified_by_audit",
        "evidence_boundary": {
            "scope": "host-monotonic attempt timestamps only",
            "not_measured": [
                "tablet_capture_to_host_receive_ms",
                "physical_display_visible_ms",
                "human_perception_or_response_ms",
                "recognition_accuracy",
                "gameplay_value_or_rank_effect",
            ],
            "interpretation": "Segment statistics are conditional local measurements; outcomes retain all opportunities.",
        },
    }


def _clock_segment(rows, name, start_key, end_key, *, start_clock_key=None, end_clock_key=None):
    values, cross_clock = [], 0
    for row in rows:
        start, end = row[start_key], row[end_key]
        if start is None or end is None:
            continue
        start_clock = row[start_clock_key] if start_clock_key else row["host_clock_domain_id"]
        end_clock = row[end_clock_key] if end_clock_key else row["host_clock_domain_id"]
        if start_clock != end_clock:
            cross_clock += 1
            continue
        values.append(end - start)
    summary = _distribution(values)
    summary.update(denominator=len(rows), unmeasured=len(rows) - len(values) - cross_clock,
                   cross_clock_not_compared=cross_clock)
    return name, summary


def _visibility_group_report(rows):
    outcomes = Counter(row["outcome"] for row in rows)
    coverage = {key: {"observed": sum(row[key] is not None for row in rows),
                      "missing": sum(row[key] is None for row in rows), "denominator": len(rows)}
                for key in VISIBILITY_HOST_TIMESTAMPS}
    frame_rows = [row for row in rows if row["record_kind"] == "frame_opportunity"]
    segments = dict((name, report) for name, report in (
        _clock_segment(frame_rows, "live_source_capture_to_host_frame", "source_capture_ns", "host_frame_received_ns",
                       start_clock_key="source_capture_clock_domain_id"),
        _clock_segment(frame_rows, "replay_input_read_to_host_frame", "replay_input_read_started_host_ns", "host_frame_received_ns"),
        _clock_segment(frame_rows, "host_frame_to_recognition", "host_frame_received_ns", "recognition_completed_ns"),
        _clock_segment(frame_rows, "recognition_to_decision", "recognition_completed_ns", "decision_completed_ns"),
        _clock_segment(frame_rows, "decision_to_overlay_ingest", "decision_completed_ns", "overlay_ingested_ns"),
        _clock_segment(frame_rows, "overlay_ingest_to_dom_report", "overlay_ingested_ns", "browser_dom_report_received_ns"),
        _clock_segment(frame_rows, "overlay_ingest_to_raf_report", "overlay_ingested_ns", "browser_raf_report_received_ns"),
        _clock_segment(frame_rows, "host_frame_to_raf_report", "host_frame_received_ns", "browser_raf_report_received_ns"),
        _clock_segment(frame_rows, "host_frame_to_physical_visible", "host_frame_received_ns", "physical_visible_ns",
                       end_clock_key="physical_visible_clock_domain_id"),
        _clock_segment(frame_rows, "host_frame_to_user_acknowledged", "host_frame_received_ns", "user_acknowledged_ns",
                       end_clock_key="user_acknowledged_clock_domain_id"),
    ))
    # The browser reports this local interval in milliseconds already; convert
    # only for the shared nanosecond distribution helper, then return ms.
    browser_values = [row["browser_dom_to_raf_ms"] * 1e6 for row in frame_rows
                      if row["browser_dom_to_raf_ms"] is not None and _report_present(row, "raf")]
    browser = _distribution(browser_values)
    browser.update(denominator=len(frame_rows), unmeasured=len(frame_rows) - len(browser_values),
                   clock_domain="browser_local_interval_only")
    return {
        "opportunities": len(rows),
        "record_kinds": dict(Counter(row["record_kind"] for row in rows)),
        "outcomes": {outcome: outcomes[outcome] for outcome in sorted(VISIBILITY_OUTCOMES)},
        "coverage": coverage,
        "host_clock_segments": segments,
        "browser_dom_to_raf": browser,
        "all_supplied_rows_accounted": bool(rows) and sum(outcomes.values()) == len(rows),
    }


def audit_visibility_attempts(attempts):
    """Audit future prompt visibility telemetry without cross-clock arithmetic."""
    normalized = [validate_visibility_attempt(row) for row in attempts]
    identifiers = [row["opportunity_id"] for row in normalized]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("duplicate_opportunity_id")
    source_identities = [(row["source_kind"], row["source_stream_id"], row["source_sequence"])
                         for row in normalized if row["record_kind"] == "frame_opportunity"]
    if len(set(source_identities)) != len(source_identities):
        raise ValueError("duplicate_source_identity")
    bindings, report_events, client_event_sequences, case_partitions, video_pts_partitions, frame_partitions = {}, {}, set(), {}, {}, {}
    audit_clients = set()
    for row in normalized:
        if row["record_kind"] == "frame_opportunity" and row["case_id"] is not None:
            for values, identity in (
                (case_partitions, row["case_id"]),
                (video_pts_partitions, (row["source_video_digest"], row["source_pts_ns"])),
                (frame_partitions, row["source_frame_digest"]),
            ):
                prior_partition = values.setdefault(identity, row["case_partition"])
                if prior_partition != row["case_partition"]:
                    raise ValueError("case_partition_overlap")
        if row["target_client_id"] is not None:
            audit_clients.add(row["target_client_id"])
        source = (row["source_kind"], row["source_stream_id"], row["source_sequence"])
        for prefix in ("dom", "raf", "withdrawn"):
            if not _report_present(row, prefix):
                continue
            binding = (row["presentation_session_id"], row["presentation_revision"], row["render_token"], row["target_client_id"])
            prior_source = bindings.setdefault(binding, source)
            if prior_source != source:
                raise ValueError("presentation_binding_conflict")
            event = (row[prefix + "_report_session_id"], row[prefix + "_report_client_id"],
                     row[prefix + "_report_client_event_sequence"], row[prefix + "_report_revision"],
                     row[prefix + "_report_render_token"], row[prefix + "_report_opportunity_id"],
                     row[prefix + "_report_stage"])
            prior_source = report_events.setdefault(event, source)
            if prior_source != source:
                raise ValueError("browser_report_event_conflict")
            client_event = (row[prefix + "_report_client_id"], row[prefix + "_report_client_event_sequence"])
            if client_event in client_event_sequences:
                raise ValueError("browser_client_event_sequence_conflict")
            client_event_sequences.add(client_event)
    if len(audit_clients) > 1:
        raise ValueError("multiple_target_clients_not_supported")
    outcomes = Counter(row["outcome"] for row in normalized)
    groups = {}
    for row in normalized:
        partition = row["case_partition"] or "no-case"
        key = "/".join((row["record_kind"], row["source_kind"], row["execution"], row["host_clock_domain_id"], partition))
        groups.setdefault(key, []).append(row)
    return {
        "schema_version": "latency-visibility-report-v1",
        "opportunities": len(normalized),
        "record_kinds": dict(Counter(row["record_kind"] for row in normalized)),
        "outcomes": {outcome: outcomes[outcome] for outcome in sorted(VISIBILITY_OUTCOMES)},
        "groups": {key: _visibility_group_report(rows) for key, rows in sorted(groups.items())},
        "all_supplied_rows_accounted": bool(normalized) and sum(outcomes.values()) == len(normalized),
        "input_opportunity_completeness": "not_verified_by_audit",
        "evidence_boundary": {
            "host_raf_report": "host receipt of a caller-declared, field-matched browser paint-opportunity report; not physical screen visibility",
            "browser_binding": "expected and reported fields are compared but not authenticated; this audit scopes one target client per input batch",
            "browser_visibility": "the browser visibility field is a document.hidden snapshot, not card or player visibility",
            "cross_clock": "timestamps from distinct clock domains are counted but never subtracted",
            "not_proven": ["tablet scene change to host receipt", "physical screen emission", "player actually saw prompt",
                            "recognition_accuracy", "decision_quality", "rank_effect"],
        },
    }


def read_attempts(path):
    """Read bounded JSONL timing rows without echoing private row contents."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 2_097_152:
        raise ValueError("attempts_file")
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise ValueError("blank_attempt_line")
        try:
            row = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError as error:
            raise ValueError("attempt_json_line_" + str(number)) from error
        rows.append(row)
        if len(rows) > 20_000:
            raise ValueError("attempt_count")
    return rows


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value
