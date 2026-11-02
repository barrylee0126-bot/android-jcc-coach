"""Allowlisted anonymous JSONL events, containing no arbitrary input strings."""

from dataclasses import dataclass, field
import json
import re
import time

from . import VERSION
from .pixels import RESULT_TTL_NS
from .processing import CANDIDATE_STATUSES, PROCESSING_KINDS, ProcessingResult


EVENT_SCHEMA = VERSION
SOURCE_CLOCKS = {
    "synthetic": frozenset({"host_monotonic_ns", "virtual_monotonic_ns"}),
    # A paced, local replay may time actual work in this process's host clock.
    # The timestamp is the new replay receipt, never the recording timestamp.
    "replay": frozenset({"virtual_monotonic_ns", "host_monotonic_ns"}),
    "live": frozenset({"host_monotonic_ns"}),
}
STAGES = {"receive", "queue", "processing", "pixel_full", "pixel_inputs", "field_gate"}
STATUSES = {"received", "started", "candidate", "simulated_candidate", "rejected", "expired", "withdrawn", "no_output"}
REASONS = {"frame_checked", "check_unknown", "pending_replaced", "pending_discarded",
           "processing_started", "ocr_empty", "ocr_unresolved", "ocr_failed", "ocr_not_run", "processing_failed",
           "pixel_inputs_only", "simulated_pixel_inputs_only", "simulated_no_output", "simulated_failed",
           "semantic_evidence_missing", "result_expired", "frame_timeout", "disconnected", "superseded",
           "pixels_or_continuity_revoked", "foreign_lease", "invalid_deadline",
           "clock_regressed_or_invalid", "stream_changed", "not_observed",
           "image_invalid_or_geometry_mismatch", "check_timeout", "sequence_gap",
           "invalid_frame_time_or_sequence", "duplicate_or_reordered", "receive_time_regressed"}


def validate_source_clock(source_kind, clock_kind):
    """Shared producer/consumer contract; labels alone are not clock proof."""
    if (not isinstance(source_kind, str) or not isinstance(clock_kind, str)
            or source_kind not in SOURCE_CLOCKS or clock_kind not in SOURCE_CLOCKS[source_kind]):
        raise ValueError("Unsupported pixel source and clock domain combination")


@dataclass
class EventSink:
    run_id: str
    source_kind: str
    clock_kind: str
    clock_ns: object = time.monotonic_ns
    file: object = None
    processing_kind: str = field(kw_only=True)

    def __post_init__(self):
        validate_source_clock(self.source_kind, self.clock_kind)
        if (not re.fullmatch(r"[0-9a-f]{16}", self.run_id)
                or self.processing_kind not in PROCESSING_KINDS):
            raise ValueError("Anonymous run identity and explicit time domain required")
        self.count = 0
        self.last_emitted = -1
        self.status_counts = {}

    def emit(self, source, stage, status, reason, *, processing=None):
        validate_source_clock(self.source_kind, self.clock_kind)
        if (stage not in STAGES or status not in STATUSES or reason not in REASONS
                or status in CANDIDATE_STATUSES and stage not in ("pixel_full", "pixel_inputs")):
            raise ValueError("Event codes must be allowlisted")
        if processing is not None and (type(processing) is not ProcessingResult
                                       or processing.processing_kind != self.processing_kind):
            raise ValueError("Event processing provenance must match its run")
        if stage in {"pixel_full", "pixel_inputs"} and processing is None:
            raise ValueError("Pixel result events require explicit processing evidence")
        if status in CANDIDATE_STATUSES and (processing is None or processing.completion() != (status, reason)):
            raise ValueError("Candidate status requires matching explicit processing evidence")
        completion_reasons = {"pixel_inputs_only", "ocr_empty", "ocr_unresolved", "ocr_not_run", "ocr_failed", "processing_failed",
                              "simulated_pixel_inputs_only", "simulated_no_output", "simulated_failed"}
        if reason in completion_reasons and (processing is None or processing.completion() != (status, reason)):
            raise ValueError("Completion codes must match the explicit processing outcome")
        processing_fields = (processing.fields() if processing is not None else
                             dict(processing_kind=self.processing_kind,
                                  processing_outcome="in_progress" if stage == "processing" and status == "started"
                                  else "not_completed", ocr_requests=None, ocr_items_count=None, ocr_errors=None))
        now = self.clock_ns()
        received = source.host_received_monotonic_ns
        if type(source.sequence) is not int or source.sequence < 1:
            raise ValueError("Anonymous sample sequence must be a positive integer")
        if type(now) is not int or now < received or now < self.last_emitted:
            raise ValueError("Event time precedes frame receipt")
        if status in CANDIDATE_STATUSES and now >= received + RESULT_TTL_NS:
            status, reason = "expired", "result_expired"
        row = dict(schema_version=EVENT_SCHEMA, event_index=self.count + 1,
                   clock_domain_id=self.run_id, clock_kind=self.clock_kind,
                   sample_id=f"{self.run_id}:{source.sequence}", source_kind=self.source_kind,
                   frame_received_monotonic_ns=received, emitted_monotonic_ns=now,
                   expires_at_monotonic_ns=received + RESULT_TTL_NS,
                   stage=stage, status=status, reason=reason,
                   evidence="pixel_only_not_game_state", **processing_fields)
        if self.file is not None:
            self.file.write(json.dumps(row, separators=(",", ":")) + "\n")
            self.file.flush()
        self.count += 1
        self.last_emitted = now
        self.status_counts[status] = self.status_counts.get(status, 0) + 1
        return row
