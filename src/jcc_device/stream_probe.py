"""Reproducible timing-only latest-stream experiment; never saves images."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import statistics
import threading
import time

from .latest import LatestFrameStream, positive_seconds
from .recognition import FrameProcessor
from .stream import TabletStream


class PacedFakeSource:
    """Synthetic bytes at a controlled rate; not images or game samples."""

    def __init__(self, fps: int):
        self.interval = 1 / fps
        self._next = time.monotonic()
        self._closed = threading.Event()
        self._sequence = 0

    def read_frame(self, timeout: float = 10.0) -> bytes:
        remaining = self._next - time.monotonic()
        if self._closed.wait(max(0, min(remaining, timeout))):
            raise RuntimeError("Synthetic source closed")
        if remaining > timeout:
            raise TimeoutError("Synthetic frame not due")
        self._sequence += 1
        self._next += self.interval
        return f"synthetic-frame-{self._sequence}".encode("ascii")

    def close(self) -> None:
        self._closed.set()


def _phase(stream: LatestFrameStream, count: int, delay: float) -> dict:
    delivery_ages, records = [], []

    def work(frame):
        if delay:
            time.sleep(delay)
        return len(frame.jpeg)

    processor = FrameProcessor(work)
    before = stream.stats()
    for _ in range(count):
        frame = stream.read_latest(timeout=15)
        delivery_ages.append(frame.age_seconds())
        records.append(processor.process(frame))
    after = stream.stats()
    refs = [record.source for record in records]
    return {
        "consumer_delay_seconds": delay,
        "frames_processed": count,
        "sequences": [ref.sequence for ref in refs],
        "sequence_gaps": sum(b.sequence - a.sequence - 1 for a, b in zip(refs, refs[1:])),
        "received_during_phase": after["frames_received"] - before["frames_received"],
        "replaced_during_phase": after["frames_replaced"] - before["frames_replaced"],
        "delivery_age_seconds_median": statistics.median(delivery_ages),
        "delivery_age_seconds_max": max(delivery_ages),
        "receive_to_finish_seconds_median": statistics.median(
            record.timing()["receive_to_finish_seconds"] for record in records),
        "processor_stats": processor.stats(),
        "frame_timings": [record.timing() for record in records],
    }


def benchmark(*, source_name: str, sample_fps: int, fast_frames: int,
              slow_frames: int, slow_seconds: float) -> dict:
    started = time.monotonic()
    opened = None
    stream = None
    report = {"source": source_name, "started_utc": datetime.now(timezone.utc).isoformat(),
              "sample_fps_requested": sample_fps,
              "device_capture_time_measured": False, "images_saved": 0,
              "note": "App receive timing only; no device capture or decision latency measured"}
    try:
        if source_name == "tablet":
            opened = TabletStream(sample_fps=sample_fps, capture_diagnostics=False).__enter__()
        else:
            opened = PacedFakeSource(sample_fps)
        stream = LatestFrameStream(opened).start()
        first = stream.read_latest(timeout=15)
        report["first_frame_seconds"] = (first.source.host_received_monotonic_ns / 1e9) - started
        report["first_frame_source"] = first.source.metadata()
        report["fast_consumer"] = _phase(stream, fast_frames, 0)
        report["slow_consumer"] = _phase(stream, slow_frames, slow_seconds)
        report["status"] = "ok"
    except Exception as exc:
        # Never emit source messages, OCR text, device IDs, or subprocess logs.
        report.update(status="error", error_type=type(exc).__name__)
    finally:
        closing = time.monotonic()
        try:
            if stream is not None:
                stream.close()
            elif opened is not None:
                opened.close()
        except Exception as exc:
            report.update(status="error", shutdown_error_type=type(exc).__name__)
        report["close_seconds"] = time.monotonic() - closing
        report["total_seconds"] = time.monotonic() - started
        if stream is not None:
            report["capture_stats"] = stream.stats()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="只读单槽取帧实验；只输出时间与计数")
    parser.add_argument("--source", choices=("synthetic", "tablet"), default="synthetic")
    parser.add_argument("--sample-fps", type=int, default=4)
    parser.add_argument("--fast-frames", type=int, default=12)
    parser.add_argument("--slow-frames", type=int, default=6)
    parser.add_argument("--slow-seconds", type=float, default=0.8)
    args = parser.parse_args()
    if min(args.sample_fps, args.fast_frames, args.slow_frames) < 1:
        parser.error("frame rates and counts must be positive")
    try:
        positive_seconds(args.slow_seconds, "slow-seconds")
    except ValueError as exc:
        parser.error(str(exc))
    report = benchmark(source_name=args.source, sample_fps=args.sample_fps,
                       fast_frames=args.fast_frames, slow_frames=args.slow_frames,
                       slow_seconds=args.slow_seconds)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
