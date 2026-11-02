"""Small separate-process JSONL fixture for exercising the local tail adapter.

Start the server on an empty file first. This producer uses this Mac's current
Python monotonic clock, writes only synthetic metadata, then exits once.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import time

from .protocol import EVENT_SCHEMA, anonymous_id
from .store import NS_MS


def emit_fixture(path: Path, clock_domain_id: str, stream_id: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK | os.O_NOFOLLOW)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_size != 0:
        os.close(descriptor)
        raise ValueError("fixture requires an empty regular file and a single writer")
    sequence = 0
    with os.fdopen(descriptor, "w", encoding="utf-8", buffering=1) as file:
        def write(status: str, sample_id: str, frame: int | None = None, expires: int | None = None) -> None:
            nonlocal sequence
            sequence += 1
            now = time.monotonic_ns()
            row = {
                "schema_version": EVENT_SCHEMA, "sample_id": sample_id, "source_kind": "synthetic",
                "stream_id": stream_id, "clock_domain_id": clock_domain_id, "sequence": sequence,
                "frame_received_monotonic_ns": frame or now, "emitted_monotonic_ns": now,
                "expires_at_monotonic_ns": expires or now + 2000 * NS_MS, "status": status,
            }
            file.write(json.dumps(row, separators=(",", ":")) + "\n")

        def wait_with_beats(ms: int) -> None:
            end = time.monotonic_ns() + ms * NS_MS
            while time.monotonic_ns() < end:
                time.sleep(min(.2, max(0, (end - time.monotonic_ns()) / 1e9)))
                write("heartbeat", "source-control")

        frame = time.monotonic_ns()
        expires = frame + 1000 * NS_MS
        write("pending", "jsonl-synthetic-0001", frame, expires)
        time.sleep(.08)
        write("ready", "jsonl-synthetic-0001", frame, expires)
        wait_with_beats(1100)
        write("no_result", "jsonl-synthetic-0002")
        wait_with_beats(250)
        frame = time.monotonic_ns()
        write("pending", "jsonl-synthetic-0003", frame, frame + 400 * NS_MS)
        wait_with_beats(600)
        write("disconnected", "source-control")


def main() -> int:
    parser = argparse.ArgumentParser(description="只写合成匿名事件，供本机 JSONL 计时适配器独立验证。")
    parser.add_argument("--output", type=Path, required=True, help="已存在、空的普通 JSONL 文件；先启动读取服务")
    parser.add_argument("--clock-domain-id", required=True)
    parser.add_argument("--stream-id", required=True)
    args = parser.parse_args()
    if not anonymous_id(args.clock_domain_id) or not anonymous_id(args.stream_id):
        parser.error("只接受匿名 ASCII 时钟和流 ID。")
    try:
        emit_fixture(args.output, args.clock_domain_id, args.stream_id)
    except (OSError, ValueError):
        parser.error("需要已存在、空的普通文件；请重新建立独立计时会话。")
    print("模拟计时 JSONL 已写入：3 个合成样本（显示、无结果、等待超时）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
