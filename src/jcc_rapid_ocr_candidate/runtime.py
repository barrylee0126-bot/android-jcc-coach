"""A persistent, bounded pipe client. No optional dependencies in this process."""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import threading
import time

from .contract import (
    CandidateError, CropRequest, MAX_MESSAGE_BYTES, MODEL_SHA256, failure, normalize_raw,
)


# The optional prototype reuses exactly the reviewed 18A interpreter. An
# environment path is never permission to execute an arbitrary local program.
REVIEWED_PYTHON = Path("/opt/homebrew/Cellar/python@3.11/3.11.16/Frameworks/Python.framework/Versions/3.11/bin/python3.11")
REVIEWED_PYTHON_SHA256 = "3494e1ea84d8b49a17c193a9825d34abd476b7f3c2075e58c14316696fe3bf6f"
REVIEWED_LIBPYTHON_SHA256 = "5c52ed99bdafcce5ed5648bb246fece28e0ea14f48efd86ed2c8332eb897168a"


def reviewed_interpreter(environment):
    executable = (environment / "bin/python").resolve()
    library = executable.parent.parent / "Python"
    try:
        if (executable != REVIEWED_PYTHON
                or hashlib.sha256(executable.read_bytes()).hexdigest() != REVIEWED_PYTHON_SHA256
                or hashlib.sha256(library.read_bytes()).hexdigest() != REVIEWED_LIBPYTHON_SHA256):
            raise CandidateError("unverified_python_runtime")
    except OSError:
        raise CandidateError("unverified_python_runtime") from None
    return executable


def absolute_path(value, *, directory=False):
    try:
        path = Path(value)
        valid = path.is_absolute() and (path.is_dir() if directory else path.is_file())
    except (TypeError, ValueError, OSError):
        valid = False
    if not valid:
        raise CandidateError("local_runtime_path_missing")
    return path


@dataclass(frozen=True)
class RuntimeConfig:
    environment: Path
    wheelhouse: Path
    rec_model: Path
    timeout_seconds: float = 30.0
    minimum_raw_score: float = 0.5

    def __post_init__(self):
        for value in (self.timeout_seconds, self.minimum_raw_score):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise CandidateError("invalid_runtime_config")
        if not 0 < self.timeout_seconds <= 60 or not 0 <= self.minimum_raw_score <= 1:
            raise CandidateError("invalid_runtime_config")
        object.__setattr__(self, "environment", absolute_path(self.environment, directory=True))
        object.__setattr__(self, "wheelhouse", absolute_path(self.wheelhouse, directory=True))
        object.__setattr__(self, "rec_model", absolute_path(self.rec_model))
        if self.rec_model.is_symlink():
            raise CandidateError("invalid_local_model")
        absolute_path(self.environment / "bin/python")
        absolute_path(self.environment / "lib/python3.11/site-packages", directory=True)


