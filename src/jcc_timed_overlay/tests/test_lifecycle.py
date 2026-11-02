from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from jcc_timed_overlay.journal import Journal
from jcc_timed_overlay.protocol import EVENT_SCHEMA
from jcc_timed_overlay.store import NS_MS, TimedOverlayStore


class Clock:
    def __init__(self) -> None:
        self.value = 10_000 * NS_MS

    def __call__(self) -> int:
        return self.value

    def advance(self, ms: float) -> None:
        self.value += int(ms * NS_MS)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.store = TimedOverlayStore(clock_domain_id="test-clock", stream_id="test-stream", clock=self.clock)
        self.sequence = 0

    def event(self, sample="s1", status="ready", **updates) -> dict:
        self.sequence += 1
        raw = {
            "schema_version": EVENT_SCHEMA, "sample_id": sample, "source_kind": self.store.source_kind,
            "stream_id": "test-stream", "clock_domain_id": "test-clock", "sequence": self.sequence,
            "frame_received_monotonic_ns": self.clock.value, "emitted_monotonic_ns": self.clock.value,
            "expires_at_monotonic_ns": self.clock.value + 4000 * NS_MS, "status": status,
        }
        raw.update(updates)
        return raw

    def browser(self, stage="paint_opportunity", view=None, **updates) -> dict:
        view = view or self.store.view()
        raw = {
            "session_id": self.store.session_id, "client_id": "browser-test", "client_event_sequence": 1,
            "revision": view["revision"], "render_token": view["render_token"], "sample_id": view["sample_id"],
            "stage": stage, "reason": "none", "visibility": "visible", "browser_monotonic_ms": 50,
            "since_fetch_start_ms": 20, "dom_to_raf_ms": 15 if stage == "paint_opportunity" else None,
        }
        raw.update(updates)
        return raw


