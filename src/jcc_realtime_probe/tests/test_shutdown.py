"""Deterministic shutdown races with generated pixels and in-memory resources."""

import io
import json
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jcc_device.latest import LatestFrameStream
from jcc_realtime_overlay_bridge.bridge import RealtimeOverlayBridge
from jcc_realtime_overlay_bridge.clocks import bind_same_process
from jcc_realtime_probe.core import ProbeCore
from jcc_realtime_probe.events import EventSink
from jcc_realtime_probe.processor import OCRProcessor
from jcc_realtime_probe.profile import load_profile
from jcc_realtime_probe.runtime import run_live
from jcc_realtime_probe.timeline import png


class MemorySource:
    def __init__(self):
        self.items = queue.Queue()
        self.closed = threading.Event()
        self.close_threads = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def read_frame(self, timeout):
        try:
            item = self.items.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.close_threads.append(threading.current_thread().name)
        self.closed.set()


def profile():
    return load_profile(dict(image_size=[160, 100], input_boxes=[[10, 10, 70, 40]]), "live")


class TracedRLock:
    """Expose an attempted receiver acquisition before it can block."""

    def __init__(self, boundary):
        self.lock = threading.RLock()
        self.boundary = boundary
        self.trace = False

    def acquire(self, *args, **kwargs):
        if self.trace and threading.current_thread().name.startswith("jcc-receiver-"):
            self.boundary.set()
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


class DisconnectRaceTests(unittest.TestCase):
    def test_disconnect_serializes_after_checked_completion_and_withdraws_every_candidate(self):
        # RuntimeError exercises the read-error branch; SystemExit exercises
        # the receiver's finally directly, without an earlier disconnect.
        for failure in (RuntimeError("private-source-detail"), SystemExit()):
            with self.subTest(exit_type=type(failure).__name__):
                source, output = MemorySource(), io.StringIO()
                stream = LatestFrameStream(source, read_timeout=.01)
                sink = EventSink("a" * 16, "live", "host_monotonic_ns", file=output, processing_kind="ocr")
                core = ProbeCore(stream.stream_id, profile(), sink)
                checked, release, boundary = threading.Event(), threading.Event(), threading.Event()
                guard = core.receive_lock = TracedRLock(boundary)
                stream.bind_observer(core)
                errors = []
                check, disconnect = core.ledger.check, core.disconnect

                def checked_then_pause(lease, scope):
                    reason = check(lease, scope)
                    if scope == "inputs" and threading.current_thread().name == "controlled-completion":
                        checked.set()
                        if not release.wait(2):
                            raise AssertionError("Completion was not released")
                    return reason

                def mark_disconnect():
                    try:
                        disconnect()
                    finally:
                        # Old code arrives here without acquiring the guard;
                        # fixed code signals at the blocked acquisition above.
                        boundary.set()

                def complete():
                    try:
                        with core.receive_lock:
                            core.complete(job, report)
                    except BaseException as error:
                        errors.append(error)

                worker = threading.Thread(target=complete, daemon=True, name="controlled-completion")
                bridge = RealtimeOverlayBridge(bind_same_process(sink))
                try:
                    stream.start()
                    source.items.put(png())
                    stream.read_latest(timeout=1)
                    with core.receive_lock:
                        job = core.start_job()
                        self.assertIsNotNone(job)
                    report = OCRProcessor(core.profile, MemoryOCR(), sink.run_id)(job)
                    with patch.object(core.ledger, "check", side_effect=checked_then_pause), \
                         patch.object(core, "disconnect", side_effect=mark_disconnect):
                        worker.start()
                        self.assertTrue(checked.wait(1))
                        guard.trace = True
                        source.items.put(failure)
                        self.assertTrue(boundary.wait(1))
                        release.set()
                        worker.join(2)
                        self.assertTrue(source.closed.wait(1))
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(errors, [])
                    with core.receive_lock:
                        self.assertTrue(core.closed)
                        self.assertEqual(core.published, {})
                        self.assertEqual(core.counts["source_disconnected"], 1)
                        rows = list(map(json.loads, output.getvalue().splitlines()))
                    candidates = [r for r in rows if r["status"] == "candidate"]
                    self.assertEqual(len(candidates), 2)
                    for candidate in candidates:
                        self.assertTrue(any(r["sample_id"] == candidate["sample_id"]
                                            and r["stage"] == candidate["stage"]
                                            and r["status"] == "withdrawn" and r["reason"] == "disconnected"
                                            and r["event_index"] > candidate["event_index"] for r in rows))
                    for row in rows:
                        self.assertTrue(bridge.ingest(row), (row["event_index"], row["stage"], bridge.faulted))
                    self.assertIsNone(bridge.store.view()["message"])
                    self.assertEqual(bridge.report()["processing"]["completed_jobs_observed"], 1)
                    self.assertEqual(bridge.report()["formal_game_state_outputs"], 0)
                    self.assertNotIn("private-source-detail", output.getvalue())
                finally:
                    release.set()
                    if worker.ident is not None:
                        worker.join(2)
                    stream.close()
                    bridge.store.close()
                self.assertEqual(len(source.close_threads), 1)
                self.assertFalse(stream.stats()["receiver_thread_alive"])


