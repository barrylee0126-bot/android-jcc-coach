"""Read-only integration of the tablet stream, Sunflower and the frame monitor."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import time

from PIL import Image
from src.frame_monitor import FrameMonitor
from sunflower.sunflower_catcher import SunflowerCatcher

from .stream import TabletStream
from .vision_ocr import VisionOCR


class ReadOnlyTransport:
    """Provide Sunflower frames without exposing any device input method."""

    available = True

    def __init__(self, stream: TabletStream):
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
    with TabletStream(sample_fps=sample_fps) as stream, VisionOCR() as ocr:
        frame = stream.read_frame()
        timestamps.append(time.monotonic())
        with Image.open(io.BytesIO(frame)) as image:
            size = image.size
        # Generic full-screen change detector; game-specific ROIs need calibration.
        monitor = FrameMonitor(size, regions={"screen": (0, 0, 1, 1)}, thresholds={"screen": 8})
        monitor.observe(frame)
        for _ in range(frames - 1):
            frame = stream.read_frame()
            timestamps.append(time.monotonic())
            changes += sum(event.triggered for event in monitor.observe(frame))
        catcher = SunflowerCatcher()
        catcher._device = ReadOnlyTransport(stream)
        catcher.ocr_engine = ocr
        ocr_started = time.monotonic()
        # Vision and Paddle confidence scales differ; no threshold calibration yet.
        text_items = await catcher.get_screen_text(confidence=0.0)
        ocr_seconds = time.monotonic() - ocr_started
    return {
        "frames": len(timestamps),
        "screen_size": list(size),
        "first_frame_seconds": round(timestamps[0] - started, 2),
        "sample_fps_observed": round((len(timestamps) - 1) / (timestamps[-1] - timestamps[0]), 2),
        "generic_change_events": changes,
        "ocr_text_count": len(text_items or []),
        "ocr_and_frame_seconds": round(ocr_seconds, 2),
        "note": "只读测试；采样速度不代表决策或操作延迟，区域尚未校准",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="对平板进行只读观察和本地文字识别")
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--sample-fps", type=int, default=4)
    args = parser.parse_args()
    if args.frames < 2 or args.sample_fps < 1:
        parser.error("--frames must be >= 2 and --sample-fps must be >= 1")
    print(json.dumps(asyncio.run(observe(args.frames, args.sample_fps)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
