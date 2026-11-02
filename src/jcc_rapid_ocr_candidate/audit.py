"""Verify the reviewed binary wheel set before any optional package import."""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

if __package__:
    from .contract import CandidateError
else:
    from contract import CandidateError


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def verify_wheels(site: Path, wheelhouse: Path, lock: Path):
    pins = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([\w-]+)==([\w.]+) --hash=sha256:([0-9a-f]{64})", line)
        if not match:
            raise CandidateError("invalid_dependency_lock")
        pins[match[3]] = (match[1], match[2])
    if len(pins) != 24:
        raise CandidateError("invalid_dependency_lock")
    wheels = list(wheelhouse.glob("*.whl"))
    if len(wheels) != len(pins):
        raise CandidateError("dependency_wheel_set_mismatch")
    # An ancestor (for example macOS /var) may be a symlink. Resolve both
    # sides of the containment check, while still rejecting a symlinked site.
    if not site.is_absolute() or site.is_symlink():
        raise CandidateError("dependency_file_missing")
    try:
        site_root = site.resolve()
    except (OSError, RuntimeError):
        raise CandidateError("dependency_file_missing") from None
    approved, seen = set(), set()
    for wheel in wheels:
        if wheel.is_symlink() or not wheel.is_file():
            raise CandidateError("invalid_dependency_wheel")
        sha = digest(wheel)
        if sha not in pins or sha in seen:
            raise CandidateError("dependency_wheel_hash_mismatch")
        seen.add(sha)
        with zipfile.ZipFile(wheel) as archive:
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if info.is_dir():
                    continue
                if path.is_absolute() or ".." in path.parts:
                    raise CandidateError("invalid_dependency_wheel")
                # pip rewrites RECORD; .data scripts are never on the import path.
                if info.filename.endswith(".dist-info/RECORD"):
                    continue
                # pip may regenerate a wheel's cached bytecode during install.
                # Bytecode is neither approved nor read by this worker.
                if path.suffix == ".pyc" or "__pycache__" in path.parts:
                    continue
                if path.parts[0].endswith(".data"):
                    if len(path.parts) < 3 or path.parts[1] not in ("purelib", "platlib"):
                        continue
                    path = PurePosixPath(*path.parts[2:])
                target = site.joinpath(*path.parts)
                if not target.is_file() or target.is_symlink():
                    raise CandidateError("dependency_file_missing")
                resolved_target = target.resolve()
                if not resolved_target.is_relative_to(site_root):
                    raise CandidateError("dependency_file_missing")
                expected = hashlib.sha256(archive.read(info)).hexdigest()
                if digest(target) != expected:
                    raise CandidateError("dependency_file_hash_mismatch")
                approved.add(resolved_target)
    return approved


class VerifiedImports(importlib.abc.MetaPathFinder):
    """Reject non-stdlib imports whose source was not in a pinned wheel.

    -I -S omits user paths and .pth hooks. A nonexistent pycache_prefix prevents
    installed, unverified .pyc files from taking precedence over verified source.
    """

    def __init__(self, approved):
        self.approved = approved
        self.stdlib = Path(sys.base_prefix).resolve() / "lib" / "python3.11"

    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None:
            return None
        if not spec.origin or spec.origin in ("built-in", "frozen"):
            # Wheel namespaces are allowed only when all namespace locations
            # contain at least one already verified member.
            locations = tuple(spec.submodule_search_locations or ())
            if locations and all(any(p.is_relative_to(Path(v).resolve()) for p in self.approved) for v in locations):
                return spec
            raise CandidateError("unverified_dependency_import")
        origin = Path(spec.origin).resolve()
        if origin in self.approved:
            return spec
        if origin.is_relative_to(self.stdlib) and "site-packages" not in origin.parts:
            return spec
        raise CandidateError("unverified_dependency_import")