class MemoryOCR:
    def __init__(self):
        self.close_threads = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.close_threads.append(threading.current_thread().name)

    def recognize(self, data):
        return [dict(text="generated-private-text", confidence=.9, x=1, y=1, width=5, height=5)]


class RuntimeCleanupTests(unittest.TestCase):
    def test_interrupted_receiver_start_keeps_source_owned_by_running_reader(self):
        source, backend, streams = MemorySource(), MemoryOCR(), []
        entered, release = threading.Event(), threading.Event()
        real_start = threading.Thread.start

        def blocked_read(timeout):
            entered.set()
            release.wait(2)
            return png()

        def create_stream(*args, **kwargs):
            stream = LatestFrameStream(*args, shutdown_timeout=.03, **kwargs)
            streams.append(stream)
            return stream

        def interrupt_receiver_after_read(thread):
            real_start(thread)
            if thread.name.startswith("jcc-receiver-"):
                if not entered.wait(1):
                    raise AssertionError("Receiver did not enter synthetic read")
                raise KeyboardInterrupt("synthetic startup interruption")

        source.read_frame = blocked_read
        try:
            with patch("jcc_realtime_probe.runtime.TabletStream", return_value=source), \
                 patch("jcc_realtime_probe.runtime.VisionOCR", return_value=backend), \
                 patch("jcc_realtime_probe.runtime.LatestFrameStream", side_effect=create_stream), \
                 patch("jcc_realtime_probe.runtime.threading.Thread.start", new=interrupt_receiver_after_read):
                with self.assertRaises(KeyboardInterrupt):
                    run_live(profile(), duration_seconds=.01, max_frames=1)
            self.assertEqual(len(streams), 1)
            self.assertTrue(streams[0].stats()["closed"])
            self.assertTrue(streams[0].stats()["receiver_thread_alive"])
            self.assertTrue(streams[0]._observer.closed)
            self.assertEqual(source.close_threads, [])
            self.assertEqual(len(backend.close_threads), 1)
        finally:
            release.set()
            for stream in streams:
                stream.close()
        self.assertEqual(len(source.close_threads), 1)
        self.assertTrue(source.close_threads[0].startswith("jcc-receiver-"))
        self.assertFalse(streams[0].stats()["receiver_thread_alive"])

    def test_ocr_thread_start_failure_joins_receiver_before_releasing_resources(self):
        source, backend, streams = MemorySource(), MemoryOCR(), []
        real_start = threading.Thread.start

        def fail_ocr(thread):
            if thread.name == "jcc-realtime-probe-ocr":
                raise RuntimeError("private-thread-start-detail")
            return real_start(thread)

        def create_stream(*args, **kwargs):
            stream = LatestFrameStream(*args, **kwargs)
            streams.append(stream)
            return stream

        try:
            with patch("jcc_realtime_probe.runtime.TabletStream", return_value=source), \
                 patch("jcc_realtime_probe.runtime.VisionOCR", return_value=backend), \
                 patch("jcc_realtime_probe.runtime.LatestFrameStream", side_effect=create_stream), \
                 patch("jcc_realtime_probe.runtime.threading.Thread.start", new=fail_ocr):
                with self.assertRaisesRegex(RuntimeError, "private-thread-start-detail"):
                    run_live(profile(), duration_seconds=.01, max_frames=1)
            self.assertEqual(len(streams), 1)
            self.assertFalse(streams[0].stats()["receiver_thread_alive"])
            self.assertTrue(streams[0].stats()["closed"])
            self.assertTrue(streams[0]._observer.closed)
            self.assertEqual(len(source.close_threads), 1)
            self.assertTrue(source.close_threads[0].startswith("jcc-receiver-"))
            self.assertEqual(len(backend.close_threads), 1)
        finally:
            for stream in streams:
                stream.close()

    def test_thread_construction_and_start_failures_close_the_source_once(self):
        for role in ("receiver", "ocr"):
            for phase in ("construct", "start"):
                for error_type in (RuntimeError, KeyboardInterrupt):
                    with self.subTest(role=role, phase=phase, error_type=error_type.__name__):
                        source, backend, streams, threads = MemorySource(), MemoryOCR(), [], []
                        real_thread = threading.Thread

                        def create_thread(*args, **kwargs):
                            name = kwargs.get("name", "")
                            matched = name.startswith("jcc-receiver-") if role == "receiver" else name == "jcc-realtime-probe-ocr"
                            if matched and phase == "construct":
                                raise error_type("private-start-detail")
                            thread = real_thread(*args, **kwargs)
                            threads.append(thread)
                            if matched and phase == "start":
                                def fail_start():
                                    raise error_type("private-start-detail")
                                thread.start = fail_start
                            return thread

                        def create_stream(*args, **kwargs):
                            stream = LatestFrameStream(*args, **kwargs)
                            streams.append(stream)
                            return stream

                        try:
                            with patch("jcc_realtime_probe.runtime.TabletStream", return_value=source), \
                                 patch("jcc_realtime_probe.runtime.VisionOCR", return_value=backend), \
                                 patch("jcc_realtime_probe.runtime.LatestFrameStream", side_effect=create_stream), \
                                 patch("jcc_realtime_probe.runtime.threading.Thread", side_effect=create_thread):
                                with self.assertRaises(error_type):
                                    run_live(profile(), duration_seconds=.01, max_frames=1)
                            self.assertTrue(streams[0].stats()["closed"])
                            self.assertTrue(streams[0]._observer.closed)
                            self.assertFalse(any(t.is_alive() for t in threads))
                            self.assertEqual(len(source.close_threads), 1)
                            self.assertEqual(source.close_threads[0].startswith("jcc-receiver-"), role == "ocr")
                            self.assertEqual(len(backend.close_threads), 1)
                        finally:
                            for stream in streams:
                                stream.close()

    def test_setup_failure_before_and_after_source_handoff_closes_once(self):
        for seam in ("LatestFrameStream", "ProbeCore", "OCRProcessor"):
            with self.subTest(seam=seam):
                source, backend = MemorySource(), MemoryOCR()
                with patch("jcc_realtime_probe.runtime.TabletStream", return_value=source), \
                     patch("jcc_realtime_probe.runtime.VisionOCR", return_value=backend), \
                     patch("jcc_realtime_probe.runtime." + seam, side_effect=ValueError("private-setup")):
                    with self.assertRaises(ValueError):
                        run_live(profile(), duration_seconds=.01, max_frames=1)
                self.assertEqual(source.close_threads, [threading.current_thread().name])
                self.assertEqual(len(backend.close_threads), 1)

    def test_timeout_only_source_finishes_as_no_frames_and_closes_on_receiver(self):
        reads = threading.Event()

        class TimeoutOnly(MemorySource):
            def read_frame(self, timeout):
                reads.set()
                raise TimeoutError("private-delay")

        class WaitForRead(LatestFrameStream):
            def start(self):
                super().start()
                if not reads.wait(1):
                    raise AssertionError("Timeout source was not read")
                return self

        source, backend = TimeoutOnly(), MemoryOCR()
        # Advance only the controller's clock over the no-first-frame deadline;
        # receiver timestamps and source timeouts still use the real clock.
        ticks = iter(range(0, 1_000_000_000_000, 20_000_000_000))
        with patch("jcc_realtime_probe.runtime.TabletStream", return_value=source), \
             patch("jcc_realtime_probe.runtime.VisionOCR", return_value=backend), \
             patch("jcc_realtime_probe.runtime.LatestFrameStream", WaitForRead), \
             patch("jcc_realtime_probe.runtime.time", SimpleNamespace(monotonic_ns=lambda: next(ticks))):
            result = run_live(profile(), duration_seconds=.01, max_frames=1)
        self.assertEqual(result["status"], "no_frames")
        self.assertEqual(result["error_types"], [])
        self.assertEqual(result["stream"]["frames_received"], 0)
        self.assertGreater(result["stream"]["source_timeouts"], 0)
        self.assertFalse(result["worker_alive"])
        self.assertFalse(result["stream"]["receiver_thread_alive"])
        self.assertEqual(result["counts"]["source_disconnected"], 1)
        self.assertEqual(len(source.close_threads), 1)
        self.assertTrue(source.close_threads[0].startswith("jcc-receiver-"))
        self.assertEqual(len(backend.close_threads), 1)
        self.assertNotIn("private-delay", json.dumps(result))

    def test_source_close_failure_is_reported_without_retry_or_live_threads(self):
        class CloseFailure(MemorySource):
            def close(self):
                super().close()
                raise OSError("private-close-detail")

        source, backend = CloseFailure(), MemoryOCR()
        source.items.put(png())
        with patch("jcc_realtime_probe.runtime.TabletStream", return_value=source), \
             patch("jcc_realtime_probe.runtime.VisionOCR", return_value=backend):
            result = run_live(profile(), duration_seconds=.01, max_frames=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stream"]["source_close_error_type"], "OSError")
        self.assertEqual(result["stream"]["close_errors"], 1)
        self.assertEqual(len(source.close_threads), 1)
        self.assertFalse(result["worker_alive"])
        self.assertFalse(result["stream"]["receiver_thread_alive"])
        self.assertNotIn("private-close-detail", json.dumps(result))

    def test_forced_ocr_shutdown_closes_backend_once_after_source_and_joins_worker(self):
        source = MemorySource()
        source.items.put(png())
        entered, cancelled = threading.Event(), threading.Event()
        joins = []

        class BlockingOCR(MemoryOCR):
            def recognize(self, data):
                entered.set()
                if not cancelled.wait(2):
                    raise AssertionError("OCR was not cancelled")
                raise RuntimeError("private-cancel-detail")

            def close(self):
                if not source.closed.is_set():
                    raise AssertionError("Source must close before OCR resources")
                super().close()
                cancelled.set()

        backend = BlockingOCR()
        real_thread = threading.Thread

        def create_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            if thread.name == "jcc-realtime-probe-ocr":
                real_start, real_join = thread.start, thread.join

                def start():
                    real_start()
                    if not entered.wait(1):
                        raise AssertionError("Controlled OCR did not start")

                def join(timeout=None):
                    joins.append(timeout)
                    # Real worker and real cancellation; shorten only the
                    # first cleanup grace period, not the tested state order.
                    real_join(.01 if timeout == 12 else timeout)

                thread.start, thread.join = start, join
            return thread

        with patch("jcc_realtime_probe.runtime.TabletStream", return_value=source), \
             patch("jcc_realtime_probe.runtime.VisionOCR", return_value=backend), \
             patch("jcc_realtime_probe.runtime.threading.Thread", side_effect=create_thread):
            result = run_live(profile(), duration_seconds=.01, max_frames=1)
        self.assertEqual(joins, [12, 2])
        self.assertIn("WorkerShutdownTimeout", result["error_types"])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["worker_alive"])
        self.assertFalse(result["stream"]["receiver_thread_alive"])
        self.assertEqual(len(source.close_threads), 1)
        self.assertEqual(len(backend.close_threads), 1)
        self.assertNotIn("private-cancel-detail", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
