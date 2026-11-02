"""Read-only integration of the tablet stream, local OCR and the frame monitor."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import io
import json
import time

from PIL import Image
from src.frame_monitor import FrameMonitor

from .latest import LatestFrameStream
from .recognition import FrameProcessor
from .stream import TabletStream
from .vision_ocr import VisionOCR


class ReadOnlyTransport:
    """Provide Sunflower frames without exposing any device input method."""

    available = True

    def __init__(self, stream: TabletStream | LatestFrameStream):
        self.stream = stream

    async def exec_out(self, command: str, decode: bool = False) -> bytes:
        if command != "screencap -p" or decode:
            raise RuntimeError("Only read-only frame capture is supported")
        return await asyncio.to_thread(self.stream.read_frame)

    async def shell(self, command: str) -> None:
        raise RuntimeError("Device control is disabled in read-only observation")


async def observe(frames: int, sample_fps: int) -> dict:
    started = time.monotonic()
    timestamps = []
    changes = 0
    with TabletStream(sample_fps=sample_fps, capture_diagnostics=False) as source, \
            LatestFrameStream(source) as stream, VisionOCR() as ocr:
        frame = stream.read_latest()
        timestamps.append(frame.source.host_received_monotonic_ns / 1e9)
        with Image.open(io.BytesIO(frame.jpeg)) as image:
            size = image.size
        # Generic full-screen change detector; game-specific ROIs need calibration.
        monitor = FrameMonitor(size, regions={"screen": (0, 0, 1, 1)}, thresholds={"screen": 8})
        change_processor = FrameProcessor(lambda frame: monitor.observe(frame.jpeg))
        change_processor.process(frame)
        for _ in range(frames - 1):
            frame = stream.read_latest()
            timestamps.append(frame.source.host_received_monotonic_ns / 1e9)
            changes += sum(event.triggered for event in change_processor.process(frame).value)
        ocr_started = time.monotonic()
        # Vision and Paddle confidence scales differ; no threshold calibration yet.
        ocr_processor = FrameProcessor(lambda frame: ocr.recognize(frame.jpeg))
        def read_and_recognize():
            return ocr_processor.process(stream.read_latest())

        result = await asyncio.to_thread(read_and_recognize)
        text_items = result.value
        ocr_seconds = time.monotonic() - ocr_started
    stream_stats = stream.stats()
    return {
        "frames": len(timestamps),
        "screen_size": list(size),
        "first_frame_seconds": round(timestamps[0] - started, 2),
        "sample_fps_observed": round((len(timestamps) - 1) / (timestamps[-1] - timestamps[0]), 2),
        "generic_change_events": changes,
        "ocr_text_count": len(text_items or []),
        "ocr_and_frame_seconds": round(ocr_seconds, 2),
        "ocr_frame_timing": result.timing(),
        "capture_stats": stream_stats,
        "change_processing_stats": change_processor.stats(),
        "ocr_processing_stats": ocr_processor.stats(),
        "note": "只读测试；采样速度不代表决策或操作延迟，区域尚未校准",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="对平板进行只读观察和本地文字识别")
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--sample-fps", type=int, default=4)
    args = parser.parse_args()
    if args.frames < 2 or args.sample_fps < 1:
        parser.error("--frames must be >= 2 and --sample-fps must be >= 1")
    started = time.monotonic()
    started_utc = datetime.now(timezone.utc).isoformat()
    try:
        report = asyncio.run(observe(args.frames, args.sample_fps))
    except Exception as exc:
        report = {"status": "error", "error_type": type(exc).__name__,
                  "started_utc": started_utc,
                  "elapsed_seconds": time.monotonic() - started,
                  "note": "只读观察失败；未输出源日志或 OCR 文字"}
    else:
        report.update(status="ok", started_utc=started_utc)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
