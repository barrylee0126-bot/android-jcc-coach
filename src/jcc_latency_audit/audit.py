"""Auditable timing summaries without silently dropping unsuccessful attempts."""

from collections import Counter
import json
import math
from pathlib import Path
import statistics


ATTEMPT_SCHEMA = "latency-attempt-v1"
SOURCE_KINDS = frozenset(("synthetic", "replay", "live"))
EXECUTION_STATES = frozenset(("none", "simulated", "attempted"))
TERMINAL_OUTCOMES = frozenset(("advice_displayed", "action_confirmed", "unknown", "timeout", "withdrawn", "failed"))
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


def _valid_ns(value):
    return type(value) is int and 0 <= value <= 9_223_372_036_854_775_807


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
