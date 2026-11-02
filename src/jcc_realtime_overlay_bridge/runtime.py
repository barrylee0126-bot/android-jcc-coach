"""A spawned, paused 10H synthetic producer feeding a running 14B consumer."""

from http.client import HTTPConnection
import json
import os
from pathlib import Path
import selectors
import secrets
import subprocess
import sys
import threading
import time

from jcc_device.latest import Frame, FrameRef
from jcc_realtime_probe.core import ProbeCore
from jcc_realtime_probe.events import EventSink
from jcc_realtime_probe.processing import simulated_report
from jcc_realtime_probe.timeline import png, synthetic_profile
from jcc_timed_overlay.server import TimedOverlayServer
from jcc_timed_overlay.source import decode_event

from .bridge import RealtimeOverlayBridge
from .clocks import bind_spawned, spawned_hello
from .transport import JsonlDecoder, MAX_CHUNK, MAX_LINE


def produce(*, nonce, run_id, cycles=1, start_delay=0.3, processing_ms=80):
    """Only generated pixels and explicit simulated work; no OCR or device."""
    print(json.dumps(spawned_hello(nonce, run_id)), flush=True)
    if sys.stdin.readline().strip() != "go":
        return 2
    time.sleep(start_delay)
    sink = EventSink(run_id, "synthetic", "host_monotonic_ns", time.monotonic_ns, sys.stdout,
                     processing_kind="simulated_work")
    stream = "generated-" + run_id
    core = ProbeCore(stream, synthetic_profile(), sink)
    sequence = 0
    work = processing_ms / 1000

    def receive():
        nonlocal sequence
        sequence += 1
        core.observe_frame(Frame(FrameRef(stream, sequence, time.monotonic_ns(), "synthetic-not-device-capture"),
                                 png(outside=sequence % 256)))

    def complete(job, outcome="simulated_ready"):
        core.complete(job, simulated_report(outcome=outcome))

    for _ in range(cycles):
        for outcome in ("simulated_ready", "simulated_no_output"):
            receive()
            job = core.start_job()
            time.sleep(work)
            complete(job, outcome)
            time.sleep(max(0, .8 - work))
        receive()
        late = core.start_job()
        time.sleep(.8)
        receive()
        time.sleep(work)
        complete(late)
        current = core.start_job()
        time.sleep(work)
        complete(current, "simulated_error")
        time.sleep(max(0, .8 - 2 * work))
        receive()
        job = core.start_job()
        time.sleep(work)
        complete(job)
        time.sleep(1.1)
        core.tick()
    core.disconnect()
    summary = core.summary()
    print(json.dumps(dict(producer_counts=summary["counts"], generated_RGB=True,
                          processing_kind=summary["processing_kind"], ocr_totals=summary["ocr_totals"],
                          processing_outcome_counts=summary["processing_outcome_counts"],
                          unfinished_active_jobs=summary["unfinished_active_jobs"],
                          actual_OCR=False)), file=sys.stderr, flush=True)
    return 0


