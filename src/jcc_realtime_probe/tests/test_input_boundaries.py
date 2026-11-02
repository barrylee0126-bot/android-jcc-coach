"""In-memory regressions at the OCR, receive-owner and event boundaries."""

import io
import json
import queue
import threading
import time
import unittest
from unittest.mock import patch

from jcc_device.latest import Frame, FrameRef, LatestFrameStream
from jcc_realtime_overlay_bridge.clocks import bind_same_process
from jcc_realtime_probe.core import ProbeCore
from jcc_realtime_probe.events import EventSink
from jcc_realtime_probe.processor import OCRProcessor
from jcc_realtime_probe.profile import load_profile
from jcc_realtime_probe.runtime import run_live
from jcc_realtime_probe.timeline import VirtualClock, png


def text_item(**updates):
    return dict(dict(text="private-ocr-text", confidence=.9, x=1, y=1, width=5, height=5), **updates)


def input_profile(route="raw", **updates):
    data = dict(image_size=[160, 100], input_boxes=[[10, 10, 70, 40]] if route == "raw" else [])
    if route == "cue":
        data["cues"] = [dict(cue_id="round_header", box=[10, 10, 70, 40])]
    if route == "number":
        data["numeric_fields"] = [dict(field_name="gold", box=[10, 10, 70, 40])]
    data.update(updates)
    return load_profile(data, "live")


class OCRBoundaryTests(unittest.TestCase):
    def probe(self, payload, *, route="raw", profile=None):
        class OCR:
            def recognize(self, data):
                if isinstance(payload, Exception):
                    raise payload
                return payload

        output = io.StringIO()
        sink = EventSink("a" * 16, "live", "host_monotonic_ns", file=output, processing_kind="ocr")
        profile = profile or input_profile(route)
        core = ProbeCore("private-stream-id", profile, sink)
        with core.receive_lock:
            core.observe_frame(Frame(FrameRef(core.stream_id, 1, time.monotonic_ns(), "private-utc"), png()))
            job = core.start_job()
            report = OCRProcessor(profile, OCR(), sink.run_id)(job)
            core.complete(job, report)
            core.disconnect()
            summary = core.summary()
        rows = list(map(json.loads, output.getvalue().splitlines()))
        self.assertEqual(summary["counts"]["formal_accepted"], 0)
        self.assertEqual(summary["formal_game_state_outputs"], 0)
        serialized = output.getvalue() + json.dumps(summary)
        for secret in ("private-ocr-text", "private-stream-id", "private-utc", "private-error-text"):
            self.assertNotIn(secret, serialized)
        return report, rows

    def test_invalid_items_never_count_as_text_on_any_ocr_route(self):
        cases = {
            "null_item": None, "wrong_item_type": "private-ocr-text", "missing_fields": {},
            "empty_text": text_item(text=""), "whitespace": text_item(text=" \t\n"),
            "non_string_text": text_item(text=42), "missing_score": {"text": "private-ocr-text"},
            "zero_width": text_item(width=0), "negative_height": text_item(height=-1),
            "negative_x": text_item(x=-1), "outside_crop": text_item(x=58),
            "outside_crop_y": text_item(y=28), "nan_coordinate": text_item(x=float("nan")),
            "infinite_size": text_item(width=float("inf")), "boolean_coordinate": text_item(x=True),
            "nan_score": text_item(confidence=float("nan")),
            "high_score": text_item(confidence=1.01), "boolean_score": text_item(confidence=True),
        }
        for route in ("raw", "cue", "number"):
            for name, item in cases.items():
                with self.subTest(route=route, case=name):
                    report, rows = self.probe([item], route=route)
                    self.assertEqual(report["ocr_items_count"], 0)
                    self.assertEqual(report["ocr_requests"], 1)
                    self.assertFalse(any(r["status"] == "candidate" for r in rows))

    def test_mixed_list_counts_only_valid_items_without_rewriting_10g_input(self):
        for route in ("raw", "cue", "number"):
            with self.subTest(route=route):
                report, rows = self.probe([None, text_item(), text_item(text=" ")], route=route)
                self.assertEqual(report["ocr_items_count"], 1)
                self.assertTrue(any(r["stage"] == "pixel_inputs" and r["status"] == "candidate" for r in rows))
                if route == "cue":
                    self.assertEqual(report["scene"]["cues"]["round_header"]["reason"], "ambiguous_ocr_items")

    def test_empty_invalid_response_and_exception_remain_distinct(self):
        for payload, outcome, errors in (([], "ocr_empty", 0), (None, "ocr_error", 1),
                                         ({"error": "private-error-text"}, "ocr_error", 1),
                                         (RuntimeError("private-error-text"), "ocr_error", 1)):
            with self.subTest(outcome=outcome, response_type=type(payload).__name__):
                report, rows = self.probe(payload)
                self.assertEqual((report["processing_outcome"], report["ocr_errors"]), (outcome, errors))
                self.assertEqual(report["ocr_items_count"], 0)
                self.assertFalse(any(r["status"] == "candidate" for r in rows))

    def test_geometry_uses_each_actual_scaled_ocr_input(self):
        for route in ("cue", "number"):
            key = "cues" if route == "cue" else "numeric_fields"
            spec = dict(box=[10, 10, 20, 20], scale=2)
            spec.update(dict(cue_id="round_header") if route == "cue" else dict(field_name="gold"))
            profile = input_profile(route, **{key: [spec]})
            with self.subTest(route=route, box="inside_scaled_input"):
                report, _ = self.probe([text_item(x=12, y=12, width=5, height=5)], profile=profile)
                self.assertEqual(report["ocr_items_count"], 1)
            with self.subTest(route=route, box="outside_scaled_input"):
                report, rows = self.probe([text_item(x=18, y=12, width=5, height=5)], profile=profile)
                self.assertEqual(report["ocr_items_count"], 0)
                self.assertFalse(any(r["status"] == "candidate" for r in rows))


