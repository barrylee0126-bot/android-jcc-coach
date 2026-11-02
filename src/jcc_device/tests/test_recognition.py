from concurrent.futures import ThreadPoolExecutor
import io
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from jcc_device.latest import Frame, FrameRef
from jcc_device.recognition import (FrameProcessor, FrameResult, LatestResultGate,
                                    PixelROI, recognize_regions)


def frame(sequence=1, *, stream_id="test-session", age_ns=0, jpeg=b"synthetic"):
    return Frame(FrameRef(stream_id, sequence, time.monotonic_ns() - age_ns,
                          "2026-11-02T00:00:00+00:00"), jpeg)


class RecognitionTests(unittest.TestCase):
    def test_late_old_result_cannot_replace_new_input_or_new_result(self):
        gate = LatestResultGate(max_age_seconds=5)
        old = frame()
        gate.advance(old.source)
        started, release = threading.Event(), threading.Event()

        def slow(_):
            started.set()
            self.assertTrue(release.wait(2))
            return "old"

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(FrameProcessor(slow).process, old)
            self.assertTrue(started.wait(1))
            try:
                newest = frame(2)
                self.assertTrue(gate.advance(newest.source))
                # Even before a new result exists, the prior input is invalid.
                self.assertIsNone(gate.get())
                fresh_result = FrameProcessor(lambda _: "new").process(newest)
                self.assertTrue(gate.publish(fresh_result))
            finally:
                release.set()
            self.assertFalse(gate.publish(pending.result(1)))
        self.assertEqual(gate.get().value, "new")
        self.assertEqual(gate.stats()["results_outdated"], 1)
        self.assertFalse(gate.publish(fresh_result))

    def test_advancing_invalidates_result_and_rejects_rewind_or_other_session(self):
        gate = LatestResultGate(max_age_seconds=5)
        first = frame()
        gate.advance(first.source)
        self.assertTrue(gate.publish(FrameProcessor(lambda _: "value").process(first)))
        self.assertIsNotNone(gate.get())
        second = frame(2)
        self.assertTrue(gate.advance(second.source))
        self.assertIsNone(gate.get())
        self.assertFalse(gate.advance(first.source))
        self.assertFalse(gate.advance(frame(3, stream_id="other-session").source))
        self.assertFalse(gate.advance(frame(3, age_ns=10_000_000_000).source))

    def test_expired_result_rejected_and_accepted_result_expires_on_read(self):
        gate = LatestResultGate(max_age_seconds=0.5)
        old = frame(age_ns=1_000_000_000)
        gate.advance(old.source)
        self.assertFalse(gate.publish(FrameProcessor(lambda _: 1).process(old)))
        self.assertEqual(gate.stats()["results_expired"], 1)
        newest = frame(2)
        gate.advance(newest.source)
        self.assertTrue(gate.publish(FrameProcessor(lambda _: 2).process(newest)))
        with patch("jcc_device.recognition.time.monotonic_ns",
                   return_value=newest.source.host_received_monotonic_ns + 600_000_000):
            self.assertIsNone(gate.get())
        self.assertEqual(gate.stats()["results_expired"], 2)

    def test_closed_gate_rejects_inflight_result(self):
        gate = LatestResultGate(max_age_seconds=1)
        current = frame()
        gate.advance(current.source)
        result = FrameProcessor(lambda _: 1).process(current)
        gate.close()
        self.assertFalse(gate.publish(result))
        self.assertFalse(gate.advance(frame(2).source))
        self.assertIsNone(gate.get())

    def test_inconsistent_or_future_processing_times_rejected(self):
        gate = LatestResultGate(max_age_seconds=1)
        current = frame()
        gate.advance(current.source)
        received = current.source.host_received_monotonic_ns
        for started, finished in ((received - 1, received), (received + 1, received),
                                   (received, time.monotonic_ns() + 10_000_000_000)):
            self.assertFalse(gate.publish(FrameResult(current.source, started, finished, 1)))

    def test_processor_counts_failures_without_marking_processed(self):
        def fail(_):
            raise ValueError("bad recognition")

        processor = FrameProcessor(fail)
        with self.assertRaises(ValueError):
            processor.process(frame())
        self.assertEqual(processor.stats()["frames_started"], 1)
        self.assertEqual(processor.stats()["frames_processed"], 0)
        self.assertEqual(processor.stats()["processing_errors"], 1)

    def test_processor_times_include_receive_age_but_not_device_latency(self):
        source = frame(age_ns=1_000_000_000)
        result = FrameProcessor(lambda f: len(f.jpeg)).process(source)
        self.assertGreaterEqual(result.timing()["receive_to_finish_seconds"], 1)
        self.assertGreaterEqual(result.timing()["processing_seconds"], 0)
        self.assertIsNone(result.timing()["device_capture_time"])


class ROIHookTests(unittest.TestCase):
    def setUp(self):
        with Image.new("RGB", (100, 80), "white") as image, io.BytesIO() as encoded:
            image.save(encoded, format="JPEG")
            self.input = frame(jpeg=encoded.getvalue())
        self.calls = []

        class FakeOCR:
            def recognize(inner, image_bytes):
                with Image.open(io.BytesIO(image_bytes)) as image:
                    self.calls.append((image.size, image.format))
                return [{"text": "42", "x": 1, "y": 2, "width": 3,
                         "height": 4, "confidence": 0.75}]

        self.ocr = FakeOCR()

    def test_crop_stays_in_memory_and_boxes_map_to_full_frame(self):
        roi = PixelROI("synthetic-region", (100, 80), (10, 20, 30, 50))
        result = recognize_regions(self.input, [roi], self.ocr)[0]
        self.assertEqual(self.calls, [((20, 30), "PNG")])
        self.assertEqual((result["items"][0]["x"], result["items"][0]["y"]), (11, 22))
        self.assertEqual(result["items"][0]["confidence"], 0.75)
        self.assertEqual(result["source"]["frame_id"], self.input.source.frame_id)
        self.assertNotIn("gold", result)

    def test_bad_dimensions_bounds_empty_and_duplicates_rejected_before_ocr(self):
        valid = PixelROI("synthetic-region", (100, 80), (10, 20, 30, 50))
        cases = ([], [valid, valid],
                 [PixelROI("bad", (80, 100), (10, 20, 30, 50))],
                 [PixelROI("bad", (100, 80), (-1, 0, 30, 50))],
                 [PixelROI("bad", (100, 80), (0, 0, 101, 50))],
                 [PixelROI("bad", (100, 80), (10, 20, 10, 50))])
        for regions in cases:
            with self.subTest(regions=regions), self.assertRaises(ValueError):
                recognize_regions(self.input, regions, self.ocr)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
