"""Public producer/consumer calls and deliberately faulty synthetic inputs."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from jcc_device.latest import FrameRef
from jcc_realtime_probe.events import EventSink
from jcc_realtime_probe.processing import ProcessingResult, simulated_report
from jcc_timed_overlay.store import TimedOverlayStore

from jcc_realtime_overlay_bridge.bridge import RealtimeOverlayBridge
from jcc_realtime_overlay_bridge.clocks import ClockBinding, bind_same_process, bind_spawned, spawned_hello
from jcc_realtime_overlay_bridge.synthetic import InProcessPipeline, MS, run_synthetic
from jcc_realtime_overlay_bridge.transport import AppendOnlyTail


class Fixture(unittest.TestCase):
    def setUp(self):
        self.pipeline = InProcessPipeline(run_id="a" * 16)
        self.clock, self.core, self.bridge = self.pipeline.clock, self.pipeline.core, self.pipeline.bridge
        self.addCleanup(self.bridge.store.close)

    def ready(self):
        frame = self.pipeline.receive()
        job = self.core.start_job()
        self.clock.now += 80 * MS
        self.pipeline.complete(job)
        self.assertEqual(self.bridge.store.view()["status"], "ready")
        return frame

    def row(self, frame, stage="pixel_inputs", status="simulated_candidate", reason="simulated_pixel_inputs_only", **updates):
        sink = EventSink(self.pipeline.sink.run_id, self.pipeline.sink.source_kind,
                         self.pipeline.sink.clock_kind, self.clock, processing_kind=self.pipeline.sink.processing_kind)
        processing = (ProcessingResult.from_report(simulated_report())
                      if stage in {"pixel_full", "pixel_inputs"} else None)
        row = sink.emit(frame.source, stage, status, reason, processing=processing)
        row["event_index"] = self.bridge.event_index + 1
        row.update(updates)
        return row

    def no_hint(self):
        self.assertIsNone(self.bridge.store.view()["message"])
        self.assertEqual(self.bridge.store.view()["lease_ms"], 0)


class BridgeTests(Fixture):
    def test_reference_uses_real_modules_and_keeps_complete_denominator(self):
        report = run_synthetic()
        self.assertEqual(report["bridge"]["opportunities"], 5)
        self.assertEqual(report["bridge"]["frames_ready"], 2)
        self.assertEqual(report["producer_counts"]["formal_accepted"], 0)
        self.assertEqual(report["producer_counts"]["formal_rejected"], 5)
        self.assertEqual(report["bridge"]["consumer"]["opportunities"], 5)
        self.assertEqual(sum(report["bridge"]["outcomes"].values()), 5)

    def test_every_new_frame_has_invalidation_before_pending(self):
        self.ready()
        events = []
        class Capture:
            def write(self, row):
                events.append(row)
            def close(self):
                pass
        self.bridge.store.journal = Capture()
        self.clock.now += 170 * MS
        frame = self.pipeline.receive(outside=1)
        self.no_hint()
        fault_at = next(i for i, row in enumerate(events) if row["kind"] == "source_fault" and row["reason"] == "bridge_new_frame")
        pending_at = next(i for i, row in enumerate(events) if row["kind"] == "source_event" and row["event"]["status"] == "pending")
        self.assertLess(fault_at, pending_at)
        self.assertEqual(self.bridge.counts["new_frame_barriers"], 2)
        self.assertEqual(self.bridge.store.view()["sample_id"], self.pipeline.sink.run_id + ":2")
        self.assertEqual(self.bridge.mapped_events[-1]["frame_received_monotonic_ns"], frame.source.host_received_monotonic_ns)

    def test_old_candidate_accepted_by_pixel_continuity_cannot_show_for_new_frame(self):
        self.pipeline.receive()
        old = self.core.start_job()
        self.clock.now += 250 * MS
        self.pipeline.receive(outside=1)
        self.clock.now += 80 * MS
        self.pipeline.complete(old)
        self.assertEqual(self.core.summary()["completion_outcomes"]["inputs"]["simulated_pixel_inputs_only"], 1)
        self.assertEqual(self.bridge.report()["frames_ready"], 0)
        self.no_hint()

    def test_aba_never_revives_first_frame(self):
        self.pipeline.receive()
        old = self.core.start_job()
        for inside in (1, 0):
            self.clock.now += 250 * MS
            self.pipeline.receive(inside=inside)
        self.pipeline.complete(old)
        self.no_hint()
        self.assertEqual(self.bridge.report()["opportunities"], 3)
        self.assertEqual(self.bridge.report()["frames_ready"], 0)

    def test_simulated_no_output_is_in_both_denominators(self):
        self.pipeline.receive()
        job = self.core.start_job()
        self.clock.now += 80 * MS
        self.pipeline.complete(job, outcome="simulated_no_output")
        report = self.bridge.report()
        self.assertEqual(report["outcomes"], {"no_result": 1})
        self.assertEqual(report["consumer"]["outcomes"], {"no_result": 1})
        self.no_hint()

    def test_processing_rejection_is_not_removed_from_denominator(self):
        self.pipeline.receive()
        job = self.core.start_job()
        self.clock.now += 80 * MS
        self.pipeline.complete(job, outcome="simulated_error")
        self.assertEqual(self.bridge.report()["outcomes"], {"processing_rejected": 1})
        self.assertEqual(self.bridge.report()["consumer"]["outcomes"], {"unknown": 1})

    def test_unknown_frame_check_has_no_machine_or_formal_output(self):
        self.pipeline.receive(data=b"not-an-image")
        self.assertIsNone(self.core.start_job())
        self.assertEqual(self.bridge.report()["outcomes"], {"check_unknown": 1})
        self.assertEqual(self.bridge.report()["consumer"]["opportunities"], 1)
        self.no_hint()

    def test_late_receive_budget_rejection_is_not_a_second_frame(self):
        pipeline = self.pipeline
        class SlowWriter:
            delayed = False
            def write(self, text):
                pipeline.write(text)
                if not self.delayed and '"stage":"receive"' in text:
                    self.delayed = True
                    time.sleep(.26)
            def flush(self):
                pass
        self.pipeline.sink.file = SlowWriter()
        self.pipeline.receive()
        self.assertIsNone(self.core.start_job())
        report = self.bridge.report()
        self.assertIsNone(report["fault"])
        self.assertEqual(report["opportunities"], 1)
        self.assertEqual(report["counters"]["receive_budget_rejections"], 1)
        self.assertTrue(report["frames"][0]["receive_check_rejected"])
        self.assertEqual(report["consumer"]["opportunities"], 1)
        self.no_hint()

    def test_formal_gate_no_output_does_not_prematurely_finish_simulation(self):
        self.pipeline.receive()
        self.assertEqual(self.bridge.store.view()["status"], "pending")
        self.assertEqual(self.bridge.report()["counters"]["formal_gate_without_output"], 1)
        job = self.core.start_job()
        self.clock.now += 80 * MS
        self.pipeline.complete(job)
        self.assertEqual(self.bridge.store.view()["status"], "ready")
        self.assertEqual(self.core.summary()["counts"]["formal_accepted"], 0)

    def test_receive_silence_deadline_wins_over_longer_original_ttl(self):
        frame = self.ready()
        deadline = frame.source.host_received_monotonic_ns + self.bridge.silence_ns
        self.assertLess(deadline, self.bridge.frames[self.bridge.current]["original_expires_at_monotonic_ns"])
        self.clock.now = deadline - 1
        self.assertEqual(self.bridge.store.view()["status"], "ready")
        self.clock.now += 1
        self.bridge.tick()
        self.no_hint()
        self.assertEqual(self.bridge.report()["outcomes"], {"deadline_elapsed": 1})

    def test_pending_timeout_stays_in_denominator_without_result(self):
        self.pipeline.receive()
        self.clock.now += self.bridge.silence_ns
        self.bridge.tick()
        report = self.bridge.report()
        self.assertEqual(report["opportunities"], 1)
        self.assertEqual(sum(report["consumer"]["outcomes"].values()), 1)
        self.no_hint()

    def test_expiry_crossed_inside_consumer_is_not_counted_as_ready(self):
        self.pipeline.receive()
        job = self.core.start_job()
        deadline = self.clock() + self.bridge.silence_ns
        original = self.bridge.store.ingest
        def crosses_boundary(row):
            if row["status"] == "ready":
                self.clock.now = deadline
            return original(row)
        self.bridge.store.ingest = crosses_boundary
        self.clock.now += 80 * MS
        self.pipeline.complete(job)
        report = self.bridge.report()
        self.assertEqual(report["frames_ready"], 0)
        self.assertEqual(report["opportunities"], 1)
        self.assertEqual(sum(report["consumer"]["outcomes"].values()), 1)
        self.no_hint()

    def test_processing_events_never_extend_receive_lease(self):
        frame = self.ready()
        original = self.bridge.frames[self.bridge.current]["deadline_ns"]
        started = self.bridge.frames[self.bridge.current]["processing_started_ns"]
        self.clock.now += 800 * MS
        self.bridge.ingest(self.row(frame, "processing", "started", "processing_started"))
        self.assertEqual(self.bridge.frames[self.bridge.current]["deadline_ns"], original)
        self.assertEqual(self.bridge.frames[self.bridge.current]["processing_started_ns"], started)
        self.clock.now = original
        self.bridge.tick()
        self.no_hint()

    def test_repeated_candidate_never_extends_or_reissues_token(self):
        frame = self.ready()
        before = self.bridge.store.view()
        self.clock.now += 20 * MS
        self.assertTrue(self.bridge.ingest(self.row(frame)))
        after = self.bridge.store.view()
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["render_token"], before["render_token"])
        self.assertLess(after["remaining_ms"], before["remaining_ms"])

    def test_event_duplicate_reorder_and_gap_are_latched(self):
        for delta in (-1, 0, 2):
            with self.subTest(delta=delta):
                self.setUp()
                frame = self.ready()
                raw = self.row(frame, event_index=self.bridge.event_index + delta)
                self.assertFalse(self.bridge.ingest(raw))
                self.no_hint()
                self.clock.now += 10 * MS
                self.assertFalse(self.bridge.ingest(self.row(frame)))
                self.no_hint()

    def test_frame_gap_counts_rejected_receive_attempt(self):
        self.ready()
        self.clock.now += 170 * MS
        self.pipeline.sequence = 2
        self.pipeline.receive()
        report = self.bridge.report()
        self.assertEqual(report["opportunities"], 2)
        self.assertEqual(report["accepted_frames"], 1)
        self.assertEqual(report["outcomes"]["rejected_receive"], 1)
        self.assertEqual(sum(report["outcomes"].values()), 2)
        self.no_hint()

    def test_nonadvancing_frame_time_is_rejected(self):
        self.pipeline.receive()
        self.pipeline.receive()
        self.assertTrue(self.bridge.faulted)
        self.assertEqual(self.bridge.report()["opportunities"], 2)
        self.no_hint()

    def test_already_expired_receive_is_not_retimed_for_consumer(self):
        frame = self.pipeline.receive()
        self.clock.now += 2000 * MS
        source = FrameRef(self.pipeline.stream_id, 2, frame.source.host_received_monotonic_ns + MS, "synthetic")
        raw = self.row(type("Frame", (), {"source": source})(), "receive", "received", "frame_checked")
        self.assertFalse(self.bridge.ingest(raw))
        report = self.bridge.report()
        self.assertEqual(report["opportunities"], 2)
        self.assertEqual(report["consumer"]["opportunities"], 2)
        self.assertEqual(report["outcomes"].get("expired_before_consumer"), 1)
        self.no_hint()

    def test_changed_origin_or_deadline_is_never_repaired_by_renaming(self):
        for field in ("frame_received_monotonic_ns", "expires_at_monotonic_ns"):
            with self.subTest(field=field):
                self.setUp()
                frame = self.ready()
                raw = self.row(frame)
                raw[field] += 1
                self.assertFalse(self.bridge.ingest(raw))
                self.no_hint()

    def test_future_boolean_float_string_and_overflow_times_rejected(self):
        for value in (True, 100.5, "10000000000", 2**63, None):
            with self.subTest(value=str(value)):
                self.setUp()
                frame = self.ready()
                raw = self.row(frame, emitted_monotonic_ns=value)
                self.assertFalse(self.bridge.ingest(raw))
                self.no_hint()
        self.setUp()
        frame = self.ready()
        self.assertFalse(self.bridge.ingest(self.row(frame, emitted_monotonic_ns=self.clock() + 1)))
        self.no_hint()

    def test_unknown_fields_cannot_carry_game_advice_or_private_strings(self):
        frame = self.ready()
        raw = self.row(frame, advice="private-untrusted-input")
        self.assertFalse(self.bridge.ingest(raw))
        self.assertNotIn("private-untrusted-input", json.dumps(self.bridge.report()))
        self.no_hint()

    def test_upstream_restart_new_domain_is_terminal(self):
        frame = self.ready()
        self.assertFalse(self.bridge.ingest(self.row(frame, clock_domain_id="b" * 16, event_index=1)))
        self.no_hint()

    def test_consumer_restart_is_detected_and_old_hint_never_enters_it(self):
        self.ready()
        old = self.bridge.store
        self.bridge.store = TimedOverlayStore(clock_domain_id=self.pipeline.sink.run_id,
                                              stream_id=self.bridge.stream_id, clock=self.clock)
        self.addCleanup(self.bridge.store.close)
        old.close()
        self.bridge.tick()
        self.assertEqual(self.bridge.faulted, "clock_or_consumer_session_changed")
        self.no_hint()

    def test_consumer_close_rejects_late_result(self):
        self.pipeline.receive()
        job = self.core.start_job()
        self.bridge.store.close()
        self.clock.now += 80 * MS
        self.pipeline.complete(job)
        self.assertTrue(self.bridge.faulted)
        self.no_hint()

    def test_new_bridge_cannot_replay_old_producer_session(self):
        frame = self.ready()
        old_row = self.row(frame)
        self.clock.now += MS
        new = RealtimeOverlayBridge(bind_same_process(self.pipeline.sink))
        self.addCleanup(new.store.close)
        self.assertFalse(new.ingest(old_row))
        self.assertIsNone(new.store.view()["message"])

    def test_clock_regression_is_latched_after_numbers_recover(self):
        frame = self.ready()
        self.clock.now -= 1
        self.bridge.tick()
        self.clock.now += 100 * MS
        self.assertFalse(self.bridge.ingest(self.row(frame)))
        self.no_hint()

    def test_large_python_nanoseconds_remain_exact_and_export_as_strings(self):
        self.clock.now = 2**53 + 100_000_001
        pipeline = InProcessPipeline(clock=self.clock)
        self.addCleanup(pipeline.bridge.store.close)
        frame = pipeline.receive()
        job = pipeline.core.start_job()
        self.clock.now += 80 * MS
        pipeline.complete(job)
        report = pipeline.bridge.report()
        self.assertEqual(report["frames"][0]["frame_received_monotonic_ns"], str(frame.source.host_received_monotonic_ns))
        self.assertEqual(int(report["frames"][0]["deadline_ns"]), frame.source.host_received_monotonic_ns + 1_000_000_000)
        self.assertIsInstance(report["consumer"]["samples"][0]["frame_received_monotonic_ns"], str)
        self.assertNotIn("monotonic_ns", json.dumps(pipeline.bridge.store.view()))

    def test_fixture_source_classification_never_changes_simulation_semantics(self):
        for source, target in (("synthetic", "synthetic"), ("replay", "replay"), ("live", "device_readonly")):
            pipeline = InProcessPipeline(source_kind=source)
            self.addCleanup(pipeline.bridge.store.close)
            pipeline.receive()
            job = pipeline.core.start_job()
            if source == "live":
                time.sleep(.001)
            else:
                pipeline.clock.now += 80 * MS
            pipeline.complete(job)
            report, view = pipeline.bridge.report(), pipeline.bridge.store.view()
            self.assertEqual(report["upstream_source_kind"], source)
            self.assertEqual(view["source_kind"], target)
            self.assertTrue(view["simulation"])
            self.assertFalse(view["execution_eligible"])
            self.assertEqual(view["message"], "模拟计时 · 仅测试显示")
            self.assertIsNone(report["user_saw_hint_at_ns"])

    def test_late_browser_report_is_historical_and_does_not_restore_display(self):
        self.ready()
        old = self.bridge.store.view()
        self.clock.now += 170 * MS
        self.pipeline.receive()
        report = dict(session_id=old["session_id"], client_id="synthetic-browser-fixture",
                      client_event_sequence=1, revision=old["revision"], render_token=old["render_token"],
                      sample_id=old["sample_id"], stage="paint_opportunity", reason="none", visibility="visible",
                      browser_monotonic_ms=100, since_fetch_start_ms=10, dom_to_raf_ms=5)
        self.assertTrue(self.bridge.store.report_browser(report))
        self.assertEqual(self.bridge.store.report()["sample_count_with_report"], 0)
        self.no_hint()


class TransportTests(Fixture):
    def test_partial_new_receive_withdraws_immediately_then_accepts_only_new_frame(self):
        self.ready()
        self.clock.now += 170 * MS
        source = FrameRef(self.pipeline.stream_id, 2, self.clock(), "synthetic")
        frame = type("Frame", (), {"source": source})()
        raw = self.row(frame, "receive", "received", "frame_checked")
        line = json.dumps(raw).encode()
        self.pipeline.decoder.feed(line[:30])
        self.no_hint()
        self.pipeline.decoder.feed(line[30:] + b"\n")
        self.assertEqual(self.bridge.store.view()["status"], "pending")
        candidate = json.dumps(self.row(frame)).encode()
        self.pipeline.decoder.feed(candidate + b"\n")
        self.assertEqual(self.bridge.store.view()["status"], "ready")
        repeated = json.dumps(self.row(frame)).encode()
        self.pipeline.decoder.feed(repeated[:10])
        self.no_hint()
        self.pipeline.decoder.feed(repeated[10:] + b"\n")
        self.no_hint()

    def test_partial_eof_is_latched(self):
        self.ready()
        self.pipeline.decoder.feed(b'{"schema_')
        self.pipeline.decoder.eof()
        self.assertEqual(self.bridge.faulted, "transport_partial_eof")
        self.no_hint()

    def test_pipe_eof_immediately_withdraws_ready(self):
        self.ready()
        self.pipeline.decoder.eof()
        self.no_hint()
        self.assertEqual(self.bridge.faulted, "transport_eof")

    def test_bad_json_duplicate_keys_nonfinite_and_oversize_are_not_echoed(self):
        for line in (b'{"x":1,"x":2}\n', b'{"x":NaN}\n', b'\xff\n', b'private-input\n', b'x' * 4097):
            with self.subTest(length=len(line)):
                self.setUp()
                self.ready()
                self.pipeline.decoder.feed(line)
                self.assertTrue(self.pipeline.decoder.closed)
                self.no_hint()
                self.assertNotIn("private-input", json.dumps(self.bridge.report()))

    def file_ready(self, directory):
        path = Path(directory) / "input.jsonl"
        path.touch()
        tail = AppendOnlyTail(path, self.pipeline.decoder)
        self.addCleanup(tail.close)
        writer = path.open("a", encoding="utf-8")
        self.addCleanup(writer.close)
        self.pipeline.sink.file = writer
        self.pipeline.receive()
        tail.poll()
        job = self.core.start_job()
        self.clock.now += 80 * MS
        self.pipeline.complete(job)
        tail.poll()
        self.assertEqual(self.bridge.store.view()["status"], "ready")
        return path, tail

    def test_file_truncation_and_refill_cannot_revive_old_hint(self):
        for refill in (False, True):
            with self.subTest(refill=refill), tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
                self.setUp()
                path, tail = self.file_ready(directory)
                old = path.read_bytes()
                path.write_bytes(b"" if not refill else b"X" + old[1:] + b"more\n")
                tail.poll()
                self.assertTrue(tail.failed)
                self.no_hint()
                path.write_bytes(old)
                tail.poll()
                self.no_hint()

    def test_replaced_file_is_terminal(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path, tail = self.file_ready(directory)
            replacement = path.with_name("replacement.jsonl")
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
            tail.poll()
            self.assertTrue(tail.failed)
            self.no_hint()

    def test_tail_requires_empty_regular_file_and_refuses_symlink_or_fifo(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "existing"
            path.write_bytes(b"old\n")
            link = Path(directory) / "link"
            link.symlink_to(path)
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo)
            for target in (path, link, fifo, Path(directory)):
                with self.subTest(target=target.name), self.assertRaises((ValueError, OSError)):
                    AppendOnlyTail(target, self.pipeline.decoder)


class ClockTests(unittest.TestCase):
    def test_domain_label_or_unsealed_binding_is_insufficient(self):
        for binding in ({"clock_domain_id": "a" * 16}, ClockBinding("a" * 16, "synthetic",
                           "host_monotonic_ns", "claimed", time.monotonic_ns, os.getpid(), None, "simulated_work")):
            with self.assertRaises(ValueError):
                RealtimeOverlayBridge(binding)

    def test_host_clock_kind_cannot_hide_a_virtual_callable(self):
        sink = EventSink("a" * 16, "synthetic", "host_monotonic_ns", lambda: 10_000_000_000, processing_kind="simulated_work")
        with self.assertRaises(ValueError):
            bind_same_process(sink)

    def test_spawned_clock_requires_matching_epoch_pid_challenge_and_bracket(self):
        start = time.monotonic_ns()
        hello = spawned_hello("challenge", "a" * 16)
        end = time.monotonic_ns()
        for change in ({"nonce": "wrong"}, {"pid": -1}, {"boot_digest": "wrong"},
                       {"clock_kind": "virtual_monotonic_ns"}, {"now_ns": end + 1}, {"now_ns": start - 1},
                       {"source_kind": "live"}, {"implementation": "other-clock"}):
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                bind_spawned(dict(hello, **change), nonce="challenge", run_id="a" * 16,
                             child_pid=os.getpid(), sent_ns=start, received_ns=end)


if __name__ == "__main__":
    unittest.main()
