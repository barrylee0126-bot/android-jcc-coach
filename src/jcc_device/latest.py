"""Continuously drain an opened frame source into one in-memory latest slot.

This bounds *application* backlog. It does not measure or eliminate buffering
inside the device, scrcpy, ffmpeg, or the OS. A receive timestamp means that
this Python worker obtained a complete JPEG, not that the tablet captured it.
"""

from __future__ import annotations

from contextlib import nullcontext
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
    After a successful start(), only the receiver thread calls these methods.
    If startup fails before the receiver claims the source, the caller closes it.
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


class FrameObserver(Protocol):
    """One state owner, bound before start; callbacks must not read this stream.

    receive_lock is a reentrant lock also held by that owner's publication and
    read paths. FrameRef allocation, synchronous checking and mailbox insertion
    share this guard, so a result cannot publish during an unchecked receipt.
    All disconnect callbacks share it too, including receiver error/finally
    paths, so publication is ordered before withdrawal or sees disconnection.
    The source read happens outside it; lower-level buffered bytes are not an
    authoritative application frame until this guarded allocation.
    """

    stream_id: str
    receive_lock: object

    def observe_frame(self, frame: Frame) -> object: ...

    def disconnect(self) -> None: ...


def positive_seconds(value: float, name: str, *, allow_zero: bool = False) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}")
    return value


