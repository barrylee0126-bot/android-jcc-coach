"""v2 integration using generated pixels and counted OCR adapter fixtures.

No Android, historical images, game state, or OCR text leaves this test.
"""

from copy import deepcopy
import io
import json
import time
import unittest
from jcc_realtime_probe.processing import ProcessingResult
from jcc_realtime_probe.core import ProbeCore
from jcc_realtime_probe.processor import OCRProcessor
from jcc_realtime_probe.profile import load_profile
from jcc_realtime_overlay_bridge.bridge import PROCESSING_KEYS, RealtimeOverlayBridge
from jcc_realtime_overlay_bridge.clocks import bind_same_process, bind_spawned, spawned_hello
from jcc_realtime_overlay_bridge.synthetic import InProcessPipeline, MS
from jcc_timed_overlay.store import TimedOverlayStore


def text_item(**changes):
    return dict(dict(text="generated-private-text", confidence=.9, x=2, y=2, width=8, height=8), **changes)


class CountedBackend:
    def __init__(self, *responses):
        self.responses, self.calls = iter(responses), 0

    def recognize(self, data):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class V2Tests(unittest.TestCase):
    def pipeline(self, *, source="synthetic", kind="simulated_work", **fields):
        profile = load_profile({"image_size": [160, 100], "input_boxes": [[10, 10, 70, 40]], **fields}, source)
        p = InProcessPipeline(source_kind=source, processing_kind=kind, profile=profile)
        self.addCleanup(p.bridge.store.close)
        return p

    def finish_ocr(self, p, backend, *, processor_profile=None):
        job = p.core.start_job()
        report = OCRProcessor(processor_profile or p.core.profile, backend, p.sink.run_id)(job)
        p.complete(job, report=report)
        self.assertEqual(report["ocr_requests"], backend.calls)
        return report

    def assert_accounting(self, p, opportunities):
        r = p.bridge.report()
        self.assertEqual(r["opportunities"], opportunities)
        self.assertEqual(sum(r["outcomes"].values()), opportunities)
        self.assertTrue(r["accounting"]["matches_observed_receive_attempts"])
        self.assertEqual(sum(r["consumer"]["outcomes"].values()), r["consumer"]["opportunities"])
        self.assertEqual(p.core.summary()["counts"]["formal_accepted"], 0)
        self.assertEqual(r["formal_game_state_outputs"], 0)
        self.assertIsNone(r["actual_end_to_end_visible_latency_ms"])
        return r

    def test_v1_is_rejected_without_inventing_processing_provenance(self):
        p = self.pipeline()
        p.sink.file = io.StringIO()
        p.receive()
        captured = list(map(json.loads, p.sink.file.getvalue().splitlines()))
        raw = captured[0]
        raw["schema_version"] = "realtime-probe-v1"
        for k in PROCESSING_KEYS:
            raw.pop(k)
        self.assertFalse(p.bridge.ingest(raw))
        r = self.assert_accounting(p, 1)
        self.assertEqual(r["outcomes"], {"rejected_receive": 1})
        self.assertEqual(r["frames_ready"], 0)
        self.assertEqual(r["upstream_audit"], [])

    def test_source_and_processing_kinds_remain_independent_in_all_six_fixtures(self):
        for source in ("synthetic", "replay", "live"):
            for kind in ("simulated_work", "ocr"):
                with self.subTest(source=source, kind=kind):
                    p = self.pipeline(source=source, kind=kind)
                    p.receive()
                    if kind == "simulated_work":
                        p.complete(p.core.start_job())
                    else:
                        self.finish_ocr(p, CountedBackend([text_item()]))
                    r = self.assert_accounting(p, 1)
                    self.assertIsNone(r["fault"])
                    self.assertEqual(r["processing_kind"], kind)
                    self.assertEqual(r["upstream_source_kind"], source)
                    expected = "candidate" if kind == "ocr" else "simulated_candidate"
                    self.assertEqual(r["frames"][0]["ready_from_status"], expected)
                    self.assertEqual(r["frames"][0]["processing_result"]["ocr_requests"], int(kind == "ocr"))
                    for event in r["upstream_audit"]:
                        self.assertEqual((event["source_kind"], event["processing_kind"]), (source, kind))
                    view = p.bridge.store.view()
                    self.assertTrue(view["simulation"])
                    self.assertFalse(view["execution_eligible"])
                    self.assertEqual(view["message"], "模拟计时 · 仅测试显示")

    def test_simulated_outcomes_keep_null_items_and_zero_ocr_calls(self):
        for outcome, expected in (("simulated_ready", "ready"), ("simulated_no_output", "no_result"),
                                  ("simulated_error", "processing_rejected")):
            with self.subTest(outcome=outcome):
                p = self.pipeline()
                p.receive()
                p.complete(p.core.start_job(), outcome=outcome)
                r = self.assert_accounting(p, 1)
                self.assertEqual(r["outcomes"], {expected: 1})
                self.assertEqual(r["processing"]["completed_jobs_observed"], 1)
                self.assertEqual(r["processing"]["ocr_totals_observed"], dict(ocr_requests=0, ocr_items_count=0, ocr_errors=0))
                self.assertFalse(any(e["status"] == "candidate" for e in r["upstream_audit"]))
                self.assertTrue(all(e["ocr_items_count"] is None for e in r["upstream_audit"]))

    def test_actual_empty_and_nonlist_and_failed_calls_keep_distinct_semantics(self):
        cases = (([], "ocr_empty", "no_result", 0),
                 ([None, text_item(text=" "), text_item(width=0)], "ocr_empty", "no_result", 0),
                 (None, "ocr_error", "processing_rejected", 1),
                 (RuntimeError("private-adapter-error"), "ocr_error", "processing_rejected", 1))
        for payload, outcome, final, errors in cases:
            with self.subTest(outcome=outcome, payload=type(payload).__name__):
                p = self.pipeline(kind="ocr")
                p.receive()
                self.finish_ocr(p, CountedBackend(payload))
                r = self.assert_accounting(p, 1)
                self.assertEqual(r["outcomes"], {final: 1})
                self.assertEqual(r["processing"]["outcome_counts"], {outcome: 1})
                self.assertEqual(r["processing"]["ocr_totals_observed"], dict(ocr_requests=1, ocr_items_count=0, ocr_errors=errors))
                self.assertEqual(r["frames_ready"], 0)
                self.assertNotIn("private-adapter-error", json.dumps(r))

    def test_valid_items_only_and_scope_withdrawals_do_not_multiply_call_count(self):
        p = self.pipeline(kind="ocr")
        p.receive()
        self.finish_ocr(p, CountedBackend([None, text_item(), text_item(text=" ")]))
        p.clock.now += 100 * MS
        p.receive(inside=1)
        p.core.disconnect()
        r = self.assert_accounting(p, 2)
        self.assertEqual(r["processing"]["completed_jobs_observed"], 1)
        self.assertEqual(r["processing"]["ocr_totals_observed"], dict(ocr_requests=1, ocr_items_count=1, ocr_errors=0))
        result_rows = [e for e in r["upstream_audit"] if e["processing_outcome"] == "ocr_text"]
        self.assertGreaterEqual(len(result_rows), 5)
        self.assertTrue(any(e["status"] == "withdrawn" for e in result_rows))
        self.assertNotIn("generated-private-text", json.dumps(r))

    def test_failure_before_first_call_remains_zero_calls_and_no_ready(self):
        p = self.pipeline(kind="ocr")
        p.receive()
        wrong_geometry = load_profile(dict(image_size=[320, 200], input_boxes=[[10, 10, 70, 40]]), "synthetic")
        self.finish_ocr(p, CountedBackend(), processor_profile=wrong_geometry)
        r = self.assert_accounting(p, 1)
        self.assertEqual(r["processing"]["ocr_totals_observed"], dict(ocr_requests=0, ocr_items_count=0, ocr_errors=0))
        self.assertEqual(r["processing"]["outcome_counts"], {"ocr_error": 1})
        self.assertEqual(r["frames_ready"], 0)

    def test_no_call_report_is_not_empty_ocr(self):
        p = self.pipeline(kind="ocr")
        p.receive()
        p.complete(p.core.start_job(), report=ProcessingResult("ocr", "ocr_not_run", 0, 0).fields())
        r = self.assert_accounting(p, 1)
        self.assertEqual(r["processing"]["outcome_counts"], {"ocr_not_run": 1})
        self.assertEqual(r["outcomes"], {"no_result": 1})

    def test_partial_failures_caught_in_raw_10f_and_10g_preserve_actual_counts(self):
        cases = (dict(input_boxes=[[10, 10, 70, 40], [80, 10, 120, 40]]),
                 dict(cues=[dict(cue_id="round_header", box=[80, 10, 120, 40])]),
                 dict(numeric_fields=[dict(field_name="gold", box=[80, 10, 120, 40])]))
        for fields in cases:
            with self.subTest(route=next(iter(fields))):
                p = self.pipeline(kind="ocr", **fields)
                p.receive()
                self.finish_ocr(p, CountedBackend([text_item()], RuntimeError("private-call-error")))
                r = self.assert_accounting(p, 1)
                self.assertEqual(r["processing"]["ocr_totals_observed"], dict(ocr_requests=2, ocr_items_count=1, ocr_errors=1))
                self.assertEqual(r["frames_ready"], 0)
                self.assertEqual(r["outcomes"], {"processing_rejected": 1})

    def test_empty_ocr_on_old_revoked_pixels_is_audited_without_reviving(self):
        p = self.pipeline(kind="ocr")
        p.receive()
        job = p.core.start_job()
        report = OCRProcessor(p.core.profile, CountedBackend([]), p.sink.run_id)(job)
        p.clock.now += 100 * MS
        p.receive(inside=1)
        p.complete(job, report=report)
        r = self.assert_accounting(p, 2)
        rows = [e for e in r["upstream_audit"] if e["stage"] == "pixel_inputs"]
        self.assertEqual((rows[0]["reason"], rows[0]["processing_outcome"]), ("pixels_or_continuity_revoked", "ocr_empty"))
        self.assertEqual(r["processing"]["ocr_totals_observed"]["ocr_requests"], 1)
        self.assertIsNone(p.bridge.store.view()["message"])

    def test_processing_semantic_contradictions_fail_closed_and_do_not_enter_audit(self):
        mutations = ({"status": "candidate", "reason": "pixel_inputs_only"},
                     {"processing_kind": "ocr"}, {"ocr_items_count": 1}, {"ocr_requests": 1},
                     {"ocr_requests": True}, {"ocr_errors": -1}, {"processing_outcome": "ocr_empty"},
                     {"stage": "field_gate"}, {"status": "no_output", "reason": "ocr_empty"},
                     {"processing_outcome": "in_progress"}, {"ocr_items_count": 0},
                     {"processing_outcome": ["untrusted"]})
        for changes in mutations:
            with self.subTest(changes=changes):
                p = self.pipeline()
                p.receive()
                p.complete(p.core.start_job())
                raw = deepcopy(next(e for e in p.bridge.upstream_audit if e["stage"] == "pixel_inputs"))
                raw.update(event_index=p.bridge.event_index + 1, **changes)
                before = len(p.bridge.upstream_audit)
                self.assertFalse(p.bridge.ingest(raw))
                self.assertEqual(len(p.bridge.upstream_audit), before)
                self.assertIsNone(p.bridge.store.view()["message"])
                self.assert_accounting(p, 1)

    def test_changed_job_result_on_second_scope_is_rejected_without_double_count(self):
        p = self.pipeline(kind="ocr")
        p.receive()
        self.finish_ocr(p, CountedBackend([text_item()]))
        raw = deepcopy(next(e for e in p.bridge.upstream_audit if e["stage"] == "pixel_inputs"))
        raw.update(event_index=p.bridge.event_index + 1, ocr_items_count=2)
        self.assertFalse(p.bridge.ingest(raw))
        r = self.assert_accounting(p, 1)
        self.assertEqual(r["fault"], "job_processing_result_changed")
        self.assertEqual(r["processing"]["ocr_totals_observed"]["ocr_items_count"], 1)

    def test_completion_at_original_ttl_is_audited_and_never_ready(self):
        p = self.pipeline()
        p.receive()
        job = p.core.start_job()
        p.clock.now += 2000 * MS
        p.complete(job)
        r = self.assert_accounting(p, 1)
        self.assertEqual(r["frames_ready"], 0)
        self.assertEqual(r["processing"]["completed_jobs_observed"], 1)
        self.assertTrue(any(e["status"] == "expired" for e in r["upstream_audit"]))
        self.assertEqual(r["consumer"]["opportunities"], 1)

    def test_pending_exact_deadline_timeout_and_next_frame_keep_continuous_output_sequence(self):
        p = self.pipeline()
        p.receive()
        first = p.bridge.mapped_events[0]
        p.clock.now = first["expires_at_monotonic_ns"]
        p.bridge.tick()
        self.assertEqual([e["status"] for e in p.bridge.mapped_events], ["pending", "timeout"])
        timeout = p.bridge.mapped_events[1]
        self.assertEqual(timeout["emitted_monotonic_ns"], timeout["expires_at_monotonic_ns"])
        p.clock.now += MS
        p.receive()
        p.complete(p.core.start_job())
        self.assertEqual([e["sequence"] for e in p.bridge.mapped_events], [1, 2, 3, 4])
        self.assertEqual(p.bridge.store.view()["status"], "ready")
        r = self.assert_accounting(p, 2)
        self.assertIsNone(r["fault"])
        self.assertEqual(r["consumer"]["outcomes"]["result_timeout"], 1)
        self.assertEqual(r["output_audit"][1]["origin"], "bridge_deadline_observed")

    def test_receive_expired_on_arrival_sends_timeout_without_pending_and_keeps_next_sequence(self):
        p = self.pipeline()
        p.sink.file = io.StringIO()
        p.receive()
        captured = list(map(json.loads, p.sink.file.getvalue().splitlines()))
        raw = captured[0]
        p.clock.now += 1100 * MS
        self.assertFalse(p.bridge.ingest(raw))
        self.assertEqual([e["status"] for e in p.bridge.mapped_events], ["timeout"])
        # Deliver the captured field-gate row too, preserving source sequence.
        self.assertTrue(p.bridge.ingest(captured[1]))
        p.sink.file = p
        p.clock.now += MS
        p.receive()
        p.complete(p.core.start_job())
        r = self.assert_accounting(p, 2)
        self.assertEqual(r["consumer"]["opportunities"], 2)
        self.assertEqual(r["accounting"]["accepted_frames_missing_consumer_sample"], 0)
        self.assertEqual([e["sequence"] for e in p.bridge.mapped_events], [1, 2, 3])
        self.assertEqual(p.bridge.store.view()["status"], "ready")

    def test_old_terminal_after_new_ready_only_updates_audit(self):
        p = self.pipeline()
        p.receive()
        p.complete(p.core.start_job())
        p.clock.now += 100 * MS
        p.receive(outside=1)
        p.complete(p.core.start_job())
        before = p.bridge.store.view()
        old = deepcopy(next(e for e in p.bridge.upstream_audit if e["stage"] == "pixel_inputs"))
        old.update(event_index=p.bridge.event_index + 1, status="withdrawn", reason="superseded",
                   emitted_monotonic_ns=p.clock())
        self.assertFalse(p.bridge.ingest(old))
        after = p.bridge.store.view()
        self.assertEqual((after["sample_id"], after["revision"]), (before["sample_id"], before["revision"]))
        r = self.assert_accounting(p, 2)
        self.assertEqual(r["processing"]["completed_jobs_observed"], 2)

    def test_no_browser_report_keeps_all_opportunities_and_missing_paint_count(self):
        p = self.pipeline()
        p.receive()
        p.complete(p.core.start_job())
        p.clock.now += 1000 * MS
        p.bridge.tick()
        r = self.assert_accounting(p, 1)
        self.assertEqual(r["display"]["timely_paint_report_samples"], 0)
        self.assertEqual(r["display"]["ready_without_timely_paint_report"], 1)
        self.assertEqual(r["consumer"]["outcomes"], {"display_timeout": 1})

    def test_upstream_public_source_clock_matrix_is_checked_by_binding(self):
        for source, clock in (("live", "virtual_monotonic_ns"),):
            p = self.pipeline()
            p.sink.source_kind, p.sink.clock_kind = source, clock
            with self.subTest(source=source), self.assertRaises(ValueError):
                bind_same_process(p.sink)

    def test_v2_spawn_handshake_binds_processing_kind_and_schema(self):
        start = time.monotonic_ns()
        hello = spawned_hello("challenge", "a" * 16)
        end = time.monotonic_ns()
        import os
        for change in ({"processing_kind": "ocr"}, {"event_schema": "realtime-probe-v1"},
                       {"kind": "bridge-local-clock-v1"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_spawned(dict(hello, **change), nonce="challenge", run_id="a" * 16,
                             child_pid=os.getpid(), sent_ns=start, received_ns=end)

    def test_faults_restarts_and_transport_barriers_keep_failure_denominators(self):
        for failure in ("duplicate", "gap", "producer_restart", "consumer_restart", "partial", "eof", "disconnect"):
            with self.subTest(failure=failure):
                p = self.pipeline()
                p.receive()
                p.complete(p.core.start_job())
                row = deepcopy(next(e for e in p.bridge.upstream_audit if e["stage"] == "pixel_inputs"))
                row["event_index"] = p.bridge.event_index + 1
                if failure == "duplicate":
                    row["event_index"] = p.bridge.event_index
                    p.bridge.ingest(row)
                elif failure == "gap":
                    row["event_index"] += 1
                    p.bridge.ingest(row)
                elif failure == "producer_restart":
                    row["clock_domain_id"] = "b" * 16
                    p.bridge.ingest(row)
                elif failure == "consumer_restart":
                    old = p.bridge.store
                    p.bridge.store = TimedOverlayStore(clock_domain_id=p.sink.run_id,
                                                       stream_id=p.bridge.stream_id, clock=p.clock)
                    self.addCleanup(p.bridge.store.close)
                    old.close()
                    p.bridge.tick()
                elif failure == "partial":
                    p.decoder.feed(b'{"schema_')
                elif failure == "eof":
                    p.decoder.eof()
                else:
                    p.core.disconnect()
                r = self.assert_accounting(p, 1)
                self.assertIsNone(p.bridge.store.view()["message"])
                self.assertNotEqual(r["outcomes"], {"ready": 1})
                self.assertIsNone(r["accounting"]["unobserved_frames_after_transport_close"])

    def test_missing_completion_preserves_unknown_unreported_ocr_calls(self):
        p = self.pipeline(kind="ocr")
        p.receive()
        p.core.start_job()
        p.core.disconnect()
        p.decoder.eof()
        r = self.assert_accounting(p, 1)
        self.assertEqual(r["processing"]["started_without_completion_observed"], 1)
        self.assertEqual(r["processing"]["completed_jobs_observed"], 0)
        self.assertIsNone(r["processing"]["ocr_requests_in_uncompleted_jobs"])

    def test_receive_rejected_after_fault_is_counted_as_a_failed_attempt(self):
        p = self.pipeline()
        p.receive()
        p.complete(p.core.start_job())
        received = deepcopy(next(e for e in p.bridge.upstream_audit if e["stage"] == "receive"))
        self.assertFalse(p.bridge.ingest(received))
        r = self.assert_accounting(p, 2)
        self.assertEqual(r["accepted_frames"], 1)
        self.assertEqual(r["outcomes"]["rejected_receive"], 1)

    def test_hard_limit_unchecked_frame_withdraws_without_becoming_a_checked_opportunity(self):
        p = self.pipeline(kind="ocr")
        p.core = ProbeCore(p.stream_id, p.core.profile, p.sink, clock_ns=p.clock, max_frames=1)
        p.receive()
        job = p.core.start_job()
        report = OCRProcessor(p.core.profile, CountedBackend([text_item()]), p.sink.run_id)(job)
        p.clock.now += 100 * MS
        p.receive(inside=1)
        p.complete(job, report=report)
        r = self.assert_accounting(p, 1)
        self.assertEqual(p.core.counts["frames_declined_unchecked"], 1)
        self.assertEqual(p.core.counts["frames_checked"], 1)
        self.assertEqual(r["frames_ready"], 0)
        self.assertEqual(r["processing"]["ocr_totals_observed"]["ocr_requests"], 1)
        self.assertIsNone(r["accounting"]["unchecked_source_frames"])
        self.assertIsNone(p.bridge.store.view()["message"])

    def test_full_scope_completion_is_audited_but_does_not_create_ready(self):
        p = self.pipeline()
        output = io.StringIO()
        p.sink.file = output
        p.receive()
        p.complete(p.core.start_job())
        rows = list(map(json.loads, output.getvalue().splitlines()))
        for row in rows:
            if row["stage"] == "pixel_inputs":
                break
            p.bridge.ingest(row)
        r = self.assert_accounting(p, 1)
        self.assertEqual(r["processing"]["completed_jobs_observed"], 1)
        self.assertEqual(r["frames_ready"], 0)
        self.assertEqual(p.bridge.store.view()["status"], "pending")


if __name__ == "__main__":
    unittest.main()
