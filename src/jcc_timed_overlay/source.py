"""Append-only local JSONL tail and an independent synthetic event producer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import threading

from .protocol import EVENT_SCHEMA
from .store import NS_MS, TimedOverlayStore


def decode_event(line: bytes) -> object:
    def unique_pairs(pairs: list[tuple]) -> dict:
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value
    return json.loads(line.decode("utf-8"), object_pairs_hook=unique_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))


class JsonlTail:
    """Only new complete lines. Removal, replacement or truncation closes source.

    Does not follow symlinks, read FIFOs, replay existing bytes, or log raw input.
    An unfinished existing line is skipped through its eventual newline.
    No filesystem path or error text is published to browsers/journals.
    """

    def __init__(self, path: Path, store: TimedOverlayStore) -> None:
        self.path, self.store = path, store
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("source must be a regular file")
            self._file = os.fdopen(descriptor, "rb", buffering=0)
        except Exception:
            os.close(descriptor)
            raise
        self._identity = (info.st_dev, info.st_ino)
        boundary = self._file.seek(0, os.SEEK_END)
        self._buffer = b""
        self._drop_until_newline = False
        if boundary:
            self._file.seek(boundary - 1)
            self._drop_until_newline = self._file.read(1) != b"\n"
            self._file.seek(boundary)
        self.failed = False

    def poll(self) -> None:
        if self.failed:
            return
        try:
            info = self.path.lstat()
            if (info.st_dev, info.st_ino) != self._identity or not stat.S_ISREG(info.st_mode) or info.st_size < self._file.tell():
                raise OSError("source changed")
            # Bound work per poll; the store rejects old production timestamps.
            chunk = self._file.read(32_768)
            if not chunk:
                return
            self._buffer += chunk
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                if self._drop_until_newline:
                    self._drop_until_newline = False
                    continue
                if not line.strip():
                    self.store.reject_input("empty_line")
                elif len(line) > 4096:
                    self.store.reject_input("line_too_long")
                else:
                    try:
                        event = decode_event(line)
                    except (ValueError, UnicodeError, RecursionError):
                        self.store.reject_input("invalid_json")
                    else:
                        self.store.ingest(event)
            if len(self._buffer) > 4096:
                if not self._drop_until_newline:
                    self.store.reject_input("line_too_long")
                self._buffer = b""
                self._drop_until_newline = True
        except OSError:
            self.failed = True
            self.store.source_fault("source_file_changed_or_unavailable")

    def close(self) -> None:
        self._file.close()


SCENARIOS = {"show", "expiring", "no_result", "pending_timeout", "invalidated", "disconnected", "silence", "bad_clock", "play"}


class SyntheticSource:
    """Actual local scheduling with fictional events, not artificial game facts."""

    def __init__(self, store: TimedOverlayStore) -> None:
        self.store = store
        self._lock = threading.RLock()
        self.sequence = 0
        self.samples = 0
        self._queue: list[tuple[int, str, object]] = []
        self._heartbeat = False
        self._last_beat = 0
        self.playing = False

    def _event(self, status: str, sample_id: str, frame: int, expires: int) -> dict:
        self.sequence += 1
        return {
            "schema_version": EVENT_SCHEMA, "sample_id": sample_id, "source_kind": "synthetic",
            "clock_domain_id": self.store.clock_domain_id, "stream_id": self.store.stream_id,
            "sequence": self.sequence, "frame_received_monotonic_ns": frame,
            "emitted_monotonic_ns": self.store.now(), "expires_at_monotonic_ns": expires, "status": status,
        }

    def _control(self, status: str) -> None:
        now = self.store.now()
        self.store.ingest(self._event(status, "source-control", now, now + 2000 * NS_MS))
        self._last_beat = now

    def _sample(self, status: str, ttl_ms: int = 4000) -> None:
        now = self.store.now()
        self.samples += 1
        sample_id = f"synthetic-{self.samples:04d}"
        expires = now + ttl_ms * NS_MS
        self._heartbeat = True
        self._last_beat = now
        if status == "ready":
            self.store.ingest(self._event("pending", sample_id, now, expires))
            self._queue.append((now + 80 * NS_MS, "ready", (sample_id, now, expires)))
        else:
            self.store.ingest(self._event(status, sample_id, now, expires))

    def _choose(self, name: str) -> None:
        if name == "show":
            self._sample("ready")
        elif name == "expiring":
            self._sample("ready", 700)
        elif name == "no_result":
            self._sample("no_result")
        elif name == "pending_timeout":
            self._sample("pending", 600)
        elif name in {"invalidated", "disconnected"}:
            self._control(name)
            self._heartbeat = name != "disconnected"
        elif name == "silence":
            self._heartbeat = False
        elif name == "bad_clock":
            now = self.store.now()
            event = self._event("ready", "future-sample", now, now + 4000 * NS_MS)
            event["emitted_monotonic_ns"] += 500 * NS_MS
            self.store.ingest(event)

    def choose(self, name: str) -> None:
        with self._lock:
            self._queue.clear()
            self.playing = name == "play"
            if self.playing:
                now = self.store.now()
                # Once only; deliberately includes no-result, timeout, and silence.
                self._choose("expiring")
                for ms, scenario in ((1100, "no_result"), (1500, "pending_timeout"), (2400, "show"),
                                     (2900, "invalidated"), (3400, "show"), (3800, "silence")):
                    self._queue.append((now + ms * NS_MS, "scenario", scenario))
                self._queue.append((now + 5600 * NS_MS, "end", None))
            else:
                self._choose(name)

    def poll(self) -> None:
        with self._lock:
            now = self.store.now()
            due = sorted((entry for entry in self._queue if entry[0] <= now), key=lambda x: x[0])
            self._queue = [entry for entry in self._queue if entry[0] > now]
            for _, kind, value in due:
                if kind == "scenario":
                    self._choose(value)
                elif kind == "ready":
                    sample_id, frame, expires = value
                    self.store.ingest(self._event("ready", sample_id, frame, expires))
                else:
                    self.playing = False
            if self._heartbeat and now - self._last_beat >= 300 * NS_MS:
                self._control("heartbeat")

    def close(self) -> None:
        pass
