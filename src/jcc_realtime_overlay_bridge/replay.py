"""Paced private PNG replay through existing freshness, OCR and overlay owners."""

from dataclasses import replace
import json
from pathlib import Path
import statistics
import threading
import time
from uuid import uuid4

from jcc_device.latest import Frame, LatestFrameStream
from jcc_device.vision_ocr import VisionOCR
from jcc_realtime_probe.core import ProbeCore
from jcc_realtime_probe.events import EventSink
from jcc_realtime_probe.pixels import MAX_BYTES
from jcc_realtime_probe.profile import outside_project, read_profile
from jcc_realtime_probe.replay_ocr import ReplayGoldOCR
from jcc_timed_overlay.server import TimedOverlayServer

from .bridge import RealtimeOverlayBridge
from .clocks import bind_same_process
from .transport import JsonlDecoder


class PacedFrames:
    """Read each private image at its playback offset, not during setup."""

    def __init__(self, manifest, *, start_delay=5):
        path = outside_project(manifest)
        if not 0 <= start_delay <= 30 or path.stat().st_size > 1_048_576:
            raise ValueError("invalid replay manifest or start delay")
        self.rows = []
        for line in path.read_text().splitlines():
            row = json.loads(line)
            sequence, offset, name = row["source_sequence"], row["received_offset_ns"], row["image_file"]
            if (type(sequence) is not int or sequence != len(self.rows) + 1 or type(offset) is not int
                    or offset < 0 or not isinstance(name, str) or not name or Path(name).is_absolute()):
                raise ValueError("invalid replay row")
            image = (path.parent / name).resolve()
            if path.parent not in image.parents or not image.is_file() or image.stat().st_size > MAX_BYTES:
                raise ValueError("invalid private replay image")
            if self.rows and offset <= self.rows[-1][0]:
                raise ValueError("replay offsets must increase")
            self.rows.append((offset, image))
            if len(self.rows) > 240 or offset > 120_000_000_000:
                raise ValueError("replay bounds exceeded")
        if not self.rows or self.rows[0][0] != 0:
            raise ValueError("replay must begin at offset zero")
        self.delay = start_delay
        self.origin = None
        self.index = 0
        self.exhausted_at = None
        self.reads = []
        self.closed = threading.Event()

    def read_frame(self, timeout=0.25):
        if self.origin is None:
            self.origin = time.monotonic_ns() + int(self.delay * 1e9)
        if self.index >= len(self.rows):
            self.closed.wait(timeout)
            raise TimeoutError
        offset, path = self.rows[self.index]
        wait = max(0, (self.origin + offset - time.monotonic_ns()) / 1e9)
        if wait and self.closed.wait(min(wait, timeout)):
            raise TimeoutError
        if wait > timeout:
            raise TimeoutError
        started = time.monotonic_ns()
        data = path.read_bytes()
        ended = time.monotonic_ns()
        if not 0 < len(data) <= MAX_BYTES:
            raise ValueError("invalid private replay image")
        self.index += 1
        self.reads.append(dict(sequence=self.index, started_ns=started, completed_ns=ended))
        if self.index == len(self.rows):
            self.exhausted_at = ended
        return data

    def close(self):
        self.closed.set()


class ReplayObserver:
    def __init__(self, core):
        self.core, self.stream_id, self.receive_lock = core, core.stream_id, core.receive_lock

    def observe_frame(self, frame):
        source = replace(frame.source, host_received_time_source="mac_offline_png_read_complete_not_capture")
        return self.core.observe_frame(Frame(source, frame.jpeg))

    def disconnect(self):
        self.core.disconnect()


