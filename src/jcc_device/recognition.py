"""Contract-independent recognition hooks, ROI OCR, and late-result protection.

These are raw observations, not game-state fields or calibrated confidence.
No default game coordinates, scene labels, or numeric interpretations exist.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import threading
import time
from typing import Callable, Generic, Protocol, Sequence, TypeVar

from .latest import Frame, FrameRef, positive_seconds


T = TypeVar("T")


@dataclass(frozen=True)
class FrameResult(Generic[T]):
    source: FrameRef
    processing_started_monotonic_ns: int
    processing_finished_monotonic_ns: int
    value: T

    def timing(self) -> dict:
        return {**self.source.metadata(),
                "processing_started_monotonic_ns": self.processing_started_monotonic_ns,
                "processing_finished_monotonic_ns": self.processing_finished_monotonic_ns,
                "processing_seconds": (self.processing_finished_monotonic_ns -
                                       self.processing_started_monotonic_ns) / 1e9,
                "receive_to_finish_seconds": (self.processing_finished_monotonic_ns -
                                             self.source.host_received_monotonic_ns) / 1e9}


class FrameProcessor(Generic[T]):
    """Synchronous callable adapter with timing/counts and no work queue.

    A caller may run process() in a bounded executor. This object does not
    publish state; use LatestResultGate before exposing asynchronous results.
    """

    def __init__(self, recognize: Callable[[Frame], T]):
        self.recognize = recognize
        self._lock = threading.Lock()
        self._started = self._processed = self._errors = self._processing_ns = 0

    def process(self, frame: Frame) -> FrameResult[T]:
        started = time.monotonic_ns()
        with self._lock:
            self._started += 1
        try:
            value = self.recognize(frame)
        except BaseException:
            with self._lock:
                self._errors += 1
            raise
        else:
            with self._lock:
                self._processed += 1
            return FrameResult(frame.source, started, time.monotonic_ns(), value)
        finally:
            with self._lock:
                self._processing_ns += time.monotonic_ns() - started

    def stats(self) -> dict:
        with self._lock:
            return {"frames_started": self._started, "frames_processed": self._processed,
                    "processing_errors": self._errors,
                    "processing_seconds_total": self._processing_ns / 1e9}


class LatestResultGate(Generic[T]):
    """Publish only the latest explicitly selected input, with a maximum age.

    advance(frame.source) invalidates any result for the previous input. A new
    gate is required for a new capture session. The capture pump does not call
    advance automatically: the integrating state owner must advance on each
    frame/scene it treats as current and close on capture failure/disconnect.
    Acceptance is based on source identity, not completion order. No image is
    retained here. get() also expires a result when capture stops producing.
    """

    def __init__(self, *, max_age_seconds: float):
        self.max_age_ns = int(positive_seconds(max_age_seconds, "max_age_seconds") * 1e9)
        self._lock = threading.Lock()
        self._current: FrameRef | None = None
        self._result: FrameResult[T] | None = None
        self._closed = False
        self._accepted = self._outdated = self._expired = 0

    def advance(self, source: FrameRef) -> bool:
        with self._lock:
            if self._closed:
                return False
            if self._current is not None:
                if (source.stream_id != self._current.stream_id or
                        source.sequence <= self._current.sequence or
                        source.host_received_monotonic_ns < self._current.host_received_monotonic_ns):
                    return False
            self._current, self._result = source, None
            return True

    def publish(self, result: FrameResult[T]) -> bool:
        with self._lock:
            if self._closed or result.source != self._current or self._result is not None:
                self._outdated += 1
                return False
            now = time.monotonic_ns()
            if (not result.source.host_received_monotonic_ns <=
                    result.processing_started_monotonic_ns <=
                    result.processing_finished_monotonic_ns <= now):
                self._outdated += 1
                return False
            if now - result.source.host_received_monotonic_ns > self.max_age_ns:
                self._expired += 1
                return False
            self._result = result
            self._accepted += 1
            return True

    def get(self) -> FrameResult[T] | None:
        with self._lock:
            if self._result is not None and (time.monotonic_ns() -
                    self._result.source.host_received_monotonic_ns > self.max_age_ns):
                self._result = None
                self._expired += 1
            return self._result

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._current = self._result = None

    def stats(self) -> dict:
        with self._lock:
            return {"results_accepted": self._accepted, "results_outdated": self._outdated,
                    "results_expired": self._expired, "closed": self._closed}


@dataclass(frozen=True)
class PixelROI:
    """Explicit calibrated top-left pixel coordinates, right/bottom exclusive."""

    name: str
    image_size: tuple[int, int]
    box: tuple[int, int, int, int]

    def validate(self, actual_size: tuple[int, int]) -> None:
        if not self.name or actual_size != self.image_size:
            raise ValueError("ROI name and exact calibrated image dimensions are required")
        if (len(self.box) != 4 or any(type(n) is not int for n in self.box) or
                not 0 <= self.box[0] < self.box[2] <= actual_size[0] or
                not 0 <= self.box[1] < self.box[3] <= actual_size[1]):
            raise ValueError("ROI box must be integer pixels inside the calibrated image")


class RawOCR(Protocol):
    def recognize(self, image_bytes: bytes) -> list[dict]: ...


def recognize_regions(frame: Frame, regions: Sequence[PixelROI], ocr: RawOCR) -> list[dict]:
    """OCR explicit ROIs in memory; return raw boxes in full-frame coordinates.

    OCR confidence remains the provider's raw score. This function does not
    translate text into gold/health/level and does not merge game state.
    """
    from PIL import Image

    if not regions or len({region.name for region in regions}) != len(regions):
        raise ValueError("Provide nonempty, uniquely named calibrated ROIs")
    with Image.open(io.BytesIO(frame.jpeg)) as image:
        for region in regions:
            region.validate(image.size)
        results = []
        for region in regions:
            with image.crop(region.box) as cropped, io.BytesIO() as encoded:
                cropped.save(encoded, format="PNG")
                items = ocr.recognize(encoded.getvalue())
            left, top, _, _ = region.box
            results.append({"roi": region.name, "box": list(region.box),
                            "items": [{**item, "x": item["x"] + left,
                                       "y": item["y"] + top} for item in items],
                            "method": "ocr_raw", "source": frame.source.metadata()})
    return results
