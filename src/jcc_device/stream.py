"""Capture a read-only scrcpy stream as in-memory JPEG frames.

scrcpy writes Matroska to a private FIFO; ffmpeg decodes it to JPEGs.  No
screen images are written to the project directory and no input is injected.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import time


PROJECT = Path(__file__).resolve().parents[2]
SCRCPY = PROJECT / "tools" / "scrcpy"


class TabletStream:
    """Own both subprocesses and the FIFO for one read-only capture session."""

    def __init__(self, max_size: int = 1600, device_fps: int = 15, sample_fps: int = 4,
                 *, capture_diagnostics: bool = True):
        if max_size <= 0 or device_fps <= 0 or sample_fps <= 0:
            raise ValueError("Frame sizes and rates must be positive")
        self.max_size = max_size
        self.device_fps = device_fps
        self.sample_fps = sample_fps
        self.capture_diagnostics = capture_diagnostics
        self._temp = None
        self._scrcpy_log = None
        self._ffmpeg_log = None
        self._capture = None
        self._decode = None
        self._buffer = bytearray()

    def __enter__(self) -> "TabletStream":
        if self._capture or self._decode:
            raise RuntimeError("The stream has already been started")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required to decode the tablet stream")
        self._temp = tempfile.TemporaryDirectory(prefix="jcc-stream-")
        fifo = Path(self._temp.name) / "capture.mkv"
        os.mkfifo(fifo, 0o600)
        self._buffer.clear()
        if self.capture_diagnostics:
            self._scrcpy_log = tempfile.TemporaryFile()
            self._ffmpeg_log = tempfile.TemporaryFile()
        try:
            self._decode = subprocess.Popen(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-probesize", "32",
                 "-analyzeduration", "0", "-i", str(fifo), "-vf", f"fps={self.sample_fps}",
                 "-an", "-c:v", "mjpeg", "-q:v", "4", "-f", "image2pipe", "pipe:1"],
                stdout=subprocess.PIPE, stderr=self._ffmpeg_log or subprocess.DEVNULL,
                bufsize=0,
            )
            self._capture = subprocess.Popen(
                [str(SCRCPY), "--no-control", "--no-window", "--no-power-on",
                 f"--max-size={self.max_size}", f"--max-fps={self.device_fps}",
                 "--record-format=mkv", f"--record={fifo}"],
                stdout=self._scrcpy_log or subprocess.DEVNULL,
                stderr=self._scrcpy_log or subprocess.DEVNULL,
            )
        except Exception:
            self.close()
            raise
        return self

    def _failure(self) -> str:
        messages = []
        for label, log in (("scrcpy", self._scrcpy_log), ("ffmpeg", self._ffmpeg_log)):
            if log:
                log.flush()
                log.seek(0)
                detail = log.read().decode("utf-8", "replace").strip()
                if detail:
                    messages.append(f"{label}: {detail[-1400:]}")
        return "\n".join(messages)

    def read_frame(self, timeout: float = 10.0) -> bytes:
        """Return one JPEG frame, or fail after a bounded wait."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if not self._decode or not self._decode.stdout or not self._capture:
            raise RuntimeError("The stream has not been started")
        deadline = time.monotonic() + timeout
        while True:
            start = self._buffer.find(b"\xff\xd8")
            end = self._buffer.find(b"\xff\xd9", max(start, 0) + 2)
            if start >= 0 and end >= 0:
                frame = bytes(self._buffer[start:end + 2])
                del self._buffer[:end + 2]
                return frame
            if len(self._buffer) > 16 * 1024 * 1024:
                raise RuntimeError("Unexpectedly large incomplete video frame")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for a tablet frame. " + self._failure())
            ready, _, _ = select.select([self._decode.stdout], [], [], min(remaining, 0.5))
            if ready:
                chunk = os.read(self._decode.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Tablet stream ended. " + self._failure())
                self._buffer.extend(chunk)
            elif self._capture.poll() is not None or self._decode.poll() is not None:
                raise RuntimeError("Tablet stream stopped. " + self._failure())

    def close(self) -> None:
        for proc in (self._capture, self._decode):
            if proc and proc.poll() is None:
                proc.terminate()
        for proc in (self._capture, self._decode):
            if proc:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
                if proc.stdout:
                    proc.stdout.close()
        for log in (self._scrcpy_log, self._ffmpeg_log):
            if log:
                log.close()
        if self._temp:
            self._temp.cleanup()
        self._capture = self._decode = None
        self._scrcpy_log = self._ffmpeg_log = self._temp = None
        self._buffer.clear()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="验证平板只读画面流")
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--sample-fps", type=int, default=4)
    parser.add_argument("--max-size", type=int, default=1600)
    args = parser.parse_args()
    if args.frames < 2:
        parser.error("--frames must be at least 2")
    started = time.monotonic()
    timestamps = []
    with TabletStream(max_size=args.max_size, sample_fps=args.sample_fps) as stream:
        for _ in range(args.frames):
            stream.read_frame()
            timestamps.append(time.monotonic())
    span = timestamps[-1] - timestamps[0]
    print(json.dumps({"frames": len(timestamps),
                      "first_frame_seconds": round(timestamps[0] - started, 2),
                      "sample_fps_observed": round((len(timestamps) - 1) / span, 2),
                      "note": "采样吞吐，不是端到端操作延迟"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
