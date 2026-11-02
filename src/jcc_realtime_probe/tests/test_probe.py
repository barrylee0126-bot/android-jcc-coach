from dataclasses import replace
import io
import json
from pathlib import Path
import queue
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

from jcc_device.latest import Frame, FrameRef, LatestFrameStream

from jcc_realtime_probe.core import ProbeCore
from jcc_realtime_probe.events import EventSink
from jcc_realtime_probe.pixels import PixelLedger, RESULT_TTL_NS
from jcc_realtime_probe.processor import OCRProcessor
from jcc_realtime_probe.processing import CANDIDATE_STATUSES, ProcessingResult
from jcc_realtime_probe.profile import load_profile, outside_project
from jcc_realtime_probe.timeline import VirtualClock, png, run_timeline, synthetic_profile, synthetic_rows
from jcc_realtime_probe.runtime import run_live


def frame(clock, seq=1, *, inside=0, outside=0, source="test", data=None):
    return Frame(FrameRef(source, seq, clock(), "private-utc"),
                 png(inside=inside, outside=outside) if data is None else data)


class PixelTests(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualClock()
        self.ledger = PixelLedger((160, 100), ((10, 10, 70, 40),), clock_ns=self.clock)
        self.first = frame(self.clock)
        self.ledger.observe(self.first)
        self.lease = self.ledger.lease(self.first)

    def add(self, seq, **kwargs):
        self.clock.now += 100_000_000
        f = frame(self.clock, seq, **kwargs)
        return self.ledger.observe(f)

    def test_unrelated_single_pixel_changes_full_but_not_inputs(self):
        self.add(2, outside=1)
        self.assertIsNone(self.ledger.check(self.lease, "inputs"))
        self.assertEqual(self.ledger.check(self.lease, "full"), "pixels_or_continuity_revoked")

    def test_exact_single_channel_one_unit_aba_never_revives(self):
        self.add(2, inside=1)
        self.add(3, inside=0)
        self.assertEqual(self.ledger.check(self.lease, "inputs"), "pixels_or_continuity_revoked")
        self.assertEqual(self.ledger.summary()["input_changed_pairs"], 2)

    def test_original_expiry_is_not_extended_by_identical_new_frames(self):
        for n in range(2, 20):
            self.add(n)
        self.clock.now = self.lease.expires_at_monotonic_ns - 1
        self.assertIsNone(self.ledger.check(self.lease, "inputs"))
        self.clock.now += 1
        self.assertEqual(self.ledger.check(self.lease, "inputs"), "result_expired")

    def test_silent_liveness_gap_cannot_be_hidden_by_next_same_frame(self):
        self.clock.now += 1_000_000_000
        self.ledger.observe(frame(self.clock, 2))
        self.assertEqual(self.ledger.check(self.lease, "inputs"), "pixels_or_continuity_revoked")

    def test_gap_frame_unknown_next_frame_may_start_only_new_lease(self):
        self.assertEqual(self.add(3)["reason"], "sequence_gap")
        self.add(4)
        self.assertEqual(self.ledger.check(self.lease, "inputs"), "pixels_or_continuity_revoked")
        self.assertEqual(self.ledger.summary()["missing_sequences"], 1)

    def test_duplicate_and_reorder_revoke(self):
        for seq in (1, 0):
            with self.subTest(seq=seq):
                self.assertEqual(self.add(seq)["status"], "unknown")
                self.assertIsNotNone(self.ledger.check(self.lease, "inputs"))

    def test_wrong_bytes_cannot_get_current_lease_for_same_reference(self):
        mixed = Frame(self.first.source, png(inside=1))
        self.assertIsNone(self.ledger.lease(mixed))

    def test_bad_image_and_geometry_revoke(self):
        for data in (b"private-invalid-image", png(size=(161, 100))):
            self.clock.now += 1
            self.ledger.observe(frame(self.clock, self.ledger.high_sequence + 1, data=data))
            self.assertIsNotNone(self.ledger.check(self.lease, "inputs"))

    def test_alpha_is_not_silently_dropped(self):
        out = io.BytesIO()
        Image.new("RGBA", (160, 100)).save(out, format="PNG")
        self.assertEqual(self.add(2, data=out.getvalue())["status"], "unknown")

    def test_foreign_and_extended_lease_are_rejected(self):
        self.assertEqual(self.ledger.check(replace(self.lease, owner=object()), "inputs"), "foreign_lease")
        self.assertEqual(self.ledger.check(replace(self.lease, expires_at_monotonic_ns=self.clock() + 3_000_000_000),
                                           "inputs"), "invalid_deadline")

    def test_receive_time_regression_and_future(self):
        self.clock.now += 5
        s = replace(self.first.source, sequence=2, host_received_monotonic_ns=self.clock() + 1)
        self.assertEqual(self.ledger.observe(Frame(s, png()))["status"], "unknown")
        s = replace(s, sequence=3, host_received_monotonic_ns=self.first.source.host_received_monotonic_ns - 1)
        self.assertEqual(self.ledger.observe(Frame(s, png()))["status"], "unknown")

    def test_clock_and_stream_changes_are_terminal(self):
        self.clock.now -= 1
        self.assertEqual(self.ledger.check(self.lease, "inputs"), "clock_regressed_or_invalid")
        self.clock.now += 2
        self.assertEqual(self.ledger.check(self.lease, "inputs"), "clock_regressed_or_invalid")

    def test_stream_change_is_terminal(self):
        self.add(2, source="other-private-stream")
        self.assertEqual(self.ledger.check(self.lease, "inputs"), "stream_changed")

    def test_check_budget_rejects_even_same_pixels(self):
        with patch("jcc_realtime_probe.pixels.time.perf_counter_ns", side_effect=[0, 250_000_000]):
            result = self.add(2)
        self.assertEqual(result["reason"], "check_timeout")
        self.assertIsNotNone(self.ledger.check(self.lease, "inputs"))

    def test_disconnect_rejects_existing_lease(self):
        self.ledger.disconnect()
        self.assertEqual(self.ledger.check(self.lease, "inputs"), "disconnected")


class IntegrationTests(unittest.TestCase):
    def test_four_fps_slow_work_is_local_only_and_every_frame_checked(self):
        r = run_timeline(synthetic_rows(), synthetic_profile())
        self.assertEqual(r["pixel_checks"]["frames_checked"], 20)
        self.assertEqual(r["pixel_checks"]["full_changed_pairs"], 19)
        self.assertEqual(r["pixel_checks"]["input_changed_pairs"], 0)
        self.assertEqual(r["completion_outcomes"]["inputs"]["simulated_pixel_inputs_only"], 3)
        self.assertEqual(r["counts"]["formal_accepted"], 0)
        self.assertEqual(r["counts"]["formal_rejected"], r["counts"]["jobs_completed"])
        self.assertEqual(r["counts"]["max_pending_jobs"], 1)
        self.assertEqual(r["counts"]["max_active_jobs"], 1)

    def test_virtual_event_timeline_is_reproducible(self):
        files = [io.StringIO(), io.StringIO()]
        reports = [run_timeline(synthetic_rows(), synthetic_profile(), events_file=f,
                                run_id="a" * 16) for f in files]
        self.assertEqual(files[0].getvalue(), files[1].getvalue())
        self.assertEqual(reports[0]["counts"], reports[1]["counts"])

    def test_aba_and_gap_remove_early_local_results(self):
        for case in ("inside_aba", "sequence_gap"):
            with self.subTest(case=case):
                events = io.StringIO()
                run_timeline(synthetic_rows(case), synthetic_profile(), events_file=events)
                first = [json.loads(line) for line in events.getvalue().splitlines()
                         if json.loads(line)["sample_id"].endswith(":1")
                         and json.loads(line)["stage"] == "pixel_inputs"]
                self.assertFalse(any(e["status"] in CANDIDATE_STATUSES for e in first))

    def test_stall_rejects_work_and_expiry_timer_runs_without_a_new_frame(self):
        events = io.StringIO()
        r = run_timeline(synthetic_rows("receive_stall"), synthetic_profile(), events_file=events)
        self.assertGreater(r["completion_outcomes"]["inputs"].get("frame_timeout", 0), 0)
        first = [e for e in map(json.loads, events.getvalue().splitlines())
                 if e["sample_id"].endswith(":1") and e["stage"] == "pixel_inputs"]
        expired = next(e for e in first if e["status"] == "expired")
        self.assertEqual(expired["emitted_monotonic_ns"], expired["expires_at_monotonic_ns"])

    def test_work_over_ttl_has_no_candidate(self):
        events = io.StringIO()
        run_timeline(synthetic_rows("static"), synthetic_profile(), processing_ns=2_000_000_000,
                     events_file=events)
        rows = [json.loads(line) for line in events.getvalue().splitlines()]
        self.assertFalse(any(e["status"] in CANDIDATE_STATUSES for e in rows))
        self.assertTrue(any(e["status"] == "expired" for e in rows))

    def test_deadlines_and_sample_clock_fields_are_explicit(self):
        events = io.StringIO()
        run_timeline(synthetic_rows(), synthetic_profile(), events_file=events)
        for e in map(json.loads, events.getvalue().splitlines()):
            self.assertEqual(e["expires_at_monotonic_ns"], e["frame_received_monotonic_ns"] + RESULT_TTL_NS)
            self.assertGreaterEqual(e["emitted_monotonic_ns"], e["frame_received_monotonic_ns"])
            self.assertEqual(e["clock_kind"], "virtual_monotonic_ns")
            if e["status"] in CANDIDATE_STATUSES:
                self.assertLess(e["emitted_monotonic_ns"], e["expires_at_monotonic_ns"])

    def test_empty_ocr_is_no_output_and_semantic_gate_stays_unknown(self):
        clock = VirtualClock()
        sink = EventSink("b" * 16, "synthetic", "virtual_monotonic_ns", clock, processing_kind="ocr")
        core = ProbeCore("test", synthetic_profile(), sink, clock_ns=clock)
        core.observe_frame(frame(clock))
        job = core.start_job()
        class EmptyOCR:
            def recognize(self, data):
                return []
        core.complete(job, OCRProcessor(core.profile, EmptyOCR(), sink.run_id)(job))
        self.assertEqual(core.summary()["completion_outcomes"]["inputs"], {"ocr_empty": 1})
        self.assertEqual(core.summary()["counts"]["formal_rejected"], 1)

    def test_private_text_ids_and_values_do_not_enter_summary_or_events(self):
        clock = VirtualClock()
        stream = "secret-player-device-text"
        out = io.StringIO()
        profile = synthetic_profile()
        sink = EventSink("b" * 16, "synthetic", "virtual_monotonic_ns", clock, out, processing_kind="ocr")
        core = ProbeCore(stream, profile, sink, clock_ns=clock)
        f = frame(clock, source=stream)
        core.observe_frame(f)
        job = core.start_job()
        class OCR:
            def recognize(self, data):
                return [dict(text="secret-ocr-content", confidence=.9, x=1, y=1, width=5, height=5)]
        report = OCRProcessor(profile, OCR(), sink.run_id)(job)
        core.complete(job, report)
        data = out.getvalue() + json.dumps(core.summary())
        for secret in (stream, "secret-ocr-content", "private-utc", "image_sha256", "candidate_value"):
            self.assertNotIn(secret, data)

    def test_all_attempted_10g_inputs_are_added_to_pixel_dependency_union(self):
        p = load_profile(dict(image_size=[160,100], input_boxes=[[10,10,30,30]],
            cues=[dict(cue_id="round_header",box=[50,10,90,30])],
            numeric_fields=[dict(field_name="gold",box=[10,50,40,80])]), "replay")
        self.assertEqual(p.dependencies, ((10,10,30,30),(50,10,90,30),(10,50,40,80)))

    def test_latest_mailbox_replacement_never_skips_receive_checks(self):
        class Source:
            def __init__(self):
                self.q = queue.Queue()
            def read_frame(self, timeout):
                try:
                    return self.q.get(timeout=timeout)
                except queue.Empty:
                    raise TimeoutError from None
            def close(self):
                pass
        source = Source()
        stream = LatestFrameStream(source, read_timeout=.01)
        sink = EventSink("c" * 16, "synthetic", "host_monotonic_ns", processing_kind="simulated_work")
        core = ProbeCore(stream.stream_id, synthetic_profile(), sink)
        stream.bind_observer(core)
        stream.start()
        try:
            original = None
            for seq, pixel in ((1,0),(2,1),(3,0)):
                source.q.put(png(inside=pixel))
                deadline = time.monotonic() + 1
                while stream.stats()["frames_received"] < seq and time.monotonic() < deadline:
                    time.sleep(.001)
                with core.receive_lock:
                    self.assertEqual(core.counts["frames_checked"], seq)
                    if seq == 1:
                        original = core.pending[1]
            self.assertEqual(stream.read_latest(timeout=.1).source.sequence, 3)
            self.assertEqual(stream.stats()["frames_replaced"], 2)
            self.assertEqual(core.ledger.summary()["input_changed_pairs"], 2)
            self.assertIsNotNone(core.ledger.check(original, "inputs"))
        finally:
            stream.close()
        self.assertFalse(stream.stats()["receiver_thread_alive"])

    def test_configured_10g_runs_on_same_frame_but_fields_abstain_without_witness(self):
        data = dict(image_size=[160,100], input_boxes=[[10,10,30,30]],
                    cues=[dict(cue_id="round_header", box=[10,10,40,30]),
                          dict(cue_id="shop_experience", box=[45,10,75,30], exact_texts=["XP"]),
                          dict(cue_id="shop_refresh", box=[80,10,115,30], exact_texts=["REFRESH"])],
                    numeric_fields=[dict(field_name=name, box=box) for name,box in zip(
                        ("round","hp","gold","level"),
                        ([10,50,40,80],[45,50,75,80],[80,50,110,80],[115,50,155,80]))])
        profile = load_profile(data, "synthetic")
        sink = EventSink("e" * 16, "synthetic", "host_monotonic_ns", processing_kind="ocr")
        core = ProbeCore("test", profile, sink)
        core.observe_frame(frame(time.monotonic_ns))
        job = core.start_job()
        class OCR:
            def __init__(self):
                self.texts = iter(("raw-private", "3-2", "XP", "REFRESH", "3-2", "100", "42", "7"))
            def recognize(self, data):
                return [dict(text=next(self.texts), confidence=.9, x=2,y=2,width=8,height=8)]
        report = OCRProcessor(profile, OCR(), sink.run_id)(job)
        self.assertEqual(report["ocr_requests"], 8)
        self.assertEqual(report["scene"]["scene"]["candidate"], "battle")
        self.assertTrue(all(d["status"] == "unknown" and d["reason"] == "frame_witness_missing"
                            for d in report["fields"]["fields"].values()))
        core.complete(job, report)
        self.assertEqual(core.summary()["counts"]["formal_accepted"], 0)
        self.assertNotIn("candidate_value", json.dumps(core.summary()))

    def test_event_api_forbids_semantic_candidate_and_virtual_live_source(self):
        clock = VirtualClock()
        with self.assertRaises(ValueError):
            EventSink("f"*16, "live", "virtual_monotonic_ns", clock, processing_kind="ocr")
        sink = EventSink("f"*16, "synthetic", "virtual_monotonic_ns", clock, processing_kind="ocr")
        with self.assertRaises(ValueError):
            sink.emit(frame(clock).source, "field_gate", "candidate", "pixel_inputs_only")
        self.assertEqual(sink.count, 0)

    def test_event_at_deadline_is_expired_never_candidate(self):
        clock = VirtualClock()
        f = frame(clock)
        sink = EventSink("f"*16, "synthetic", "virtual_monotonic_ns", clock, processing_kind="ocr")
        clock.now += RESULT_TTL_NS
        event = sink.emit(f.source, "pixel_inputs", "candidate", "pixel_inputs_only",
                          processing=ProcessingResult("ocr", "ocr_text", 1, 1))
        self.assertEqual((event["status"], event["reason"]), ("expired", "result_expired"))

    def test_slow_event_write_also_revokes_and_counts_the_complete_check_budget(self):
        class SlowOnce:
            def __init__(self):
                self.first = True
            def write(self, line):
                if self.first:
                    self.first = False
                    time.sleep(.26)
            def flush(self):
                pass
        clock = VirtualClock()
        sink = EventSink("f"*16, "synthetic", "virtual_monotonic_ns", clock, SlowOnce(),
                         processing_kind="simulated_work")
        core = ProbeCore("test", synthetic_profile(), sink, clock_ns=clock)
        core.observe_frame(frame(clock))
        self.assertIsNone(core.pending)
        self.assertEqual(core.ledger.summary()["check_timeouts"], 1)
        self.assertEqual(core.ledger.summary()["unknown_checks"], 1)
        self.assertGreaterEqual(core.timings["receive_check"][0], 250_000_000)

    def test_source_failure_is_redacted_and_not_reported_as_success(self):
        class FailingSource:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
            def read_frame(self, timeout):
                raise RuntimeError("private-device-identifier-and-log")
            def close(self):
                pass
        class OCR:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def close(self):
                pass
        profile = load_profile(dict(image_size=[160,100], input_boxes=[[10,10,70,40]]), "live")
        with patch("jcc_realtime_probe.runtime.TabletStream", return_value=FailingSource()), \
             patch("jcc_realtime_probe.runtime.VisionOCR", return_value=OCR()):
            result = run_live(profile, duration_seconds=.01, max_frames=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["counts"]["frames_checked"], 0)
        self.assertEqual(result["stream"]["source_errors"], 1)
        self.assertFalse(result["stream"]["receiver_thread_alive"])
        self.assertIn("RuntimeError", result["error_types"])
        self.assertNotIn("private-device-identifier-and-log", json.dumps(result))

    def test_profile_cannot_relabel_source_kind(self):
        sink = EventSink("f"*16, "replay", "virtual_monotonic_ns", VirtualClock(), processing_kind="simulated_work")
        with self.assertRaises(ValueError):
            ProbeCore("test", synthetic_profile(), sink)

    def test_private_path_cannot_point_into_repository_even_via_symlink(self):
        with self.assertRaises(ValueError):
            outside_project(Path(__file__).resolve())
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "alias"
            link.symlink_to(Path(__file__).resolve().parents[3], target_is_directory=True)
            with self.assertRaises(ValueError):
                outside_project(link / "private.json")


if __name__ == "__main__":
    unittest.main()
