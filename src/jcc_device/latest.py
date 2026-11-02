"""Continuously drain an opened frame source into one in-memory latest slot.

This bounds *application* backlog. It does not measure or eliminate buffering
inside the device, scrcpy, ffmpeg, or the OS. A receive timestamp means that
this Python worker obtained a complete JPEG, not that the tablet captured it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
import threading
import time
from typing import Protocol
from uuid import uuid4


class FrameSource(Protocol):
    """An already-open source. Both read_frame and close must be bounded.

    A read timeout is recoverable; any other read exception is terminal.
    Only the receiver thread calls these methods after start().
    """

    def read_frame(self, timeout: float = 10.0) -> bytes: ...

    def close(self) -> None: ...


class FrameStreamError(RuntimeError):
    pass


class FrameStreamClosed(FrameStreamError):
    pass


class FrameSourceFailed(FrameStreamError):
    pass


class FrameShutdownTimeout(FrameStreamError):
    pass


@dataclass(frozen=True)
class FrameRef:
    """Image-free provenance; monotonic times are comparable on this Mac only."""

    stream_id: str
    sequence: int
    host_received_monotonic_ns: int
    host_received_utc: str
    host_received_time_source: str = "mac_python_complete_jpeg_read"
    device_capture_time: None = None

    @property
    def frame_id(self) -> str:
        return f"{self.stream_id}:{self.sequence}"

    def metadata(self) -> dict:
        return {"frame_id": self.frame_id, **asdict(self)}


@dataclass(frozen=True)
class Frame:
    source: FrameRef
    jpeg: bytes = field(repr=False)

    def age_seconds(self) -> float:
        return (time.monotonic_ns() - self.source.host_received_monotonic_ns) / 1e9


def positive_seconds(value: float, name: str, *, allow_zero: bool = False) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}")
    return value


class LatestFrameStream:
    """Single-consumer latest-frame mailbox owning an already-open source.

    start() never opens the source. close() wakes waiting consumers immediately,
    then joins the receiver, which closes the source itself. This avoids closing
    a pipe while another thread is reading it. A misbehaving source that ignores
    its timeout is reported as FrameShutdownTimeout, never as a clean shutdown.
    Instances cannot be restarted; a new instance gets a new stream_id.
    """

    def __init__(self, source: FrameSource, *, read_timeout: float = 0.25,
                 shutdown_timeout: float = 8.0):
        self.source = source
        self.read_timeout = positive_seconds(read_timeout, "read_timeout")
        self.shutdown_timeout = positive_seconds(shutdown_timeout, "shutdown_timeout")
        self.stream_id = uuid4().hex
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: Frame | None = None
        self._closed = False
        self._source_closed = False
        self._error_type: str | None = None
        self._close_error_type: str | None = None
        self._counts = dict(frames_received=0, frames_delivered=0, frames_replaced=0,
                            frames_discarded=0, frames_expired=0,
                            source_timeouts=0, consumer_timeouts=0,
                            source_errors=0, close_errors=0, max_pending_frames=0)

    def start(self) -> LatestFrameStream:
        with self._condition:
            if self._closed:
                raise FrameStreamClosed("A closed latest-frame stream cannot be restarted")
            if self._thread is not None:
                raise FrameStreamError("Latest-frame receiver has already been started")
            self._thread = threading.Thread(target=self._receive, daemon=True,
                                            name=f"jcc-receiver-{self.stream_id[:8]}")
            try:
                self._thread.start()
            except BaseException:
                self._thread = None
                self._closed = True
                self._close_source()
                raise
        return self

    def _discard_pending(self) -> None:
        if self._latest is not None:
            self._counts["frames_discarded"] += 1
            self._latest = None

    def _close_source(self) -> None:
        with self._condition:
            if self._source_closed:
                return
            self._source_closed = True
        try:
            self.source.close()
        except Exception as exc:
            with self._condition:
                self._counts["close_errors"] += 1
                self._close_error_type = type(exc).__name__
                self._condition.notify_all()

    def _receive(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    jpeg = self.source.read_frame(timeout=self.read_timeout)
                    received_ns = time.monotonic_ns()
                    if not isinstance(jpeg, bytes) or not jpeg:
                        raise ValueError("Frame source must return nonempty immutable bytes")
                except TimeoutError:
                    with self._condition:
                        if not self._stop.is_set():
                            self._counts["source_timeouts"] += 1
                    continue
                except Exception as exc:
                    with self._condition:
                        if not self._stop.is_set():
                            self._counts["source_errors"] += 1
                            # No source messages/logs: they may contain device identifiers.
                            self._error_type = type(exc).__name__
                            self._discard_pending()
                            self._condition.notify_all()
                    break
                received_utc = datetime.now(timezone.utc).isoformat()
                with self._condition:
                    self._counts["frames_received"] += 1
                    frame = Frame(FrameRef(self.stream_id, self._counts["frames_received"],
                                           received_ns, received_utc), jpeg)
                    if self._stop.is_set():
                        self._counts["frames_discarded"] += 1
                        break
                    if self._latest is not None:
                        self._counts["frames_replaced"] += 1
                    self._latest = frame
                    self._counts["max_pending_frames"] = 1
                    self._condition.notify_all()
        finally:
            self._close_source()
            with self._condition:
                self._condition.notify_all()

    def read_latest(self, timeout: float = 10.0, *, max_age_seconds: float | None = None) -> Frame:
        """Take and remove the newest unread frame. timeout=0 is a poll.

        max_age_seconds optionally rejects frames too old in the Mac mailbox.
        A consumer timeout does not stop capture. Terminal failure/close discards
        the slot, so cached frames cannot masquerade as a healthy live source.
        """
        timeout = positive_seconds(timeout, "timeout", allow_zero=True)
        max_age = (None if max_age_seconds is None else
                   positive_seconds(max_age_seconds, "max_age_seconds"))
        deadline = time.monotonic() + timeout
        with self._condition:
            if self._thread is None and not self._closed:
                raise FrameStreamError("Latest-frame receiver has not been started")
            while True:
                if self._closed:
                    raise FrameStreamClosed("Latest-frame stream is closed")
                if self._error_type:
                    raise FrameSourceFailed(f"Frame source failed ({self._error_type})") from None
                if self._latest is not None:
                    if max_age is not None and self._latest.age_seconds() > max_age:
                        self._discard_pending()
                        self._counts["frames_expired"] += 1
                    else:
                        frame, self._latest = self._latest, None
                        self._counts["frames_delivered"] += 1
                        return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._counts["consumer_timeouts"] += 1
                    raise TimeoutError("Timed out waiting for a new frame")
                self._condition.wait(remaining)

    def read_frame(self, timeout: float = 10.0) -> bytes:
        """Compatibility adapter for byte-only callers, including Sunflower."""
        return self.read_latest(timeout).jpeg

    def stats(self) -> dict:
        with self._condition:
            alive = self._thread is not None and self._thread.is_alive()
            return {**self._counts, "stream_id": self.stream_id,
                    "pending_frames": int(self._latest is not None),
                    "receiver_thread_alive": alive, "closed": self._closed,
                    "source_error_type": self._error_type,
                    "source_close_error_type": self._close_error_type}

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._stop.set()
            self._discard_pending()
            self._condition.notify_all()
            thread = self._thread
        if thread is None:
            self._close_source()
        elif thread is threading.current_thread():
            raise FrameStreamError("Receiver cannot join itself")
        else:
            thread.join(self.shutdown_timeout)
            if thread.is_alive():
                raise FrameShutdownTimeout("Source did not stop within shutdown_timeout")
        if self._close_error_type:
            raise FrameSourceFailed(f"Frame source close failed ({self._close_error_type})")

    def __enter__(self) -> LatestFrameStream:
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except FrameStreamError as close_error:
            if exc is None:
                raise
            exc.add_note(str(close_error))
