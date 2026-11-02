"""Append-only sanitized lifecycle journal. Caller owns its lifetime."""

from __future__ import annotations

import json
import os
from pathlib import Path


class Journal:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._file = None
        if path is not None:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self._file = os.fdopen(descriptor, "w", encoding="utf-8")

    def write(self, row: dict) -> None:
        if self._file is not None:
            self._file.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
