"""Frame retirement and fixed leases between two distinct public protocols."""

from collections import Counter
from copy import deepcopy
import re
import threading
from uuid import uuid4

from jcc_realtime_probe.events import EVENT_SCHEMA as PRODUCER_SCHEMA, REASONS, STAGES, STATUSES
from jcc_realtime_probe.processing import CANDIDATE_STATUSES, ProcessingResult
from jcc_realtime_probe.pixels import FRAME_LIVENESS_NS, RESULT_TTL_NS
from jcc_timed_overlay.protocol import EVENT_SCHEMA
from jcc_timed_overlay.store import TimedOverlayStore

from .clocks import ClockBinding, MAX_NS, nanoseconds

UPSTREAM_SCHEMA = "realtime-probe-v2"
UPSTREAM_KEYS = {
    "schema_version", "event_index", "clock_domain_id", "clock_kind", "sample_id",
    "source_kind", "frame_received_monotonic_ns", "emitted_monotonic_ns",
    "expires_at_monotonic_ns", "stage", "status", "reason", "evidence",
    "processing_kind", "processing_outcome", "ocr_requests", "ocr_items_count", "ocr_errors",
}
SOURCE_MAP = {"synthetic": "synthetic", "replay": "replay", "live": "device_readonly"}
PROCESSING_KEYS = ("processing_kind", "processing_outcome", "ocr_requests", "ocr_items_count", "ocr_errors")
INCOMPLETE_CODES = {
    ("receive", "received", "frame_checked"), ("receive", "received", "check_unknown"),
    ("receive", "rejected", "check_timeout"),
    ("queue", "rejected", "pending_replaced"), ("queue", "rejected", "pending_discarded"),
    ("processing", "started", "processing_started"),
    ("field_gate", "no_output", "semantic_evidence_missing"),
}
PIXEL_REJECTIONS = {
    "frame_timeout", "disconnected", "pixels_or_continuity_revoked", "foreign_lease", "invalid_deadline",
    "clock_regressed_or_invalid", "stream_changed", "not_observed", "image_invalid_or_geometry_mismatch",
    "check_timeout", "sequence_gap", "invalid_frame_time_or_sequence", "duplicate_or_reordered",
    "receive_time_regressed",
}


def processing_error(raw, expected_kind):
    """Validate v2 provenance using the producer's public completion contract.

    Pixel rejection and processing outcome are independent: e.g. empty OCR on
    revoked pixels remains ocr_empty, not a successful OCR result or a failure.
    """
    if (type(raw["processing_kind"]) is not str or raw["processing_kind"] != expected_kind
            or type(raw["processing_outcome"]) is not str):
        return "processing_binding_or_outcome"
    codes = raw["stage"], raw["status"], raw["reason"]
    outcome = raw["processing_outcome"]
    if outcome in {"not_completed", "in_progress"}:
        expected = "in_progress" if codes == ("processing", "started", "processing_started") else "not_completed"
        if (codes not in INCOMPLETE_CODES or outcome != expected
                or any(raw[k] is not None for k in PROCESSING_KEYS[2:])):
            return "incomplete_processing_semantics"
        return None
    if any(raw[k] is not None and (type(raw[k]) is not int or not 0 <= raw[k] <= MAX_NS)
           for k in PROCESSING_KEYS[2:]):
        return "processing_count_type"
    try:
        result = ProcessingResult.from_report(raw)
    except (TypeError, ValueError):
        return "processing_result_semantics"
    stage, status, reason = codes
    if stage == "field_gate":
        return None if codes == ("field_gate", "rejected", "semantic_evidence_missing") else "field_gate_semantics"
    if stage not in {"pixel_full", "pixel_inputs"}:
        return "completed_processing_stage"
    if (status, reason) == result.completion():
        if status in CANDIDATE_STATUSES and raw["emitted_monotonic_ns"] >= raw["expires_at_monotonic_ns"]:
            return "candidate_after_upstream_deadline"
        return None
    if not result.failed:
        if status == "rejected" and reason in PIXEL_REJECTIONS:
            return None
        if status == "expired" and reason == "result_expired":
            return None
        if (status == "withdrawn" and outcome in {"simulated_ready", "ocr_text"}
                and reason in PIXEL_REJECTIONS | {"superseded"}):
            return None
    return "pixel_processing_semantics"