class LatestFrameStream:
    """Single-consumer latest-frame mailbox owning an already-open source.

    start() never opens the source. close() wakes waiting consumers immediately,
    then waits for receiver work and source closure to finish. The receiver closes
    a source it owns, avoiding closing a pipe while another thread is reading it.
    A misbehaving source that ignores its timeout is reported as
    FrameShutdownTimeout, never as a clean shutdown.
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
        # Thread.join()/is_alive() can falsely report a stopped thread after
        # SIGINT on Python 3.11. Keep independent witnesses for the target and
        # source-close completion, and never use join to prove cleanup.
        self._receiver_entered = threading.Event()
        self._receiver_finished = threading.Event()
        self._receiver_expected = False
        self._source_close_finished = threading.Event()
        # The receiver claims the source before its first read. Startup failure
        # can instead leave it with the caller, even if a native thread was
        # created but has not yet reached its target. Never acquire _condition
        # or the observer's receive_lock while holding this lock.
        self._source_owner_lock = threading.Lock()
        self._source_owner: str | None = None
        self._latest: Frame | None = None
        self._closed = False
        self._source_closed = False
        self._error_type: str | None = None
        self._close_error_type: str | None = None
        self._observer: FrameObserver | None = None
        self._observer_disconnect_error_type: str | None = None
        self._counts = dict(frames_received=0, frames_delivered=0, frames_replaced=0,
                            frames_discarded=0, frames_expired=0,
                            source_timeouts=0, consumer_timeouts=0,
                            source_errors=0, close_errors=0, max_pending_frames=0)

    def bind_observer(self, observer: FrameObserver) -> None:
        """Attach exactly one receive-path owner before capture starts.

        The optional hook does not change the byte-only/latest-frame APIs.
        Every real sequence is checked, including frames later replaced in the
        mailbox. Hook errors are terminal, sanitized source failures.
        """
        with self._condition:
            if self._observer is not None or self._thread is not None or self._closed:
                raise FrameStreamError("Bind one observer before starting capture")
            if observer.stream_id != self.stream_id:
                raise ValueError("Observer must own this stream")
            self._observer = observer

    def _receive_guard(self):
        return nullcontext() if self._observer is None else self._observer.receive_lock

    def _disconnect_observer(self) -> None:
        if self._observer is not None:
            # Never enter from inside _condition: publication/receipt uses
            # receive_lock -> _condition. Reentrancy permits calls from the
            # observe hook and close's bounded acquisition of the same guard.
            with self._receive_guard():
                try:
                    self._observer.disconnect()
                except Exception as exc:
                    # Cleanup must still close the source when the owner's clock
                    # or contract fails. Never expose the callback exception text.
                    with self._condition:
                        if self._observer_disconnect_error_type is None:
                            self._counts["source_errors"] += 1
                        self._observer_disconnect_error_type = type(exc).__name__
                        self._error_type = type(exc).__name__
                        self._discard_pending()
                        self._condition.notify_all()

    def start(self) -> LatestFrameStream:
        start_error = None
        with self._condition:
            if self._closed:
                raise FrameStreamClosed("A closed latest-frame stream cannot be restarted")
            if self._thread is not None:
                raise FrameStreamError("Latest-frame receiver has already been started")
            try:
                self._thread = threading.Thread(target=self._receive_entry, daemon=True,
                                                name=f"jcc-receiver-{self.stream_id[:8]}")
                self._thread.start()
                self._receiver_expected = True
                with self._source_owner_lock:
                    self._source_owner = "receiver"
            except BaseException as error:
                self._closed = True
                with self._source_owner_lock:
                    self._stop.set()
                    if self._source_owner is None:
                        self._source_owner = "caller"
                self._discard_pending()
                self._condition.notify_all()
                start_error = error
        if start_error is not None:
            # close() preserves receive_lock -> _condition order and has a
            # bounded wait. If a receiver owns the source, only it closes it.
            try:
                self.close()
            except FrameStreamError as cleanup_error:
                start_error.add_note(str(cleanup_error))
            except BaseException as cleanup_error:
                # Preserve the first startup error without suppressing a second
                # KeyboardInterrupt (or another interruption during cleanup).
                raise cleanup_error from start_error
            finally:
                # If the observer lock is stuck, there is no receiver to close
                # an unclaimed source; do it even when bounded cleanup fails.
                with self._source_owner_lock:
                    caller_owns_source = self._source_owner == "caller"
                if caller_owns_source:
                    self._close_source()
            raise start_error
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
        except BaseException as exc:
            with self._condition:
                self._counts["close_errors"] += 1
                self._close_error_type = type(exc).__name__
                self._condition.notify_all()
            if not isinstance(exc, Exception):
                raise
        finally:
            # This marks the end of the close attempt, not necessarily success;
            # failures are retained in _close_error_type and reported by close().
            self._source_close_finished.set()

    def _receive_entry(self) -> None:
        self._receiver_entered.set()
        try:
            self._receive()
        finally:
            self._receiver_finished.set()

    def _receive(self) -> None:
        with self._source_owner_lock:
            if self._source_owner == "caller":
                return
            self._source_owner = "receiver"
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
                    self._disconnect_observer()
                    with self._condition:
                        if not self._stop.is_set():
                            self._counts["source_errors"] += 1
                            # No source messages/logs: they may contain device identifiers.
                            self._error_type = type(exc).__name__
                            self._discard_pending()
                            self._condition.notify_all()
                    break
                received_utc = datetime.now(timezone.utc).isoformat()
                with self._receive_guard():
                    with self._condition:
                        self._counts["frames_received"] += 1
                        frame = Frame(FrameRef(self.stream_id, self._counts["frames_received"],
                                               received_ns, received_utc), jpeg)
                        if self._stop.is_set():
                            self._counts["frames_discarded"] += 1
                            break
                    try:
                        if self._observer is not None:
                            self._observer.observe_frame(frame)
                    except Exception as exc:
                        self._disconnect_observer()
                        with self._condition:
                            self._counts["source_errors"] += 1
                            self._counts["frames_discarded"] += 1
                            self._error_type = type(exc).__name__
                            self._discard_pending()
                            self._condition.notify_all()
                        break
                    with self._condition:
                        stopped = self._stop.is_set()
                        if stopped:
                            self._counts["frames_discarded"] += 1
                        else:
                            if self._latest is not None:
                                self._counts["frames_replaced"] += 1
                            self._latest = frame
                            self._counts["max_pending_frames"] = 1
                            self._condition.notify_all()
                    if stopped:
                        self._disconnect_observer()
                        break
        finally:
            try:
                self._disconnect_observer()
            finally:
                try:
                    self._close_source()
                finally:
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
            alive = self._receiver_pending(self._thread)
            return {**self._counts, "stream_id": self.stream_id,
                    "pending_frames": int(self._latest is not None),
                    "receiver_thread_alive": alive, "closed": self._closed,
                    "observer_bound": self._observer is not None,
                    "source_error_type": self._error_type,
                    "source_close_error_type": self._close_error_type,
                    "observer_disconnect_error_type": self._observer_disconnect_error_type}

    def _receiver_pending(self, thread: threading.Thread | None) -> bool:
        if thread is None or self._receiver_finished.is_set():
            return False
        # is_alive is only a positive witness before the target enters, never
        # proof of completion. An interrupted pre-start can create a thread
        # which has not entered yet; the source-owner gate prevents a late read.
        return (self._receiver_expected or self._receiver_entered.is_set()
                or thread.is_alive())

    def close(self) -> None:
        started = time.monotonic()
        # Wake consumers even when an observer is slow. A hook still running
        # keeps the owner guard, blocking publication until it sees stop below.
        with self._condition:
            self._closed = True
            self._stop.set()
            self._discard_pending()
            self._condition.notify_all()
            thread = self._thread
        with self._source_owner_lock:
            caller_owns_source = self._source_owner == "caller"
        if self._observer is not None:
            lock = self._observer.receive_lock
            if not lock.acquire(timeout=self.shutdown_timeout):
                raise FrameShutdownTimeout("Receive observer did not stop within shutdown_timeout")
            try:
                self._disconnect_observer()
            finally:
                lock.release()
        if thread is None or caller_owns_source:
            self._close_source()
        if thread is threading.current_thread():
            raise FrameStreamError("Receiver cannot join itself")
        elif self._receiver_pending(thread):
            if not self._receiver_finished.wait(max(0, self.shutdown_timeout - (time.monotonic() - started))):
                raise FrameShutdownTimeout("Source did not stop within shutdown_timeout")
        if not self._source_close_finished.wait(max(0, self.shutdown_timeout - (time.monotonic() - started))):
            raise FrameShutdownTimeout("Source close did not finish within shutdown_timeout")
        if self._close_error_type:
            raise FrameSourceFailed(f"Frame source close failed ({self._close_error_type})")
        if self._observer_disconnect_error_type:
            raise FrameSourceFailed(f"Frame observer disconnect failed ({self._observer_disconnect_error_type})")

    def __enter__(self) -> LatestFrameStream:
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except FrameStreamError as close_error:
            if exc is None:
                raise
            exc.add_note(str(close_error))
