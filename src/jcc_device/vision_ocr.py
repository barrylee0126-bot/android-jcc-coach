"""Persistent local Apple Vision worker and PaddleOCR-shaped adapter."""

from __future__ import annotations

import json
import os
import select
import struct
import subprocess
import threading
import time
from pathlib import Path

from .latest import positive_seconds


PROJECT = Path(__file__).resolve().parents[2]
WORKER = PROJECT / "tools" / "vision_ocr"


class VisionOCR:
    def __init__(self, timeout: float = 30.0):
        self.timeout = positive_seconds(timeout, "timeout")
        self._worker: subprocess.Popen | None = None
        self._request_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._closing = threading.Event()

    def __enter__(self) -> "VisionOCR":
        if not WORKER.exists():
            raise RuntimeError("Apple Vision worker is missing; run tools/setup-macos")
        with self._lifecycle_lock:
            if self._worker is not None:
                raise RuntimeError("OCR worker has already been started")
            worker = subprocess.Popen(
                [str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
            try:
                os.set_blocking(worker.stdin.fileno(), False)
                os.set_blocking(worker.stdout.fileno(), False)
            except BaseException:
                self._stop_worker(worker)
                raise
            self._worker = worker
            self._closing.clear()
        return self

    def recognize(self, image_bytes: bytes) -> list[dict]:
        if not 0 < len(image_bytes) <= 25 * 1024 * 1024:
            raise ValueError("Invalid image length")
        # One request/response at a time: this protocol has no request IDs.
        timeout = positive_seconds(self.timeout, "timeout")
        deadline = time.monotonic() + timeout
        if not self._request_lock.acquire(timeout=timeout):
            raise TimeoutError("Timed out waiting for the previous OCR request")
        try:
            worker = self._worker
            if worker is None:
                raise RuntimeError("OCR worker has not been started; reopen after failure")
            try:
                payload = struct.pack(">I", len(image_bytes)) + image_bytes
                result = json.loads(self._exchange(worker, payload, deadline))
                if not isinstance(result, dict):
                    raise RuntimeError("Apple Vision OCR returned an invalid response")
                if result.get("error"):
                    raise RuntimeError("Apple Vision OCR: " + str(result["error"]))
                if not isinstance(result.get("results"), list):
                    raise RuntimeError("Apple Vision OCR returned no result list")
                return result["results"]
            except BaseException:
                # A late response must never be consumed by a subsequent image.
                self._invalidate(worker)
                raise
        finally:
            self._request_lock.release()

    def _exchange(self, worker: subprocess.Popen, payload: bytes, deadline: float) -> bytes:
        def transfer(*, writing: bool, data=None):
            # Keep identity check + fd operation atomic with close/reopen. Without
            # this lock a recycled pipe descriptor could target a newer worker.
            with self._lifecycle_lock:
                if worker is not self._worker or self._closing.is_set():
                    raise RuntimeError("OCR worker was closed during recognition")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Apple Vision OCR timed out")
                write_fd, read_fd = worker.stdin.fileno(), worker.stdout.fileno()
                readable, writable, _ = select.select(
                    [] if writing else [read_fd], [write_fd] if writing else [], [],
                    min(remaining, 0.1),
                )
                if not (writable if writing else readable):
                    return None
                try:
                    return os.write(write_fd, data) if writing else os.read(read_fd, 65536)
                except BlockingIOError:
                    return None

        offset = 0
        view = memoryview(payload)
        while offset < len(payload):
            sent = transfer(writing=True, data=view[offset:])
            if sent is not None:
                offset += sent
        response = bytearray()
        while True:
            chunk = transfer(writing=False)
            if chunk is None:
                continue
            if not chunk:
                raise RuntimeError("Apple Vision OCR worker stopped unexpectedly")
            response.extend(chunk)
            if len(response) > 8 * 1024 * 1024:
                raise RuntimeError("Apple Vision OCR response exceeded the size limit")
            if b"\n" in response:
                line, trailing = response.split(b"\n", 1)
                if trailing:
                    raise RuntimeError("Apple Vision OCR returned extra protocol data")
                return bytes(line)

    @staticmethod
    def _stop_worker(worker: subprocess.Popen) -> None:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=1)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=1)
        for pipe in (worker.stdin, worker.stdout):
            if pipe:
                pipe.close()

    def _invalidate(self, worker: subprocess.Popen) -> None:
        with self._lifecycle_lock:
            if worker is not self._worker:
                return
            self._worker = None
            self._closing.set()
            # Keep cleanup serialized too: close() must not return while an
            # invalidation thread is still terminating the previous process.
            self._stop_worker(worker)

    def ocr(self, image, cls: bool = False) -> list:
        """Expose the subset of PaddleOCR 2.x used by Sunflower's reader."""
        import cv2

        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise RuntimeError("Could not encode image for OCR")
        items = []
        for r in self.recognize(encoded.tobytes()):
            x, y, w, h = (r[k] for k in ("x", "y", "width", "height"))
            items.append([[[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
                          [r["text"], r["confidence"]]])
        return [items]

    def close(self) -> None:
        self._closing.set()
        with self._lifecycle_lock:
            worker = self._worker
            self._worker = None
            if worker is not None:
                self._stop_worker(worker)

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