class LifecycleTests(Fixture):
    def test_pending_ready_and_precise_expiry_do_not_renew(self):
        pending = self.event(status="pending")
        self.assertTrue(self.store.ingest(pending))
        self.assertIsNone(self.store.view()["message"])
        self.clock.advance(80)
        ready = self.event(frame_received_monotonic_ns=pending["frame_received_monotonic_ns"],
                           expires_at_monotonic_ns=pending["expires_at_monotonic_ns"])
        self.assertTrue(self.store.ingest(ready))
        self.assertEqual(self.store.view()["remaining_ms"], 3920)
        self.assertTrue(self.store.report_browser(self.browser()))
        self.assertEqual(self.store.report()["samples"][0]["frame_to_report_received_ms"], 80)
        for _ in range(3):
            self.clock.advance(1000)
            self.store.ingest(self.event("beat", "heartbeat"))
            self.assertLessEqual(self.store.view()["remaining_ms"], 3920)
        self.clock.advance(920)
        view = self.store.view()
        self.assertEqual(view["status"], "expired")
        self.assertIsNone(view["message"])
        self.assertIsNone(view["sample_id"])
        self.assertEqual(self.store.report()["outcomes"], {"paint_opportunity_reported": 1})

    def test_no_browser_is_a_display_timeout_not_a_missing_denominator(self):
        self.store.ingest(self.event())
        self.clock.advance(1200)
        self.store.tick()
        self.assertEqual(self.store.view()["status"], "timeout")
        report = self.store.report()
        self.assertEqual(report["opportunities"], 1)
        self.assertEqual(report["outcomes"], {"display_timeout": 1})
        self.assertIsNone(report["frame_to_report_received_ms"]["p95"])

    def test_pending_expiry_is_result_timeout(self):
        self.store.ingest(self.event(status="pending", expires_at_monotonic_ns=self.clock.value + 500 * NS_MS))
        self.clock.advance(500)
        self.store.tick()
        self.assertEqual(self.store.report()["outcomes"], {"result_timeout": 1})

    def test_explicit_no_result_timeout_unknown_all_count(self):
        for index, status in enumerate(("no_result", "timeout", "unknown")):
            self.clock.advance(1)
            self.assertTrue(self.store.ingest(self.event(f"s{index}", status)))
            self.assertIsNone(self.store.view()["message"])
        report = self.store.report()
        self.assertEqual(report["opportunities"], 3)
        self.assertEqual(report["outcomes"], {"no_result": 1, "result_timeout": 1, "unknown": 1})

    def test_new_pending_withdraws_ready_and_old_result_cannot_resurrect(self):
        original = self.event()
        self.store.ingest(original)
        self.clock.advance(1)
        self.store.ingest(self.event("s2", "pending"))
        self.assertEqual(self.store.view()["sample_id"], "s2")
        self.assertIsNone(self.store.view()["message"])
        late = dict(original, sequence=self.sequence + 1)
        self.assertFalse(self.store.ingest(late))
        self.assertIsNone(self.store.view()["sample_id"])
        self.assertEqual(self.store.report()["opportunities"], 2)

    def test_duplicate_sequence_is_barrier_and_same_sample_does_not_return(self):
        raw = self.event()
        self.store.ingest(raw)
        self.assertFalse(self.store.ingest(raw))
        self.assertFalse(self.store.ingest(dict(raw, sequence=2)))
        self.assertIsNone(self.store.view()["message"])

    def test_context_control_retires_unseen_frames_and_new_frame_can_continue(self):
        for status in ("invalidated", "disconnected"):
            with self.subTest(status=status):
                self.store = TimedOverlayStore(clock_domain_id="test-clock", stream_id="test-stream", clock=self.clock)
                self.assertTrue(self.store.ingest(self.event("a")))
                self.clock.advance(1)
                frame_b = self.clock.value
                self.assertTrue(self.store.ingest(self.event("control", status)))
                self.assertIsNone(self.store.view()["message"])
                self.clock.advance(1)
                for sample, frame in (("b", frame_b), ("renamed-b", frame_b), ("older", frame_b - 1)):
                    self.assertFalse(self.store.ingest(self.event(sample, frame_received_monotonic_ns=frame)))
                    self.assertIsNone(self.store.view()["message"])
                self.assertTrue(self.store.ingest(self.event("c")))
                self.assertEqual(self.store.view()["sample_id"], "c")
                self.assertEqual(self.store.report()["opportunities"], 2)

    def test_sequence_gap_closes_session_even_for_heartbeat_or_ready(self):
        for status in ("heartbeat", "ready"):
            with self.subTest(status=status):
                self.sequence = 0
                self.store = TimedOverlayStore(clock_domain_id="test-clock", stream_id="test-stream", clock=self.clock)
                self.assertTrue(self.store.ingest(self.event("a")))
                self.clock.advance(1)
                self.assertFalse(self.store.ingest(self.event("gap", status, sequence=3)))
                self.assertIsNone(self.store.view()["message"])
                # Neither later heartbeats/new frames nor a late missing event prove recovery.
                for sequence, recovery in ((4, "heartbeat"), (5, "ready"), (2, "invalidated"), (6, "pending")):
                    self.clock.advance(1)
                    self.assertFalse(self.store.ingest(self.event("recovery", recovery, sequence=sequence)))
                    self.assertIsNone(self.store.view()["message"])
                self.assertEqual(self.store.report()["opportunities"], 1)
                self.assertEqual(self.store.report()["counters"]["accepted_events"], 1)
                # Recovery requires a new store session, not a newer event number.
                self.store = TimedOverlayStore(clock_domain_id="test-clock", stream_id="test-stream", clock=self.clock)
                self.sequence = 0
                self.assertTrue(self.store.ingest(self.event("new-session")))

    def test_attached_stream_can_start_at_positive_sequence_then_continue(self):
        pending = self.event(status="pending", sequence=17)
        self.assertTrue(self.store.ingest(pending))
        self.clock.advance(1)
        self.assertTrue(self.store.ingest(self.event(sequence=18,
            frame_received_monotonic_ns=pending["frame_received_monotonic_ns"],
            expires_at_monotonic_ns=pending["expires_at_monotonic_ns"])))
        self.assertEqual(self.store.view()["status"], "ready")

    def test_pending_cannot_extend_original_deadline(self):
        pending = self.event(status="pending")
        self.store.ingest(pending)
        self.clock.advance(10)
        self.assertFalse(self.store.ingest(self.event(frame_received_monotonic_ns=pending["frame_received_monotonic_ns"])))
        self.assertIsNone(self.store.view()["message"])

    def test_invalid_time_types_bindings_and_extra_data_are_rejected(self):
        bad_values = (
            {"emitted_monotonic_ns": self.clock.value + 1},
            {"frame_received_monotonic_ns": self.clock.value - 1},
            {"emitted_monotonic_ns": True}, {"emitted_monotonic_ns": float("nan")},
            {"expires_at_monotonic_ns": self.clock.value}, {"clock_domain_id": "other-clock"},
            {"source_kind": "replay"}, {"stream_id": "other-stream"},
            {"sample_id": "private/path"}, {"screenshot": "must-not-log-this"},
            {"sequence": False}, {"status": ["ready"]},
        )
        for change in bad_values:
            with self.subTest(change=change):
                self.assertFalse(self.store.ingest(self.event(**change)))
                self.assertIsNone(self.store.view()["message"])
        self.assertEqual(self.store.report()["opportunities"], 0)

    def test_old_emission_does_not_refresh_source_lease(self):
        self.store.ingest(self.event(status="pending"))
        old = self.clock.value
        self.clock.advance(1500)
        self.assertFalse(self.store.ingest(self.event("beat", "heartbeat", emitted_monotonic_ns=old,
                                                     frame_received_monotonic_ns=old)))
        self.assertIsNone(self.store.view()["message"])

    def test_source_silence_and_later_heartbeat_cannot_restore_old_sample(self):
        self.store.ingest(self.event())
        shown = self.store.view()
        self.store.report_browser(self.browser(view=shown))
        self.clock.advance(1500)
        self.assertEqual(self.store.view()["status"], "disconnected")
        self.assertTrue(self.store.ingest(self.event("beat", "heartbeat")))
        self.assertIsNone(self.store.view()["message"])
        self.assertTrue(self.store.report_browser(self.browser(view=shown, client_event_sequence=2)))
        self.assertIsNone(self.store.view()["message"])
        self.assertEqual(self.store.report()["opportunities"], 1)

    def test_replay_and_device_events_keep_fixed_simulation_and_unverified_clock(self):
        for kind in ("synthetic", "replay", "device_readonly"):
            self.store = TimedOverlayStore(clock_domain_id="test-clock", stream_id="test-stream", source_kind=kind, clock=self.clock)
            self.assertTrue(self.store.ingest(self.event()))
            view = self.store.view()
            self.assertTrue(view["simulation"])
            self.assertFalse(view["execution_eligible"])
            self.assertEqual(view["message"], "模拟计时 · 仅测试显示")
            self.assertEqual(view["clock_basis"], "producer_declared_same_host_unverified")

    def test_late_paint_and_hidden_report_do_not_turn_timeout_into_success(self):
        self.store.ingest(self.event())
        shown = self.store.view()
        self.assertTrue(self.store.report_browser(self.browser(view=shown, visibility="hidden")))
        self.clock.advance(1200)
        self.assertTrue(self.store.report_browser(self.browser(view=shown, client_event_sequence=2)))
        self.assertEqual(self.store.report()["outcomes"], {"display_timeout": 1})
        self.assertIsNone(self.store.report()["samples"][0]["frame_to_report_received_ms"])

    def test_report_binding_duplicate_and_two_windows(self):
        self.store.ingest(self.event())
        report = self.browser()
        self.assertFalse(self.store.report_browser(dict(report, session_id="wrong")))
        self.assertFalse(self.store.report_browser(dict(report, revision=report["revision"] + 1)))
        self.assertFalse(self.store.report_browser(dict(report, dom_to_raf_ms=None)))
        self.assertFalse(self.store.report_browser(dict(report, browser_monotonic_ms=10**1000)))
        self.assertTrue(self.store.report_browser(report))
        self.assertFalse(self.store.report_browser(report))
        self.clock.advance(100)
        self.assertTrue(self.store.report_browser(dict(report, client_id="browser-two")))
        summary = self.store.report()
        self.assertEqual(summary["browser_clients"], 2)
        self.assertEqual(summary["sample_count_with_report"], 1)
        self.assertEqual(summary["samples"][0]["frame_to_report_received_ms"], 0)

    def test_clock_rollback_permanently_closes_display(self):
        self.store.ingest(self.event())
        self.clock.value -= 1
        self.assertIsNone(self.store.view()["message"])
        self.clock.advance(1000)
        self.assertFalse(self.store.ingest(self.event("s2")))
        self.assertIsNone(self.store.view()["message"])

    def test_expired_ready_counts_timeout_and_never_shows(self):
        now = self.clock.value
        self.clock.advance(200)
        self.assertTrue(self.store.ingest(self.event(frame_received_monotonic_ns=now, emitted_monotonic_ns=now,
                                                     expires_at_monotonic_ns=now + 100 * NS_MS)))
        self.assertEqual(self.store.report()["outcomes"], {"result_timeout": 1})
        self.assertIsNone(self.store.view()["message"])

    def test_session_sample_capacity_fails_closed(self):
        self.store.max_samples = 1
        self.store.ingest(self.event())
        self.clock.advance(1)
        self.assertFalse(self.store.ingest(self.event("s2")))
        self.assertIsNone(self.store.view()["message"])
        self.assertEqual(self.store.report()["opportunities"], 1)

    def test_journal_records_all_outcomes_and_does_not_echo_rejected_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            self.store.journal = Journal(path)
            self.store.ingest(self.event())
            self.store.report_browser(self.browser("dom_updated"))
            self.store.report_browser(self.browser(client_event_sequence=2))
            self.clock.advance(1)
            self.store.ingest(self.event("s2", "no_result"))
            self.clock.advance(1)
            self.store.ingest(self.event("s3", "pending", expires_at_monotonic_ns=self.clock.value + 100 * NS_MS))
            self.clock.advance(100)
            self.store.tick()
            self.store.ingest(self.event("s4", screenshot="must-not-log-this"))
            self.store.close()
            text = path.read_text()
            rows = [json.loads(line) for line in text.splitlines()]
            self.assertNotIn("must-not-log-this", text)
            self.assertTrue(all(r["schema_version"] == "timed-overlay-log-v1" for r in rows))
            self.assertEqual(rows[-1]["report"]["opportunities"], 3)
            self.assertEqual(rows[-1]["report"]["outcomes"], {"paint_opportunity_reported": 1, "no_result": 1, "result_timeout": 1})
            self.assertIsInstance(rows[-1]["report"]["samples"][0]["frame_received_monotonic_ns"], str)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                Journal(path)

    def test_log_write_failure_withdraws(self):
        class BrokenJournal:
            def write(self, row):
                raise OSError("disk unavailable")
        self.store.ingest(self.event())
        self.store.journal = BrokenJournal()
        self.store.report_browser(self.browser())
        self.assertIsNone(self.store.view()["message"])
        self.assertFalse(self.store.report()["journal_available"])


if __name__ == "__main__":
    unittest.main()
