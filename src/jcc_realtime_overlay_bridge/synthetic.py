"""Public 10H producer calls with generated pixels; never invokes runtime.py."""

from uuid import uuid4
import time

from jcc_device.latest import Frame, FrameRef
from jcc_realtime_probe.core import ProbeCore
from jcc_realtime_probe.events import EventSink
from jcc_realtime_probe.profile import load_profile
from jcc_realtime_probe.processing import simulated_report
from jcc_realtime_probe.timeline import VirtualClock, png, synthetic_profile

from .bridge import RealtimeOverlayBridge
from .clocks import bind_same_process
from .transport import JsonlDecoder

MS = 1_000_000


class InProcessPipeline:
    def __init__(self, *, clock=None, source_kind="synthetic", run_id=None,
                 processing_kind="simulated_work", profile=None):
        self.clock = clock if clock is not None else (time.monotonic_ns if source_kind == "live" else VirtualClock())
        self.sink = EventSink(run_id or uuid4().hex[:16], source_kind,
                              "host_monotonic_ns" if source_kind == "live" or self.clock is time.monotonic_ns
                              else "virtual_monotonic_ns", self.clock, processing_kind=processing_kind)
        self.bridge = RealtimeOverlayBridge(bind_same_process(self.sink))
        self.decoder = JsonlDecoder(self.bridge)
        self.sink.file = self
        self.stream_id = "synthetic-" + self.sink.run_id
        profile = profile or (synthetic_profile() if source_kind == "synthetic" else
                   load_profile(dict(image_size=[160, 100], input_boxes=[[10, 10, 70, 40]]), source_kind))
        self.core = ProbeCore(self.stream_id, profile, self.sink, clock_ns=self.clock)
        self.sequence = 0

    def write(self, text):
        self.decoder.feed(text.encode("utf-8"))

    def flush(self):
        pass

    def receive(self, *, inside=0, outside=0, data=None):
        self.sequence += 1
        frame = Frame(FrameRef(self.stream_id, self.sequence, self.clock(), "synthetic-not-capture-time"),
                      png(inside=inside, outside=outside) if data is None else data)
        self.core.observe_frame(frame)
        return frame

    def complete(self, job, *, outcome="simulated_ready", report=None):
        self.core.complete(job, simulated_report(outcome=outcome) if report is None else report)


def run_synthetic():
    """An executable, repeatable reference case. Checks genuine module outputs."""
    pipeline = InProcessPipeline(run_id="15a015a015a015a0")
    clock, core, bridge = pipeline.clock, pipeline.core, pipeline.bridge
    states = []

    def observe(label):
        view = bridge.store.view()
        states.append(dict(point=label, status=view["status"], sample_id=view["sample_id"],
                           message=view["message"]))
        return view

    pipeline.receive()
    observe("frame1_pending")
    job = core.start_job()
    clock.now += 80 * MS
    pipeline.complete(job)
    if observe("frame1_ready")["status"] != "ready":
        raise RuntimeError("reference_first_ready")
    clock.now += 170 * MS
    pipeline.receive(outside=1)
    if observe("frame2_barrier")["message"] is not None:
        raise RuntimeError("reference_new_frame_must_withdraw")
    job = core.start_job()
    clock.now += 80 * MS
    pipeline.complete(job, outcome="simulated_no_output")
    observe("frame2_no_result")
    clock.now += 170 * MS
    pipeline.receive(outside=2)
    late = core.start_job()
    clock.now += 250 * MS
    pipeline.receive(outside=3)
    clock.now += 80 * MS
    pipeline.complete(late)
    if observe("frame3_late_candidate_after_frame4")["message"] is not None:
        raise RuntimeError("reference_late_candidate_revived")
    job = core.start_job()
    clock.now += 80 * MS
    pipeline.complete(job, outcome="simulated_error")
    observe("frame4_processing_rejected")
    clock.now += 90 * MS
    pipeline.receive(outside=4)
    last_received = clock()
    job = core.start_job()
    clock.now += 80 * MS
    pipeline.complete(job)
    observe("frame5_ready")
    clock.now = last_received + bridge.silence_ns
    bridge.tick()
    if observe("exact_receive_silence_deadline")["message"] is not None:
        raise RuntimeError("reference_deadline_extended")
    core.tick()
    core.disconnect()
    pipeline.decoder.eof()
    report = bridge.report()
    if (report["opportunities"] != 5 or report["frames_ready"] != 2
            or report["consumer"]["opportunities"] != 5
            or sum(report["outcomes"].values()) != 5
            or core.summary()["counts"]["formal_accepted"] != 0):
        raise RuntimeError("reference_denominator_or_formal_gate")
    summary = core.summary()
    result = dict(validation="controlled_clock_public_modules_passed", source_fixture="generated_RGB",
                timing_basis="virtual_schedule_inputs_not_measured_OCR", states=states,
                producer_counts=summary["counts"], producer_ocr_totals=summary["ocr_totals"],
                processing_kind=summary["processing_kind"], bridge=report)
    bridge.store.close()
    return result
