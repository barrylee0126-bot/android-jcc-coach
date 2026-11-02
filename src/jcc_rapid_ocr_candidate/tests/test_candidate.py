from __future__ import annotations

import base64
import contextlib
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile
import zlib

from jcc_rapid_ocr_candidate import (
    CandidateError, CropRequest, FixedROI, RapidOCRCandidate, RuntimeConfig, reconcile,
)
from jcc_rapid_ocr_candidate.audit import VerifiedImports, verify_wheels
from jcc_rapid_ocr_candidate.contract import MAX_IMAGE_BYTES, blank_png, normalize_raw
from jcc_rapid_ocr_candidate.worker import validate_crop

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]


class Contracts(unittest.TestCase):
    def setUp(self):
        self.request = CropRequest(FixedROI("synthetic.crop", 96, 48), blank_png())

    def result(self, text="42", score=0.99, **changes):
        raw = {"result_type": "TextRecOutput", "txts": [text], "scores": [score],
               "boxes": None, "inference_ms": 1.0, **changes}
        return normalize_raw(self.request, raw, 0.5)

    def test_preserves_raw_text_without_value_or_geometry(self):
        text = " １２O6 阿茲尔 "
        result = self.result(text)
        output = result.to_dict()
        self.assertEqual(result.transcription, text)
        self.assertEqual(result.raw_transcription, text)
        self.assertEqual(output["evidence_kind"], "transcription_only")
        self.assertIsNone(output["text_boxes"])
        for key in ("geometry_evidence", "human_annotation", "live_use_authorized",
                    "eligible_for_geometry_gate", "eligible_for_policy", "eligible_for_execution"):
            self.assertIs(output[key], False)
        self.assertIsNone(output["accuracy"])
        self.assertNotIn("value", output)
        self.assertFalse(hasattr(RapidOCRCandidate, "recognize"))
        json.dumps(output, allow_nan=False)

    def test_empty_and_whitespace_are_unknown_not_empty_slot(self):
        for text in ("", " \n\t"):
            result = self.result(text, 0)
            self.assertEqual(result.reason, "empty_transcription")
            self.assertEqual(result.status, "unknown")
            self.assertIsNone(result.transcription)

    def test_low_score_retains_machine_diagnostic_but_no_candidate(self):
        result = self.result("42", 0.49)
        self.assertEqual(result.reason, "low_raw_score")
        self.assertIsNone(result.transcription)
        self.assertEqual(result.raw_transcription, "42")
        self.assertEqual(self.result("42", 0.5).status, "development_candidate")

    def test_never_picks_highest_scoring_line(self):
        result = self.result(txts=["42", "24"], scores=[0.99, 0.01])
        self.assertEqual(result.reason, "ambiguous_transcription")
        self.assertIsNone(result.transcription)

    def test_wrong_type_or_geometry_cannot_supply_candidate(self):
        for changes in ({"result_type": "RapidOCROutput"}, {"boxes": [[0, 0, 96, 48]]},
                        {"scores": None}, {"txts": None}, {"txts": [None]},
                        {"txts": "42"}, {"scores": []}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.result(**changes).transcription)

    def test_invalid_numbers_fail_instead_of_becoming_confidence(self):
        for score in (True, False, float("nan"), float("inf"), -0.01, 1.01, "0.99", None):
            self.assertEqual(self.result(score=score).reason, "invalid_backend_output")
        for elapsed in (True, -1.0, float("nan"), None):
            self.assertIsNone(self.result(inference_ms=elapsed).transcription)
        with self.assertRaisesRegex(CandidateError, "invalid_runtime_config"):
            normalize_raw(self.request, {}, float("nan"))

    def test_conflict_is_unknown_without_selecting_a_score(self):
        a, b = self.result("42", 0.5), self.result("24", 0.99999)
        result = reconcile([a, b])
        self.assertEqual(result.reason, "transcription_conflict")
        self.assertIsNone(result.transcription)
        self.assertIsNone(result.raw_transcription)
        self.assertEqual((a.transcription, b.transcription), ("42", "24"))

    def test_agreement_remains_non_authorized_machine_candidate(self):
        result = reconcile([self.result(), self.result(score=0.6)])
        self.assertEqual(result.status, "development_candidate")
        self.assertEqual(result.reason, "same_crop_agreement")
        self.assertFalse(result.to_dict()["eligible_for_geometry_gate"])
        self.assertFalse(result.to_dict()["human_annotation"])

    def test_comparison_rejects_unknown_and_different_inputs(self):
        good = self.result()
        self.assertEqual(reconcile([good, self.result(score=0.1)]).reason, "comparison_incomplete")
        for other in (replace(good, input_sha256="0" * 64),
                      replace(good, roi=FixedROI("synthetic.other", 96, 48))):
            self.assertEqual(reconcile([good, other]).reason, "comparison_input_mismatch")
        with self.assertRaises(CandidateError):
            reconcile([good])

    def test_input_contract_has_resource_bounds_and_no_implicit_resize(self):
        for args in (("", 1, 1), ("x", True, 1), ("x", 0, 1), ("x", 4097, 1),
                     ("x", 2000, 2000), ("http://example.test", 1, 1)):
            with self.assertRaises(CandidateError):
                FixedROI(*args)
        for data in ("https://example.test/a.png", bytearray(b"png"), b"", b"x" * (MAX_IMAGE_BYTES + 1)):
            with self.assertRaises(CandidateError):
                CropRequest(self.request.roi, data)

    def test_synthetic_png_contains_only_generated_pixels(self):
        data = blank_png(5, 3)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        offset, rows = 8, None
        while offset < len(data):
            size = struct.unpack(">I", data[offset:offset + 4])[0]
            kind = data[offset + 4:offset + 8]
            payload = data[offset + 8:offset + 8 + size]
            if kind == b"IDAT":
                rows = zlib.decompress(payload)
            offset += size + 12
        self.assertEqual(rows, (b"\x00" + b"\xff" * 15) * 3)


