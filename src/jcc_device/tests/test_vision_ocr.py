"""Exercise the pipe protocol with fake workers, not game OCR accuracy."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from jcc_device.vision_ocr import VisionOCR


READ_REQUEST = '''
import json, struct, sys, time
def read_exact(n):
    data = b""
    while len(data) < n:
        part = sys.stdin.buffer.read(n - len(data))
        if not part:
            raise SystemExit
        data += part
    return data
while True:
    header = read_exact(4)
    image = read_exact(struct.unpack(">I", header)[0])
'''


class OCRProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jcc-ocr-test-")
        self.addCleanup(self.temp.cleanup)
        self.worker = Path(self.temp.name) / "fake-worker"
        self.patch = patch("jcc_device.vision_ocr.WORKER", self.worker)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def script(self, body):
        self.worker.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        self.worker.chmod(0o700)

    def echo_worker(self):
        self.script(READ_REQUEST + '''
    time.sleep(0.01)
    print(json.dumps({"results": [{"text": image.decode("ascii")}]}), flush=True)
''')

    def test_concurrent_requests_are_serialized_and_keep_image_identity(self):
        self.echo_worker()
        with VisionOCR(timeout=2) as ocr, ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(ocr.recognize, b"A")
            b = executor.submit(ocr.recognize, b"B")
            self.assertEqual(a.result(2), [{"text": "A"}])
            self.assertEqual(b.result(2), [{"text": "B"}])

    def test_partial_response_timeout_invalidates_worker_then_requires_reopen(self):
        self.script(READ_REQUEST + '''
    sys.stdout.write('{"results":')
    sys.stdout.flush()
    time.sleep(3)
''')
        with VisionOCR(timeout=0.15) as ocr:
            worker = ocr._worker
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                ocr.recognize(b"old-image")
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertIsNone(ocr._worker)
            self.assertIsNotNone(worker.poll())
            with self.assertRaises(RuntimeError):
                ocr.recognize(b"new-image")
        self.echo_worker()
        # Reopening starts a cold Python process; it has a separate deadline
        # from the deliberately short request used to exercise timeout above.
        ocr.timeout = 1
        with ocr:
            self.assertEqual(ocr.recognize(b"new-image"), [{"text": "new-image"}])

    def test_deadline_also_covers_blocked_input_pipe(self):
        self.script("import time\ntime.sleep(3)\n")
        with VisionOCR(timeout=0.15) as ocr:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                ocr.recognize(b"x" * (2 * 1024 * 1024))
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertIsNone(ocr._worker)

    def test_eof_invalid_json_and_invalid_shape_retire_worker(self):
        bodies = (READ_REQUEST + "    raise SystemExit\n",
                  READ_REQUEST + "    print('not-json', flush=True)\n",
                  READ_REQUEST + "    print('[]', flush=True)\n",
                  READ_REQUEST + "    print('{\"results\": null}', flush=True)\n")
        for body in bodies:
            with self.subTest(body=body[-55:]):
                self.script(body)
                with VisionOCR(timeout=1) as ocr:
                    with self.assertRaises((RuntimeError, ValueError)):
                        ocr.recognize(b"image")
                    self.assertIsNone(ocr._worker)

    def test_close_interrupts_waiting_request_and_is_idempotent(self):
        marker = Path(self.temp.name) / "request-started"
        self.script(READ_REQUEST + f'''
    from pathlib import Path
    Path({str(marker)!r}).touch()
    time.sleep(3)
''')
        ocr = VisionOCR(timeout=5).__enter__()
        self.addCleanup(ocr.close)
        worker = ocr._worker
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(ocr.recognize, b"image")
            deadline = time.monotonic() + 1
            while not marker.exists():
                if time.monotonic() >= deadline:
                    self.fail("Fake worker did not receive its request")
                time.sleep(0.002)
            started = time.monotonic()
            ocr.close()
            with self.assertRaises(RuntimeError):
                pending.result(1)
            self.assertLess(time.monotonic() - started, 0.8)
        self.assertIsNotNone(worker.poll())
        ocr.close()

    def test_adapter_result_shape_remains_compatible(self):
        self.script(READ_REQUEST + '''
    print(json.dumps({"results": [{"text": "42", "confidence": 0.75,
          "x": 1, "y": 2, "width": 3, "height": 4}]}), flush=True)
''')
        import numpy as np

        with VisionOCR(timeout=1) as ocr:
            result = ocr.ocr(np.zeros((12, 12, 3), dtype=np.uint8))
        self.assertEqual(result, [[[[[1, 2], [4, 2], [4, 6], [1, 6]], ["42", 0.75]]]])

    def test_close_waits_for_inflight_failure_cleanup(self):
        self.script(READ_REQUEST + "    print('not-json', flush=True)\n")
        with VisionOCR(timeout=1) as ocr:
            real_stop = ocr._stop_worker
            worker = ocr._worker
            cleaning, release = threading.Event(), threading.Event()
            close_called, close_returned = threading.Event(), threading.Event()

            def delayed_stop(proc):
                cleaning.set()
                release.wait(2)
                real_stop(proc)

            def close():
                close_called.set()
                ocr.close()
                close_returned.set()

            with patch.object(ocr, "_stop_worker", side_effect=delayed_stop), \
                    ThreadPoolExecutor(max_workers=2) as executor:
                recognition = executor.submit(ocr.recognize, b"image")
                self.assertTrue(cleaning.wait(1))
                closing = executor.submit(close)
                try:
                    self.assertTrue(close_called.wait(1))
                    self.assertFalse(close_returned.wait(0.03))
                finally:
                    release.set()
                closing.result(1)
                with self.assertRaises(ValueError):
                    recognition.result(1)
            self.assertIsNotNone(worker.poll())

    def test_invalid_timeout_and_image_length(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                VisionOCR(timeout=timeout)
        with self.assertRaises(ValueError):
            VisionOCR().recognize(b"")


if __name__ == "__main__":
    unittest.main()
