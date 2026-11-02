"""Deterministic replay of actual RGB pixels and explicit simulated work time."""

import io
from uuid import uuid4

from PIL import Image

from jcc_device.latest import Frame, FrameRef

from .core import ProbeCore
from .events import EventSink
from .profile import load_profile
from .processing import simulated_report


class VirtualClock:
    def __init__(self):
        self.now = 10_000_000_000

    def __call__(self):
        return self.now


def png(*, inside=0, outside=0, size=(160, 100)):
    with Image.new("RGB", size, (10, 20, 30)) as im, io.BytesIO() as buf:
        im.putpixel((21, 21), (inside, 80, 90))
        im.putpixel((140, 90), (outside, 81, 91))
        im.save(buf, format="PNG")
        return buf.getvalue()


def synthetic_profile():
    return load_profile(dict(image_size=[160, 100], input_boxes=[[10, 10, 70, 40]]), "synthetic")


def synthetic_rows(case="outside_motion", count=20):
    if case not in ("static", "outside_motion", "inside_aba", "sequence_gap", "receive_stall"):
        raise ValueError("Unknown synthetic case")
    return [(i * 250_000_000 + (1_500_000_000 if case == "receive_stall" and i >= 8 else 0),
             i + 1 + (1 if case == "sequence_gap" and i >= 5 else 0),
             png(inside=int(case == "inside_aba" and i == 4),
                 outside=i if case == "outside_motion" else 0)) for i in range(count)]


def run_timeline(rows, profile, *, source_kind="synthetic", processing_ns=1_450_000_000,
                 events_file=None, run_id=None):
    """Durations here are simulator INPUTS, not measured recognition latency.

    Receives at an exact completion boundary are applied first (conservative).
    Real replay sequence gaps are preserved, never renumbered to hide losses.
    """
    if type(processing_ns) is not int or processing_ns < 0:
        raise ValueError("Nonnegative simulated work time required")
    clock = VirtualClock()
    base = clock.now
    sink = EventSink(run_id or uuid4().hex[:16], source_kind, "virtual_monotonic_ns", clock, events_file,
                     processing_kind="simulated_work")
    core = ProbeCore("virtual-local-stream", profile, sink, clock_ns=clock)
    complete_at = None
    index = 0
    end_at = base + rows[-1][0] + max(processing_ns * 2 + 1, 2_100_000_000)
    while index < len(rows) or core.active is not None or core.pending is not None or core.published:
        next_receive = base + rows[index][0] if index < len(rows) else None
        timers = [job.lease.expires_at_monotonic_ns for job, _ in core.published.values()]
        if core.ledger.latest is not None and (core.published or core.active or core.pending):
            timers.append(core.ledger.latest.host_received_monotonic_ns + 1_000_000_000)
        next_timer = min((t for t in timers if t > clock.now), default=None)
        candidates = [x for x in (next_receive, complete_at, next_timer) if x is not None]
        if not candidates:
            break
        clock.now = min(candidates)
        if clock.now > end_at:
            raise RuntimeError("Unbounded timeline")
        if next_receive == clock.now:
            offset, sequence, data = rows[index]
            frame = Frame(FrameRef("virtual-local-stream", sequence, clock.now, "virtual-time",
                                   "replay_virtual_receipt_not_original_capture"), data)
            core.observe_frame(frame)
            index += 1
        if complete_at == clock.now:
            job = core.active
            core.complete(job, simulated_report(simulated_processing_ns=processing_ns))
            complete_at = None
        core.tick()
        if core.active is None:
            job = core.start_job()
            if job:
                complete_at = clock.now + processing_ns
    clock.now = max(clock.now, base + rows[-1][0] + 2_100_000_000)
    core.tick()
    before = core.summary()
    core.disconnect()
    after = core.summary()
    after["event_status_counts_before_disconnect"] = before["event_status_counts"]
    after["simulated_processing_ns"] = processing_ns
    after["replay_notice"] = "Pixel timing simulation; never original match time or actual OCR speed"
    return after