class FileBoundTests(unittest.TestCase):
    def setUp(self):
        # TemporaryDirectory creates a private directory, removed after the test.
        self.tmp = tempfile.TemporaryDirectory(prefix="jcc-rapid-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_runtime(self):
        env = self.root / "venv"
        (env / "bin").mkdir(parents=True)
        (env / "bin/python").write_bytes(b"synthetic_not_executed")
        (env / "lib/python3.11/site-packages").mkdir(parents=True)
        wheelhouse = self.root / "wheels"
        wheelhouse.mkdir()
        model = self.root / "synthetic.onnx"
        model.write_bytes(b"not_a_model")
        return RuntimeConfig(env, wheelhouse, model)


class RuntimeTests(FileBoundTests):
    def setUp(self):
        super().setUp()
        self.config = self.make_runtime()
        self.backend = RapidOCRCandidate(self.config)
        self.request = CropRequest(FixedROI("synthetic.crop", 96, 48), blank_png())

    def ready_fake(self):
        self.backend._worker = object()
        self.backend._warmed = True
        def invalidate():
            self.backend._worker = None
            self.backend._warmed = False
        self.backend._invalidate = Mock(side_effect=invalidate)

    def response(self, **changes):
        return {"status": "result", "roi": self.request.roi.to_dict(),
                "input_sha256": self.request.sha256,
                "raw": {"result_type": "TextRecOutput", "txts": ["42"], "scores": [0.9],
                        "boxes": None, "inference_ms": 1.0}, **changes}

    def test_missing_runtime_or_model_is_explicit(self):
        with self.assertRaisesRegex(CandidateError, "local_runtime_path_missing"):
            RuntimeConfig(self.root / "missing", self.config.wheelhouse, self.config.rec_model)
        self.config.rec_model.unlink()
        with self.assertRaisesRegex(CandidateError, "local_runtime_path_missing"):
            replace(self.config)

    def test_symlink_model_and_nonfinite_parameters_rejected(self):
        link = self.root / "link.onnx"
        link.symlink_to(self.config.rec_model)
        with self.assertRaisesRegex(CandidateError, "invalid_local_model"):
            replace(self.config, rec_model=link)
        for kwargs in ({"timeout_seconds": 0}, {"timeout_seconds": 61}, {"timeout_seconds": float("nan")},
                       {"minimum_raw_score": float("nan")}, {"minimum_raw_score": True}):
            with self.assertRaises(CandidateError):
                replace(self.config, **kwargs)

    def test_no_inference_before_explicit_start_and_warmup(self):
        self.assertEqual(self.backend.transcribe(self.request).reason, "backend_not_started")
        self.backend._worker = object()
        self.assertEqual(self.backend.transcribe(self.request).reason, "backend_not_warmed")
        self.backend._worker = None

    def test_unsupported_platform_never_starts_unguarded_process(self):
        with patch("jcc_rapid_ocr_candidate.runtime.sys.platform", "linux"), patch("subprocess.Popen") as launch:
            with self.assertRaisesRegex(CandidateError, "network_isolation_unavailable"):
                self.backend.start()
            launch.assert_not_called()

    def test_unknown_python_executable_is_never_run(self):
        with patch("jcc_rapid_ocr_candidate.runtime.sys.platform", "darwin"), \
                patch("jcc_rapid_ocr_candidate.runtime.Path.is_file", return_value=True), \
                patch("subprocess.Popen") as launch:
            with self.assertRaisesRegex(CandidateError, "unverified_python_runtime"):
                self.backend.start()
            launch.assert_not_called()

    def test_fake_backend_preserves_transcription_only_contract(self):
        self.ready_fake()
        self.backend._request = Mock(return_value=self.response())
        result = self.backend.transcribe(self.request)
        self.assertEqual(result.transcription, "42")
        self.assertFalse(result.to_dict()["eligible_for_execution"])
        self.assertEqual(self.backend._request.call_args.args[0]["input_sha256"], self.request.sha256)
        self.backend._worker = None

    def test_wrong_crop_binding_invalidates_without_a_second_attempt(self):
        for changes in ({"input_sha256": "f" * 64}, {"roi": FixedROI("synthetic.wrong", 96, 48).to_dict()}):
            self.ready_fake()
            self.backend._request = Mock(return_value=self.response(**changes))
            self.assertEqual(self.backend.transcribe(self.request).reason, "response_input_mismatch")
            self.backend._invalidate.assert_called_once()
            self.assertEqual(self.backend.transcribe(self.request).reason, "backend_not_started")
            self.assertEqual(self.backend._request.call_count, 1)

    def test_worker_error_is_sanitized_and_invalidates(self):
        for reason in ("private secret 42", [], {}):
            self.ready_fake()
            self.backend._request = Mock(return_value={"status": "error", "reason": reason})
            result = self.backend.transcribe(self.request)
            self.assertEqual(result.reason, "worker_protocol_failed")
            self.assertNotIn("secret", json.dumps(result.to_dict()))
            self.backend._invalidate.assert_called_once()

    def test_transport_timeout_closes_worker_no_retry(self):
        self.ready_fake()
        self.backend._transfer = Mock(side_effect=CandidateError("worker_timeout"))
        result = self.backend.transcribe(self.request)
        self.assertEqual(result.reason, "worker_timeout")
        self.backend._invalidate.assert_called_once()
        self.assertEqual(self.backend._transfer.call_count, 1)
        self.assertEqual(self.backend.transcribe(self.request).reason, "backend_not_started")

    def test_wrong_response_id_closes_worker(self):
        self.ready_fake()
        self.backend._transfer = Mock()
        self.backend._receive = Mock(return_value={"id": -1, **self.response()})
        result = self.backend.transcribe(self.request)
        self.assertEqual(result.reason, "response_request_mismatch")
        self.backend._invalidate.assert_called_once()

    def test_unexpected_geometry_invalidates_even_with_good_text(self):
        self.ready_fake()
        response = self.response()
        response["raw"]["boxes"] = [[0, 0, 96, 48]]
        self.backend._request = Mock(return_value=response)
        result = self.backend.transcribe(self.request)
        self.assertEqual(result.reason, "unexpected_geometry")
        self.assertIsNone(result.transcription)
        self.backend._invalidate.assert_called_once()

    def test_bounded_lock_contention_is_error_not_old_result(self):
        self.ready_fake()
        self.backend.config = replace(self.config, timeout_seconds=0.001)
        self.backend._lock.acquire()
        try:
            self.assertEqual(self.backend.transcribe(self.request).reason, "backend_busy")
        finally:
            self.backend._lock.release()
            self.backend._worker = None


class WheelAuditTests(FileBoundTests):
    def setUp(self):
        super().setUp()
        self.site, self.wheels = self.root / "site", self.root / "wheels"
        self.site.mkdir()
        self.wheels.mkdir()
        self.lock = self.root / "synthetic.lock"
        entries = []
        for index in range(24):
            name, data = f"synthetic{index}.py", f"VALUE = {index}\n".encode()
            wheel = self.wheels / f"synthetic{index}-1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(name, data)
            (self.site / name).write_bytes(data)
            entries.append(f"synthetic{index}==1.0 --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}")
        self.lock.write_text("\n".join(entries))

    def replace_first_wheel_member(self, member):
        wheel = self.wheels / "synthetic0-1.0-py3-none-any.whl"
        old_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(member, b"VALUE = 0\n")
        new_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
        self.lock.write_text(self.lock.read_text().replace(old_hash, new_hash))

    def test_verifies_installed_bytes_against_all_pinned_wheels(self):
        self.assertEqual(len(verify_wheels(self.site, self.wheels, self.lock)), 24)

    def test_symlinked_parent_keeps_installed_files_inside_resolved_site(self):
        alias = self.root / "synthetic-parent-alias"
        alias.symlink_to(self.root.resolve(), target_is_directory=True)
        aliased_site = alias / "site"
        self.assertFalse(aliased_site.is_symlink())
        approved = verify_wheels(aliased_site, self.wheels, self.lock)
        self.assertEqual(approved, {path.resolve() for path in self.site.glob("*.py")})

    def test_site_directory_symlink_is_rejected(self):
        alias = self.root / "synthetic-site-alias"
        alias.symlink_to(self.site.resolve(), target_is_directory=True)
        with self.assertRaisesRegex(CandidateError, "dependency_file_missing"):
            verify_wheels(alias, self.wheels, self.lock)

    def test_symlink_loop_in_site_parent_fails_closed(self):
        loop = self.root / "synthetic-loop"
        loop.symlink_to(loop, target_is_directory=True)
        with self.assertRaisesRegex(CandidateError, "dependency_file_missing"):
            verify_wheels(loop / "site", self.wheels, self.lock)

    def test_installed_file_symlink_is_rejected_even_if_bytes_match(self):
        target = self.site / "synthetic0.py"
        copy = self.root / "synthetic-copy.py"
        copy.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(copy.resolve())
        with self.assertRaisesRegex(CandidateError, "dependency_file_missing"):
            verify_wheels(self.site, self.wheels, self.lock)

    def test_symlinked_subdirectory_cannot_escape_site(self):
        outside = self.root / "synthetic-outside"
        outside.mkdir()
        (outside / "synthetic0.py").write_bytes(b"VALUE = 0\n")
        (self.site / "synthetic-escape").symlink_to(outside.resolve(), target_is_directory=True)
        self.replace_first_wheel_member("synthetic-escape/synthetic0.py")
        with self.assertRaisesRegex(CandidateError, "dependency_file_missing"):
            verify_wheels(self.site, self.wheels, self.lock)

    def test_wheel_path_symlink_is_rejected(self):
        wheel = self.wheels / "synthetic0-1.0-py3-none-any.whl"
        copy = self.root / "synthetic-copy.whl"
        copy.write_bytes(wheel.read_bytes())
        wheel.unlink()
        wheel.symlink_to(copy.resolve())
        with self.assertRaisesRegex(CandidateError, "invalid_dependency_wheel"):
            verify_wheels(self.site, self.wheels, self.lock)

    def test_absolute_and_parent_traversal_wheel_members_are_rejected(self):
        for member in ("../synthetic0.py", "/synthetic0.py"):
            with self.subTest(member=member):
                self.replace_first_wheel_member(member)
                with self.assertRaisesRegex(CandidateError, "invalid_dependency_wheel"):
                    verify_wheels(self.site, self.wheels, self.lock)

    def test_modified_or_missing_installed_code_fails(self):
        target = self.site / "synthetic0.py"
        target.write_bytes(b"arbitrary_code()")
        with self.assertRaisesRegex(CandidateError, "dependency_file_hash_mismatch"):
            verify_wheels(self.site, self.wheels, self.lock)
        target.unlink()
        with self.assertRaisesRegex(CandidateError, "dependency_file_missing"):
            verify_wheels(self.site, self.wheels, self.lock)

    def test_modified_wheel_and_incomplete_set_fail(self):
        wheel = next(self.wheels.glob("*.whl"))
        wheel.write_bytes(b"unreviewed wheel")
        with self.assertRaisesRegex(CandidateError, "dependency_wheel_hash_mismatch"):
            verify_wheels(self.site, self.wheels, self.lock)
        wheel.unlink()
        with self.assertRaisesRegex(CandidateError, "dependency_wheel_set_mismatch"):
            verify_wheels(self.site, self.wheels, self.lock)

    def test_regenerated_bytecode_is_never_approved(self):
        wheel = next(self.wheels.glob("*.whl"))
        old_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr("__pycache__/synthetic0.cpython-311.pyc", b"old bytecode")
        cache = self.site / "__pycache__"
        cache.mkdir()
        pyc = cache / "synthetic0.cpython-311.pyc"
        pyc.write_bytes(b"regenerated different bytecode")
        new_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
        self.lock.write_text(self.lock.read_text().replace(old_hash, new_hash))
        approved = verify_wheels(self.site, self.wheels, self.lock)
        self.assertNotIn(pyc.resolve(), approved)

    def test_unlisted_code_is_blocked_before_execution(self):
        approved = verify_wheels(self.site, self.wheels, self.lock)
        unknown = self.site / "unknown_candidate.py"
        unknown.write_text("raise RuntimeError('must never execute')")
        guard = VerifiedImports(approved)
        with patch.object(sys, "path", [str(self.site)]):
            self.assertEqual(Path(guard.find_spec("synthetic0").origin), self.site / "synthetic0.py")
            with self.assertRaisesRegex(CandidateError, "unverified_dependency_import"):
                guard.find_spec("unknown_candidate")
        self.assertNotIn("unknown_candidate", sys.modules)


class ImageValidation(unittest.TestCase):
    def request(self):
        data = blank_png()
        return {"roi": {"roi_id": "synthetic.crop", "width": 96, "height": 48},
                "input_sha256": hashlib.sha256(data).hexdigest(),
                "png_base64": base64.b64encode(data).decode()}

    def fake_image(self, **changes):
        image = Mock()
        image.format, image.mode, image.n_frames, image.size = "PNG", "RGB", 1, (96, 48)
        image.getexif.return_value = {}
        for key, value in changes.items():
            setattr(image, key, value)
        context = Mock()
        context.__enter__ = Mock(return_value=image)
        context.__exit__ = Mock(return_value=False)
        module = Mock()
        module.DecompressionBombWarning = RuntimeWarning
        module.open.return_value = context
        return module, image

    def test_fake_decode_checks_exact_dimensions_then_keeps_identical_bytes(self):
        module, image = self.fake_image()
        crop = validate_crop(self.request(), module)
        self.assertEqual(crop.png, blank_png())
        self.assertEqual(module.open.call_count, 2)
        image.verify.assert_called_once()
        image.load.assert_called_once()

    def test_geometry_mode_multiframe_and_exif_fail_before_inference(self):
        for changes in ({"size": (95, 48)}, {"mode": "P"}, {"n_frames": 2}, {"format": "JPEG"}):
            module, _ = self.fake_image(**changes)
            with self.assertRaises(CandidateError):
                validate_crop(self.request(), module)
        module, image = self.fake_image()
        image.getexif.return_value = {274: 6}
        with self.assertRaisesRegex(CandidateError, "invalid_image_geometry"):
            validate_crop(self.request(), module)

    def test_hash_mismatch_and_bad_base64_never_decode(self):
        module, _ = self.fake_image()
        for changes in ({"input_sha256": "0" * 64}, {"png_base64": "not-base64!"}):
            with self.assertRaises(CandidateError):
                validate_crop({**self.request(), **changes}, module)
        module.open.assert_not_called()

    def test_decoder_exception_is_fixed_error_without_private_text(self):
        module, image = self.fake_image()
        image.verify.side_effect = ValueError("private image secret")
        with self.assertRaisesRegex(CandidateError, "^invalid_image_bytes$"):
            validate_crop(self.request(), module)

    def test_png_integrity_is_verified_before_exif_can_advance_stream(self):
        module, image = self.fake_image()
        image.verify.side_effect = lambda: self.assertEqual(image.getexif.call_count, 0)
        validate_crop(self.request(), module)
        image.verify.assert_called_once()
        image.getexif.assert_called_once()


class CLITests(FileBoundTests):
    def test_fresh_stdlib_import_and_synthetic_cli_without_optional_packages(self):
        source = PROJECT / "src"
        environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(source)}
        code = "import sys,jcc_rapid_ocr_candidate; assert not any(m in sys.modules for m in ('numpy','rapidocr','PIL','cv2','onnxruntime'))"
        done = subprocess.run([sys.executable, "-B", "-S", "-c", code], env=environment,
                              capture_output=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        done = subprocess.run([sys.executable, "-B", "-S", "-m", "jcc_rapid_ocr_candidate", "--synthetic-check"],
                              env=environment, capture_output=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["status"], "passed")

    def test_cli_conflict_does_not_leave_last_candidate_in_result(self):
        from jcc_rapid_ocr_candidate.__main__ import main
        path = self.root / "synthetic.png"
        data = blank_png()
        path.write_bytes(data)
        request = CropRequest(FixedROI("synthetic.crop", 96, 48), data)
        def fake_result(text):
            return replace(normalize_raw(request, {"result_type": "TextRecOutput", "txts": [text],
                           "scores": [0.99], "boxes": None, "inference_ms": 1.0}, 0.5), request_wall_ms=2.0)
        engine = Mock()
        engine.startup = {"status": "ready"}
        engine.warmup.return_value = {"status": "warmed"}
        engine.transcribe.side_effect = [fake_result("42"), fake_result("24")]
        context = Mock()
        context.__enter__ = Mock(return_value=engine)
        context.__exit__ = Mock(return_value=False)
        output = io.StringIO()
        with patch("jcc_rapid_ocr_candidate.__main__.RuntimeConfig"), \
                patch("jcc_rapid_ocr_candidate.__main__.RapidOCRCandidate", return_value=context), \
                contextlib.redirect_stdout(output):
            code = main(["--environment", str(self.root), "--wheelhouse", str(self.root),
                         "--rec-model", str(self.root), "--crop", str(path),
                         "--input-sha256", request.sha256, "--roi-id", request.roi.roi_id,
                         "--width", "96", "--height", "48", "--repeat", "2", "--include-transcription"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(result["result"]["status"], "unknown")
        self.assertIsNone(result["result"]["transcription"])
        self.assertEqual(result["result"]["reason"], "transcription_conflict")


if __name__ == "__main__":
    unittest.main()
