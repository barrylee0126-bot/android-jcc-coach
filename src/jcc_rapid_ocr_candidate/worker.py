"""Private pipe endpoint, launched only by runtime.py with OS network denial."""

from __future__ import annotations

import base64
import contextlib
import ctypes
import io
import json
import math
from numbers import Real
from pathlib import Path
import struct
import sys
import time
import warnings

# -I -S omits all external paths. Add only our own package directory first.
PACKAGE = Path(__file__).resolve().parent
if __package__:
    from .contract import (CandidateError, CropRequest, FixedROI,
                           MAX_MESSAGE_BYTES, MAX_TEXT_CHARS, MODEL_BYTES, MODEL_SHA256, blank_png)
    from .audit import VerifiedImports, digest, verify_wheels
else:
    sys.path.insert(0, str(PACKAGE))
    from contract import (CandidateError, CropRequest, FixedROI,
                          MAX_MESSAGE_BYTES, MAX_TEXT_CHARS, MODEL_BYTES, MODEL_SHA256, blank_png)
    from audit import VerifiedImports, digest, verify_wheels
    sys.path.remove(str(PACKAGE))


def network_is_denied():
    """Read the macOS policy without opening sockets or sending a packet."""
    if sys.platform != "darwin":
        return False
    try:
        check = ctypes.CDLL("/usr/lib/libSystem.B.dylib").sandbox_check
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        check.restype = ctypes.c_int
        import os
        return all(check(os.getpid(), operation, 0) == 1 for operation in
                   (b"network-outbound", b"network-inbound", b"network-bind"))
    except (AttributeError, OSError):
        return False


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def receive():
    size = struct.unpack(">I", read_exact(sys.stdin.buffer, 4))[0]
    if not 0 < size <= MAX_MESSAGE_BYTES:
        raise CandidateError("worker_protocol_failed")
    value = json.loads(read_exact(sys.stdin.buffer, size))
    if not isinstance(value, dict):
        raise CandidateError("worker_protocol_failed")
    return value


