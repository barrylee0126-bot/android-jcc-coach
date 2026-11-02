"""Exact RGB input leases, deliberately WITHOUT semantic context certification."""

from dataclasses import dataclass, field
import hashlib
import io
import time

from PIL import Image

from jcc_device.latest import Frame, FrameRef
from jcc_device.recognition import PixelROI


RESULT_TTL_NS = 2_000_000_000
FRAME_LIVENESS_NS = 1_000_000_000
CHECK_BUDGET_NS = 250_000_000
MAX_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 16_000_000


@dataclass(frozen=True)
class PixelLease:
    source: FrameRef
    full_generation: int
    input_generation: int
    expires_at_monotonic_ns: int
    digest: bytes = field(repr=False)
    owner: object = field(repr=False)


class PixelLedger:
    """Only states sampled RGB equality, not visibility, identity or game meaning.

    Every authoritative received frame must pass observe before mailbox loss.
    Caller serializes observe/lease/check. Generations never revive, including
    A -> B -> A and a silent receive timeout before another identical frame.
    """

    def __init__(self, image_size, input_boxes, *, clock_ns=time.monotonic_ns):
        self.size = tuple(image_size)
        if (len(self.size) != 2 or any(type(n) is not int or n <= 0 for n in self.size)
                or self.size[0] * self.size[1] > MAX_PIXELS):
            raise ValueError("Invalid explicit image geometry")
        self.boxes = tuple(tuple(b) for b in input_boxes)
        if not self.boxes or len(self.boxes) > 32:
            raise ValueError("Provide one to 32 complete OCR input regions")
        for i, b in enumerate(self.boxes):
            PixelROI(f"input_{i}", self.size, b).validate(self.size)
        self.clock = clock_ns
        self.owner = object()
        self.latest = None
        self.high_sequence = 0
        self.high_received = -1
        self.stream_id = None
        self.last_now = -1
        self.terminal = None
        self.reason = "not_observed"
        self.full_generation = self.input_generation = 0
        self.full = self.inputs = None
        self.digest = None
        self.counts = dict(frames_checked=0, frames_decoded=0, unknown_checks=0,
                           adjacent_pairs=0, full_changed_pairs=0, input_changed_pairs=0,
                           sequence_gaps=0, missing_sequences=0, check_timeouts=0)
        self.region_changed_pairs = [0] * len(self.boxes)

    def revoke(self, reason):
        self.full_generation += 1
        self.input_generation += 1
        self.latest = self.full = self.inputs = self.digest = None
        self.reason = reason

    def _now(self):
        now = self.clock()
        if type(now) is not int or now < 0 or now < self.last_now:
            self.terminal = "clock_regressed_or_invalid"
            self.revoke(self.terminal)
            return None
        self.last_now = now
        return now

    def _refresh(self, now):
        if self.latest is not None and now - self.latest.host_received_monotonic_ns >= FRAME_LIVENESS_NS:
            self.revoke("frame_timeout")

    def observe(self, frame: Frame, *, total_check_started_ns=None):
        """Bounded by policy AFTER work returns; cannot preempt Pillow/caller work."""
        started = time.perf_counter_ns() if total_check_started_ns is None else total_check_started_ns
        self.counts["frames_checked"] += 1
        now, s = self._now(), frame.source
        reason = None
        if self.terminal or now is None:
            reason = self.terminal
        else:
            self._refresh(now)
            if self.stream_id is not None and s.stream_id != self.stream_id:
                self.terminal = reason = "stream_changed"
            elif (type(s.sequence) is not int or s.sequence < 1
                  or type(s.host_received_monotonic_ns) is not int
                  or s.host_received_monotonic_ns < 0
                  or s.host_received_monotonic_ns > now):
                reason = "invalid_frame_time_or_sequence"
            elif s.sequence <= self.high_sequence:
                reason = "duplicate_or_reordered"
            elif s.host_received_monotonic_ns < self.high_received:
                reason = "receive_time_regressed"
            elif s.sequence != self.high_sequence + 1:
                self.counts["sequence_gaps"] += 1
                self.counts["missing_sequences"] += s.sequence - self.high_sequence - 1
                reason = "sequence_gap"
            if type(s.sequence) is int:
                self.high_sequence = max(self.high_sequence, s.sequence)
            if type(s.host_received_monotonic_ns) is int:
                self.high_received = max(self.high_received, s.host_received_monotonic_ns)
            self.stream_id = s.stream_id if self.stream_id is None else self.stream_id
        full = inputs = digest = None
        if not reason:
            try:
                if not isinstance(frame.jpeg, bytes) or not 0 < len(frame.jpeg) <= MAX_BYTES:
                    raise ValueError
                with Image.open(io.BytesIO(frame.jpeg)) as im:
                    if (im.format not in ("PNG", "JPEG") or im.size != self.size or im.mode != "RGB"
                            or getattr(im, "n_frames", 1) != 1 or im.getexif().get(274, 1) != 1):
                        raise ValueError
                    im.load()
                    full = im.tobytes()
                    crops = []
                    for box in self.boxes:
                        with im.crop(box) as crop:
                            crops.append(crop.tobytes())
                    inputs = tuple(crops)
                digest = hashlib.sha256(frame.jpeg).digest()  # private mixing guard, not continuity
                self.counts["frames_decoded"] += 1
            except Exception:
                reason = "image_invalid_or_geometry_mismatch"
        elapsed = time.perf_counter_ns() - started
        finished_now = self._now()
        if finished_now is None:
            reason = self.terminal
        if not reason and (elapsed >= CHECK_BUDGET_NS or finished_now - s.host_received_monotonic_ns >= FRAME_LIVENESS_NS):
            self.counts["check_timeouts"] += 1
            reason = "check_timeout"
        if reason:
            self.counts["unknown_checks"] += 1
            self.revoke(reason)
            return dict(status="unknown", reason=reason, elapsed_ns=elapsed)
        full_changed = input_changed = None
        if self.full is not None:
            self.counts["adjacent_pairs"] += 1
            full_changed, input_changed = full != self.full, inputs != self.inputs
            self.counts["full_changed_pairs"] += int(full_changed)
            self.counts["input_changed_pairs"] += int(input_changed)
            self.full_generation += int(full_changed)
            self.input_generation += int(input_changed)
            for i, (a, b) in enumerate(zip(inputs, self.inputs)):
                self.region_changed_pairs[i] += int(a != b)
        self.latest, self.full, self.inputs, self.digest = s, full, inputs, digest
        self.reason = "pixel_inputs_checked_only"
        return dict(status="checked", reason=self.reason, elapsed_ns=elapsed,
                    full_changed=full_changed, input_changed=input_changed)

    def lease(self, frame):
        now = self._now()
        if now is None or self.terminal:
            return None
        self._refresh(now)
        if (self.latest != frame.source or self.digest != hashlib.sha256(frame.jpeg).digest()
                or now >= frame.source.host_received_monotonic_ns + RESULT_TTL_NS):
            return None
        return PixelLease(frame.source, self.full_generation, self.input_generation,
                          frame.source.host_received_monotonic_ns + RESULT_TTL_NS, self.digest, self.owner)

    def check(self, lease, scope):
        if scope not in ("full", "inputs"):
            raise ValueError("Unknown comparison scope")
        now = self._now()
        if now is None or self.terminal:
            return self.terminal
        self._refresh(now)
        if not isinstance(lease, PixelLease) or lease.owner is not self.owner:
            return "foreign_lease"
        if lease.expires_at_monotonic_ns != lease.source.host_received_monotonic_ns + RESULT_TTL_NS:
            return "invalid_deadline"
        if now >= lease.expires_at_monotonic_ns:
            return "result_expired"
        if self.latest is None:
            return self.reason
        a, b = ((lease.full_generation, self.full_generation) if scope == "full"
                else (lease.input_generation, self.input_generation))
        if a != b or lease.source.stream_id != self.latest.stream_id:
            return "pixels_or_continuity_revoked"
        return None

    def disconnect(self):
        self.terminal = "disconnected"
        self.revoke(self.terminal)

    def summary(self):
        return {**self.counts, "region_changed_pairs": list(self.region_changed_pairs)}
