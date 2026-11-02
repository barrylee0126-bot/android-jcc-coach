"""Behavioral tests with controlled sources; no device or saved image needed."""

from concurrent.futures import ThreadPoolExecutor
import queue
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jcc_device.latest import (FrameShutdownTimeout, FrameSourceFailed,
                               FrameStreamClosed, FrameStreamError, LatestFrameStream)
from jcc_device.recognition import FrameProcessor
from jcc_device.stream import TabletStream


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Condition was not reached before timeout")
        time.sleep(0.002)


class ControlledSource:
    def __init__(self):
        self.items = queue.Queue()
        self.entered = threading.Event()
        self.close_calls = 0

    def read_frame(self, timeout=10):
        self.entered.set()
        try:
            item = self.items.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.close_calls += 1


class LatestFrameTests(unittest.TestCase):
    def make_stream(self, source=None, **kwargs):
        source = source or ControlledSource()
        stream = LatestFrameStream(source, read_timeout=0.01, **kwargs).start()
        self.addCleanup(stream.close)
        return source, stream

    def test_replaces_unread_frames_and_provenance_has_no_image(self):
        source, stream = self.make_stream()
        for value in (b"private-payload-1", b"private-payload-2", b"private-payload-3"):
            source.items.put(value)
        eventually(lambda: stream.stats()["frames_received"] == 3)
        frame = stream.read_latest(0)
        self.assertEqual(frame.jpeg, b"private-payload-3")
        self.assertEqual(frame.source.sequence, 3)
        self.assertEqual(frame.source.device_capture_time, None)
        self.assertGreaterEqual(frame.age_seconds(), 0)
        self.assertNotIn("private-payload", repr(frame))
        self.assertNotIn("jpeg", frame.source.metadata())
        stats = stream.stats()
        self.assertEqual((stats["frames_replaced"], stats["frames_delivered"],
                          stats["pending_frames"], stats["max_pending_frames"]), (2, 1, 0, 1))

    def test_capture_keeps_draining_while_processor_is_blocked(self):
        source, stream = self.make_stream()
        source.items.put(b"first")
        first = stream.read_latest(1)
        started, release = threading.Event(), threading.Event()

        def slow(frame):
            started.set()
            self.assertTrue(release.wait(2))
            return frame.jpeg

        processor = FrameProcessor(slow)
        with ThreadPoolExecutor(max_workers=1) as executor:
            work = executor.submit(processor.process, first)
            self.assertTrue(started.wait(1))
            try:
                for sequence in range(2, 11):
                    source.items.put(str(sequence).encode())
                eventually(lambda: stream.stats()["frames_received"] == 10)
                self.assertFalse(work.done())
                self.assertEqual(stream.stats()["pending_frames"], 1)
            finally:
                release.set()
            self.assertEqual(work.result(1).source.sequence, 1)
        newest = stream.read_latest(0)
        self.assertEqual(newest.source.sequence, 10)
        self.assertEqual(stream.stats()["frames_replaced"], 8)
        self.assertEqual(processor.stats()["frames_processed"], 1)

    def test_consumer_and_source_timeouts_recover(self):
        source, stream = self.make_stream()
        with self.assertRaises(TimeoutError):
            stream.read_latest(0.04)
        source.items.put(TimeoutError("temporary source delay"))
        source.items.put(b"recovered")
        self.assertEqual(stream.read_frame(1), b"recovered")
        self.assertGreater(stream.stats()["source_timeouts"], 0)
        self.assertEqual(stream.stats()["consumer_timeouts"], 1)
        self.assertEqual(stream.stats()["source_errors"], 0)

    def test_terminal_failure_discards_slot_and_redacts_source_detail(self):
        source, stream = self.make_stream()
        source.items.put(b"old")
        eventually(lambda: stream.stats()["frames_received"] == 1)
        source.items.put(RuntimeError("private-device-identifier"))
        eventually(lambda: stream.stats()["source_errors"] == 1)
        with self.assertRaises(FrameSourceFailed) as failure:
            stream.read_latest(0)
        self.assertNotIn("private-device", str(failure.exception))
        self.assertEqual(stream.stats()["frames_discarded"], 1)
        eventually(lambda: source.close_calls == 1)

    def test_optional_receive_age_limit_discards_stalled_pending_frame(self):
        source, stream = self.make_stream()
        source.items.put(b"expires")
        eventually(lambda: stream.stats()["frames_received"] == 1)
        with self.assertRaises(TimeoutError):
            stream.read_latest(0, max_age_seconds=1e-9)
        self.assertEqual(stream.stats()["frames_expired"], 1)
        self.assertEqual(stream.stats()["frames_discarded"], 1)
        source.items.put(b"fresh")
        self.assertEqual(stream.read_latest(1, max_age_seconds=1).jpeg, b"fresh")

    def test_invalid_payload_fails_instead_of_publishing(self):
        for payload in (b"", bytearray(b"mutable"), None):
            with self.subTest(payload=type(payload).__name__):
                source, stream = self.make_stream()
                source.items.put(payload)
                eventually(lambda: stream.stats()["source_errors"] == 1)
                with self.assertRaises(FrameSourceFailed):
                    stream.read_latest(0)
                stream.close()

    def test_close_wakes_waiter_and_is_idempotent(self):
        source, stream = self.make_stream()
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiter = executor.submit(stream.read_latest, 5)
            self.assertTrue(source.entered.wait(1))
            stream.close()
            with self.assertRaises(FrameStreamClosed):
                waiter.result(1)
        stream.close()
        self.assertEqual(source.close_calls, 1)
        self.assertFalse(stream.stats()["receiver_thread_alive"])
        with self.assertRaises(FrameStreamClosed):
            stream.start()

    def test_close_discards_pending_and_reconciles_counts(self):
        source, stream = self.make_stream()
        for value in (b"one", b"two", b"three"):
            source.items.put(value)
        eventually(lambda: stream.stats()["frames_received"] == 3)
        stream.read_latest(0)
        source.items.put(b"four")
        eventually(lambda: stream.stats()["frames_received"] == 4)
        stream.close()
        stats = stream.stats()
        self.assertEqual(stats["frames_received"], stats["frames_delivered"] +
                         stats["frames_replaced"] + stats["frames_discarded"])
        self.assertEqual(stats["frames_discarded"], 1)

    def test_uncooperative_source_cannot_claim_clean_shutdown(self):
        release, entered = threading.Event(), threading.Event()
        source = ControlledSource()

        def unbounded_read(timeout):
            entered.set()
            release.wait(3)
            return b"late"

        source.read_frame = unbounded_read
        stream = LatestFrameStream(source, shutdown_timeout=0.02).start()
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(FrameShutdownTimeout):
                stream.close()
            self.assertTrue(stream.stats()["receiver_thread_alive"])
            with self.assertRaises(FrameStreamClosed):
                stream.read_latest(0)
        finally:
            release.set()
            stream.close()
        self.assertEqual(stream.stats()["frames_received"], 1)
        self.assertEqual(stream.stats()["frames_discarded"], 1)
        self.assertEqual(source.close_calls, 1)

    def test_close_error_is_observable_without_private_message(self):
        source = ControlledSource()
        source.close = Mock(side_effect=OSError("private close detail"))
        stream = LatestFrameStream(source, read_timeout=0.01).start()
        with self.assertRaises(FrameSourceFailed) as failure:
            stream.close()
        self.assertNotIn("private", str(failure.exception))
        self.assertEqual(stream.stats()["close_errors"], 1)
        self.assertFalse(stream.stats()["receiver_thread_alive"])

    def test_concurrent_close_waits_for_in_progress_source_close(self):
        source = ControlledSource()
        entered, release = threading.Event(), threading.Event()
        calls, errors = [], []
        stream = LatestFrameStream(source, shutdown_timeout=.03)

        def slow_close():
            calls.append(threading.current_thread().name)
            entered.set()
            release.wait(2)

        def close_once():
            try:
                stream.close()
            except BaseException as error:
                errors.append(error)

        source.close = slow_close
        closer = threading.Thread(target=close_once, daemon=True)
        closer.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(FrameShutdownTimeout):
                stream.close()
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        stream.close()
        self.assertEqual(len(calls), 1)

    def test_interrupted_source_close_stays_reported_as_failure(self):
        source = ControlledSource()
        source.close = Mock(side_effect=KeyboardInterrupt("private-close-detail"))
        stream = LatestFrameStream(source)
        with self.assertRaises(KeyboardInterrupt):
            stream.close()
        with self.assertRaises(FrameSourceFailed) as failure:
            stream.close()
        self.assertNotIn("private-close-detail", str(failure.exception))
        self.assertEqual(stream.stats()["source_close_error_type"], "KeyboardInterrupt")
        source.close.assert_called_once_with()

    def test_lifecycle_and_invalid_timeouts(self):
        source = ControlledSource()
        stream = LatestFrameStream(source)
        with self.assertRaises(FrameStreamError):
            stream.read_latest(0)
        stream.close()
        self.assertEqual(source.close_calls, 1)
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                LatestFrameStream(source, read_timeout=value)
        source, stream = self.make_stream()
        with self.assertRaises(FrameStreamError):
            stream.start()
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                stream.read_latest(value)

    def test_receiver_start_failure_disconnects_without_inverting_observer_lock_order(self):
        source = ControlledSource()
        stream = LatestFrameStream(source, read_timeout=.01)
        guard = threading.RLock()
        holding, attempted, inspected = threading.Event(), threading.Event(), threading.Event()
        disconnects, errors, start_errors = [], [], []

        def disconnect():
            self.assertTrue(guard._is_owned())
            disconnects.append(stream.stats()["closed"])

        stream.bind_observer(SimpleNamespace(stream_id=stream.stream_id, receive_lock=guard,
                                             disconnect=disconnect, observe_frame=lambda frame: None))

        def publish_and_inspect():
            try:
                with guard:
                    holding.set()
                    if not attempted.wait(1):
                        raise AssertionError("Receiver start was not attempted")
                    # This may take _condition while holding receive_lock.
                    # Startup failure cleanup must release _condition first.
                    stream.stats()
                    inspected.set()
            except BaseException as error:
                errors.append(type(error).__name__)

        def start_stream():
            try:
                stream.start()
            except BaseException as error:
                start_errors.append(type(error).__name__)

        real_start = threading.Thread.start

        def fail_receiver(thread):
            if thread.name.startswith("jcc-receiver-"):
                attempted.set()
                raise RuntimeError("private-thread-start-detail")
            return real_start(thread)

        holder = threading.Thread(target=publish_and_inspect, daemon=True)
        starter = threading.Thread(target=start_stream, daemon=True)
        holder.start()
        self.assertTrue(holding.wait(1))
        with patch("jcc_device.latest.threading.Thread.start", new=fail_receiver):
            starter.start()
            starter.join(2)
            holder.join(2)
        self.assertFalse(starter.is_alive())
        self.assertFalse(holder.is_alive())
        self.assertTrue(inspected.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(start_errors, ["RuntimeError"])
        self.assertEqual(disconnects, [True])
        self.assertEqual(source.close_calls, 1)
        self.assertFalse(source.entered.is_set())
        stream.close()
        self.assertEqual(source.close_calls, 1)

    def test_interrupted_start_after_receiver_enters_read_preserves_ownership(self):
        source = ControlledSource()
        release = threading.Event()
        close_threads = []
        receiver = []

        def read_frame(timeout):
            source.entered.set()
            release.wait(2)
            return b"late-synthetic-frame"

        def close_source():
            close_threads.append(threading.current_thread().name)
            source.close_calls += 1

        source.read_frame = read_frame
        source.close = close_source
        stream = LatestFrameStream(source, shutdown_timeout=.03)
        observer = SimpleNamespace(stream_id=stream.stream_id, receive_lock=threading.RLock(),
                                   disconnect=Mock(), observe_frame=Mock())
        stream.bind_observer(observer)
        real_start = threading.Thread.start

        def interrupt_after_read(thread):
            receiver.append(thread)
            real_start(thread)
            if not source.entered.wait(1):
                raise AssertionError("Receiver did not enter synthetic read")
            raise KeyboardInterrupt("synthetic startup interruption")

        try:
            with patch("jcc_device.latest.threading.Thread.start", new=interrupt_after_read):
                with self.assertRaises(KeyboardInterrupt) as interruption:
                    stream.start()
            self.assertTrue(any("shutdown_timeout" in note
                                for note in interruption.exception.__notes__))
            self.assertTrue(stream.stats()["closed"])
            self.assertTrue(stream.stats()["receiver_thread_alive"])
            self.assertEqual(source.close_calls, 0)
            observer.disconnect.assert_called()
            with self.assertRaises(FrameStreamClosed):
                stream.read_latest(0)
        finally:
            release.set()
            if receiver:
                receiver[0].join(2)
            stream.close()
        self.assertEqual(close_threads, [receiver[0].name])
        self.assertEqual(source.close_calls, 1)
        self.assertFalse(stream.stats()["receiver_thread_alive"])
        self.assertEqual(stream.stats()["pending_frames"], 0)
        observer.observe_frame.assert_not_called()

    def test_interrupted_before_receiver_start_closes_in_caller(self):
        source = ControlledSource()
        source.close = Mock(wraps=source.close)
        stream = LatestFrameStream(source, shutdown_timeout=.03)
        with patch("jcc_device.latest.threading.Thread.start", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                stream.start()
        self.assertTrue(stream.stats()["closed"])
        self.assertFalse(stream.stats()["receiver_thread_alive"])
        self.assertFalse(source.entered.is_set())
        source.close.assert_called_once_with()
        stream.close()
        source.close.assert_called_once_with()

    def test_failed_start_with_busy_observer_is_bounded_and_closes_unclaimed_source(self):
        source = ControlledSource()
        stream = LatestFrameStream(source, shutdown_timeout=.03)
        guard = threading.RLock()
        held, release = threading.Event(), threading.Event()
        observer = SimpleNamespace(stream_id=stream.stream_id, receive_lock=guard,
                                   disconnect=Mock(), observe_frame=Mock())
        stream.bind_observer(observer)

        def hold_observer():
            with guard:
                held.set()
                release.wait(2)

        holder = threading.Thread(target=hold_observer, daemon=True)
        holder.start()
        try:
            self.assertTrue(held.wait(1))
            with patch("jcc_device.latest.threading.Thread.start", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt) as interruption:
                    stream.start()
            self.assertTrue(any("observer" in note.lower()
                                for note in interruption.exception.__notes__))
            self.assertEqual(source.close_calls, 1)
            self.assertFalse(source.entered.is_set())
            observer.disconnect.assert_not_called()
        finally:
            release.set()
            holder.join(2)
            stream.close()
        observer.disconnect.assert_called_once_with()
        self.assertEqual(source.close_calls, 1)

    def test_interrupted_start_before_source_claim_blocks_late_receiver_read(self):
        source = ControlledSource()
        entered_target, release_target = threading.Event(), threading.Event()
        close_threads, receiver = [], []
        source.close = lambda: close_threads.append(threading.current_thread().name)
        stream = LatestFrameStream(source, shutdown_timeout=.03)
        original_receive = stream._receive

        def delayed_receive():
            entered_target.set()
            release_target.wait(2)
            original_receive()

        stream._receive = delayed_receive
        real_start = threading.Thread.start

        def interrupt_before_claim(thread):
            receiver.append(thread)
            real_start(thread)
            if not entered_target.wait(1):
                raise AssertionError("Synthetic receiver did not reach target")
            raise KeyboardInterrupt("synthetic startup interruption")

        try:
            with patch("jcc_device.latest.threading.Thread.start", new=interrupt_before_claim):
                with self.assertRaises(KeyboardInterrupt) as interruption:
                    stream.start()
            self.assertTrue(any("shutdown_timeout" in note
                                for note in interruption.exception.__notes__))
            self.assertTrue(stream.stats()["receiver_thread_alive"])
            self.assertEqual(close_threads, [threading.current_thread().name])
            self.assertFalse(source.entered.is_set())
        finally:
            release_target.set()
            if receiver:
                receiver[0].join(2)
            stream.close()
        self.assertFalse(source.entered.is_set())
        self.assertEqual(close_threads, [threading.current_thread().name])
        self.assertFalse(stream.stats()["receiver_thread_alive"])

    def test_late_thread_bootstrap_after_interruption_cannot_read_closed_source(self):
        source = ControlledSource()
        entered_target, release_target, launch = (threading.Event() for _ in range(3))
        receiver, errors = [], []
        stream = LatestFrameStream(source, shutdown_timeout=.03)
        original_receive = stream._receive
        real_start = threading.Thread.start

        def delayed_receive():
            entered_target.set()
            release_target.wait(2)
            original_receive()

        def launch_late():
            try:
                if not launch.wait(2):
                    raise AssertionError("Synthetic late launch was not released")
                real_start(receiver[0])
                receiver[0].join(2)
            except BaseException as error:
                errors.append(error)

        def interrupt_before_bootstrap(thread):
            receiver.append(thread)
            raise KeyboardInterrupt("synthetic startup interruption")

        stream._receive = delayed_receive
        launcher = threading.Thread(target=launch_late, daemon=True)
        launcher.start()
        try:
            with patch("jcc_device.latest.threading.Thread.start", new=interrupt_before_bootstrap):
                with self.assertRaises(KeyboardInterrupt):
                    stream.start()
            self.assertFalse(stream.stats()["receiver_thread_alive"])
            self.assertEqual(source.close_calls, 1)
            launch.set()
            self.assertTrue(entered_target.wait(1))
            self.assertTrue(stream.stats()["receiver_thread_alive"])
            self.assertFalse(source.entered.is_set())
        finally:
            launch.set()
            release_target.set()
            launcher.join(2)
            stream.close()
        self.assertFalse(launcher.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(source.entered.is_set())
        self.assertEqual(source.close_calls, 1)
        self.assertFalse(stream.stats()["receiver_thread_alive"])


class LegacyByteSourceTests(unittest.TestCase):
    def test_jpeg_extraction_and_close_clear_old_session_buffer(self):
        stream = TabletStream(capture_diagnostics=False)
        proc = Mock()
        proc.poll.return_value = 0
        stream._capture = proc
        stream._decode = SimpleNamespace(stdout=Mock(), poll=lambda: 0,
                                         wait=lambda timeout: None)
        stream._buffer.extend(b"junk\xff\xd8one\xff\xd9\xff\xd8two\xff\xd9")
        self.assertEqual(stream.read_frame(), b"\xff\xd8one\xff\xd9")
        self.assertEqual(stream.read_frame(), b"\xff\xd8two\xff\xd9")
        stream._buffer.extend(b"old-session")
        stream.close()
        self.assertEqual(stream._buffer, bytearray())
        stream.close()


if __name__ == "__main__":
    unittest.main()
