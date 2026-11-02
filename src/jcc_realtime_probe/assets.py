"""Optional private receive recordings and replay manifests, never public assets."""

import json
import os
from pathlib import Path

from .pixels import MAX_BYTES
from .profile import outside_project


class Recorder:
    def __init__(self, directory):
        self.root = outside_project(directory)
        self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.manifest = (self.root / "frames.jsonl").open("x", encoding="utf-8")
        os.chmod(self.root / "frames.jsonl", 0o600)
        self.first = None
        self.count = 0

    def record(self, frame):
        s = frame.source
        self.first = s.host_received_monotonic_ns if self.first is None else self.first
        name = f"frame-{s.sequence:06d}.jpg"
        with (self.root / name).open("xb") as out:
            os.chmod(self.root / name, 0o600)
            out.write(frame.jpeg)
        row = dict(source_sequence=s.sequence, received_offset_ns=s.host_received_monotonic_ns - self.first,
                   image_file=name, receipt_time_basis="host_complete_jpeg_not_device_capture")
        self.manifest.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.manifest.flush()
        self.count += 1

    def close(self):
        self.manifest.close()


def read_recording(manifest):
    path = outside_project(manifest)
    if path.stat().st_size > 1_048_576:
        raise ValueError("Replay manifest too large")
    rows = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        seq, offset, name = row["source_sequence"], row["received_offset_ns"], row["image_file"]
        if (type(seq) is not int or seq < 1 or type(offset) is not int or offset < 0
                or not isinstance(name, str) or not name or Path(name).is_absolute()):
            raise ValueError("Invalid replay entry")
        image_path = (path.parent / name).resolve()
        if path.parent not in image_path.parents or image_path.stat().st_size > MAX_BYTES:
            raise ValueError("Replay image outside private recording or oversized")
        rows.append((offset, seq, image_path.read_bytes()))
        if len(rows) > 240:
            raise ValueError("Replay limited to 240 frames")
    if (not rows or rows[0][0] != 0 or any(a[0] > b[0] for a, b in zip(rows, rows[1:]))
            or rows[-1][0] > 120_000_000_000):
        raise ValueError("Replay must have ordered offsets starting at zero")
    return rows
