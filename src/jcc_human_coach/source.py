"""Bounded local JSONL input for the player-coach presentation path.

It only follows appended complete lines.  The adapter's event sequence is
separate from ``source_sequence``: callers must preserve the latter instead of
renumbering source frames to make a presentation stream look continuous.
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import stat

from .core import CoachAdvice, CoachContractError, CoachEvent, CoachObservation, HumanCoachStore, SCHEMA


def _unique_pairs(pairs: list[tuple]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = item
    return value


def decode_json(line: bytes) -> object:
    return json.loads(line.decode("utf-8"), object_pairs_hook=_unique_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))


def decode_event(value: object) -> CoachEvent:
    """Decode only the anonymous, structured candidate contract.

    No image path, OCR text, account, device id, coordinates, or HTML is part
    of this protocol.  The store performs the semantic/freshness validation.
    """
    fields = {"schema_version", "source_kind", "stream_id", "frame_id", "sequence", "source_sequence",
              "frame_received_ms", "emitted_ms", "expires_ms", "status", "reason", "context_version",
              "evidence_id", "evidence_status", "advice", "observation"}
    if type(value) is not dict or set(value) != fields:
        raise CoachContractError("event_shape")
    raw_advice = value["advice"]
    advice: CoachAdvice | None
    if raw_advice is None:
        advice = None
    else:
        advice_fields = {"advice_id", "category", "title_zh", "action_zh", "reason_zh"}
        if type(raw_advice) is not dict or set(raw_advice) != advice_fields:
            raise CoachContractError("advice_shape")
        advice = CoachAdvice(raw_advice["advice_id"], raw_advice["category"], raw_advice["title_zh"],
                             raw_advice["action_zh"], raw_advice["reason_zh"])
    raw_observation = value["observation"]
    observation: CoachObservation | None
    if raw_observation is None:
        observation = None
    else:
        observation_fields = {"observation_id", "title_zh", "facts_zh", "unknown_zh"}
        if type(raw_observation) is not dict or set(raw_observation) != observation_fields:
            raise CoachContractError("observation_shape")
        observation = CoachObservation(raw_observation["observation_id"], raw_observation["title_zh"],
                                       raw_observation["facts_zh"], raw_observation["unknown_zh"])
    event = CoachEvent(value["schema_version"], value["source_kind"], value["stream_id"], value["frame_id"],
                       value["sequence"], value["frame_received_ms"], value["emitted_ms"], value["expires_ms"],
                       value["status"], value["reason"], advice, value["context_version"], value["evidence_id"],
                       value["source_sequence"], value["evidence_status"], observation)
    if event.schema_version != SCHEMA:
        raise CoachContractError("schema")
    return event


class JsonlTail:
    """Read only future complete records; source mutation fails closed."""

    def __init__(self, path: Path, store: HumanCoachStore) -> None:
        self.path, self.store = path, store
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("not_regular")
            self._file = os.fdopen(descriptor, "rb", buffering=0)
        except Exception:
            os.close(descriptor)
            raise
        self._identity = (info.st_dev, info.st_ino)
        self._file.seek(0, os.SEEK_END)  # no accidental replay of a private prior run
        self._buffer = b""
        self._drop_until_newline = False
        self._guard_start = max(0, self._file.tell() - 4096)
        self._guard_digest = hashlib.sha256(os.pread(self._file.fileno(), self._file.tell() - self._guard_start,
                                                     self._guard_start)).digest()
        self.failed = False

    def _check_consumed_prefix(self) -> None:
        """Bounded guard against common same-inode truncate-and-rewrite races.

        The source is still an append-only producer contract: no tailer can
        prove arbitrary historical rewrites without rereading the whole file.
        We guard the last consumed 4 KiB and fail closed on a mismatch.
        """
        position = self._file.tell()
        if position < self._guard_start:
            raise OSError("source_rewound")
        current = os.pread(self._file.fileno(), position - self._guard_start, self._guard_start)
        if hashlib.sha256(current).digest() != self._guard_digest:
            raise OSError("consumed_prefix_changed")

    def _refresh_guard(self) -> None:
        position = self._file.tell()
        self._guard_start = max(0, position - 4096)
        self._guard_digest = hashlib.sha256(os.pread(self._file.fileno(), position - self._guard_start,
                                                     self._guard_start)).digest()

    def poll(self) -> None:
        if self.failed:
            return
        try:
            info = self.path.lstat()
            if (info.st_dev, info.st_ino) != self._identity or not stat.S_ISREG(info.st_mode) or info.st_size < self._file.tell():
                raise OSError("changed")
            self._check_consumed_prefix()
            chunk = self._file.read(32_768)
            if not chunk:
                return
            self._buffer += chunk
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                if self._drop_until_newline:
                    self._drop_until_newline = False
                    continue
                if not line.strip() or len(line) > 8192:
                    self.store.reject_input()
                    continue
                try:
                    self.store.ingest(decode_event(decode_json(line)))
                except (CoachContractError, ValueError, UnicodeError, RecursionError, TypeError):
                    self.store.reject_input()
            if len(self._buffer) > 8192:
                self._buffer = b""
                if not self._drop_until_newline:
                    self.store.reject_input()
                self._drop_until_newline = True
            self._refresh_guard()
        except OSError:
            self.failed = True
            self.store.source_fault("source_unavailable")

    def close(self) -> None:
        self._file.close()
