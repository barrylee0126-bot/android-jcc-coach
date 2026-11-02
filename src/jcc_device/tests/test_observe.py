"""Offline integration: real latest-frame pump, generated pixels, fake OCR."""

import asyncio
from contextlib import redirect_stdout
import importlib
import io
import json
import sys
import types
import unittest
from unittest.mock import patch

from PIL import Image

from jcc_device.stream_probe import PacedFakeSource


class FakeMonitor:
    def __init__(self, size, regions, thresholds):
        self.calls = 0

    def observe(self, image):
        self.calls += 1
        return [types.SimpleNamespace(triggered=self.calls > 1)]


with patch.dict(sys.modules, {"src.frame_monitor": types.SimpleNamespace(FrameMonitor=FakeMonitor)}):
    observe_module = importlib.import_module("jcc_device.observe")


class ObserveIntegrationTests(unittest.TestCase):
    def test_offline_observe_retains_old_metrics_and_adds_source_timing(self):
        with Image.new("RGB", (100, 60), "white") as image, io.BytesIO() as encoded:
            image.save(encoded, format="JPEG")
            jpeg = encoded.getvalue()

        class Source(PacedFakeSource):
            def __init__(self, sample_fps, capture_diagnostics):
                self.sample_fps = sample_fps
                super().__init__(sample_fps)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

            def read_frame(self, timeout=10):
                super().read_frame(timeout)
                return jpeg

        class FakeOCR:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def recognize(self, image):
                return [{"text": "synthetic text"}]

        with patch.object(observe_module, "TabletStream", Source), \
                patch.object(observe_module, "VisionOCR", FakeOCR), \
                patch.object(observe_module, "FrameMonitor", FakeMonitor):
            report = asyncio.run(observe_module.observe(3, 50))
        self.assertEqual(report["frames"], 3)
        self.assertEqual(report["screen_size"], [100, 60])
        self.assertEqual(report["generic_change_events"], 2)
        self.assertEqual(report["ocr_text_count"], 1)
        self.assertEqual(report["change_processing_stats"]["frames_processed"], 3)
        self.assertEqual(report["ocr_processing_stats"]["frames_processed"], 1)
        self.assertEqual(report["capture_stats"]["frames_delivered"], 4)
        self.assertFalse(report["capture_stats"]["receiver_thread_alive"])
        self.assertIsNone(report["ocr_frame_timing"]["device_capture_time"])
        self.assertNotIn("synthetic text", json.dumps(report))

    def test_read_only_transport_compatibility_and_control_rejection(self):
        source = types.SimpleNamespace(read_frame=lambda: b"frame")
        transport = observe_module.ReadOnlyTransport(source)
        self.assertEqual(asyncio.run(transport.exec_out("screencap -p")), b"frame")
        for command, decode in (("input tap 1 2", False), ("screencap -p", True)):
            with self.assertRaises(RuntimeError):
                asyncio.run(transport.exec_out(command, decode))
        with self.assertRaises(RuntimeError):
            asyncio.run(transport.shell("input tap 1 2"))

    def test_cli_failure_is_structured_without_source_message(self):
        async def fail(*_):
            raise TimeoutError("private-device-identifier")

        output = io.StringIO()
        with patch.object(observe_module, "observe", fail), \
                patch.object(sys, "argv", ["observe", "--frames", "2"]), redirect_stdout(output):
            self.assertEqual(observe_module.main(), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["error_type"], "TimeoutError")
        self.assertNotIn("private-device", output.getvalue())


if __name__ == "__main__":
    unittest.main()
