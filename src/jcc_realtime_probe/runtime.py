"""Read-only TabletStream -> authoritative LatestFrameStream -> probe observer."""

from contextlib import ExitStack
import threading
import time
from uuid import uuid4

from jcc_device.latest import FrameStreamError, LatestFrameStream
from jcc_device.stream import TabletStream
from jcc_device.vision_ocr import VisionOCR

from .assets import Recorder
from .core import ProbeCore
from .events import EventSink
from .processor import OCRProcessor


def run_live(profile, *, duration_seconds=20, max_frames=80, events_file=None, recording_dir=None,
             sample_fps=4):
    if (not 0 < duration_seconds <= 60 or type(max_frames) is not int or not 1 <= max_frames <= 240
            or type(sample_fps) is not int or not 1 <= sample_fps <= 15):
        raise ValueError("Use a bounded 1-60 second, <=240 frame probe")
    started = time.monotonic_ns()
    errors = []
    stop = threading.Event()
    worker = None
    recorder = None
    stream = None
    core = None
    with ExitStack() as stack:
        if recording_dir:
            recorder = Recorder(recording_dir)
            stack.callback(recorder.close)
        ocr_resources = stack.enter_context(ExitStack())
        ocr = ocr_resources.enter_context(VisionOCR(timeout=5))
        raw_setup = stack.enter_context(ExitStack())
        raw = raw_setup.enter_context(TabletStream(max_size=max(profile.image_size), sample_fps=sample_fps,
                                                   capture_diagnostics=False))
        stream = LatestFrameStream(raw, read_timeout=.25)
        try:
            # Transfer the already-open source only inside the cleanup guard.
            # Before this point raw_setup owns it; afterwards stream alone
            # closes it (on its receiver after start, or here before start).
            raw_setup.pop_all()
            sink = EventSink(uuid4().hex[:16], "live", "host_monotonic_ns", file=events_file, processing_kind="ocr")
            core = ProbeCore(stream.stream_id, profile, sink, recorder=recorder, max_frames=max_frames)
            processor = OCRProcessor(profile, ocr, sink.run_id)
            stream.bind_observer(core)

            def work():
                try:
                    while not stop.is_set():
                        with core.receive_lock:
                            job = core.start_job()
                        if job is None:
                            stop.wait(.01)
                            continue
                        report = processor(job)
                        errors.extend(report["error_types"])
                        with core.receive_lock:
                            core.complete(job, report)
                except Exception as error:
                    errors.append(type(error).__name__)
                    stop.set()

            stream.start()
            worker = threading.Thread(target=work, daemon=True, name="jcc-realtime-probe-ocr")
            worker.start()
            run_started = time.monotonic_ns()
            while not stop.is_set():
                with core.receive_lock:
                    core.tick()
                    first = core.first_received
                    enough_frames = core.counts["frames_checked"] >= max_frames
                    enough_time = first is not None and time.monotonic_ns() - first >= duration_seconds * 1e9
                if enough_frames or enough_time or time.monotonic_ns() - run_started > (duration_seconds + 12) * 1e9:
                    break
                try:
                    # The consumer is intentionally independent of all-frame checks.
                    stream.read_latest(timeout=.05)
                except TimeoutError:
                    pass
                except FrameStreamError as error:
                    errors.append(type(error).__name__)
                    break
                state = stream.stats()
                if state["source_error_type"] or not state["receiver_thread_alive"]:
                    break
        finally:
            stop.set()
            try:
                stream.close()  # immediately withdraws all published candidates
            except Exception as error:
                errors.append(type(error).__name__)
            # Thread.join rejects a Thread whose start failed. A constructed
            # worker owns no running work until it has a thread identity.
            if worker is not None and worker.ident is not None:
                worker.join(timeout=12)
                if worker.is_alive():
                    errors.append("WorkerShutdownTimeout")
                    # Consume its registered close here so ExitStack does not
                    # close the same backend again on return or an exception.
                    ocr_resources.close()
                    worker.join(timeout=2)
        with core.receive_lock:
            summary = core.summary()
            state = stream.stats()
            summary["stream"] = {k: v for k, v in state.items() if k != "stream_id"}
            # Includes both owner-declined frames and in-flight reads discarded
            # by LatestFrameStream on close before it calls the observer. These
            # are not an extra disjoint category of mailbox disposal counters.
            summary["stream"]["frames_discarded_unchecked"] = (
                state["frames_received"] - core.counts["frames_checked"])
            summary["worker_alive"] = worker.is_alive()
            summary["recorded_frames"] = recorder.count if recorder else 0
            errors.extend(state[key] for key in ("source_error_type", "source_close_error_type",
                                                 "observer_disconnect_error_type") if state[key])
            summary["error_types"] = sorted(set(errors))
            summary["status"] = ("failed" if errors or worker.is_alive() or core.counts["processing_errors"] else
                                 "completed" if core.counts["frames_checked"] else "no_frames")
            summary["total_call_ns_before_resource_close"] = time.monotonic_ns() - started
            summary["first_frame_from_start_ns"] = (None if core.first_received is None
                                                      else core.first_received - started)
            summary["sampling"] = dict(device_max_fps=15, requested_host_fps=sample_fps,
                                       ffmpeg_fps_filter_may_drop_or_duplicate=True,
                                       unobserved_upstream_frames="unknown")
            summary["receipt_notice"] = "Complete JPEG read at host, not device capture time"
    summary["total_call_ns"] = time.monotonic_ns() - started
    return summary