class RapidOCRCandidate:
    """Explicit start -> synthetic warmup -> serial crop requests -> close.

    No automatic retry/restart/fallback and no recognize() geometry interface.
    A failed exchange invalidates the process before any later request.
    """

    def __init__(self, config: RuntimeConfig):
        if not isinstance(config, RuntimeConfig):
            raise CandidateError("invalid_runtime_config")
        self.config = config
        self.startup = None
        self._worker = None
        self._warmed = False
        self._lock = threading.Lock()
        self._counter = 0

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()

    def start(self):
        with self._lock:
            if self._worker is not None:
                raise CandidateError("backend_already_started")
            if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
                raise CandidateError("network_isolation_unavailable")
            executable = reviewed_interpreter(self.config.environment)
            worker_script = Path(__file__).with_name("worker.py").resolve()
            command = [
                "/usr/bin/sandbox-exec", "-p",
                "(version 1)(allow default)(deny network*)(deny file-write*)",
                str(executable), "-I", "-B", "-S",
                "-X", "pycache_prefix=/dev/null/jcc-rapid-no-bytecode",
                str(worker_script),
                str((self.config.environment / "lib/python3.11/site-packages").resolve()),
                str(self.config.wheelhouse.resolve()), str(self.config.rec_model.resolve()),
            ]
            start = time.perf_counter()
            try:
                self._worker = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, cwd=worker_script.parent,
                    env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}, bufsize=0,
                )
                os.set_blocking(self._worker.stdin.fileno(), False)
                os.set_blocking(self._worker.stdout.fileno(), False)
                response = self._receive(time.monotonic() + self.config.timeout_seconds)
                if response.get("status") != "ready":
                    raise CandidateError(_worker_error(response))
                if (response.get("network_denied") is not True
                        or response.get("model_sha256") != MODEL_SHA256
                        or response.get("providers") != ["CPUExecutionProvider"]):
                    raise CandidateError("invalid_startup_response")
                self.startup = {**response, "process_start_to_ready_ms": (time.perf_counter() - start) * 1000}
                self._warmed = False
                return self.startup
            except Exception as exc:
                self._invalidate()
                if isinstance(exc, CandidateError):
                    raise
                raise CandidateError("worker_start_failed") from None

    def warmup(self):
        with self._lock:
            if self._worker is None:
                raise CandidateError("backend_not_started")
            response = self._request({"op": "warmup"})
            if response.get("status") != "warmed" or response.get("providers") != ["CPUExecutionProvider"]:
                self._invalidate()
                raise CandidateError(_worker_error(response))
            self._warmed = True
            return response

    def transcribe(self, request: CropRequest):
        if not isinstance(request, CropRequest):
            raise CandidateError("invalid_crop_request")
        start = time.perf_counter()
        acquired = self._lock.acquire(timeout=self.config.timeout_seconds)
        if not acquired:
            return failure(request, "backend_busy", status="error")
        try:
            if self._worker is None:
                return failure(request, "backend_not_started", status="error")
            if not self._warmed:
                return failure(request, "backend_not_warmed", status="error")
            try:
                response = self._request({
                    "op": "transcribe", "roi": request.roi.to_dict(),
                    "input_sha256": request.sha256,
                    "png_base64": base64.b64encode(request.png).decode("ascii"),
                })
                if response.get("status") == "error":
                    result = failure(request, _worker_error(response), status="error")
                    self._invalidate()
                elif (response.get("status") != "result"
                        or response.get("input_sha256") != request.sha256
                        or response.get("roi") != request.roi.to_dict()):
                    self._invalidate()
                    result = failure(request, "response_input_mismatch", status="error")
                else:
                    result = normalize_raw(request, response.get("raw"), self.config.minimum_raw_score)
                    if result.reason in ("invalid_backend_output", "unexpected_geometry", "ambiguous_transcription"):
                        self._invalidate()
                return replace(result, request_wall_ms=(time.perf_counter() - start) * 1000)
            except CandidateError as exc:
                return replace(failure(request, exc.code, status="error"),
                               request_wall_ms=(time.perf_counter() - start) * 1000)
        finally:
            self._lock.release()

    def close(self):
        with self._lock:
            self._invalidate()

    def _invalidate(self):
        worker, self._worker = self._worker, None
        self._warmed = False
        if worker is not None:
            if worker.poll() is None:
                worker.kill()
            try:
                worker.wait(timeout=2)
            finally:
                for stream in (worker.stdin, worker.stdout):
                    if stream is not None:
                        stream.close()

    def _request(self, payload):
        self._counter += 1
        payload = {**payload, "id": self._counter}
        deadline = time.monotonic() + self.config.timeout_seconds
        try:
            data = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(data) > MAX_MESSAGE_BYTES:
                raise CandidateError("message_too_large")
            self._transfer(deadline, data=struct.pack(">I", len(data)) + data)
            response = self._receive(deadline)
            if type(response.get("id")) is not int or response.get("id") != payload["id"]:
                raise CandidateError("response_request_mismatch")
            return response
        except Exception as exc:
            self._invalidate()
            if isinstance(exc, CandidateError):
                raise
            raise CandidateError("worker_protocol_failed") from None

    def _receive(self, deadline):
        size = struct.unpack(">I", self._transfer(deadline, size=4))[0]
        if not 0 < size <= MAX_MESSAGE_BYTES:
            raise CandidateError("invalid_response_size")
        response = json.loads(self._transfer(deadline, size=size),
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(response, dict):
            raise CandidateError("invalid_response_schema")
        return response

    def _transfer(self, deadline, *, data=None, size=0):
        writing = data is not None
        stream = self._worker.stdin if writing else self._worker.stdout
        fd = stream.fileno()
        received, offset = bytearray(), 0
        target = len(data) if writing else size
        while offset < target:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CandidateError("worker_timeout")
            readers, writers, _ = select.select([] if writing else [fd], [fd] if writing else [], [], remaining)
            if not (readers or writers):
                raise CandidateError("worker_timeout")
            try:
                if writing:
                    count = os.write(fd, data[offset:offset + 65536])
                else:
                    chunk = os.read(fd, min(65536, target - offset))
                    count = len(chunk)
                    received.extend(chunk)
                if count == 0:
                    raise CandidateError("worker_closed")
                offset += count
            except BlockingIOError:
                continue
        return bytes(received)


WORKER_ERRORS = frozenset({
    "unsupported_runtime", "invalid_dependency_lock", "dependency_wheel_set_mismatch",
    "invalid_dependency_wheel", "dependency_wheel_hash_mismatch", "dependency_file_missing",
    "dependency_file_hash_mismatch", "unverified_dependency_import", "invalid_local_model",
    "model_hash_mismatch", "model_changed", "network_isolation_failed", "download_forbidden",
    "backend_initialization_failed", "unexpected_execution_provider", "invalid_image_bytes",
    "invalid_image_format", "invalid_image_geometry", "invalid_roi_contract",
    "input_hash_mismatch", "inference_failed", "invalid_backend_output", "worker_protocol_failed",
})


def _worker_error(response):
    code = response.get("reason")
    return code if isinstance(code, str) and code in WORKER_ERRORS else "worker_protocol_failed"
