from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from jcc_timed_overlay.protocol import CONTROL_STATUSES, SAMPLE_STATUSES, validate_event
from jcc_timed_overlay.source import JsonlTail
from jcc_timed_overlay.store import NS_MS
from jcc_timed_overlay.tests.test_lifecycle import Fixture


class RecordingJournal:
    def __init__(self):
        self.rows = []

    def write(self, row):
        self.rows.append(deepcopy(row))

    def close(self):
        pass


class TimeoutEventTests(Fixture):
    def setUp(self):
        super().setUp()
        self.journal = RecordingJournal()
        self.store.journal = self.journal

    def terminal(self, original, status="timeout", **updates):
        raw = self.event(original["sample_id"], status,
                         frame_received_monotonic_ns=original["frame_received_monotonic_ns"],
                         expires_at_monotonic_ns=original["expires_at_monotonic_ns"])
        raw.update(updates)
        return raw

    def outcome_rows(self, sample_id):
        return [row for row in self.journal.rows
                if row["kind"] == "sample_outcome" and row["sample_id"] == sample_id]

    def next_ready(self):
        self.clock.advance(1)
        self.assertTrue(self.store.ingest(self.event("next")))
        self.assertEqual(self.store.view()["sample_id"], "next")
        self.assertEqual(self.store.view()["status"], "ready")

    def test_timeout_may_be_emitted_before_at_or_after_deadline(self):
        raw = self.event(status="timeout", expires_at_monotonic_ns=self.clock.value + 500 * NS_MS)
        for delta in (-1, 0, 1, 200 * NS_MS, 30_000 * NS_MS):
            with self.subTest(delta_ns=delta):
                self.assertIsNone(validate_event(dict(raw,
                    emitted_monotonic_ns=raw["expires_at_monotonic_ns"] + delta)))

    def test_timeout_still_requires_positive_bounded_lifetime(self):
        frame = self.clock.value
        raw = self.event(status="timeout", emitted_monotonic_ns=frame + 35_000 * NS_MS)
        self.assertIsNone(validate_event(dict(raw, expires_at_monotonic_ns=frame + 30_000 * NS_MS)))
        for change in ({"expires_at_monotonic_ns": frame},
                       {"expires_at_monotonic_ns": frame - 1},
                       {"expires_at_monotonic_ns": frame + 30_000 * NS_MS + 1},
                       {"emitted_monotonic_ns": frame - 1},
                       {"emitted_monotonic_ns": True}):
            with self.subTest(change=change):
                self.assertIsNotNone(validate_event(dict(raw, **change)))

    def test_other_statuses_reject_emission_at_or_after_deadline(self):
        raw = self.event(expires_at_monotonic_ns=self.clock.value + 500 * NS_MS)
        for status in sorted((SAMPLE_STATUSES | CONTROL_STATUSES) - {"timeout"}):
            for delta in (0, 1):
                with self.subTest(status=status, delta_ns=delta):
                    self.assertEqual(validate_event(dict(raw, status=status,
                        emitted_monotonic_ns=raw["expires_at_monotonic_ns"] + delta)),
                        "time_order_or_window")

    def test_expired_is_view_only(self):
        self.assertEqual(validate_event(self.event(status="expired")), "status")

    def test_pending_timeout_at_boundaries_is_accepted_once_and_continues(self):
        for tick_first in (False, True):
            for delta in (-1, 0, 1, 200 * NS_MS):
                with self.subTest(tick_first=tick_first, delta_ns=delta):
                    self.setUp()
                    pending = self.event(status="pending",
                                         expires_at_monotonic_ns=self.clock.value + 500 * NS_MS)
                    self.assertTrue(self.store.ingest(pending))
                    self.clock.value = pending["expires_at_monotonic_ns"] + delta
                    if tick_first:
                        self.store.tick()
                    self.assertTrue(self.store.ingest(self.terminal(pending)))
                    self.assertTrue(self.store.ingest(self.terminal(pending)))
                    report = self.store.report()
                    self.assertEqual(report["opportunities"], 1)
                    self.assertEqual(report["outcomes"], {"result_timeout": 1})
                    self.assertEqual(len(self.outcome_rows("s1")), 1)
                    self.assertIsNone(self.store.view()["message"])
                    self.next_ready()
                    self.assertEqual(self.store.report()["opportunities"], 2)
                    self.assertEqual(self.store.report()["counters"]["accepted_events"], 4)

    def test_timeout_without_pending_counts_once_and_continues(self):
        for delta in (0, 1, 200 * NS_MS):
            with self.subTest(delta_ns=delta):
                self.setUp()
                timeout = self.event(status="timeout", sequence=17,
                                     expires_at_monotonic_ns=self.clock.value + 500 * NS_MS)
                self.sequence = 17
                self.clock.value = timeout["expires_at_monotonic_ns"] + delta
                timeout["emitted_monotonic_ns"] = self.clock.value
                self.assertTrue(self.store.ingest(timeout))
                original_record = self.store.report()["samples"][0]
                self.assertTrue(self.store.ingest(self.terminal(timeout)))
                report = self.store.report()
                self.assertEqual(report["samples"][0], original_record)
                self.assertEqual(report["opportunities"], 1)
                self.assertEqual(report["outcomes"], {"result_timeout": 1})
                self.assertEqual(len(self.outcome_rows("s1")), 1)
                self.next_ready()
                self.assertEqual(self.store.report()["opportunities"], 2)
                self.assertEqual(self.store.report()["counters"]["accepted_events"], 3)

    def test_auto_expiry_ack_keeps_final_record_and_revision(self):
        pending = self.event(status="pending", expires_at_monotonic_ns=self.clock.value + 100 * NS_MS)
        self.assertTrue(self.store.ingest(pending))
        self.clock.advance(100)
        self.store.tick()
        before = self.store.view()
        original_record = self.store.report()["samples"][0]
        self.assertEqual(before["status"], "expired")
        self.assertTrue(self.store.ingest(self.terminal(pending)))
        self.assertEqual(self.store.view(), before)
        self.assertEqual(self.store.report()["samples"][0], original_record)
        self.assertEqual(len(self.outcome_rows("s1")), 1)

    def test_old_terminal_after_newer_pending_or_ready_keeps_new_view(self):
        for newer_status in ("pending", "ready"):
            for terminal_status in ("timeout", "no_result", "unknown"):
                with self.subTest(newer_status=newer_status, terminal_status=terminal_status):
                    self.setUp()
                    old = self.event("old", "pending", expires_at_monotonic_ns=self.clock.value + 100 * NS_MS)
                    self.assertTrue(self.store.ingest(old))
                    self.clock.advance(1)
                    self.assertTrue(self.store.ingest(self.event("new", newer_status)))
                    self.clock.advance(1)
                    if terminal_status == "timeout":
                        self.clock.value = old["expires_at_monotonic_ns"] + 1
                    before = self.store.view()
                    original_samples = self.store.report()["samples"]
                    self.assertTrue(self.store.ingest(self.terminal(old, terminal_status)))
                    self.assertEqual(self.store.view(), before)
                    self.assertEqual(self.store.report()["samples"], original_samples)
                    self.assertEqual(self.store.report()["opportunities"], 2)
                    self.assertEqual(len(self.outcome_rows("old")), 1)

    def test_terminal_ack_preserves_existing_outcomes(self):
        cases = ("ready", "painted", "display_timeout", "expired", "silence",
                 "invalidated", "disconnected", "no_result", "unknown", "timeout")
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                status = case if case in {"no_result", "unknown", "timeout"} else (
                    "ready" if case in {"ready", "painted", "display_timeout"} else "pending")
                ttl_ms = 4000 if case in {"display_timeout", "silence"} else 500
                old = self.event("old", status, expires_at_monotonic_ns=self.clock.value + ttl_ms * NS_MS)
                self.assertTrue(self.store.ingest(old))
                if case == "painted":
                    self.assertTrue(self.store.report_browser(self.browser()))
                elif case in {"display_timeout", "expired", "silence"}:
                    self.clock.advance({"display_timeout": 1200, "expired": 500, "silence": 1500}[case])
                    self.store.tick()
                elif case in {"invalidated", "disconnected"}:
                    self.clock.advance(1)
                    self.assertTrue(self.store.ingest(self.event("control", case)))
                self.clock.advance(1)
                self.assertTrue(self.store.ingest(self.event("new")))
                before = self.store.view()
                original_record = self.store.report()["samples"][0]
                self.assertTrue(self.store.ingest(self.terminal(old)))
                self.assertEqual(self.store.view(), before)
                self.assertEqual(self.store.report()["samples"][0], original_record)
                self.assertEqual(len(self.outcome_rows("old")), 1)

    def test_unseen_old_terminal_is_rejected_without_withdrawing_newer_view(self):
        for status in ("timeout", "no_result", "unknown"):
            for same_frame in (False, True):
                with self.subTest(status=status, same_frame=same_frame):
                    self.setUp()
                    old_frame = self.clock.value
                    self.clock.advance(1)
                    new = self.event("new")
                    self.assertTrue(self.store.ingest(new))
                    if same_frame:
                        old_frame = new["frame_received_monotonic_ns"]
                    self.clock.advance(1)
                    before = self.store.view()
                    self.assertFalse(self.store.ingest(self.event("unseen-old", status,
                        frame_received_monotonic_ns=old_frame, expires_at_monotonic_ns=old_frame + 500 * NS_MS)))
                    self.assertEqual(self.store.view(), before)
                    self.assertEqual(self.store.report()["opportunities"], 1)
                    self.assertTrue(self.store.ingest(self.event("beat", "heartbeat")))
                    self.assertEqual(self.store.view(), before)

    def test_new_timeout_with_new_frame_withdraws_older_ready(self):
        self.assertTrue(self.store.ingest(self.event("old")))
        self.clock.advance(1)
        timeout = self.event("new-timeout", "timeout",
                             expires_at_monotonic_ns=self.clock.value + 100 * NS_MS)
        self.clock.advance(100)
        timeout["emitted_monotonic_ns"] = self.clock.value
        self.assertTrue(self.store.ingest(timeout))
        self.assertIsNone(self.store.view()["message"])
        self.assertEqual(self.store.report()["opportunities"], 2)
        self.assertEqual(self.store.report()["outcomes"],
                         {"superseded_before_paint": 1, "result_timeout": 1})
        self.next_ready()

    def test_terminal_history_keeps_time_binding_and_nondecreasing_emission(self):
        for change_kind in ("frame", "expires", "emission"):
            with self.subTest(change_kind=change_kind):
                self.setUp()
                old = self.event(status="no_result")
                self.assertTrue(self.store.ingest(old))
                self.clock.advance(100)
                self.assertTrue(self.store.ingest(self.terminal(old)))
                latest_emission = self.clock.value
                self.clock.advance(1)
                change = {"frame": {"frame_received_monotonic_ns": old["frame_received_monotonic_ns"] + 1},
                          "expires": {"expires_at_monotonic_ns": old["expires_at_monotonic_ns"] + 1},
                          "emission": {"emitted_monotonic_ns": latest_emission - 1}}[change_kind]
                self.assertFalse(self.store.ingest(self.terminal(old, **change)))
                self.assertEqual(self.journal.rows[-1]["reason"], "sample_time_changed")
                self.assertEqual(self.store.report()["outcomes"], {"no_result": 1})

    def test_timeout_does_not_bypass_source_binding_or_time_freshness(self):
        for case in ("future", "pre_session", "stale", "clock", "stream", "source"):
            with self.subTest(case=case):
                self.setUp()
                raw = self.event(status="timeout", expires_at_monotonic_ns=self.clock.value + 500 * NS_MS)
                self.clock.advance(500)
                raw["emitted_monotonic_ns"] = self.clock.value
                if case == "future":
                    raw["emitted_monotonic_ns"] += 1
                elif case == "pre_session":
                    raw["frame_received_monotonic_ns"] -= 1
                elif case == "stale":
                    self.clock.advance(1500)
                else:
                    key, value = {"clock": ("clock_domain_id", "other-clock"),
                                  "stream": ("stream_id", "other-stream"),
                                  "source": ("source_kind", "replay")}[case]
                    raw[key] = value
                self.assertFalse(self.store.ingest(raw))
                self.assertEqual(self.store.report()["opportunities"], 0)
                self.assertIsNone(self.store.view()["message"])

    def test_timeout_duplicates_and_sequence_gaps_keep_existing_policy(self):
        for gap in (False, True):
            with self.subTest(gap=gap):
                self.setUp()
                old = self.event(status="pending", expires_at_monotonic_ns=self.clock.value + 100 * NS_MS)
                self.assertTrue(self.store.ingest(old))
                self.clock.advance(100)
                timeout = self.terminal(old, sequence=3 if gap else 2)
                self.assertEqual(self.store.ingest(timeout), not gap)
                if not gap:
                    self.assertFalse(self.store.ingest(timeout))
                self.clock.advance(1)
                self.assertEqual(self.store.ingest(self.event("next", sequence=4 if gap else 3)), not gap)
                self.assertEqual(self.store.report()["opportunities"], 1 if gap else 2)

    def test_timeout_after_heartbeat_uses_next_sequence(self):
        old = self.event(status="pending", expires_at_monotonic_ns=self.clock.value + 500 * NS_MS)
        self.assertTrue(self.store.ingest(old))
        self.clock.advance(400)
        self.assertTrue(self.store.ingest(self.event("beat", "heartbeat")))
        self.clock.advance(100)
        self.assertTrue(self.store.ingest(self.terminal(old)))
        self.next_ready()
        self.assertEqual(self.store.report()["outcomes"], {"result_timeout": 1, "awaiting_paint_report": 1})
        self.assertEqual(self.store.report()["counters"]["accepted_events"], 4)

    def test_jsonl_timeout_preserves_denominator_and_next_record(self):
        for with_pending in (False, True):
            with self.subTest(with_pending=with_pending), tempfile.TemporaryDirectory() as directory:
                self.setUp()
                path = Path(directory) / "input.jsonl"
                path.touch()
                tail = JsonlTail(path, self.store)
                try:
                    old = self.event(status="pending", expires_at_monotonic_ns=self.clock.value + 100 * NS_MS)
                    if with_pending:
                        with path.open("a") as file:
                            file.write(json.dumps(old) + "\n")
                        tail.poll()
                    else:
                        self.sequence = 0
                    self.clock.advance(100)
                    timeout = self.terminal(old)
                    self.clock.advance(1)
                    next_event = self.event("next")
                    with path.open("a") as file:
                        file.write(json.dumps(timeout) + "\n" + json.dumps(next_event) + "\n")
                    tail.poll()
                    report = self.store.report()
                    self.assertFalse(tail.failed)
                    self.assertEqual(report["opportunities"], 2)
                    self.assertEqual(report["outcomes"], {"result_timeout": 1, "awaiting_paint_report": 1})
                    self.assertEqual(report["counters"].get("rejected_inputs", 0), 0)
                    self.assertEqual(self.store.view()["sample_id"], "next")
                finally:
                    tail.close()


if __name__ == "__main__":
    unittest.main()
