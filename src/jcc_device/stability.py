"""Exact received-frame continuity and bounded asynchronous recognition.

Only explicitly calibrated RGB regions are compared. Per-frame context and
visibility evidence must come from the caller; identical pixels alone do not
establish either. No device access, game coordinates, or semantic inference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import io
import threading
import time
from typing import Callable, Generic, Protocol, TypeVar

from .latest import Frame, FrameRef, positive_seconds
from .recognition import FrameResult, PixelROI


T = TypeVar("T")


def _duration_ns(seconds: float, name: str) -> int:
    value = int(positive_seconds(seconds, name) * 1_000_000_000)
    if value < 1:
        raise ValueError(f"{name} must be at least one nanosecond")
    return value


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class RegionCalibration:
    """Immutable caller-reviewed coverage; coordinates are never inferred.

    context_regions must include the pixels needed to distinguish the intended
    scene/layout and relevant occlusion, not merely a static decorative border.
    The module cannot verify the sufficiency of this calibration.
    """

    calibration_id: str
    calibration_ref: str
    image_size: tuple[int, int]
    relevant_regions: tuple[PixelROI, ...]
    context_regions: tuple[PixelROI, ...]
    applied_rotation_degrees: int = 0

    def __post_init__(self) -> None:
        size = tuple(self.image_size)
        if (not _nonempty(self.calibration_id) or not _nonempty(self.calibration_ref)
                or len(size) != 2 or any(type(n) is not int or n <= 0 for n in size)
                or type(self.applied_rotation_degrees) is not int
                or self.applied_rotation_degrees not in (0, 90, 180, 270)):
            raise ValueError("Explicit calibration identity, dimensions and rotation are required")
        object.__setattr__(self, "image_size", size)
        names: set[str] = set()
        for attribute in ("relevant_regions", "context_regions"):
            regions = []
            for roi in getattr(self, attribute):
                # Copy nested coordinates too: frozen dataclasses may contain lists.
                copied = PixelROI(roi.name, tuple(roi.image_size), tuple(roi.box))
                copied.validate(size)
                if not _nonempty(copied.name) or copied.name in names:
                    raise ValueError("Region names must be unique across both coverage sets")
                names.add(copied.name)
                regions.append(copied)
            if not regions:
                raise ValueError("Both relevant and context regions must be configured")
            object.__setattr__(self, attribute, tuple(regions))


@dataclass(frozen=True)
class ContextEvidence:
    """A caller's assertion for THIS frame, not an automatic scene detector.

    context_key must identify all relevant scene/phase/layout/viewport/mode
    state. verification_ref identifies the applicable verifier/calibration.
    Unknown context or visibility must stay None, never default to True.
    """

    source: FrameRef
    host_clock_id: str
    context_epoch: int
    context_key: str | None
    calibration_id: str | None
    verification_ref: str | None
    orientation: str | None
    applied_rotation_degrees: int | None
    unobscured: bool | None


@dataclass(frozen=True)
class FrameCheck:
    source: FrameRef
    status: str  # ready means eligible to recognize, not recognized game state.
    reason: str
    generation: int
    check_elapsed_ns: int


@dataclass(frozen=True)
class RecognitionTicket:
    source: FrameRef
    generation: int
    expires_host_monotonic_ns: int
    _owner: object = field(repr=False)


@dataclass(frozen=True)
class ContinuityProof:
    source: FrameRef
    verified_through: FrameRef
    checked_frame_count: int
    generation: int
    host_clock_id: str
    context_epoch: int
    context_key: str
    verification_ref: str
    calibration: RegionCalibration
    checked_host_monotonic_ns: int
    expires_host_monotonic_ns: int
    policy: str = "exact_received_rgb_regions_v1"


@dataclass(frozen=True)
class ProofDecision:
    status: str  # proven / unknown
    reason: str | None
    proof: ContinuityProof | None = None


class ExactPixelContinuity:
    """Single-stream proof owner; call observe ON EVERY received frame in order.

    Decoding/comparison and proof reads share one lock. A new observed pixel,
    context, gap or error revokes all earlier tickets permanently, including
    A->B->A. Only ticket source reception fixes its deadline. No hashes,
    thresholds, sampled intermediate frames or automatic TTL renewal are used.
    """

    def __init__(self, calibration: RegionCalibration | None, *, host_clock_id: str,
                 max_result_age_seconds: float, max_frame_age_seconds: float,
                 clock_ns: Callable[[], int] = time.monotonic_ns):
        if not _nonempty(host_clock_id):
            raise ValueError("An explicit host clock domain is required")
        if calibration is not None and not isinstance(calibration, RegionCalibration):
            raise ValueError("calibration must be RegionCalibration or None")
        self._calibration = calibration
        self._host_clock_id = host_clock_id
        self._max_result_age_ns = _duration_ns(max_result_age_seconds, "max_result_age_seconds")
        self._max_frame_age_ns = _duration_ns(max_frame_age_seconds, "max_frame_age_seconds")
        self._clock = clock_ns
        self._lock = threading.RLock()
        self._owner = object()
        self._generation = 0
        self._last_seen: FrameRef | None = None
        self._received_watermark_ns = -1
        self._last_now = -1
        self._max_epoch = -1
        self._latest: FrameRef | None = None
        self._first: FrameRef | None = None
        self._context: ContextEvidence | None = None
        self._pixels: tuple[bytes, ...] | None = None
        self._reason = "not_observed"
        self._terminal_reason: str | None = None
        self._counts = dict(frames_checked=0, frames_ready=0, frames_unknown=0,
                            frames_decoded=0, region_bytes_extracted=0,
                            invalidations=0, check_ns_total=0, check_ns_max=0)

    @property
    def calibration(self) -> RegionCalibration | None:
        return self._calibration

    @property
    def host_clock_id(self) -> str:
        return self._host_clock_id

    @property
    def max_result_age_ns(self) -> int:
        return self._max_result_age_ns

    @property
    def max_frame_age_ns(self) -> int:
        return self._max_frame_age_ns

    def _invalidate(self, reason: str) -> None:
        self._generation += 1
        self._latest = self._first = None
        self._context = self._pixels = None
        self._reason = reason
        self._counts["invalidations"] += 1

    def _now(self) -> int | None:
        now = self._clock()
        if type(now) is not int or now < 0 or now < self._last_now:
            self._terminal_reason = "clock_regressed_or_invalid"
            self._invalidate(self._terminal_reason)
            return None
        self._last_now = now
        return now

    def _live(self, now: int) -> bool:
        if self._latest is not None and now - self._latest.host_received_monotonic_ns >= self.max_frame_age_ns:
            self._invalidate("frame_timeout")
        return self._latest is not None

    def observe(self, frame: Frame, context: ContextEvidence | None) -> FrameCheck:
        """Synchronous receive-path check, before any lossy latest-frame slot.

        Calls must follow receipt order. A skipped/duplicate/reordered source is
        unknown and revokes previous proof. The next consecutive valid frame
        may start a NEW segment; it cannot restore an older ticket.
        """
        started = time.perf_counter_ns()
        with self._lock:
            status, reason = self._observe(frame, context)
            elapsed = time.perf_counter_ns() - started
            self._counts["frames_checked"] += 1
            self._counts["frames_" + status] += 1
            self._counts["check_ns_total"] += elapsed
            self._counts["check_ns_max"] = max(self._counts["check_ns_max"], elapsed)
            return FrameCheck(frame.source, status, reason, self._generation, elapsed)

    def _unknown(self, reason: str) -> tuple[str, str]:
        self._invalidate(reason)
        return "unknown", reason

    def _observe(self, frame: Frame, context: ContextEvidence | None) -> tuple[str, str]:
        if self._terminal_reason:
            return "unknown", self._terminal_reason
        now = self._now()
        if now is None:
            return "unknown", self._reason
        source = frame.source
        if (not isinstance(source, FrameRef) or not _nonempty(source.stream_id)
                or type(source.sequence) is not int or source.sequence < 1
                or type(source.host_received_monotonic_ns) is not int
                or source.host_received_monotonic_ns < 0):
            return self._unknown("invalid_source")
        prior = self._last_seen
        if prior is not None and source.stream_id != prior.stream_id:
            self._terminal_reason = "session_changed"
            return self._unknown(self._terminal_reason)
        if prior is not None and source.sequence <= prior.sequence:
            return self._unknown("out_of_order_or_duplicate")
        self._last_seen = source
        received = source.host_received_monotonic_ns
        if received > now or received < self._received_watermark_ns:
            return self._unknown("invalid_receive_time")
        self._received_watermark_ns = received
        if prior is not None and source.sequence != prior.sequence + 1:
            return self._unknown("sequence_gap")
        if now - received >= self.max_frame_age_ns:
            return self._unknown("frame_timeout")
        self._live(now)  # A silence interval breaks proof even if pixels resume.
        calibration = self.calibration
        if calibration is None:
            return self._unknown("regions_unconfigured")
        if not isinstance(context, ContextEvidence) or context.source != source:
            return self._unknown("context_unproven")
        if context.unobscured is not True:
            return self._unknown("occluded_or_visibility_unknown")
        if (not _nonempty(context.context_key) or not _nonempty(context.verification_ref)
                or type(context.context_epoch) is not int or context.context_epoch < 0):
            return self._unknown("context_unproven")
        if context.host_clock_id != self.host_clock_id:
            self._terminal_reason = "clock_domain_changed"
            return self._unknown(self._terminal_reason)
        if context.context_epoch < self._max_epoch:
            return self._unknown("context_epoch_regressed")
        self._max_epoch = context.context_epoch
        if context.calibration_id != calibration.calibration_id:
            return self._unknown("calibration_mismatch")
        width, height = calibration.image_size
        orientation = "landscape" if width > height else "portrait" if width < height else "square"
        if (context.orientation != orientation
                or type(context.applied_rotation_degrees) is not int
                or context.applied_rotation_degrees != calibration.applied_rotation_degrees):
            return self._unknown("orientation_mismatch")
        if not isinstance(frame.jpeg, bytes) or not frame.jpeg:
            return self._unknown("decode_failed")
        try:
            from PIL import Image

            with Image.open(io.BytesIO(frame.jpeg)) as image:
                if image.size != calibration.image_size:
                    return self._unknown("geometry_mismatch")
                if (image.format not in ("PNG", "JPEG") or image.mode != "RGB"
                        or getattr(image, "is_animated", False) or image.getexif().get(274, 1) != 1):
                    return self._unknown("unsupported_image_encoding")
                image.load()
                regions = calibration.relevant_regions + calibration.context_regions
                samples = []
                for roi in regions:
                    with image.crop(roi.box) as cropped:
                        samples.append(cropped.tobytes())
                pixels = tuple(samples)
                self._counts["frames_decoded"] += 1
                self._counts["region_bytes_extracted"] += sum(map(len, pixels))
        except Exception:
            # No image, path, OCR text or exception message in diagnostics.
            return self._unknown("decode_failed")
        signature = (context.context_epoch, context.context_key, context.verification_ref)
        previous_signature = (None if self._context is None else
                              (self._context.context_epoch, self._context.context_key,
                               self._context.verification_ref))
        reason = "baseline" if self._pixels is None else "unchanged"
        if self._pixels is not None and signature != previous_signature:
            self._invalidate("context_changed")
            reason = "context_changed"
        elif self._pixels is not None and pixels != self._pixels:
            self._invalidate("pixels_changed")
            reason = "pixels_changed"
        if self._pixels is None:
            self._first, self._pixels, self._context = source, pixels, context
        self._latest = source
        self._reason = reason
        return "ready", reason

    def ticket(self) -> RecognitionTicket | None:
        """Issue a ticket for the most recent fully checked frame only."""
        with self._lock:
            now = self._now()
            if self._terminal_reason or now is None or not self._live(now):
                return None
            source = self._latest
            deadline = source.host_received_monotonic_ns + self.max_result_age_ns
            if now >= deadline:
                return None
            return RecognitionTicket(source, self._generation, deadline, self._owner)

    def _proof(self, ticket: RecognitionTicket, now: int) -> ProofDecision:
        if self._terminal_reason:
            return ProofDecision("unknown", self._terminal_reason)
        if not self._live(now):
            return ProofDecision("unknown", self._reason)
        if not isinstance(ticket, RecognitionTicket) or ticket._owner is not self._owner:
            return ProofDecision("unknown", "foreign_ticket")
        if ticket.expires_host_monotonic_ns != ticket.source.host_received_monotonic_ns + self.max_result_age_ns:
            return ProofDecision("unknown", "invalid_ticket_deadline")
        if (ticket.generation != self._generation
                or ticket.source.stream_id != self._latest.stream_id
                or not self._first.sequence <= ticket.source.sequence <= self._latest.sequence):
            return ProofDecision("unknown", "continuity_revoked")
        if now >= ticket.expires_host_monotonic_ns:
            return ProofDecision("unknown", "result_expired")
        context = self._context
        proof = ContinuityProof(ticket.source, self._latest,
                                self._latest.sequence - ticket.source.sequence + 1,
                                self._generation, self.host_clock_id, context.context_epoch,
                                context.context_key, context.verification_ref, self.calibration,
                                now, ticket.expires_host_monotonic_ns)
        return ProofDecision("proven", None, proof)

    def proof_for(self, ticket: RecognitionTicket) -> ProofDecision:
        with self._lock:
            now = self._now()
            return (ProofDecision("unknown", self._reason) if now is None else self._proof(ticket, now))

    def validate_result(self, ticket: RecognitionTicket, result: FrameResult[T]) -> ProofDecision:
        """Check exact source/timing as well as continuity, at publication/read."""
        with self._lock:
            now = self._now()
            if now is None:
                return ProofDecision("unknown", self._reason)
            decision = self._proof(ticket, now)
            if decision.status != "proven":
                return decision
            if not isinstance(result, FrameResult) or result.source != ticket.source:
                return ProofDecision("unknown", "result_source_mismatch")
            started, finished = result.processing_started_monotonic_ns, result.processing_finished_monotonic_ns
            if (type(started) is not int or type(finished) is not int
                    or not ticket.source.host_received_monotonic_ns <= started <= finished <= now):
                return ProofDecision("unknown", "invalid_processing_time")
            return decision

    def disconnect(self) -> None:
        """Terminal; reconnect requires a new stream and a new proof owner."""
        with self._lock:
            if self._terminal_reason is None:
                self._terminal_reason = "disconnected"
                self._invalidate(self._terminal_reason)

    def stats(self) -> dict:
        with self._lock:
            return {**self._counts, "generation": self._generation,
                    "last_reason": self._reason, "terminal_reason": self._terminal_reason}


class ResultProcessor(Protocol[T]):
    def process(self, frame: Frame) -> FrameResult[T]: ...


@dataclass(frozen=True)
class RecognitionRead(Generic[T]):
    status: str  # available / unknown; does not declare game-state recognized.
    reason: str | None
    result: FrameResult[T] | None = None
    proof: ContinuityProof | None = None


class StableRecognitionScheduler(Generic[T]):
    """One worker plus one replaceable pending frame; observe never runs OCR.

    The receiver must supply every frame, not just read_latest() deliveries.
    Slow work cannot be forcefully cancelled: its result is checked on return.
    Use current() for every read; copied results/proofs are only point-in-time.
    """

    def __init__(self, continuity: ExactPixelContinuity, processor: ResultProcessor[T]):
        self.continuity, self.processor = continuity, processor
        self._condition = threading.Condition()
        self._pending: tuple[Frame, RecognitionTicket] | None = None
        self._published: tuple[RecognitionTicket, FrameResult[T]] | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._reason = "not_observed"
        self._counts = dict(jobs_started=0, jobs_completed=0, processing_errors=0,
                            results_accepted=0, results_rejected=0, pending_replaced=0,
                            pending_discarded=0, max_pending_frames=0, active_jobs=0,
                            max_active_jobs=0)
        self._error_type: str | None = None

    def start(self) -> StableRecognitionScheduler[T]:
        with self._condition:
            if self._closed or self._thread is not None:
                raise RuntimeError("A scheduler can be started exactly once")
            self._thread = threading.Thread(target=self._run, daemon=True, name="jcc-stable-recognition")
            self._thread.start()
        return self

    def _discard_pending(self) -> None:
        if self._pending is not None:
            self._counts["pending_discarded"] += 1
            self._pending = None

    def observe(self, frame: Frame, context: ContextEvidence | None) -> FrameCheck:
        with self._condition:
            if self._thread is None:
                raise RuntimeError("Start the scheduler before supplying frames")
            check = self.continuity.observe(frame, context)
            self._refresh()
            ticket = None if self._closed else self.continuity.ticket()
            if ticket is None:
                self._discard_pending()
                self._reason = check.reason
            else:
                if self._pending is not None:
                    self._counts["pending_replaced"] += 1
                self._pending = (frame, ticket)
                self._counts["max_pending_frames"] = 1
                if self._reason == "not_observed":
                    self._reason = "recognition_pending"
            self._condition.notify_all()
            return check

    def _refresh(self) -> RecognitionRead[T]:
        if self._published is not None:
            ticket, result = self._published
            decision = self.continuity.validate_result(ticket, result)
            if decision.status == "proven":
                return RecognitionRead("available", None, result, decision.proof)
            self._published = None
            self._reason = decision.reason
        return RecognitionRead("unknown", self._reason)

    def current(self) -> RecognitionRead[T]:
        with self._condition:
            return self._refresh()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                frame, ticket = self._pending
                self._pending = None
                if self.continuity.proof_for(ticket).status != "proven":
                    self._counts["pending_discarded"] += 1
                    continue
                self._counts["jobs_started"] += 1
                self._counts["active_jobs"] = self._counts["max_active_jobs"] = 1
            try:
                result = self.processor.process(frame)
            except BaseException as error:
                with self._condition:
                    self._counts["processing_errors"] += 1
                    self._counts["active_jobs"] = 0
                    self._error_type = type(error).__name__
                    self._published = None
                    if not self._closed:
                        self._reason = "recognition_failed"
                continue
            with self._condition:
                self._counts["jobs_completed"] += 1
                self._counts["active_jobs"] = 0
                decision = self.continuity.validate_result(ticket, result)
                if not self._closed and decision.status == "proven":
                    self._published = (ticket, result)
                    self._counts["results_accepted"] += 1
                else:
                    self._counts["results_rejected"] += 1
                    self._reason = decision.reason or "disconnected"

    def disconnect(self) -> None:
        with self._condition:
            self._closed = True
            self.continuity.disconnect()
            self._discard_pending()
            self._published = None
            self._reason = "disconnected"
            self._condition.notify_all()

    def close(self, timeout_seconds: float = 2.0) -> None:
        timeout = positive_seconds(timeout_seconds, "timeout_seconds", allow_zero=True)
        self.disconnect()
        thread = self._thread
        if thread is not None:
            if thread is threading.current_thread():
                raise RuntimeError("Recognition worker cannot join itself")
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("Recognition worker did not stop within timeout_seconds")

    def stats(self) -> dict:
        with self._condition:
            return {**self._counts, "pending_frames": int(self._pending is not None),
                    "closed": self._closed, "processing_error_type": self._error_type,
                    "worker_alive": self._thread is not None and self._thread.is_alive(),
                    "continuity": self.continuity.stats()}

    def __enter__(self) -> StableRecognitionScheduler[T]:
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except TimeoutError as error:
            if exc is None:
                raise
            exc.add_note(str(error))
