"""One receive owner, one active job, one pending slot; no gameplay actions."""

from collections import Counter
from dataclasses import dataclass, field
import statistics
import time

from jcc_device.latest import Frame
from jcc_device.recognition import FrameResult
from jcc_device.state_pipeline import SceneEvidence, StatePipeline

from .events import EVENT_SCHEMA
from .pixels import CHECK_BUDGET_NS, PixelLedger
from .processing import CANDIDATE_STATUSES, ProcessingResult


def distribution(values):
    values = sorted(values)
    n = len(values)
    return dict(n=n, median_ms=statistics.median(values) / 1e6 if n else None,
                max_ms=values[-1] / 1e6 if n else None,
                empirical_p95_ms=values[(95 * n + 99) // 100 - 1] / 1e6 if n >= 20 else None)


@dataclass(frozen=True)
class Job:
    frame: Frame = field(repr=False)
    lease: object = field(repr=False)
    context: object = field(repr=False)
    started_monotonic_ns: int


class ProbeCore:
    """All methods are called with receive_lock held by the integration owner.

    Pixel continuity is a diagnostic independent of 10E's semantic gate. Real
    calibration/context is intentionally unconfigured. 10G never supplies it.
    """

    def __init__(self, stream_id, profile, sink, *, clock_ns=time.monotonic_ns, recorder=None, max_frames=None):
        if profile.key.sample_kind != sink.source_kind:
            raise ValueError("Profile and event source kinds must agree")
        if max_frames is not None and (type(max_frames) is not int or max_frames < 1):
            raise ValueError("Frame check limit must be a positive integer")
        self.stream_id, self.profile, self.sink, self.clock = stream_id, profile, sink, clock_ns
        self.pipeline = StatePipeline(stream_id=stream_id, sample_kind=sink.source_kind,
                                      host_clock_id=sink.run_id, clock_ns=clock_ns,
                                      continuity_enabled=True)
        self.receive_lock = self.pipeline.receive_lock
        self.ledger = PixelLedger(profile.image_size, profile.dependencies, clock_ns=clock_ns)
        self.recorder = recorder
        self.max_frames = max_frames
        self.pending = self.active = None
        self.published = {}
        self.closed = False
        self.first_received = self.last_received = None
        self.counts = Counter(frames_checked=0, frames_declined_unchecked=0,
                              frames_without_formal_output=0, pending_replaced=0,
                              pending_discarded=0, jobs_started=0, jobs_completed=0,
                              processing_errors=0, formal_accepted=0, formal_rejected=0,
                              max_pending_jobs=0, max_active_jobs=0, source_disconnected=0)
        self.outcomes = {s: Counter() for s in ("full", "inputs")}
        self.withdrawals = {s: Counter() for s in ("full", "inputs")}
        self.timings = {s: [] for s in ("receive_check", "processing", "receive_to_complete",
                                       "receive_to_emit", "queue_wait", "gate", "recording")}
        self.job_reports = []
        self.processing_outcomes = Counter()
        self.ocr_totals = Counter(ocr_requests=0, ocr_items_count=0, ocr_errors=0)

    def tick(self):
        for scope, (job, processing) in list(self.published.items()):
            reason = self.ledger.check(job.lease, scope)
            if reason:
                self.sink.emit(job.frame.source, "pixel_" + scope,
                               "expired" if reason == "result_expired" else "withdrawn", reason,
                               processing=processing)
                self.withdrawals[scope][reason] += 1
                del self.published[scope]

    def _discard_pending(self, reason):
        if self.pending is not None:
            self.sink.emit(self.pending[0].source, "queue", "rejected", reason)
            self.counts[reason] += 1
            self.pending = None

    def observe_frame(self, frame):
        # LatestFrameStream holds receive_lock across allocation and this hook.
        # Enforce the cap here before recording/checking, not in a polling loop.
        if self.closed or self.max_frames is not None and self.counts["frames_checked"] >= self.max_frames:
            self.counts["frames_declined_unchecked"] += 1
            # An unchecked receipt cannot keep an old candidate or active lease
            # alive. Signal terminal state only; the controller closes/joins the
            # stream outside this guard, never from the receiver itself.
            self.disconnect()
            return
        started = time.perf_counter_ns()
        context = self.pipeline.observe_frame(frame)  # reuse the unchanged 10E owner
        if self.recorder is not None:
            rstart = time.perf_counter_ns()
            self.recorder.record(frame)
            self.timings["recording"].append(time.perf_counter_ns() - rstart)
        check = self.ledger.observe(frame, total_check_started_ns=started)
        self.counts["frames_checked"] += 1
        self.counts["frames_without_formal_output"] += 1
        self.first_received = (frame.source.host_received_monotonic_ns if self.first_received is None
                               else self.first_received)
        self.last_received = frame.source.host_received_monotonic_ns
        self.tick()  # revoke immediately, before a slow job can publish
        self.sink.emit(frame.source, "receive", "received",
                       "frame_checked" if check["status"] == "checked" else "check_unknown")
        self.sink.emit(frame.source, "field_gate", "no_output", "semantic_evidence_missing")
        lease = self.ledger.lease(frame) if check["status"] == "checked" else None
        if lease is None or context is None:
            self._discard_pending("pending_discarded")
        else:
            self._discard_pending("pending_replaced")
            self.pending = (frame, lease, context)
            self.counts["max_pending_jobs"] = 1
        elapsed = time.perf_counter_ns() - started
        if elapsed >= CHECK_BUDGET_NS:
            if check["reason"] != "check_timeout":
                self.ledger.counts["check_timeouts"] += 1
                self.ledger.counts["unknown_checks"] += int(check["status"] == "checked")
            self.ledger.revoke("check_timeout")
            self._discard_pending("pending_discarded")
            self.tick()
            self.sink.emit(frame.source, "receive", "rejected", "check_timeout")
        self.timings["receive_check"].append(time.perf_counter_ns() - started)
        return check

    def start_job(self):
        self.tick()
        if self.closed or self.active is not None or self.pending is None:
            return None
        frame, lease, context = self.pending
        reason = self.ledger.check(lease, "inputs")
        if reason:
            self._discard_pending("pending_discarded")
            return None
        self.pending = None
        started = self.clock()
        self.active = Job(frame, lease, context, started)
        self.counts["jobs_started"] += 1
        self.counts["max_active_jobs"] = 1
        self.timings["queue_wait"].append(started - frame.source.host_received_monotonic_ns)
        self.sink.emit(frame.source, "processing", "started", "processing_started")
        return self.active

    def complete(self, job, report):
        if job is not self.active:
            raise ValueError("Completion must match the active job")
        processing = ProcessingResult.from_report(report)
        if processing.processing_kind != self.sink.processing_kind:
            raise ValueError("Completion processing provenance must match its run")
        now = self.clock()
        if now < job.started_monotonic_ns:
            raise ValueError("Processing clock regressed")
        self.active = None
        self.counts["jobs_completed"] += 1
        self.counts["processing_errors"] += int(processing.failed)
        self.processing_outcomes[processing.processing_outcome] += 1
        for key in self.ocr_totals:
            self.ocr_totals[key] += getattr(processing, key) or 0
        self.timings["processing"].append(now - job.started_monotonic_ns)
        self.timings["receive_to_complete"].append(now - job.frame.source.host_received_monotonic_ns)
        self.tick()
        for scope in ("full", "inputs"):
            reason = self.ledger.check(job.lease, scope)
            status, why = processing.completion()
            if reason and not processing.failed:
                status, why = ("expired" if reason == "result_expired" else "rejected"), reason
            if status in CANDIDATE_STATUSES and scope in self.published:
                previous, previous_processing = self.published.pop(scope)
                self.sink.emit(previous.frame.source, "pixel_" + scope, "withdrawn", "superseded",
                               processing=previous_processing)
                self.withdrawals[scope]["superseded"] += 1
            event = self.sink.emit(job.frame.source, "pixel_" + scope, status, why, processing=processing)
            self.outcomes[scope][event["reason"]] += 1
            if event["status"] in CANDIDATE_STATUSES:
                self.published[scope] = (job, processing)
                self.timings["receive_to_emit"].append(self.clock() - job.frame.source.host_received_monotonic_ns)
        gstart = self.clock()
        # Never construct a real ContextEvidence from pixel equality or 10G.
        actual_items = processing.ocr_items_count if processing.processing_outcome == "ocr_text" else 0
        formal = FrameResult(job.frame.source, job.started_monotonic_ns, now,
                             SceneEvidence(ocr_items_count=actual_items))
        accepted = self.pipeline.publish(job.context, formal)
        self.counts["formal_accepted" if accepted else "formal_rejected"] += 1
        self.sink.emit(job.frame.source, "field_gate", "rejected", "semantic_evidence_missing", processing=processing)
        self.timings["gate"].append(self.clock() - gstart)
        self.job_reports.append({**report, **processing.fields()})

    def disconnect(self):
        if self.closed:
            return
        self.closed = True
        self.counts["source_disconnected"] += 1
        self.ledger.disconnect()
        self.tick()
        self._discard_pending("pending_discarded")
        self.pipeline.disconnect()

    def summary(self):
        self.tick()
        span = None if self.first_received is None else self.last_received - self.first_received
        return dict(version=EVENT_SCHEMA, source_kind=self.sink.source_kind,
                    processing_kind=self.sink.processing_kind,
                    processing_outcome_counts=dict(self.processing_outcomes), ocr_totals=dict(self.ocr_totals),
                    clock_kind=self.sink.clock_kind, clock_domain_id=self.sink.run_id,
                    policy=dict(result_ttl_ms=2000, receive_liveness_ms=1000,
                                check_budget_ms=250, max_checked_frames=self.max_frames,
                                basis="unchanged_uncalibrated_10E_policy"),
                    counts=dict(self.counts), pixel_checks=self.ledger.summary(),
                    completion_outcomes={s: dict(c) for s, c in self.outcomes.items()},
                    withdrawal_outcomes={s: dict(c) for s, c in self.withdrawals.items()},
                    timings={s: distribution(v) for s, v in self.timings.items()},
                    timing_samples_ns={s: list(v) for s, v in self.timings.items()},
                    timing_first_and_remaining={s: dict(first_ms=v[0] / 1e6 if v else None,
                                                         remaining=distribution(v[1:]))
                                                for s, v in self.timings.items() if s != "receive_to_emit"},
                    unfinished_active_jobs=int(self.active is not None),
                    pending_jobs=int(self.pending is not None),
                    received_span_ns=span,
                    received_fps=((self.counts["frames_checked"] - 1) * 1e9 / span if span else None),
                    pipeline=self.pipeline.stats(), events=self.sink.count,
                    event_status_counts=dict(self.sink.status_counts),
                    jobs=list(self.job_reports), accuracy=None, matched_games_started=0,
                    usable_decision_opportunities=None, visible_hint_latency_ms=None,
                    device_capture_to_receive_ms=None, formal_game_state_outputs=0)
