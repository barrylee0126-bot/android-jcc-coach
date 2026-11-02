"""Small pinned-file reader for the offline adapter (no sample workspace)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat


PROJECT = Path(__file__).resolve().parents[2]
SHA = re.compile(r"[a-f0-9]{64}\Z")
JSON_LIMIT = 12 * 1024 * 1024
IMAGE_LIMIT = 25 * 1024 * 1024


class ReviewError(ValueError):
    """Only fixed codes should reach the console."""


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def safe_path(value) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ReviewError("unsafe_path")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ReviewError("symlink_forbidden")
    path = path.resolve()
    if path == PROJECT or PROJECT in path.parents:
        raise ReviewError("private_data_in_project_forbidden")
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            raise ReviewError("private_data_in_git_forbidden")
        name = parent.name.casefold()
        if (name in {"cloudstorage", "mobile documents", "dropbox", "box", "synologydrive", "synology drive"}
                or name.startswith(("onedrive", "googledrive", "google drive", "icloud", "dropbox "))):
            raise ReviewError("cloud_location_forbidden")
    return path


def private_dir(value, *, create=False) -> Path:
    path = safe_path(value)
    if create:
        # Ancestors must already exist; never create an unexpected tree.
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ReviewError("unsafe_directory_permissions")
    return path


def _open_read(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        os.close(descriptor)
        raise ReviewError("unsafe_file_permissions")
    return os.fdopen(descriptor, "rb"), info.st_size


def read_private(value, limit=JSON_LIMIT) -> bytes:
    path = safe_path(value)
    private_dir(path.parent)
    stream, size = _open_read(path)
    with stream:
        if size > limit:
            raise ReviewError("input_size_limit")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ReviewError("input_size_limit")
    return raw


def file_digest(value) -> str:
    path = safe_path(value)
    private_dir(path.parent)
    stream, size = _open_read(path)
    if size > 8 * 1024**3:
        stream.close()
        raise ReviewError("input_size_limit")
    hashed = hashlib.sha256()
    with stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hashed.update(chunk)
    return hashed.hexdigest()


def json_document(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReviewError("duplicate_json_key")
            result[key] = value
        return result

    def nonfinite(_):
        raise ReviewError("nonfinite_json")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (UnicodeError, json.JSONDecodeError):
        raise ReviewError("invalid_json") from None


def write_new(value, raw: bytes):
    path = safe_path(value)
    private_dir(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


class PinnedInputs:
    """Only explicit hashes, never a glob, mtime cache or list-position join."""

    def __init__(self, root, pins: dict, output):
        self.root = private_dir(root)
        self.output = safe_path(output)
        self.pins = pins
        self.used = set()
        self.cache = {}
        if not isinstance(pins, dict) or not 1 <= len(pins) <= 96:
            raise ReviewError("pin_allowlist_invalid")
        for key, item in pins.items():
            if not re.fullmatch(r"[a-z0-9_-]{1,80}", key) or not isinstance(item, dict):
                raise ReviewError("pin_allowlist_invalid")
            if set(item) != {"path", "sha256"} or not isinstance(item["sha256"], str) or not SHA.fullmatch(item["sha256"]):
                raise ReviewError("pin_digest_invalid")
            path = safe_path(item["path"])
            if not path.is_relative_to(self.root) or path.is_relative_to(self.output):
                raise ReviewError("input_output_boundary")

    def path(self, key) -> Path:
        if key not in self.pins:
            raise ReviewError("pin_missing")
        return safe_path(self.pins[key]["path"])

    def read(self, key, *, limit=JSON_LIMIT) -> bytes:
        path = self.path(key)
        raw = read_private(path, limit)
        if digest(raw) != self.pins[key]["sha256"]:
            raise ReviewError("pinned_source_mismatch")
        self.used.add(key)
        return raw

    def json(self, key):
        if key not in self.cache:
            self.cache[key] = json_document(self.read(key))
        return self.cache[key]

    def fingerprint(self, key):
        if file_digest(self.path(key)) != self.pins[key]["sha256"]:
            raise ReviewError("pinned_source_mismatch")
        self.used.add(key)

    def verify_unchanged(self):
        for key in sorted(self.used):
            if file_digest(self.path(key)) != self.pins[key]["sha256"]:
                raise ReviewError("source_changed_during_review")
