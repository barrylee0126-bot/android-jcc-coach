"""Generated pixels and controlled OCR backends; never contacts a tablet."""

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from jcc_device.latest import Frame, FrameRef
from jcc_realtime_overlay_bridge.bridge import RealtimeOverlayBridge
from jcc_realtime_overlay_bridge.clocks import bind_same_process
from jcc_realtime_probe.assets import Recorder, read_recording
from jcc_realtime_probe.core import ProbeCore
from jcc_realtime_probe.events import EVENT_SCHEMA, EventSink
from jcc_realtime_probe.pixels import RESULT_TTL_NS
from jcc_realtime_probe.processing import ProcessingResult, simulated_report
from jcc_realtime_probe.processor import OCRProcessor
from jcc_realtime_probe.profile import load_profile
from jcc_realtime_probe.runtime import run_live
from jcc_realtime_probe.timeline import VirtualClock, png, run_timeline, synthetic_rows


def item():
    return dict(text="private-test-text", confidence=.9, x=2, y=2, width=8, height=8)


class ScriptedOCR:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = 0

    def recognize(self, data):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class ProcessingSemanticsTests(unittest.TestCase):
    def setup_core(self, kind="simulated_work", source="synthetic", **profile_fields):
        self.clock = VirtualClock()
        self.output = io.StringIO()
        self.profile = load_profile({"image_size": [160, 100], "input_boxes": [[10, 10, 70, 40]],
                                     **profile_fields}, source)
        self.sink = EventSink("a" * 16, source, "virtual_monotonic_ns", self.clock, self.output,
                              processing_kind=kind)
        self.core = ProbeCore("generated-test-stream", self.profile, self.sink, clock_ns=self.clock)
        return self.core

    def receive(self, sequence=1, **pixels):
        frame = Frame(FrameRef(self.core.stream_id, sequence, self.clock(), "generated-test"), png(**pixels))
        self.core.observe_frame(frame)
        return frame

    def rows(self):
        return list(map(json.loads, self.output.getvalue().splitlines()))

    def pixel_rows(self):
        return [row for row in self.rows() if row["stage"] in {"pixel_full", "pixel_inputs"}]

    def complete_ocr(self, backend):
        job = self.core.start_job()
        report = OCRProcessor(self.profile, backend, self.sink.run_id)(job)
        self.core.complete(job, report)
        self.assertEqual(report["ocr_requests"], backend.calls)
        return report

    def test_synthetic_and_replay_never_claim_ocr_items_or_actual_candidates(self):
        for source in ("synthetic", "replay"):
            with self.subTest(source=source):
                self.setup_core(source=source)
                with patch("jcc_realtime_probe.runtime.VisionOCR", side_effect=AssertionError("No OCR")):
                    report = run_timeline(synthetic_rows(), self.profile, source_kind=source,
                                          events_file=self.output, run_id="b" * 16)
                rows = self.rows()
                candidates = [r for r in rows if r["status"] == "simulated_candidate"]
                self.assertTrue(candidates)
                self.assertFalse(any(r["status"] == "candidate" for r in rows))
                for row in rows:
                    self.assertEqual(row["schema_version"], EVENT_SCHEMA)
                    self.assertEqual(row["source_kind"], source)
                    self.assertEqual(row["processing_kind"], "simulated_work")
                    self.assertIsNone(row["ocr_items_count"])
                    self.assertIn(row["ocr_requests"], (None, 0))
                for row in candidates:
                    self.assertEqual(row["processing_outcome"], "simulated_ready")
                    self.assertEqual(row["reason"], "simulated_pixel_inputs_only")
                    self.assertEqual(row["ocr_requests"], 0)
                self.assertEqual(report["ocr_totals"], dict(ocr_requests=0, ocr_items_count=0, ocr_errors=0))
                self.assertTrue(all(j["ocr_items_count"] is None for j in report["jobs"]))
                self.assertEqual(report["counts"]["formal_accepted"], 0)

    def test_recording_replay_preserves_gaps_and_explicit_simulation(self):
        self.setup_core(source="replay")
        with tempfile.TemporaryDirectory() as temp:
            recording = Recorder(Path(temp) / "generated-recording")
            try:
                for index, seq in enumerate((1, 2, 4, 5)):
                    recording.record(Frame(FrameRef("generated", seq, 10_000_000_000 + index * 250_000_000,
                                                     "generated-test"), png()))
            finally:
                recording.close()
            rows = read_recording(recording.root / "frames.jsonl")
            report = run_timeline(rows, self.profile, source_kind="replay", processing_ns=50_000_000,
                                  events_file=self.output)
        received = [r for r in self.rows() if r["stage"] == "receive"]
        self.assertEqual([int(r["sample_id"].split(":")[1]) for r in received], [1, 2, 4, 5])
        self.assertEqual(report["pixel_checks"]["missing_sequences"], 1)
        self.assertEqual(report["processing_kind"], "simulated_work")
        self.assertFalse(any(r["status"] == "candidate" for r in self.rows()))

    def test_virtual_simulation_cannot_relabel_itself_live(self):
        self.setup_core()
        with self.assertRaises(ValueError):
            run_timeline(synthetic_rows(), self.profile, source_kind="live")

    def test_simulated_completion_does_not_feed_dummy_items_to_10e(self):
        self.setup_core()
        self.receive()
        with patch.object(self.core.pipeline, "publish", wraps=self.core.pipeline.publish) as publish:
            self.core.complete(self.core.start_job(), simulated_report())
        self.assertEqual(publish.call_args.args[1].value.ocr_items_count, 0)
        self.assertEqual(self.core.summary()["counts"]["formal_accepted"], 0)

    def test_simulated_empty_and_failure_are_not_ocr_empty_or_ocr_failure(self):
        for outcome, status, reason in (("simulated_no_output", "no_output", "simulated_no_output"),
                                        ("simulated_error", "rejected", "simulated_failed")):
            with self.subTest(outcome=outcome):
                self.setup_core()
                self.receive()
                self.core.complete(self.core.start_job(), simulated_report(outcome=outcome))
                for row in self.pixel_rows():
                    self.assertEqual((row["status"], row["reason"]), (status, reason))
                    self.assertEqual(row["processing_outcome"], outcome)
                    self.assertEqual(row["ocr_requests"], 0)
                    self.assertIsNone(row["ocr_items_count"])

    def test_superseded_and_pixel_revoked_events_keep_simulation_provenance(self):
        self.setup_core()
        self.receive()
        self.core.complete(self.core.start_job(), simulated_report())
        self.clock.now += 100_000_000
        self.receive(2)
        self.core.complete(self.core.start_job(), simulated_report())
        self.clock.now += 100_000_000
        self.receive(3, inside=1)
        withdrawals = [r for r in self.pixel_rows() if r["status"] == "withdrawn"]
        self.assertEqual({r["reason"] for r in withdrawals}, {"superseded", "pixels_or_continuity_revoked"})
        for row in withdrawals:
            self.assertEqual(row["processing_outcome"], "simulated_ready")
            self.assertIsNone(row["ocr_items_count"])
            self.assertEqual(row["ocr_requests"], 0)

    def test_simulated_deadline_and_disconnect_keep_provenance(self):
        self.setup_core()
        f = self.receive()
        self.clock.now += RESULT_TTL_NS
        row = self.sink.emit(f.source, "pixel_inputs", "simulated_candidate", "simulated_pixel_inputs_only",
                             processing=ProcessingResult.from_report(simulated_report()))
        self.assertEqual((row["status"], row["reason"]), ("expired", "result_expired"))
        self.assertEqual(row["processing_outcome"], "simulated_ready")
        self.setup_core()
        self.receive()
        self.core.complete(self.core.start_job(), simulated_report())
        self.core.disconnect()
        rows = [r for r in self.pixel_rows() if r["status"] == "withdrawn"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["reason"] == "disconnected" and r["processing_kind"] == "simulated_work" for r in rows))

    def test_ocr_calls_on_generated_or_replayed_pixels_can_only_make_ocr_candidates(self):
        for source in ("synthetic", "replay"):
            with self.subTest(source=source):
                self.setup_core("ocr", source)
                self.receive()
                report = self.complete_ocr(ScriptedOCR([item(), item()]))
                self.assertEqual((report["ocr_requests"], report["ocr_items_count"], report["ocr_errors"]), (1, 2, 0))
                self.assertEqual(report["processing_outcome"], "ocr_text")
                self.assertTrue(all(r["status"] == "candidate" and r["processing_kind"] == "ocr"
                                    and r["source_kind"] == source for r in self.pixel_rows()))
                self.clock.now += 100_000_000
                self.receive(2, inside=1)
                withdrawals = [r for r in self.pixel_rows() if r["status"] == "withdrawn"]
                self.assertTrue(all(r["processing_outcome"] == "ocr_text" and r["ocr_requests"] == 1
                                    and r["ocr_items_count"] == 2 for r in withdrawals))
                self.assertEqual(self.core.summary()["counts"]["formal_accepted"], 0)

    def test_real_empty_is_distinct_from_no_ocr_and_from_error(self):
        self.setup_core("ocr")
        self.receive()
        report = self.complete_ocr(ScriptedOCR([]))
        self.assertEqual(report["processing_outcome"], "ocr_empty")
        self.assertEqual((report["ocr_requests"], report["ocr_items_count"]), (1, 0))
        self.assertTrue(all((r["status"], r["reason"]) == ("no_output", "ocr_empty") for r in self.pixel_rows()))

    def test_empty_ocr_and_pixel_rejection_are_both_visible_in_event(self):
        self.setup_core("ocr")
        self.receive()
        job = self.core.start_job()
        report = OCRProcessor(self.profile, ScriptedOCR([]), self.sink.run_id)(job)
        self.clock.now += 100_000_000
        self.receive(2, inside=1)
        self.core.complete(job, report)
        self.assertTrue(all(r["reason"] == "pixels_or_continuity_revoked" and r["processing_outcome"] == "ocr_empty"
                            and r["ocr_requests"] == 1 for r in self.pixel_rows()))

    def test_failed_call_is_counted_and_exception_text_never_escapes(self):
        self.setup_core("ocr")
        self.receive()
        report = self.complete_ocr(ScriptedOCR(RuntimeError("private-device-and-ocr-text")))
        self.assertEqual((report["processing_outcome"], report["ocr_requests"], report["ocr_errors"]), ("ocr_error", 1, 1))
        self.assertTrue(all((r["status"], r["reason"]) == ("rejected", "ocr_failed") for r in self.pixel_rows()))
        self.assertNotIn("private-device-and-ocr-text", self.output.getvalue() + json.dumps(self.core.summary()))

    def test_partial_raw_failure_preserves_counts_and_rejects_partial_text(self):
        self.setup_core("ocr", input_boxes=[[10, 10, 70, 40], [80, 10, 120, 40]])
        self.receive()
        with patch.object(self.core.pipeline, "publish", wraps=self.core.pipeline.publish) as publish:
            report = self.complete_ocr(ScriptedOCR([item()], RuntimeError("private")))
        self.assertEqual((report["ocr_requests"], report["ocr_items_count"], report["ocr_errors"]), (2, 1, 1))
        self.assertEqual(publish.call_args.args[1].value.ocr_items_count, 0)
        self.assertFalse(any(r["status"] == "candidate" for r in self.pixel_rows()))

    def test_caught_10g_cue_and_10f_numeric_failures_are_not_success(self):
        for fields in (dict(cues=[dict(cue_id="round_header", box=[80, 10, 120, 40])]),
                       dict(numeric_fields=[dict(field_name="gold", box=[80, 10, 120, 40])])):
            with self.subTest(fields=fields):
                self.setup_core("ocr", **fields)
                self.receive()
                report = self.complete_ocr(ScriptedOCR([item()], RuntimeError("private")))
                self.assertEqual((report["ocr_requests"], report["ocr_items_count"], report["ocr_errors"]), (2, 1, 1))
                self.assertEqual(report["processing_outcome"], "ocr_error")
                self.assertTrue(all(r["status"] == "rejected" and r["reason"] == "ocr_failed" for r in self.pixel_rows()))

    def test_error_before_ocr_does_not_invent_a_request(self):
        self.setup_core("ocr")
        self.receive()
        job = self.core.start_job()
        other = load_profile(dict(image_size=[320, 200], input_boxes=[[10, 10, 70, 40]]), "synthetic")
        backend = ScriptedOCR()
        report = OCRProcessor(other, backend, self.sink.run_id)(job)
        self.core.complete(job, report)
        self.assertEqual(backend.calls, 0)
        self.assertEqual((report["ocr_requests"], report["ocr_items_count"], report["processing_outcome"]), (0, 0, "ocr_error"))
        self.assertTrue(all(r["reason"] == "processing_failed" for r in self.pixel_rows()))

    def test_no_invocation_and_invalid_backend_response_are_explicit(self):
        for backend, expected, calls in ((object(), "ocr_error", 0), (ScriptedOCR(None), "ocr_error", 1)):
            with self.subTest(expected=expected, calls=calls):
                self.setup_core("ocr")
                self.receive()
                job = self.core.start_job()
                report = OCRProcessor(self.profile, backend, self.sink.run_id)(job)
                self.core.complete(job, report)
                self.assertEqual((report["processing_outcome"], report["ocr_requests"]), (expected, calls))
                self.assertFalse(any(r["status"] == "candidate" for r in self.pixel_rows()))

    def test_unconfigured_ocr_inputs_do_not_get_the_empty_ocr_label(self):
        self.setup_core("ocr")
        self.receive()
        job = self.core.start_job()
        profile = load_profile(dict(image_size=[160, 100], input_boxes=[],
                                    cues=[dict(cue_id="round_header", box=[10, 10, 70, 40])]), "synthetic")
        with patch("jcc_realtime_probe.processor.probe_scene") as probe:
            probe.return_value.cues = ()
            probe.return_value.summary.return_value = {}
            report = OCRProcessor(profile, ScriptedOCR(), self.sink.run_id)(job)
        self.core.complete(job, report)
        self.assertEqual(report["processing_outcome"], "ocr_not_run")
        self.assertTrue(all(r["reason"] == "ocr_not_run" for r in self.pixel_rows()))

    def test_legacy_or_contradictory_reports_are_rejected_before_state_changes(self):
        self.setup_core()
        self.receive()
        job = self.core.start_job()
        reports = [dict(processor="simulated_work_not_ocr", ocr_items_count=1, ocr_requests=0),
                   {**simulated_report(), "ocr_items_count": 1},
                   {**simulated_report(), "ocr_requests": 1},
                   dict(processing_kind="ocr", processing_outcome="ocr_text", ocr_requests=0, ocr_items_count=1, ocr_errors=0),
                   dict(processing_kind="ocr", processing_outcome="ocr_empty", ocr_requests=0, ocr_items_count=0, ocr_errors=0),
                   dict(processing_kind="ocr", processing_outcome="ocr_error", ocr_requests=1, ocr_items_count=1, ocr_errors=1),
                   ProcessingResult("ocr", "ocr_text", 1, 1).fields()]
        for report in reports:
            with self.subTest(report=report), self.assertRaises(ValueError):
                self.core.complete(job, report)
            self.assertIs(self.core.active, job)
            self.assertEqual(self.core.counts["jobs_completed"], 0)
        self.core.complete(job, simulated_report())

    def test_event_api_requires_provenance_and_prevents_cross_kind_candidates(self):
        self.setup_core()
        f = self.receive()
        with self.assertRaises(TypeError):
            EventSink("b" * 16, "synthetic", "virtual_monotonic_ns", self.clock)
        for status, reason, processing in (("candidate", "pixel_inputs_only", None),
                                           ("candidate", "pixel_inputs_only", ProcessingResult.from_report(simulated_report())),
                                           ("simulated_candidate", "simulated_pixel_inputs_only", ProcessingResult("ocr", "ocr_text", 1, 1))):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.sink.emit(f.source, "pixel_inputs", status, reason, processing=processing)

    def test_simulated_run_cannot_emit_ocr_empty_or_failure_codes(self):
        self.setup_core()
        f = self.receive()
        for status, reason in (("no_output", "ocr_empty"), ("rejected", "ocr_failed")):
            for processing in (None, ProcessingResult.from_report(simulated_report())):
                with self.subTest(reason=reason, processing=processing), self.assertRaises(ValueError):
                    self.sink.emit(f.source, "pixel_inputs", status, reason, processing=processing)

    def test_v2_bridge_preserves_real_and_simulated_processing_without_formal_output(self):
        for kind in ("simulated_work", "ocr"):
            with self.subTest(kind=kind):
                self.setup_core(kind)
                bridge = RealtimeOverlayBridge(bind_same_process(self.sink))
                self.addCleanup(bridge.store.close)
                self.receive()
                if kind == "ocr":
                    self.complete_ocr(ScriptedOCR([item()]))
                else:
                    self.core.complete(self.core.start_job(), simulated_report())
                rows = self.rows()
                for row in rows:
                    self.assertTrue(bridge.ingest(row))
                report = bridge.report()
                self.assertIsNone(bridge.faulted)
                self.assertEqual(report["frames_ready"], 1)
                self.assertEqual(report["processing_kind"], kind)
                audit = [r for r in report["upstream_audit"] if r["status"] in {"candidate", "simulated_candidate"}]
                self.assertEqual(len(audit), 2)
                for row in audit:
                    self.assertEqual(row["processing_kind"], kind)
                    self.assertEqual(row["status"], "candidate" if kind == "ocr" else "simulated_candidate")
                    self.assertEqual(row["ocr_requests"], int(kind == "ocr"))
                    self.assertEqual(row["ocr_items_count"], 1 if kind == "ocr" else None)
                view = bridge.store.view()
                self.assertTrue(view["simulation"])
                self.assertFalse(view["execution_eligible"])
                self.assertEqual(view["message"], "模拟计时 · 仅测试显示")
                self.assertEqual(self.core.counts["formal_accepted"], 0)

    def test_runtime_preserves_the_failed_call_count_with_generated_source(self):
        class Source:
            def __init__(self):
                self.sent = False
                self.closed = threading.Event()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
            def close(self):
                self.closed.set()
            def read_frame(self, timeout):
                if not self.sent:
                    self.sent = True
                    return png()
                self.closed.wait(min(timeout, .01))
                raise TimeoutError
        class Backend(ScriptedOCR):
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def close(self):
                pass
        backend = Backend(RuntimeError("private-worker-log"))
        profile = load_profile(dict(image_size=[160, 100], input_boxes=[[10, 10, 70, 40]]), "live")
        output = io.StringIO()
        with patch("jcc_realtime_probe.runtime.TabletStream", return_value=Source()), \
             patch("jcc_realtime_probe.runtime.VisionOCR", return_value=backend):
            report = run_live(profile, duration_seconds=.1, max_frames=10, events_file=output)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["ocr_totals"], dict(ocr_requests=1, ocr_items_count=0, ocr_errors=1))
        self.assertEqual(backend.calls, 1)
        self.assertEqual(report["counts"]["jobs_completed"], 1)
        self.assertFalse(report["worker_alive"])
        self.assertNotIn("private-worker-log", output.getvalue() + json.dumps(report))


if __name__ == "__main__":
    unittest.main()
