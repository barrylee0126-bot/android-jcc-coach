"""Real SIGINT races in an isolated process with an in-memory source."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


COMMON = r'''
import os
import signal
import threading
from jcc_device.latest import FrameShutdownTimeout, LatestFrameStream

signal.signal(signal.SIGINT, signal.default_int_handler)

class Source:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.close_threads = []

    def read_frame(self, timeout):
        self.entered.set()
        self.release.wait(timeout)
        raise TimeoutError

    def close(self):
        self.close_threads.append(threading.current_thread().name)
        self.closed.set()

def interrupt():
    os.kill(os.getpid(), signal.SIGINT)

def assert_pending_and_bounded(stream, source):
    assert stream.stats()["receiver_thread_alive"], "active receiver misreported as stopped"
    assert not source.closed.is_set(), "source closed while read was pending"
    try:
        stream.close()
    except FrameShutdownTimeout:
        pass
    else:
        raise AssertionError("close returned before receiver completed")
    assert stream.stats()["receiver_thread_alive"], "timed-out receiver misreported as stopped"
    assert not source.closed.is_set(), "caller closed an in-use source"
'''


class RealSignalTests(unittest.TestCase):
    def run_isolated(self, body):
        env = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run([sys.executable, "-B", "-c", COMMON + textwrap.dedent(body)],
                                env=env, capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_sigint_during_close_does_not_falsely_finish_receiver(self):
        self.run_isolated('''
            source = Source()
            stream = LatestFrameStream(source, read_timeout=1.5, shutdown_timeout=.12).start()
            assert source.entered.wait(1)
            timer = threading.Timer(.04, interrupt)
            timer.start()
            try:
                try:
                    stream.close()
                except KeyboardInterrupt:
                    pass
                else:
                    raise AssertionError("SIGINT did not interrupt close")
                assert_pending_and_bounded(stream, source)
            finally:
                source.release.set()
                assert source.closed.wait(2)
                timer.join(1)
            stream.close()
            assert not stream.stats()["receiver_thread_alive"]
            assert len(source.close_threads) == 1
            assert source.close_threads[0].startswith("jcc-receiver-")
        ''')

    def test_real_second_sigint_during_interrupted_start_cleanup(self):
        self.run_isolated('''
            from unittest.mock import patch

            source = Source()
            stream = LatestFrameStream(source, read_timeout=1.5, shutdown_timeout=.12)
            real_start = threading.Thread.start
            timers = []

            def start_then_interrupt(thread):
                real_start(thread)
                assert source.entered.wait(1)
                timer = threading.Timer(.04, interrupt)
                timers.append(timer)
                real_start(timer)
                raise KeyboardInterrupt("first interruption")

            try:
                with patch("jcc_device.latest.threading.Thread.start", new=start_then_interrupt):
                    try:
                        stream.start()
                    except KeyboardInterrupt as error:
                        assert not error.args, "second SIGINT was not propagated"
                        assert isinstance(error.__cause__, KeyboardInterrupt)
                        assert error.__cause__.args == ("first interruption",)
                    else:
                        raise AssertionError("startup interruption did not propagate")
                assert_pending_and_bounded(stream, source)
            finally:
                source.release.set()
                assert source.closed.wait(2)
                for timer in timers:
                    timer.join(1)
            stream.close()
            assert not stream.stats()["receiver_thread_alive"]
            assert len(source.close_threads) == 1
            assert source.close_threads[0].startswith("jcc-receiver-")
        ''')

    def test_interrupted_external_join_cannot_hide_running_receiver(self):
        self.run_isolated('''
            source = Source()
            stream = LatestFrameStream(source, read_timeout=1.5, shutdown_timeout=.12).start()
            receiver = stream._thread
            assert source.entered.wait(1)
            timer = threading.Timer(.04, interrupt)
            timer.start()
            try:
                try:
                    receiver.join(1)
                except KeyboardInterrupt:
                    pass
                else:
                    raise AssertionError("SIGINT did not interrupt external join")
                # Some Python versions now report receiver.is_alive() false
                # despite the source still being inside read_frame().
                assert_pending_and_bounded(stream, source)
            finally:
                source.release.set()
                assert source.closed.wait(2)
                timer.join(1)
            stream.close()
            assert not stream.stats()["receiver_thread_alive"]
            assert len(source.close_threads) == 1
            assert source.close_threads[0].startswith("jcc-receiver-")
        ''')


if __name__ == "__main__":
    unittest.main()