def json_safe(value):
    """Nanoseconds never enter a browser as a JSON numeric value."""
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: (str(v) if k.endswith("_ns") and type(v) is int else json_safe(v))
                for k, v in value.items()}
    return value


class RealtimeOverlayBridge:
    def __init__(self, binding, *, silence_ns=FRAME_LIVENESS_NS, journal=None):
        if PRODUCER_SCHEMA != UPSTREAM_SCHEMA:
            raise ValueError("unsupported_producer_schema")
        if type(binding) is not ClockBinding:
            raise ValueError("clock_proof_required")
        binding.validate()
        if type(silence_ns) is not int or not 250_000_000 <= silence_ns <= FRAME_LIVENESS_NS:
            raise ValueError("silence_may_not_exceed_upstream_policy")
        self.binding, self.clock, self.silence_ns = binding, binding.clock, silence_ns
        self.stream_id = "bridge-" + uuid4().hex
        self.store = TimedOverlayStore(
            clock_domain_id=binding.run_id, stream_id=self.stream_id,
            source_kind=SOURCE_MAP[binding.source_kind], clock=self.clock,
            source_timeout_ms=silence_ns // 1_000_000, journal=journal,
            in_process_clock=binding.assurance.startswith("same_process_") and binding.source_kind == "synthetic",
        )
        self.consumer_session = self.store.session_id
        self.lock = threading.RLock()
        self.started_ns = self.last_now = self.store.started_ns
        self.event_index = self.output_sequence = self.frame_sequence = self.last_emitted = 0
        self.last_received = 0
        self.current = None
        self.frames = {}
        self.counts = Counter()
        self.rejections = Counter()
        self.mapped_events = []
        self.output_audit = []
        self.upstream_audit = []
        self.processing_outcomes = Counter()
        self.ocr_totals = Counter(ocr_requests=0, ocr_items_count=0, ocr_errors=0)
        self.scope_events = {s: Counter() for s in ("pixel_full", "pixel_inputs")}
        self.faulted = None

    def _retire(self, reason):
        if self.current is not None:
            self.frames[self.current]["outcome"] = reason
            self.frames[self.current]["retired_at_ns"] = self.last_now
        self.current = None

    def fault(self, reason):
        with self.lock:
            if self.faulted is None:
                self.faulted = reason
                self._retire(reason)
                self.store.source_fault("bridge_" + reason)
            self.rejections[reason] += 1

    def _now(self):
        try:
            self.binding.validate()
            now = self.clock()
        except Exception:
            now = None
        if (not nanoseconds(now) or now < self.last_now
                or self.store.session_id != self.consumer_session):
            self.fault("clock_or_consumer_session_changed")
            return None
        self.last_now = now
        return now

    def tick(self):
        with self.lock:
            now = self._now()
            if now is None or self.faulted:
                return
            if self.current is not None and now >= self.frames[self.current]["deadline_ns"]:
                # A bridge observation, signed now in the verified shared clock;
                # never retime a source event or extend its original deadline.
                record = self.frames[self.current]
                self._send(record, "timeout", now, origin="bridge_deadline_observed")
                self._retire("deadline_elapsed")
            self.store.tick()
            if (self.current is not None and self.frames[self.current]["outcome"] == "ready"
                    and self.store.view()["status"] != "ready"):
                self._retire("consumer_withdrawal")

    def partial_line(self):
        with self.lock:
            self.counts["partial_line_barriers"] += 1
            self._retire("partial_line")
            self.store.source_fault("bridge_partial_jsonl")

    def _error(self, raw, now):
        if type(raw) is not dict or set(raw) != UPSTREAM_KEYS:
            return "upstream_shape"
        if raw["schema_version"] != UPSTREAM_SCHEMA or raw["evidence"] != "pixel_only_not_game_state":
            return "upstream_semantics"
        if any(type(raw[k]) is not str for k in ("stage", "status", "reason", "sample_id")):
            return "upstream_codes"
        if raw["stage"] not in STAGES or raw["status"] not in STATUSES or raw["reason"] not in REASONS:
            return "upstream_codes"
        if (raw["clock_domain_id"], raw["clock_kind"], raw["source_kind"]) != (
                self.binding.run_id, self.binding.clock_kind, self.binding.source_kind):
            return "source_or_clock_changed"
        if not nanoseconds(raw["event_index"]) or raw["event_index"] != self.event_index + 1:
            return "event_duplicate_reorder_or_gap"
        match = re.fullmatch(re.escape(self.binding.run_id) + r":([1-9][0-9]{0,18})", raw["sample_id"])
        if match is None or int(match[1]) > MAX_NS:
            return "sample_identity"
        frame, emitted, expires = (raw[k] for k in ("frame_received_monotonic_ns", "emitted_monotonic_ns",
                                                   "expires_at_monotonic_ns"))
        if not all(nanoseconds(v) for v in (frame, emitted, expires)):
            return "time_integer"
        if not self.started_ns <= frame <= emitted <= now or emitted < self.last_emitted:
            return "time_order_or_old_session"
        if expires != frame + RESULT_TTL_NS:
            return "upstream_deadline_changed"
        return processing_error(raw, self.binding.processing_kind)

    def _send(self, record, status, emitted, *, origin="upstream_event"):
        if self.output_sequence >= 20_000:
            self.fault("output_capacity")
            return False
        self.output_sequence += 1
        row = dict(schema_version=EVENT_SCHEMA, stream_id=self.stream_id, sequence=self.output_sequence,
                   clock_domain_id=self.binding.run_id, source_kind=SOURCE_MAP[self.binding.source_kind],
                   sample_id=record["sample_id"], frame_received_monotonic_ns=record["frame_received_monotonic_ns"],
                   emitted_monotonic_ns=emitted, expires_at_monotonic_ns=record["deadline_ns"], status=status)
        self.mapped_events.append(row)
        accepted = self.store.ingest(row)
        self.output_audit.append(dict(event=deepcopy(row), origin=origin,
                                      upstream_event_index=self.event_index if origin == "upstream_event" else None,
                                      processing=deepcopy(record["processing_result"]), accepted=accepted))
        if not accepted:
            self.fault("consumer_rejected")
            return False
        return True

    def _audit(self, raw, record):
        """One immutable audit row per valid event, one total per completed job.

        Both scopes and later field-gate/withdrawal rows repeat counts; they
        must never multiply the OCR-call or job denominator.
        """
        fields = {k: raw[k] for k in PROCESSING_KEYS}
        completed = raw["processing_outcome"] not in {"not_completed", "in_progress"}
        previous = record["processing_result"]
        if completed and previous is not None and previous != fields:
            self.fault("job_processing_result_changed")
            return False
        self.upstream_audit.append(deepcopy(raw))
        self.counts["validated_events"] += 1
        if raw["stage"] == "processing" and record["processing_started_ns"] is None:
            record["processing_started_ns"] = raw["emitted_monotonic_ns"]
            self.counts["processing_started_jobs_observed"] += 1
        if completed and previous is None:
            record["processing_result"] = fields
            record["completion_first_observed_ns"] = self.last_now
            record["completion_first_emitted_ns"] = raw["emitted_monotonic_ns"]
            self.counts["completed_jobs_observed"] += 1
            self.processing_outcomes[raw["processing_outcome"]] += 1
            for k in self.ocr_totals:
                self.ocr_totals[k] += fields[k] or 0
        if raw["stage"] in self.scope_events:
            scope = raw["stage"]
            self.scope_events[scope][raw["status"] + "/" + raw["reason"]] += 1
            record["scope_first_results"].setdefault(scope, dict(status=raw["status"], reason=raw["reason"]))
        return True

    def ingest(self, raw):
        with self.lock:
            is_receive = (type(raw) is dict and raw.get("stage") == "receive"
                          and raw.get("status") == "received")
            self.counts["input_events_seen"] += 1
            self.counts["receive_events_seen"] += int(is_receive)
            self.tick()
            now = self.last_now
            error = self.faulted or self._error(raw, now)
            if self.counts["input_events_seen"] > 20_000:
                error = "input_capacity"
            if error:
                self.counts["rejected_events"] += 1
                self.counts["rejected_receive_events"] += int(is_receive)
                self.fault(error)
                return False
            self.event_index = raw["event_index"]
            self.last_emitted = raw["emitted_monotonic_ns"]
            sample = raw["sample_id"]
            sequence = int(sample.rsplit(":", 1)[1])
            if is_receive:
                if (sequence != self.frame_sequence + 1 or raw["frame_received_monotonic_ns"] <= self.last_received
                        or len(self.frames) >= 1000):
                    self.counts["rejected_events"] += 1
                    self.counts["rejected_receive_events"] += 1
                    self.fault("frame_gap_duplicate_time_or_capacity")
                    return False
                self._retire("superseded")
                self.frame_sequence, self.last_received = sequence, raw["frame_received_monotonic_ns"]
                record = dict(sample_id=sample, frame_received_monotonic_ns=self.last_received,
                              original_expires_at_monotonic_ns=raw["expires_at_monotonic_ns"],
                              deadline_ns=min(raw["expires_at_monotonic_ns"], self.last_received + self.silence_ns),
                              received_by_bridge_ns=now, processing_started_ns=None, result_emitted_ns=None,
                              ready_at_ns=None, retired_at_ns=None, outcome="pending",
                              processing_kind=self.binding.processing_kind, processing_result=None,
                              completion_first_observed_ns=None, completion_first_emitted_ns=None,
                              scope_first_results={})
                self.frames[sample] = record
                self._audit(raw, record)
                self.store.source_fault("bridge_new_frame")
                self.counts["new_frame_barriers"] += 1
                if now >= record["deadline_ns"]:
                    record.update(outcome="expired_before_consumer", retired_at_ns=now)
                    self._send(record, "timeout", now, origin="bridge_expired_on_arrival")
                    return False
                # 14B invalidated is an inclusive frame watermark. Sending it
                # with this new frame would prohibit its following pending.
                # The public fault barrier retires the old sample without
                # manufacturing a timestamp or consuming the new frame.
                self.current = sample
                if not self._send(record, "pending", self.last_emitted):
                    return False
                if raw["reason"] == "check_unknown":
                    self._send(record, "unknown", self.last_emitted)
                    self._retire("check_unknown")
                return not self.faulted
            record = self.frames.get(sample)
            if record is None:
                self.fault("frame_not_received")
                return False
            if (raw["frame_received_monotonic_ns"] != record["frame_received_monotonic_ns"]
                    or raw["expires_at_monotonic_ns"] != record["original_expires_at_monotonic_ns"]):
                self.fault("sample_time_changed")
                return False
            if not self._audit(raw, record):
                return False
            stage, status = raw["stage"], raw["status"]
            if stage == "field_gate":
                self.counts["formal_gate_without_output"] += 1
                return True
            if stage == "receive" and status == "rejected":
                self.counts["receive_budget_rejections"] += 1
                record["receive_check_rejected"] = True
            if self.current != sample:
                self.counts["retired_frame_events_ignored"] += 1
                return False
            if stage == "pixel_inputs" or (stage in {"queue", "receive"} and status == "rejected"):
                if status in CANDIDATE_STATUSES and record["outcome"] == "pending":
                    record["result_emitted_ns"] = self.last_emitted
                    record["ready_from_status"] = status
                    if self._send(record, "ready", self.last_emitted):
                        view = self.store.view()
                        if view["status"] == "ready" and view["sample_id"] == sample:
                            record.update(outcome="ready", ready_at_ns=now)
                        else:
                            # 14B may accept an event solely to record timeout
                            # if its clock crosses expiry during consumption.
                            self._retire("consumer_withdrew_before_ready")
                elif status in CANDIDATE_STATUSES:
                    self.counts["repeated_results_ignored"] += 1
                elif status in {"no_output", "rejected", "expired", "withdrawn"}:
                    record["result_emitted_ns"] = self.last_emitted
                    if record["outcome"] == "pending":
                        self._send(record, "no_result" if status == "no_output" else "unknown", self.last_emitted)
                    else:
                        self.store.source_fault("bridge_upstream_withdrawal")
                    self._retire("no_result" if status == "no_output" else
                                 "receive_check_rejected" if stage == "receive" else "processing_" + status)
            return not self.faulted

    def report(self):
        with self.lock:
            self.tick()
            frames = list(self.frames.values())
            outcomes = Counter(r["outcome"] for r in frames)
            if self.counts["rejected_receive_events"]:
                outcomes["rejected_receive"] = self.counts["rejected_receive_events"]
            consumer = self.store.report()
            ready = sum(r["ready_ingested_monotonic_ns"] is not None for r in consumer["samples"])
            paint = consumer["sample_count_with_report"]
            unfinished = sum(r["processing_started_ns"] is not None and r["processing_result"] is None for r in frames)
            result = dict(schema_version="realtime-overlay-bridge-report-v2", upstream_schema_version=UPSTREAM_SCHEMA,
                          simulation=True,
                          execution_eligible=False, upstream_source_kind=self.binding.source_kind,
                          processing_kind=self.binding.processing_kind,
                          consumer_source_kind=SOURCE_MAP[self.binding.source_kind],
                          clock_assurance=self.binding.assurance, clock_kind=self.binding.clock_kind,
                          fault=self.faulted, opportunities=self.counts["receive_events_seen"],
                          accepted_frames=len(frames), outcomes=dict(outcomes),
                          frames_ready=sum(r["ready_at_ns"] is not None for r in frames),
                          counters=dict(self.counts), rejections=dict(self.rejections), frames=deepcopy(frames),
                          processing=dict(completed_jobs_observed=self.counts["completed_jobs_observed"],
                                          started_jobs_observed=self.counts["processing_started_jobs_observed"],
                                          started_without_completion_observed=unfinished,
                                          ocr_requests_in_uncompleted_jobs=None if unfinished else 0,
                                          outcome_counts=dict(self.processing_outcomes),
                                          ocr_totals_observed=dict(self.ocr_totals),
                                          count_basis="unique_sample_completion_report_not_scope_or_withdrawal_rows",
                                          ocr_invocations_independently_observed_by_bridge=False,
                                          pixel_scope_event_counts={s: dict(c) for s, c in self.scope_events.items()}),
                          upstream_audit=deepcopy(self.upstream_audit), output_audit=deepcopy(self.output_audit),
                          consumer=consumer,
                          accounting=dict(outcome_total=sum(outcomes.values()),
                                          matches_observed_receive_attempts=sum(outcomes.values()) == self.counts["receive_events_seen"],
                                          accepted_frames_missing_consumer_sample=len(frames) - consumer["opportunities"],
                                          unchecked_source_frames=None, unobserved_frames_after_transport_close=None,
                                          physical_device_frames=None),
                          display=dict(consumer_ready_samples=ready, timely_paint_report_samples=paint,
                                       timely_dom_report_samples=sum(r["first_dom_report_received_ns"] is not None for r in consumer["samples"]),
                                       ready_without_timely_paint_report=ready - paint,
                                       observed_receive_attempts_without_timely_paint_report=self.counts["receive_events_seen"] - paint),
                          user_saw_hint_at_ns=None,
                          device_change_to_receive_ms=None, actual_end_to_end_visible_latency_ms=None,
                          formal_game_state_outputs=0)
            return json_safe(result)
