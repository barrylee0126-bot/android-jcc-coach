"""Single source / current sample, bounded session, fail-closed timing store.

Host timestamps are compared only under an explicit producer clock declaration.
Browser timestamps remain in the browser domain; a report is not proof of pixels.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math
import secrets
import threading
import time
from typing import Callable
from uuid import uuid4

from .journal import Journal
from .protocol import CONTROL_STATUSES, TERMINAL_STATUSES, VIEW_SCHEMA, integer, validate_event, validate_report

NS_MS = 1_000_000
WAITING = {"awaiting_result", "awaiting_paint_report"}


class TimedOverlayStore:
    def __init__(
        self, *, clock_domain_id: str, stream_id: str, source_kind: str = "synthetic",
        clock: Callable[[], int] = time.monotonic_ns, journal: Journal | None = None,
        source_timeout_ms: int = 1500, paint_timeout_ms: int = 1200,
        max_samples: int = 1000, max_events: int = 20_000,
        in_process_clock: bool = False,
    ) -> None:
        from .protocol import SOURCE_KINDS, anonymous_id
        if not anonymous_id(clock_domain_id) or not anonymous_id(stream_id) or source_kind not in SOURCE_KINDS:
            raise ValueError("invalid source declaration")
        if not 250 <= source_timeout_ms <= 10_000 or not 100 <= paint_timeout_ms <= 10_000:
            raise ValueError("invalid timing policy")
        self.session_id = uuid4().hex
        self.clock_domain_id, self.stream_id, self.source_kind = clock_domain_id, stream_id, source_kind
        self._in_process_clock = in_process_clock and source_kind == "synthetic"
        self._clock, self.journal = clock, journal if journal is not None else Journal()
        self.source_timeout_ns, self.paint_timeout_ns = source_timeout_ms * NS_MS, paint_timeout_ms * NS_MS
        self.max_samples, self.max_events = max_samples, max_events
        self.lock = threading.RLock()
        self._last_clock: int | None = None
        self._fault = False
        self._journal_fault = False
        self._closed = False
        self._event_count = 0
        self._sequence = 0
        self._last_frame = 0
        self._last_source: int | None = None
        self._current: str | None = None
        self._state, self._reason = "unknown", "waiting_for_event"
        self._revision = 0
        self._records: dict[str, dict] = {}
        # Track historical emissions without rewriting published result timing.
        self._sample_emissions: dict[str, int] = {}
        self._tokens: dict[str, tuple[str, int]] = {}
        self._reports_seen: set[tuple[str, int]] = set()
        self._clients: set[str] = set()
        self.counters: Counter = Counter()
        self.started_ns = self._now()
        if self.started_ns is None:
            raise ValueError("host clock unavailable")
        self._log("session_started", self.started_ns, source_kind=source_kind,
                  clock_basis=self.clock_basis, source_timeout_ms=source_timeout_ms,
                  paint_timeout_ms=paint_timeout_ms)

    @property
    def clock_basis(self) -> str:
        return "in_process_synthetic" if self._in_process_clock else "producer_declared_same_host_unverified"

    def _log(self, kind: str, now: int | None, **fields: object) -> None:
        try:
            self.journal.write({"schema_version": "timed-overlay-log-v1", "session_id": self.session_id,
                                "kind": kind, "host_monotonic_ns": now, **fields})
        except (OSError, ValueError):
            # A prototype without its denominator journal must stop displaying.
            self._journal_fault = True
            self._barrier("unknown", "journal_unavailable", now, log=False)

    def _now(self) -> int | None:
        try:
            value = self._clock()
        except Exception:
            value = None
        if self._fault or not integer(value, 1) or (self._last_clock is not None and value < self._last_clock):
            self._fault = True
            self._barrier("unknown", "host_clock_unreliable", self._last_clock)
            return None
        self._last_clock = value
        return value

    def now(self) -> int:
        with self.lock:
            value = self._now()
            if value is None:
                raise ValueError("host clock unavailable")
            return value

    def _outcome(self, record: dict, outcome: str, now: int | None, reason: str) -> None:
        if record["outcome"] not in WAITING:
            return
        record.update(outcome=outcome, outcome_at_monotonic_ns=now, outcome_reason=reason)
        self._log("sample_outcome", now, sample_id=record["sample_id"], source_kind=record["source_kind"],
                  outcome=outcome, reason=reason)

    def _barrier(self, state: str, reason: str, now: int | None, *, log: bool = True) -> None:
        previous = self._current
        if previous is not None:
            record = self._records[previous]
            if record["outcome"] in WAITING:
                if reason in {"expired", "paint_report_timeout"}:
                    outcome = "result_timeout" if record["outcome"] == "awaiting_result" else "display_timeout"
                else:
                    outcome = reason + "_before_" + ("result" if record["outcome"] == "awaiting_result" else "paint")
                if log:
                    self._outcome(record, outcome, now, reason)
                else:
                    record.update(outcome=outcome, outcome_reason=reason, outcome_at_monotonic_ns=now)
            record["retired_reason"], record["retired_at_monotonic_ns"] = reason, now
        if previous is not None or (self._state, self._reason) != (state, reason):
            self._revision += 1
            if log:
                self._log("withdrawal_barrier", now, sample_id=previous, reason=reason, revision=self._revision)
        self._current, self._state, self._reason = None, state, reason

    def _tick(self, now: int) -> None:
        if self._current is not None:
            record = self._records[self._current]
            if now >= record["expires_at_monotonic_ns"]:
                self._barrier("expired", "expired", now)
            elif record["outcome"] == "awaiting_paint_report" and now >= record["paint_due_ns"]:
                self._barrier("timeout", "paint_report_timeout", now)
        if self._last_source is not None and now - self._last_source >= self.source_timeout_ns:
            self._barrier("disconnected", "source_silence", now)

    def tick(self) -> None:
        with self.lock:
            now = self._now()
            if now is not None:
                self._tick(now)

    def reject_input(self, reason: str, *, withdraw: bool = True) -> bool:
        # Callers pass fixed reason codes, never a file, exception text, or raw row.
        # Only validated, in-order historical terminal metadata may skip withdrawal.
        with self.lock:
            now = self._now()
            self.counters["rejected_inputs"] += 1
            if withdraw:
                self._barrier("unknown", "invalid_input", now)
            self._log("input_rejected", now, reason=reason, withdrawal_required=withdraw)
            return False

    def source_fault(self, reason: str) -> None:
        with self.lock:
            now = self._now()
            self._barrier("disconnected", reason, now)
            self._log("source_fault", now, reason=reason)

    def ingest(self, raw: object) -> bool:
        with self.lock:
            now = self._now()
            if now is None or self._journal_fault or self._closed:
                return False
            self._tick(now)
            self._event_count += 1
            if self._event_count > self.max_events:
                self._closed = True
                return self.reject_input("session_event_limit")
            error = validate_event(raw)
            if error is not None:
                return self.reject_input(error)
            assert isinstance(raw, dict)
            if (raw["stream_id"], raw["clock_domain_id"], raw["source_kind"]) != (self.stream_id, self.clock_domain_id, self.source_kind):
                return self.reject_input("source_or_clock_binding")
            if raw["sequence"] <= self._sequence:
                return self.reject_input("late_or_duplicate_sequence")
            if self._sequence and raw["sequence"] != self._sequence + 1:
                # A missing event may be an invalidation; this protocol has no resync proof.
                self._closed = True
                return self.reject_input("sequence_gap_requires_new_session")
            self._sequence = raw["sequence"]
            frame, emitted, expires = (raw[k] for k in (
                "frame_received_monotonic_ns", "emitted_monotonic_ns", "expires_at_monotonic_ns"))
            if frame < self.started_ns or emitted > now:
                return self.reject_input("pre_session_or_future_time")
            if now - emitted >= self.source_timeout_ns:
                return self.reject_input("stale_emission")
            # Source leases are anchored to production time, never file read time.
            self._last_source = max(self._last_source or 0, emitted)
            status, sample_id = raw["status"], raw["sample_id"]
            if status in CONTROL_STATUSES:
                self.counters["accepted_events"] += 1
                self._log("source_event", now, event=raw)
                if status != "heartbeat":
                    # Inclusive frame barrier, even if that frame has no sample yet.
                    self._last_frame = max(self._last_frame, frame)
                    self._barrier(status, "source_" + status, now)
                return not self._journal_fault
            record = self._records.get(sample_id)
            if record is None:
                if len(self._records) >= self.max_samples:
                    self._closed = True
                    return self.reject_input("session_sample_limit")
                if frame <= self._last_frame:
                    # Valid next-sequence terminal metadata cannot display an
                    # old frame. Reject the uncountable/possibly renamed sample
                    # without letting its historical outcome withdraw a newer one.
                    return self.reject_input("nonadvancing_frame", withdraw=status not in TERMINAL_STATUSES)
                self._barrier("unknown", "superseded", now)
                self._last_frame = frame
                record = {
                    "sample_id": sample_id, "source_kind": raw["source_kind"],
                    "frame_received_monotonic_ns": frame, "expires_at_monotonic_ns": expires,
                    "first_ingested_monotonic_ns": now, "emitted_monotonic_ns": emitted,
                    "ready_ingested_monotonic_ns": None, "paint_due_ns": None,
                    "outcome": "awaiting_result", "outcome_reason": None, "outcome_at_monotonic_ns": None,
                    "retired_reason": None, "retired_at_monotonic_ns": None,
                    "first_dom_report_received_ns": None, "first_paint_report_received_ns": None,
                    "frame_to_report_received_ms": None, "browser_dom_to_raf_ms": None,
                    "frame_to_emitted_ms": None, "emitted_to_ingested_ms": None,
                    "ready_ingested_to_report_received_ms": None,
                    "render_token": None, "ready_revision": None,
                }
                self._records[sample_id], self._current = record, sample_id
            elif frame != record["frame_received_monotonic_ns"] or expires != record["expires_at_monotonic_ns"] or emitted < self._sample_emissions[sample_id]:
                return self.reject_input("sample_time_changed")
            elif status in TERMINAL_STATUSES and (self._current != sample_id or record["outcome"] != "awaiting_result"):
                # _tick may already have finalized a pending sample at expiry.
                # Later terminal observations are journal-only: keep its first
                # outcome, latencies, retirement, and any newer view unchanged.
                self.counters["accepted_events"] += 1
                self._log("source_event", now, event=raw, historical_only=True)
                if self._journal_fault:
                    return False
                self._sample_emissions[sample_id] = emitted
                return True
            elif self._current != sample_id or record["outcome"] != "awaiting_result":
                return self.reject_input("retired_or_completed_sample")
            elif status == "pending":
                return self.reject_input("duplicate_pending")
            self.counters["accepted_events"] += 1
            self._log("source_event", now, event=raw)
            if self._journal_fault:
                return False
            self._sample_emissions[sample_id] = emitted
            record["emitted_monotonic_ns"] = emitted
            record["frame_to_emitted_ms"] = (emitted - frame) / NS_MS
            record["emitted_to_ingested_ms"] = (now - emitted) / NS_MS
            self._revision += 1
            self._state, self._reason = status, "source_" + status
            if now >= expires:
                self._outcome(record, "result_timeout", now, "expired_input")
                self._barrier("expired", "expired_input", now)
            elif status == "ready":
                token = secrets.token_hex(12)
                record.update(outcome="awaiting_paint_report", ready_ingested_monotonic_ns=now,
                              paint_due_ns=min(expires, now + self.paint_timeout_ns),
                              render_token=token, ready_revision=self._revision)
                self._tokens[token] = (sample_id, self._revision)
            elif status != "pending":
                self._outcome(record, {"no_result": "no_result", "timeout": "result_timeout", "unknown": "unknown"}[status], now, "source_" + status)
                self._barrier(status, "source_" + status, now)
            return not self._journal_fault

    def report_browser(self, raw: object) -> bool:
        with self.lock:
            now = self._now()
            if now is not None:
                self._tick(now)
            error = validate_report(raw)
            if error is not None or now is None or self._journal_fault or self._closed:
                self.counters["rejected_browser_reports"] += 1
                self._log("browser_report_rejected", now, reason=error or "session_unavailable")
                return False
            assert isinstance(raw, dict)
            identity = (raw["client_id"], raw["client_event_sequence"])
            token_binding = self._tokens.get(raw["render_token"])
            if raw["session_id"] != self.session_id or token_binding != (raw["sample_id"], raw["revision"]):
                self.counters["rejected_browser_reports"] += 1
                self._log("browser_report_rejected", now, reason="report_binding")
                return False
            if identity in self._reports_seen:
                return False
            if len(self._reports_seen) >= self.max_events:
                self._closed = True
                self._barrier("unknown", "session_report_limit", now)
                return False
            self._reports_seen.add(identity)
            self._clients.add(raw["client_id"])
            record = self._records[raw["sample_id"]]
            timely = (self._current == raw["sample_id"] and self._state == "ready"
                      and record["outcome"] in {"awaiting_paint_report", "paint_opportunity_reported"}
                      and raw["visibility"] == "visible")
            self._log("browser_" + raw["stage"], now, **raw, timely=timely)
            self.counters["browser_" + raw["stage"] + "_reports"] += 1
            if not timely or raw["stage"] == "withdrawn":
                return True  # Historical observation only. Never revives a sample.
            if raw["stage"] == "dom_updated" and record["first_dom_report_received_ns"] is None:
                record["first_dom_report_received_ns"] = now
            if raw["stage"] == "paint_opportunity" and record["outcome"] == "awaiting_paint_report":
                record["first_paint_report_received_ns"] = now
                record["frame_to_report_received_ms"] = (now - record["frame_received_monotonic_ns"]) / NS_MS
                record["ready_ingested_to_report_received_ms"] = (now - record["ready_ingested_monotonic_ns"]) / NS_MS
                record["browser_dom_to_raf_ms"] = raw["dom_to_raf_ms"]
                self._outcome(record, "paint_opportunity_reported", now, "visible_double_raf_report")
            return True

    def view(self) -> dict:
        with self.lock:
            now = self._now()
            if now is not None:
                self._tick(now)
            if self._journal_fault:
                self._barrier("unknown", "journal_unavailable", now, log=False)
            record = self._records.get(self._current)
            remaining = max(0, (record["expires_at_monotonic_ns"] - now) / NS_MS) if record and now else 0
            source_remaining = max(0, (self.source_timeout_ns - (now - self._last_source)) / NS_MS) if now and self._last_source else 0
            ready = self._state == "ready" and record is not None
            return {
                "schema_version": VIEW_SCHEMA, "session_id": self.session_id, "revision": self._revision,
                "simulation": True, "execution_eligible": False, "source_kind": self.source_kind,
                "clock_basis": self.clock_basis, "status": self._state, "reason": self._reason,
                "sample_id": self._current, "render_token": record["render_token"] if ready else None,
                "remaining_ms": remaining, "lease_ms": min(1000, remaining, source_remaining) if ready else 0,
                "message": "模拟计时 · 仅测试显示" if ready else None,
            }

    def report(self) -> dict:
        with self.lock:
            self.tick()
            outcomes = Counter(record["outcome"] for record in self._records.values())
            samples = list(self._records.values())
            latencies = sorted(r["frame_to_report_received_ms"] for r in samples if r["frame_to_report_received_ms"] is not None)
            def percentile(fraction: float) -> float | None:
                return latencies[max(0, math.ceil(len(latencies) * fraction) - 1)] if latencies else None
            public = []
            for record in samples:
                row = deepcopy(record)
                row.pop("render_token")
                for key, value in row.items():
                    if key.endswith("_ns") and value is not None:
                        row[key] = str(value)  # Avoid JavaScript's unsafe nanosecond integers.
                public.append(row)
            return {
                "schema_version": "timed-overlay-report-v1", "session_id": self.session_id,
                "simulation": True, "source_kind": self.source_kind, "clock_basis": self.clock_basis,
                "opportunities": len(samples), "outcomes": dict(outcomes), "counters": dict(self.counters),
                "browser_clients": len(self._clients), "sample_count_with_report": len(latencies),
                "frame_to_report_received_ms": {"p50": percentile(.5), "p95": percentile(.95)},
                "latency_meaning": "host_frame_receipt_to_host_browser_report_receipt_includes_return_trip_not_physical_visibility",
                "samples": public, "journal_available": not self._journal_fault,
            }

    def close(self) -> None:
        with self.lock:
            now = self._now()
            self._barrier("disconnected", "server_stopped", now)
            self._closed = True
            self._log("session_ended", now, report=self.report())
            self.journal.close()
