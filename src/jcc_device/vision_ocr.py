"""Persistent local Apple Vision worker and PaddleOCR-shaped adapter."""

from __future__ import annotations

import json
import select
import struct
import subprocess
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[2]
WORKER = PROJECT / "tools" / "vision_ocr"


class VisionOCR:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self._worker: subprocess.Popen | None = None

    def __enter__(self) -> "VisionOCR":
        if not WORKER.exists():
            raise RuntimeError("Apple Vision worker is missing; run tools/setup-macos")
        self._worker = subprocess.Popen(
            [str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        return self

    def recognize(self, image_bytes: bytes) -> list[dict]:
        if not self._worker or not self._worker.stdin or not self._worker.stdout:
            raise RuntimeError("OCR worker has not been started")
        if not 0 < len(image_bytes) <= 25 * 1024 * 1024:
            raise ValueError("Invalid image length")
        self._worker.stdin.write(struct.pack(">I", len(image_bytes)) + image_bytes)
        ready, _, _ = select.select([self._worker.stdout], [], [], self.timeout)
        if not ready:
            raise TimeoutError("Apple Vision OCR timed out")
        line = self._worker.stdout.readline()
        if not line:
            raise RuntimeError("Apple Vision OCR worker stopped unexpectedly")
        result = json.loads(line)
        if result.get("error"):
            raise RuntimeError("Apple Vision OCR: " + result["error"])
        if result.get("results") is None:
            raise RuntimeError("Apple Vision OCR returned no result")
        return result["results"]

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
        if self._worker:
            if self._worker.stdin:
                self._worker.stdin.close()
            try:
                self._worker.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._worker.kill()
                self._worker.wait(timeout=3)
            if self._worker.stdout:
                self._worker.stdout.close()
            self._worker = None

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