class MemorySource:
    def __init__(self):
        self.items = queue.Queue()
        self.close_threads = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def read_frame(self, timeout):
        try:
            return self.items.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None

    def close(self):
        self.close_threads.append(threading.current_thread().name)


class MemoryRecorder:
    def __init__(self):
        self.sequences = []

    @property
    def count(self):
        return len(self.sequences)

    def record(self, frame):
        self.sequences.append(frame.source.sequence)

    def close(self):
        pass


class MemoryOCR:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        pass

    def recognize(self, data):
        return [text_item()]


def eventually(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("In-memory receive did not reach the expected boundary")
        threading.Event().wait(.001)


class ReceiveLimitTests(unittest.TestCase):
    def test_fast_source_obeys_hard_check_and_recording_limit(self):
        for cap in (1, 3):
            with self.subTest(max_frames=cap):
                class Burst(MemorySource):
                    def __init__(self):
                        super().__init__()
                        self.drained = threading.Event()
                        for _ in range(8):
                            self.items.put(png())

                    def read_frame(self, timeout):
                        if self.items.empty():
                            self.drained.set()
                        return super().read_frame(min(timeout, .01))

                source, recorder, output = Burst(), MemoryRecorder(), io.StringIO()

                class WaitForBurst(LatestFrameStream):
                    def start(self):
                        super().start()
                        if not source.drained.wait(2):
                            self.close()
                            raise AssertionError("In-memory burst did not drain")
                        return self

                with patch("jcc_realtime_probe.runtime.TabletStream", return_value=source), \
                     patch("jcc_realtime_probe.runtime.VisionOCR", return_value=MemoryOCR()), \
                     patch("jcc_realtime_probe.runtime.Recorder", return_value=recorder), \
                     patch("jcc_realtime_probe.runtime.LatestFrameStream", WaitForBurst):
                    result = run_live(input_profile(), duration_seconds=.1, max_frames=cap,
                                      recording_dir="in-memory-recorder", events_file=output)
                self.assertEqual(result["counts"]["frames_checked"], cap)
                self.assertEqual(result["recorded_frames"], cap)
                self.assertEqual(recorder.sequences, list(range(1, cap + 1)))
                self.assertEqual(result["stream"]["frames_received"], 8)
                self.assertEqual(result["stream"]["frames_discarded_unchecked"], 8 - cap)
                self.assertEqual(result["counts"]["frames_declined_unchecked"], 8 - cap)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["error_types"], [])
                self.assertFalse(result["worker_alive"])
                self.assertFalse(result["stream"]["receiver_thread_alive"])
                rows = list(map(json.loads, output.getvalue().splitlines()))
                self.assertEqual(sum(r["stage"] == "receive" and r["status"] == "received" for r in rows), cap)

    def test_limit_retracts_published_result_before_external_close(self):
        source, recorder, output = MemorySource(), MemoryRecorder(), io.StringIO()
        stream = LatestFrameStream(source, read_timeout=.01)
        sink = EventSink("b" * 16, "live", "host_monotonic_ns", file=output, processing_kind="ocr")
        core = ProbeCore(stream.stream_id, input_profile(), sink, recorder=recorder, max_frames=1)
        stream.bind_observer(core)
        stream.start()
        try:
            source.items.put(png())
            stream.read_latest(timeout=1)
            with core.receive_lock:
                job = core.start_job()
                core.complete(job, OCRProcessor(core.profile, MemoryOCR(), sink.run_id)(job))
                self.assertEqual(len(core.published), 2)
            source.items.put(png(inside=1))
            eventually(lambda: core.closed)
            with core.receive_lock:
                self.assertEqual(core.counts["frames_checked"], 1)
                self.assertEqual(core.counts["frames_declined_unchecked"], 1)
                self.assertEqual(recorder.count, 1)
                self.assertEqual(core.published, {})
                self.assertIsNone(core.start_job())
                rows = list(map(json.loads, output.getvalue().splitlines()))
                withdrawn = [r for r in rows if r["status"] == "withdrawn"]
                self.assertEqual(len(withdrawn), 2)
                self.assertTrue(all(r["reason"] == "disconnected" for r in withdrawn))
            self.assertTrue(stream.stats()["receiver_thread_alive"])
        finally:
            stream.close()
        self.assertFalse(stream.stats()["receiver_thread_alive"])
        self.assertEqual(stream.stats()["source_errors"], 0)
        self.assertEqual(len(source.close_threads), 1)
        self.assertTrue(source.close_threads[0].startswith("jcc-receiver-"))

    def test_active_result_cannot_publish_after_limit_disconnect(self):
        output = io.StringIO()
        sink = EventSink("c" * 16, "live", "host_monotonic_ns", file=output, processing_kind="ocr")
        core = ProbeCore("memory", input_profile(), sink, max_frames=1)
        with core.receive_lock:
            core.observe_frame(Frame(FrameRef("memory", 1, time.monotonic_ns(), "private"), png()))
            job = core.start_job()
            core.observe_frame(Frame(FrameRef("memory", 2, time.monotonic_ns(), "private"), png()))
            core.complete(job, OCRProcessor(core.profile, MemoryOCR(), sink.run_id)(job))
        self.assertTrue(core.closed)
        self.assertEqual(core.counts["jobs_completed"], 1)
        self.assertEqual(core.counts["formal_accepted"], 0)
        self.assertEqual(core.published, {})
        self.assertFalse(any(r["status"] == "candidate" for r in map(json.loads, output.getvalue().splitlines())))

    def test_close_racing_with_limit_callback_waits_then_exits_without_deadlock(self):
        source, output = MemorySource(), io.StringIO()
        stream = LatestFrameStream(source, read_timeout=.01)
        sink = EventSink("d" * 16, "live", "host_monotonic_ns", file=output, processing_kind="ocr")
        core = ProbeCore(stream.stream_id, input_profile(), sink, max_frames=1)
        stream.bind_observer(core)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        disconnect = core.disconnect

        def held_disconnect():
            if threading.current_thread().name.startswith("jcc-receiver-") and not entered.is_set():
                entered.set()
                if not release.wait(2):
                    raise AssertionError("Limit callback was not released")
            disconnect()

        def close_stream():
            try:
                stream.close()
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                finished.set()

        stream.start()
        closer = threading.Thread(target=close_stream, daemon=True)
        try:
            source.items.put(png())
            stream.read_latest(timeout=1)
            with core.receive_lock:
                job = core.start_job()
                core.complete(job, OCRProcessor(core.profile, MemoryOCR(), sink.run_id)(job))
            with patch.object(core, "disconnect", side_effect=held_disconnect):
                source.items.put(png())
                self.assertTrue(entered.wait(1))
                closer.start()
                eventually(lambda: stream.stats()["closed"])
                self.assertFalse(finished.is_set())
                release.set()
                closer.join(2)
            self.assertFalse(closer.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(core.published, {})
            self.assertFalse(stream.stats()["receiver_thread_alive"])
            self.assertEqual(core.counts["frames_checked"], 1)
            self.assertEqual(len(source.close_threads), 1)
        finally:
            release.set()
            if closer.ident is not None:
                closer.join(2)
            stream.close()

    def test_shutdown_counts_in_flight_read_discarded_before_observer(self):
        class InFlight(MemorySource):
            def __init__(self):
                super().__init__()
                self.waiting, self.reads, self.stream = threading.Event(), 0, None

            def read_frame(self, timeout):
                self.reads += 1
                if self.reads == 1:
                    return png()
                self.waiting.set()
                deadline = time.monotonic() + timeout
                while not self.stream.stats()["closed"]:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    threading.Event().wait(.001)
                return png()

        source = InFlight()

        class WaitForRead(LatestFrameStream):
            def start(self):
                source.stream = self
                super().start()
                if not source.waiting.wait(2):
                    self.close()
                    raise AssertionError("Second read did not start")
                return self

        with patch("jcc_realtime_probe.runtime.TabletStream", return_value=source), \
             patch("jcc_realtime_probe.runtime.VisionOCR", return_value=MemoryOCR()), \
             patch("jcc_realtime_probe.runtime.LatestFrameStream", WaitForRead):
            result = run_live(input_profile(), duration_seconds=.1, max_frames=1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stream"]["frames_received"], 2)
        self.assertEqual(result["counts"]["frames_checked"], 1)
        self.assertEqual(result["counts"]["frames_declined_unchecked"], 0)
        self.assertEqual(result["stream"]["frames_discarded_unchecked"], 1)
        self.assertFalse(result["worker_alive"])
        self.assertFalse(result["stream"]["receiver_thread_alive"])

    def test_non_integer_frame_limit_fails_before_opening_resources(self):
        for cap in (True, 1.5, 0, 241):
            with self.subTest(max_frames=cap):
                with patch("jcc_realtime_probe.runtime.VisionOCR") as ocr:
                    with self.assertRaises(ValueError):
                        run_live(input_profile(), max_frames=cap)
                    ocr.assert_not_called()


class EventClockBoundaryTests(unittest.TestCase):
    def test_source_clock_support_matrix_at_producer_and_consumer_binding(self):
        supported = {("synthetic", "virtual_monotonic_ns"), ("synthetic", "host_monotonic_ns"),
                     ("replay", "virtual_monotonic_ns"), ("replay", "host_monotonic_ns"),
                     ("live", "host_monotonic_ns")}
        for source in ("synthetic", "replay", "live"):
            for kind in ("virtual_monotonic_ns", "host_monotonic_ns"):
                with self.subTest(source=source, clock_kind=kind):
                    clock = time.monotonic_ns if kind == "host_monotonic_ns" else VirtualClock()
                    output = io.StringIO()
                    args = ("e" * 16, source, kind, clock, output)
                    if (source, kind) not in supported:
                        with self.assertRaises(ValueError):
                            EventSink(*args, processing_kind="ocr")
                        self.assertEqual(output.getvalue(), "")
                        continue
                    sink = EventSink(*args, processing_kind="ocr")
                    binding = bind_same_process(sink)
                    event = sink.emit(FrameRef("private", 1, clock(), "private"),
                                      "receive", "received", "frame_checked")
                    decoded = json.loads(output.getvalue())
                    self.assertEqual(decoded, event)
                    self.assertEqual((decoded["source_kind"], decoded["clock_kind"]),
                                     (binding.source_kind, binding.clock_kind))
                    self.assertIs(binding.clock, clock)
                    self.assertEqual(sink.count, 1)

    def test_mutated_unsupported_pair_is_rejected_before_writing(self):
        output = io.StringIO()
        sink = EventSink("f" * 16, "synthetic", "host_monotonic_ns", file=output, processing_kind="ocr")
        sink.source_kind = "live"
        sink.clock_kind = "virtual_monotonic_ns"
        with self.assertRaises(ValueError):
            sink.emit(FrameRef("private", 1, time.monotonic_ns(), "private"),
                      "receive", "received", "frame_checked")
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(sink.count, 0)

    def test_host_label_with_virtual_callable_fails_at_consumer_binding(self):
        sink = EventSink("f" * 16, "synthetic", "host_monotonic_ns", VirtualClock(), processing_kind="ocr")
        with self.assertRaisesRegex(ValueError, "host_clock_callable_mismatch"):
            bind_same_process(sink)


if __name__ == "__main__":
    unittest.main()