class ReplayService:
    """Same-process bridge; only sanitized events, never images, enter HTTP."""

    def __init__(self, profile, frames, ocr):
        self.frames = frames
        self.stream = LatestFrameStream(frames)
        self.sink = EventSink(uuid4().hex[:16], "replay", "host_monotonic_ns", time.monotonic_ns,
                              processing_kind="ocr")
        self.bridge = RealtimeOverlayBridge(bind_same_process(self.sink))
        self.store = self.bridge.store
        self.decoder = JsonlDecoder(self.bridge)
        self.sink.file = self
        self.core = ProbeCore(self.stream.stream_id, profile, self.sink)
        self.processor = ReplayGoldOCR(profile, ocr)
        self.stream.bind_observer(ReplayObserver(self.core))
        self.stop = threading.Event()
        self.errors = []
        self.worker = None

    def write(self, text):
        self.decoder.feed(text.encode("utf-8"))

    def flush(self):
        pass

    def poll(self):
        with self.core.receive_lock:
            self.core.tick()
        self.bridge.tick()

    def start(self):
        self.stream.start()
        self.worker = threading.Thread(target=self._work, daemon=True, name="jcc-replay-local-ocr")
        self.worker.start()

    def _work(self):
        try:
            while not self.stop.is_set():
                with self.core.receive_lock:
                    job = self.core.start_job()
                if job is None:
                    self.stop.wait(.005)
                    continue
                report = self.processor(job)
                self.errors.extend(report["error_types"])
                with self.core.receive_lock:
                    self.core.complete(job, report)
        except Exception as error:
            self.errors.append(type(error).__name__)
            self.stop.set()
            self.bridge.fault("replay_worker_unavailable")

    def close(self):
        self.stop.set()
        try:
            self.stream.close()
        except Exception as error:
            self.errors.append(type(error).__name__)
            self.bridge.fault("replay_stream_close_failed")
        if self.worker is not None and self.worker.ident is not None:
            self.worker.join(timeout=8)
            if self.worker.is_alive():
                self.errors.append("WorkerShutdownTimeout")
                self.bridge.fault("replay_worker_shutdown_timeout")


def _distribution(values):
    values = sorted(values)
    return dict(n=len(values), median_ms=statistics.median(values) if values else None,
                min_ms=values[0] if values else None, max_ms=values[-1] if values else None,
                p95_ms=values[(95 * len(values) + 99) // 100 - 1] if len(values) >= 20 else None)


def replay_report(service):
    bridge = service.bridge.report()
    producer = service.core.summary()
    consumer = service.store.report()
    reads = {r["sequence"]: r for r in service.frames.reads}
    dom, paint = [], []
    for item in consumer["samples"]:
        sequence = int(item["sample_id"].rsplit(":", 1)[1])
        read = reads.get(sequence)
        if read is None:
            continue
        for target, key in ((dom, "first_dom_report_received_ns"),
                            (paint, "first_paint_report_received_ns")):
            if item[key] is not None:
                # Store reports serialize nanosecond integers as decimal text
                # so JavaScript cannot silently round them.
                target.append((int(item[key]) - read["started_ns"]) / 1e6)
    return dict(schema="offline-replay-display-v1", source_kind="replay",
                processing="actual_local_ocr_not_verified_game_state", simulation=True,
                execution_eligible=False, matched_games_started=0,
                time_basis="same Mac; each frame's local PNG read start, not recorded capture",
                excludes="offline video extraction, tablet capture, user eyesight and display hardware",
                recordings_read=len(reads), total_frames_in_manifest=len(service.frames.rows),
                local_read_ms=_distribution([(v["completed_ns"] - v["started_ns"]) / 1e6 for v in reads.values()]),
                read_start_to_dom_report_received_ms=_distribution(dom),
                read_start_to_paint_report_received_ms=_distribution(paint),
                physical_visible_latency_ms=None, error_types=sorted(set(service.errors)),
                producer=producer, bridge=bridge)


def run_replay(manifest, profile_path, *, port=0, start_delay=5):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid port")
    profile = read_profile(profile_path, "replay")
    frames = PacedFrames(manifest, start_delay=start_delay)
    server = thread = service = None
    with VisionOCR(timeout=5) as ocr:
        try:
            service = ReplayService(profile, frames, ocr)
            server = TimedOverlayServer(port, service.store, service)
            server.start_maintenance()
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
            thread.start()
            print(f"离线回放模拟提示：http://127.0.0.1:{server.server_address[1]}/?compact=1", flush=True)
            print("仅回放候选的模拟显示；不是实战建议。", flush=True)
            service.start()
            end = time.monotonic() + start_delay + frames.rows[-1][0] / 1e9 + 10
            while not service.stop.wait(.025) and time.monotonic() < end:
                if frames.exhausted_at is not None and time.monotonic_ns() >= frames.exhausted_at + 1_300_000_000:
                    break
                state = service.stream.stats()
                if state["source_error_type"] or not state["receiver_thread_alive"]:
                    service.errors.append(state["source_error_type"] or "ReceiverStopped")
                    break
            service.close()
            # Keep HTTP open briefly for the browser to observe withdrawal.
            time.sleep(.5)
            result = replay_report(service)
        finally:
            if service is not None and not service.stop.is_set():
                service.close()
            if server is not None:
                if thread is not None and thread.is_alive():
                    server.shutdown()
                server.server_close()
            if thread is not None:
                thread.join(timeout=2)
            if service is not None:
                service.store.close()
    return result