def send(value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise CandidateError("worker_protocol_failed")
    sys.stdout.buffer.write(struct.pack(">I", len(data)) + data)
    sys.stdout.buffer.flush()


def providers(engine):
    if engine.text_det is not None or engine.text_cls is not None or engine.text_rec is None:
        raise CandidateError("unexpected_execution_provider")
    actual = engine.text_rec.session.session.get_providers()
    if actual != ["CPUExecutionProvider"]:
        raise CandidateError("unexpected_execution_provider")
    return actual


def initialize():
    if sys.platform != "darwin" or sys.version_info[:2] != (3, 11):
        raise CandidateError("unsupported_runtime")
    # sandbox-exec has already imposed deny network*. Check its policy without
    # a connection probe, even when somebody mistakenly starts this file alone.
    if not network_is_denied():
        raise CandidateError("network_isolation_failed")
    site, wheelhouse, model = (Path(v) for v in sys.argv[1:])
    if not model.is_absolute() or not model.is_file() or model.is_symlink() or model.stat().st_size != MODEL_BYTES:
        raise CandidateError("invalid_local_model")
    if digest(model) != MODEL_SHA256:
        raise CandidateError("model_hash_mismatch")
    started = time.perf_counter()
    approved = verify_wheels(site, wheelhouse, PACKAGE / "requirements-18a.lock")
    verified_ms = (time.perf_counter() - started) * 1000
    sys.meta_path.insert(0, VerifiedImports(approved))
    sys.path.append(str(site))
    started = time.perf_counter()
    with contextlib.redirect_stdout(sys.stderr):
        from rapidocr import RapidOCR
        from rapidocr.utils.download_file import DownloadFile
        from PIL import Image

        def forbidden_download(*args, **kwargs):
            raise CandidateError("download_forbidden")

        # Guard even library branches that would otherwise try a dictionary download.
        DownloadFile.run = forbidden_download
        import_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        engine = RapidOCR(params={
            "Global.use_det": False, "Global.use_cls": False, "Global.use_rec": True,
            "Global.return_word_box": False, "Global.return_single_char_box": False,
            "Global.log_level": "critical", "Global.model_root_dir": str(model.parent),
            "Rec.model_path": str(model), "Rec.rec_keys_path": None,
            "EngineConfig.onnxruntime.use_cuda": False,
            "EngineConfig.onnxruntime.use_coreml": False,
            "EngineConfig.onnxruntime.use_dml": False,
            "EngineConfig.onnxruntime.use_cann": False,
        })
        # Version-pinned lazy loader: fail at startup rather than a later crop.
        engine._load_rec_model()
        model_load_ms = (time.perf_counter() - started) * 1000
    if digest(model) != MODEL_SHA256:
        raise CandidateError("model_changed")
    return engine, Image, {
        "status": "ready", "network_denied": True, "model_sha256": MODEL_SHA256,
        "providers": providers(engine),
        "timing_ms": {"wheel_and_installed_file_verification": verified_ms,
                      "optional_import": import_ms, "engine_and_model_load": model_load_ms},
    }


def validate_crop(request, Image):
    try:
        roi = FixedROI(**request["roi"])
        data = base64.b64decode(request["png_base64"], validate=True)
        crop = CropRequest(roi, data)
    except CandidateError:
        raise
    except Exception:
        raise CandidateError("invalid_image_bytes") from None
    if crop.sha256 != request.get("input_sha256"):
        raise CandidateError("input_hash_mismatch")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CandidateError("invalid_image_format")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG" or image.mode not in ("RGB", "RGBA", "L"):
                    raise CandidateError("invalid_image_format")
                if image.size != (roi.width, roi.height):
                    raise CandidateError("invalid_image_geometry")
                # Pillow PNG getexif()/n_frames may advance or load the file.
                # Verify CRCs on this fresh handle, before either operation.
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                if getattr(image, "n_frames", 1) != 1:
                    raise CandidateError("invalid_image_format")
                if image.getexif().get(274, 1) != 1:
                    raise CandidateError("invalid_image_geometry")
                image.load()
    except CandidateError:
        raise
    except Exception:
        raise CandidateError("invalid_image_bytes") from None
    return crop


def infer(engine, data):
    started = time.perf_counter()
    with contextlib.redirect_stdout(sys.stderr):
        result = engine(data, use_det=False, use_cls=False, use_rec=True,
                        return_word_box=False, return_single_char_box=False)
    providers(engine)
    try:
        # This import runs only inside the explicitly initialized, wheel-verified
        # worker. A matching class name or a coercible value is not the contract.
        from rapidocr.ch_ppocr_rec.typings import TextRecOutput

        if type(result) is not TextRecOutput or getattr(result, "boxes", None) is not None:
            raise ValueError
        texts, scores = result.txts, result.scores
        # The pinned backend returns tuples; its public dataclass also allows
        # lists. Do not turn a string, mapping, iterator or ndarray into a list.
        if type(texts) not in (list, tuple) or type(scores) not in (list, tuple) or len(texts) != len(scores):
            raise ValueError
        if any(not isinstance(v, str) or len(v) > MAX_TEXT_CHARS for v in texts):
            raise ValueError
        # NumPy's real numeric scalars implement Real. Its bool scalars do not;
        # Python bool must be excluded explicitly before float() loses its type.
        if any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) or not 0 <= v <= 1
               for v in scores):
            raise ValueError
        return {
            "result_type": "TextRecOutput",
            "txts": list(texts),
            "scores": [float(v) for v in scores],
            "boxes": None,
            "inference_ms": (time.perf_counter() - started) * 1000,
        }
    except Exception:
        raise CandidateError("invalid_backend_output") from None


def main():
    try:
        engine, Image, ready = initialize()
        send(ready)
    except Exception as exc:
        code = exc.code if isinstance(exc, CandidateError) else "backend_initialization_failed"
        send({"status": "error", "reason": code})
        return 1
    while True:
        try:
            request = receive()
        except EOFError:
            return 0
        except Exception:
            return 1
        response = {"id": request.get("id")}
        try:
            if request.get("op") == "warmup":
                started = time.perf_counter()
                raw = infer(engine, blank_png())
                if raw["result_type"] != "TextRecOutput" or raw["boxes"] is not None:
                    raise CandidateError("invalid_backend_output")
                response.update(status="warmed", providers=providers(engine),
                                synthetic_only=True, warmup_wall_ms=(time.perf_counter() - started) * 1000)
            elif request.get("op") == "transcribe":
                crop = validate_crop(request, Image)
                response.update(status="result", input_sha256=crop.sha256,
                                roi=crop.roi.to_dict(), raw=infer(engine, crop.png))
            else:
                raise CandidateError("worker_protocol_failed")
        except Exception as exc:
            code = exc.code if isinstance(exc, CandidateError) else "inference_failed"
            response.update(status="error", reason=code)
            send(response)
            # A failure never leaves a possibly damaged model alive for retry.
            return 1
        send(response)


if __name__ == "__main__":
    raise SystemExit(main())