class ProcessSource:
    def __init__(self, *, cycles=1, start_delay=.3, processing_ms=80):
        self.lock = threading.RLock()
        self.finished = False
        self.closed = False
        self.ingested_while_producer_alive = 0
        self.producer_summary = None
        nonce, run_id = secrets.token_hex(16), secrets.token_hex(8)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        sent = time.monotonic_ns()
        self.process = subprocess.Popen(
            [sys.executable, "-B", "-m", "jcc_realtime_overlay_bridge", "_producer", "--nonce", nonce,
             "--run-id", run_id, "--cycles", str(cycles), "--start-delay", str(start_delay),
             "--processing-ms", str(processing_ms)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        try:
            descriptor = self.process.stdout.fileno()
            os.set_blocking(descriptor, False)
            hello_bytes = b""
            deadline = time.monotonic() + 5
            with selectors.DefaultSelector() as selector:
                selector.register(descriptor, selectors.EVENT_READ)
                while b"\n" not in hello_bytes:
                    if time.monotonic() >= deadline:
                        raise ValueError("clock_handshake_timeout")
                    if selector.select(.1):
                        chunk = os.read(descriptor, MAX_LINE + 1)
                        if not chunk:
                            raise ValueError("clock_handshake_eof")
                        hello_bytes += chunk
                        if len(hello_bytes) > MAX_LINE:
                            raise ValueError("clock_handshake_size")
            hello_line, extra = hello_bytes.split(b"\n", 1)
            if extra:
                raise ValueError("production_before_consumer")
            hello = decode_event(hello_line)
            binding = bind_spawned(hello, nonce=nonce, run_id=run_id, child_pid=self.process.pid,
                                   sent_ns=sent, received_ns=time.monotonic_ns())
            self.bridge = RealtimeOverlayBridge(binding)
            self.decoder = JsonlDecoder(self.bridge)
            self.process.stdin.write(b"go\n")
            self.process.stdin.flush()
            self.process.stdin.close()
        except Exception:
            self._stop_process()
            self._close_pipes()
            raise

    @property
    def store(self):
        return self.bridge.store

    def _stop_process(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)

    def _close_pipes(self):
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()

    def poll(self):
        with self.lock:
            if self.finished or self.closed:
                return
            alive = self.process.poll() is None
            try:
                data = os.read(self.process.stdout.fileno(), MAX_CHUNK)
            except BlockingIOError:
                self.bridge.tick()
                return
            if data:
                before = self.bridge.counts["input_events_seen"]
                self.decoder.feed(data)
                if alive:
                    self.ingested_while_producer_alive += self.bridge.counts["input_events_seen"] - before
            else:
                self.finished = True
                self.decoder.eof()
                try:
                    self.process.wait(timeout=2)
                    self.producer_summary = decode_event(self.process.stderr.read(32768))
                except (ValueError, UnicodeError, subprocess.TimeoutExpired):
                    self.bridge.fault("producer_end_unproven")
            self.bridge.tick()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if not self.finished:
                self.decoder.fail("producer_stopped")
            self._stop_process()
            self._close_pipes()


def run_stream(*, serve=False, seconds=12, port=0, cycles=1, start_delay=.3, processing_ms=80):
    source = ProcessSource(cycles=cycles, start_delay=start_delay, processing_ms=processing_ms)
    server = None
    thread = None
    trace = []
    http_ready = 0
    try:
        server = TimedOverlayServer(port, source.store, source)
        server.start_maintenance()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        address = server.server_address[1]
        if serve:
            print(f"模拟桥接显示：http://127.0.0.1:{address}/?compact=1", flush=True)
            print("输入为子进程自造RGB，提示只测试显示；到时自动停止，Ctrl+C可提前结束。", flush=True)
        end = time.monotonic() + seconds
        try:
            while time.monotonic() < end:
                connection = HTTPConnection("127.0.0.1", address, timeout=1)
                try:
                    connection.request("GET", "/api/view")
                    response = connection.getresponse()
                    view = decode_event(response.read())
                    if response.status != 200:
                        raise ValueError("http_view_unavailable")
                finally:
                    connection.close()
                state = dict(status=view["status"], sample_id=view["sample_id"], message=view["message"])
                if not trace or trace[-1] != state:
                    trace.append(state)
                http_ready += int(view["status"] == "ready")
                if source.finished and not serve:
                    break
                time.sleep(.025 if not serve else .1)
        except KeyboardInterrupt:
            pass
    finally:
        if server is not None:
            if thread is not None and thread.is_alive():
                server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        source.close()
    report = source.bridge.report()
    verified = (source.finished and source.process.returncode == 0
                and source.ingested_while_producer_alive > 0 and http_ready > 0
                and report["fault"] == "transport_eof"
                and report["opportunities"] == 5 * cycles
                and report["consumer"]["opportunities"] == 5 * cycles
                and sum(report["outcomes"].values()) == 5 * cycles
                and sum(report["consumer"]["outcomes"].values()) == 5 * cycles
                and source.producer_summary is not None
                and source.producer_summary.get("producer_counts", {}).get("frames_checked") == report["accepted_frames"]
                and source.producer_summary.get("producer_counts", {}).get("jobs_completed") == report["processing"]["completed_jobs_observed"]
                and source.producer_summary.get("ocr_totals") == report["processing"]["ocr_totals_observed"]
                and source.producer_summary.get("producer_counts", {}).get("formal_accepted") == 0)
    result = dict(validation="running_synthetic_stream_passed" if verified else "running_synthetic_stream_not_passed",
                  producer_to_consumer_streaming_verified=verified, producer_exit_code=source.process.returncode,
                  events_ingested_while_producer_alive=source.ingested_while_producer_alive,
                  ready_observations_via_real_http=http_ready, display_state_trace=trace,
                  producer=source.producer_summary, bridge=report)
    source.store.close()
    return result
