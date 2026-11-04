"""Synthetic contract tests.  No tablet, replay, or private image is read."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

from jcc_human_coach import CoachAdvice, CoachEvent, CoachObservation, HumanCoachStore, build_demo
from jcc_human_coach.core import SCHEMA, replay_example_events
from jcc_human_coach.server import run_server
from jcc_human_coach.source import JsonlTail, decode_event


class Clock:
    def __init__(self, now=1_000):
        self.now = now

    def __call__(self):
        return self.now


def event(stream, sequence, frame, emitted, expires, *, status="candidate_visible", advice=None):
    if advice is None and status == "candidate_visible":
        advice = CoachAdvice(f"advice-{sequence}", "lineup", "准备阶段", "先检查上场人数", "合成字段完整。")
    return CoachEvent(SCHEMA, "synthetic", stream, f"frame-{sequence}", sequence, frame,
                      emitted, expires, status, "candidate" if status == "candidate_visible" else status,
                      advice)


class HumanCoachStoreTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = HumanCoachStore(stream_id="test-stream", clock=self.clock)

    def test_visible_refresh_then_unknown_hides_old_card(self):
        self.assertTrue(self.store.ingest(event("test-stream", 1, 900, 950, 1_800)))
        first = self.store.view()
        self.assertTrue(first["visible"])
        self.assertEqual(first["advice"]["action_zh"], "先检查上场人数")
        self.clock.now = 1_100
        self.assertTrue(self.store.ingest(event("test-stream", 2, 1_050, 1_080, 1_900,
                                                status="unknown", advice=None)))
        withdrawn = self.store.view()
        self.assertFalse(withdrawn["visible"])
        self.assertIsNone(withdrawn["advice"])
        self.assertEqual(withdrawn["status"], "unknown")

    def test_expiry_hides_without_a_new_event(self):
        self.assertTrue(self.store.ingest(event("test-stream", 1, 900, 950, 1_200)))
        self.clock.now = 1_200
        view = self.store.view()
        self.assertFalse(view["visible"])
        self.assertEqual(view["status"], "expired")
        self.assertIsNone(view["advice"])

    def test_clock_regression_withdraws_with_a_new_revision(self):
        self.assertTrue(self.store.ingest(event("test-stream", 1, 900, 950, 1_800)))
        revision = self.store.view()["revision"]
        self.clock.now = 999
        view = self.store.view()
        self.assertFalse(view["visible"])
        self.assertEqual(view["reason"], "local_clock_unreliable")
        self.assertEqual(view["revision"], revision + 1)

    def test_stale_or_reordered_event_fails_closed(self):
        self.assertTrue(self.store.ingest(event("test-stream", 1, 900, 950, 1_800)))
        self.clock.now = 1_100
        self.assertFalse(self.store.ingest(event("test-stream", 1, 1_000, 1_050, 1_900)))
        self.assertFalse(self.store.view()["visible"])
        self.assertEqual(self.store.view()["reason"], "nonadvancing_source")

    def test_reused_frame_id_with_new_sequence_withdraws(self):
        first = event("test-stream", 1, 900, 950, 1_800)
        self.assertTrue(self.store.ingest(first))
        self.clock.now = 1_100
        replayed = CoachEvent(SCHEMA, "synthetic", "test-stream", first.frame_id, 2, 1_000,
                              1_050, 1_900, "candidate_visible", "candidate",
                              CoachAdvice("other-advice", "lineup", "新标题", "新动作", "新理由"))
        self.assertFalse(self.store.ingest(replayed))
        self.assertFalse(self.store.view()["visible"])
        self.assertEqual(self.store.view()["reason"], "nonadvancing_source")

    def test_live_machine_candidate_cannot_become_advice_without_reviewed_context(self):
        live = CoachEvent(SCHEMA, "live_candidate", "test-stream", "frame-1", 1, 900, 950,
                          1_800, "candidate_visible", "candidate",
                          CoachAdvice("live-advice", "economy", "经济候选", "保留预算", "字段由调用方声明。"))
        self.assertFalse(self.store.ingest(live))
        view = self.store.view()
        self.assertFalse(view["visible"])
        self.assertFalse(view["source_authenticated"])
        self.assertFalse(view["strategy_verified"])
        self.assertFalse(view["execution_eligible"])

    def test_synthetic_machine_candidate_cannot_become_advice(self):
        machine = CoachEvent(SCHEMA, "synthetic", "test-stream", "frame-1", 1, 900, 950, 1_800,
                             "candidate_visible", "candidate", CoachAdvice("advice-1", "lineup", "标题", "动作", "理由"),
                             evidence_status="machine_candidate")
        self.assertFalse(self.store.ingest(machine))
        self.assertFalse(self.store.view()["visible"])

    def test_invalid_event_withdraws_current_card(self):
        self.assertTrue(self.store.ingest(event("test-stream", 1, 900, 950, 1_800)))
        bad = CoachEvent(SCHEMA, "synthetic", "test-stream", "frame-2", 2, 1_000, 1_050,
                         1_900, "candidate_visible", "candidate", None)
        self.assertFalse(self.store.ingest(bad))
        self.assertFalse(self.store.view()["visible"])
        self.assertEqual(self.store.view()["status"], "unknown")

    def test_silence_is_disconnected_without_a_fabricated_source_frame(self):
        self.assertTrue(self.store.ingest(event("test-stream", 1, 900, 950, 5_000)))
        sequence, frame = self.store.view()["source_sequence"], self.store.view()["frame_id"]
        self.clock.now = 1_250
        self.store.tick(source_timeout_ms=200)
        view = self.store.view()
        self.assertFalse(view["visible"])
        self.assertEqual((view["status"], view["reason"]), ("disconnected", "source_silence"))
        report = self.store.report()["opportunities"][-1]
        self.assertEqual((report["source_sequence"], report["frame_id"]), (sequence, frame))

    def test_raw_source_sequence_is_not_renumbered_for_adapter_order(self):
        original = event("test-stream", 1, 900, 950, 1_800)
        raw = CoachEvent(original.schema_version, original.source_kind, original.stream_id, original.frame_id,
                         original.sequence, original.frame_received_ms, original.emitted_ms, original.expires_ms,
                         original.status, original.reason, original.advice, source_sequence=3)
        self.assertTrue(self.store.ingest(raw))
        self.assertEqual(self.store.report()["opportunities"][-1]["source_sequence"], 3)

    def test_historical_browser_report_cannot_revive_withdrawn_card(self):
        self.assertTrue(self.store.ingest(event("test-stream", 1, 900, 950, 1_800)))
        view = self.store.view()
        self.clock.now = 1_100
        self.assertTrue(self.store.ingest(event("test-stream", 2, 1_000, 1_050, 1_900, status="unknown")))
        report = {"session_id": view["session_id"], "client_id": "browser-test", "client_event_sequence": 1,
                  "revision": view["revision"], "render_token": view["render_token"], "opportunity_id": view["opportunity_id"],
                  "stage": "paint_opportunity", "visibility": "visible", "browser_monotonic_ms": 10.0, "dom_to_raf_ms": 2.0}
        self.assertFalse(self.store.report_browser(report))
        self.assertFalse(self.store.view()["visible"])
        self.assertEqual(self.store.report()["counters"]["browser_reports_historical"], 1)

    def test_invalid_second_payload_cannot_bypass_advice_observation_exclusion(self):
        mixed = CoachEvent(SCHEMA, "synthetic", "test-stream", "frame-1", 1, 900, 950, 1_800,
                           "candidate_visible", "candidate", CoachAdvice("a-1", "lineup", "标题", "动作", "理由"),
                           observation=object())
        self.assertFalse(self.store.ingest(mixed))
        self.assertFalse(self.store.view()["visible"])

    def test_original_source_sequence_regression_with_new_adapter_sequence_withdraws(self):
        first = CoachEvent(SCHEMA, "replay_candidate", "test-stream", "frame-1", 1, 900, 950, 1_800,
                           "candidate_visible", "candidate", None, "ctx-1", "evidence-1", 10, "machine_candidate",
                           CoachObservation("obs-1", "观察", "事实", "未知"))
        self.assertTrue(self.store.ingest(first))
        self.clock.now = 1_100
        regressed = CoachEvent(SCHEMA, "replay_candidate", "test-stream", "frame-2", 2, 1_000, 1_050, 1_900,
                               "candidate_visible", "candidate", None, "ctx-2", "evidence-2", 1, "machine_candidate",
                               CoachObservation("obs-2", "观察", "事实", "未知"))
        self.assertFalse(self.store.ingest(regressed))
        self.assertEqual(self.store.view()["reason"], "nonadvancing_source_sequence")

    def test_browser_duplicate_hidden_paint_and_malformed_stage_are_rejected(self):
        self.assertTrue(self.store.ingest(event("test-stream", 1, 900, 950, 1_800)))
        view = self.store.view()
        payload = {"session_id": view["session_id"], "client_id": "browser-test", "client_event_sequence": 1,
                   "revision": view["revision"], "render_token": view["render_token"], "opportunity_id": view["opportunity_id"],
                   "stage": "paint_opportunity", "visibility": "hidden", "browser_monotonic_ms": 1.0, "dom_to_raf_ms": 1.0}
        self.assertFalse(self.store.report_browser(payload))
        payload["visibility"] = "visible"
        self.assertTrue(self.store.report_browser(payload))
        self.assertFalse(self.store.report_browser(payload))
        payload["client_event_sequence"] = 2
        payload["stage"] = []
        self.assertFalse(self.store.report_browser(payload))

    def test_browser_huge_number_and_late_paint_fail_closed(self):
        self.assertTrue(self.store.ingest(event("test-stream", 1, 900, 950, 1_100)))
        view = self.store.view()
        payload = {"session_id": view["session_id"], "client_id": "browser-test", "client_event_sequence": 1,
                   "revision": view["revision"], "render_token": view["render_token"], "opportunity_id": view["opportunity_id"],
                   "stage": "paint_opportunity", "visibility": "visible", "browser_monotonic_ms": 10**400, "dom_to_raf_ms": 1.0}
        self.assertFalse(self.store.report_browser(payload))
        self.clock.now = 1_200
        payload["browser_monotonic_ms"] = 1.0
        self.assertFalse(self.store.report_browser(payload))
        self.assertEqual(self.store.view()["status"], "expired")

    def test_browser_report_after_configured_source_silence_is_not_accepted(self):
        self.assertTrue(self.store.ingest(event("test-stream", 1, 900, 950, 9_000)))
        view = self.store.view()
        self.clock.now = 3_000
        payload = {"session_id": view["session_id"], "client_id": "browser-test", "client_event_sequence": 1,
                   "revision": view["revision"], "render_token": view["render_token"], "opportunity_id": view["opportunity_id"],
                   "stage": "paint_opportunity", "visibility": "visible", "browser_monotonic_ms": 1.0, "dom_to_raf_ms": 1.0}
        self.assertFalse(self.store.report_browser(payload, source_timeout_ms=1_500))
        self.assertEqual((self.store.view()["status"], self.store.view()["reason"]), ("disconnected", "source_silence"))

    def test_malformed_unhashable_fields_withdraw_without_raising(self):
        self.assertTrue(self.store.ingest(event("test-stream", 1, 900, 950, 1_800)))
        malformed = CoachEvent(SCHEMA, [], "test-stream", "frame-2", 2, 1_000, 1_050,
                               1_900, "candidate_visible", "candidate", None)
        self.assertFalse(self.store.ingest(malformed))
        self.assertFalse(self.store.view()["visible"])

    def test_symlinked_parent_resolving_inside_project_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o700)
            link = Path(root) / "linked-project"
            link.symlink_to(Path(__file__).resolve().parents[3], target_is_directory=True)
            with self.assertRaisesRegex(Exception, "output_parent_invalid"):
                build_demo(link / "private-demo")
        self.assertEqual(self.store.view()["status"], "unknown")
        malformed_advice = CoachEvent(SCHEMA, "synthetic", "test-stream", "frame-3", 2, 1_000,
                                      1_050, 1_900, "candidate_visible", "candidate",
                                      CoachAdvice("bad-category", [], "标题", "动作", "理由"))
        self.assertFalse(self.store.ingest(malformed_advice))
        self.assertFalse(self.store.view()["visible"])

    def test_private_demo_contains_replay_and_hypothetical_views_and_no_external_url(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o700)
            output = Path(root) / "demo"
            result = build_demo(output)
            page = Path(result["page"])
            data = page.read_text(encoding="utf-8")
            report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
            self.assertEqual(result["frames"], 6)
            self.assertTrue(result["simulation"])
            self.assertIn("state.replaceChildren", data)
            self.assertNotIn("http://", data)
            self.assertNotIn("https://", data)
            self.assertEqual(report["source_kinds"], ["synthetic"])
            self.assertFalse(report["views"][-1]["visible"])
            self.assertEqual(report["views"][-1]["advice"], None)
            self.assertEqual(stat_mode(page), 0o600)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class ReplayExampleFixturesTest(unittest.TestCase):
    def test_replay_example_has_refresh_and_withdrawal(self):
        frames = replay_example_events("fixture-stream")
        self.assertEqual([frame.status for frame in frames], ["candidate_visible"] * 5 + ["unknown"])
        self.assertNotEqual(frames[0].advice.advice_id, frames[1].advice.advice_id)
        self.assertEqual(frames[0].source_kind, "synthetic")
        self.assertIn("虚构例子", frames[0].advice.title_zh)
        self.assertIn("虚构可用单位", frames[1].advice.action_zh)
        self.assertIn("虚构羁绊甲", frames[2].advice.action_zh)
        self.assertIn("巨人杀手", frames[3].advice.action_zh)
        self.assertIn("虚构羁绊乙", frames[4].advice.action_zh)
        self.assertIsNone(frames[-1].advice)


class LocalDynamicRouteTest(unittest.TestCase):
    def test_jsonl_future_append_refreshes_view_and_browser_report(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o700)
            path = Path(root) / "input.jsonl"
            path.touch(mode=0o600)
            server = run_server(input_path=path, stream_id="dynamic-stream", clock_domain_id="host-mono-test", source_timeout_ms=2_000)
            server.start_maintenance()
            serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
            serving.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}"
                now = time.monotonic_ns() // 1_000_000
                payload = {"schema_version": SCHEMA, "source_kind": "synthetic", "stream_id": "dynamic-stream",
                           "frame_id": "fixture-frame-alpha", "sequence": 1, "source_sequence": 3,
                           "frame_received_ms": now - 20, "emitted_ms": now - 10, "expires_ms": now + 3_000,
                           "status": "candidate_visible", "reason": "candidate", "context_version": "ctx-unknown",
                           "evidence_id": "fixture-evidence-alpha", "evidence_status": "unverified", "observation": None, "advice": {"advice_id": "fixture-advice",
                           "category": "lineup", "title_zh": "虚构提示", "action_zh": "等待完整字段",
                           "reason_zh": "这是无来源夹具。"}}
                with path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(payload, ensure_ascii=False) + "\n")
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with urlopen(url + "/api/view", timeout=1) as response:
                        view = json.loads(response.read())
                    if view["visible"]:
                        break
                    time.sleep(.03)
                self.assertTrue(view["visible"])
                self.assertEqual(view["source_sequence"], 3)
                browser = {"session_id": view["session_id"], "client_id": "browser-test", "client_event_sequence": 1,
                           "revision": view["revision"], "render_token": view["render_token"], "opportunity_id": view["opportunity_id"],
                           "stage": "paint_opportunity", "visibility": "visible", "browser_monotonic_ms": 2.0, "dom_to_raf_ms": 1.0}
                request = Request(url + "/api/browser-event", data=json.dumps(browser).encode(), method="POST",
                                  headers={"Content-Type": "application/json", "Origin": url})
                with urlopen(request, timeout=1) as response:
                    self.assertTrue(json.loads(response.read())["accepted"])
                self.assertIsNotNone(server.store.report()["opportunities"][-1]["input_to_paint_report_received_ms"])
            finally:
                server.server_close()
                server.source.close()
                serving.join(timeout=1)


class JsonlTailSafetyTest(unittest.TestCase):
    def test_overlong_physical_line_tail_is_discarded_through_its_newline(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o700)
            path = Path(root) / "tail.jsonl"; path.touch()
            clock = Clock(1_000); store = HumanCoachStore(stream_id="tail-stream", clock=clock)
            tail = JsonlTail(path, store)
            try:
                with path.open("ab") as output: output.write(b"x" * 8193)
                tail.poll()
                valid = {"schema_version": SCHEMA, "source_kind": "synthetic", "stream_id": "tail-stream", "frame_id": "frame-1",
                         "sequence": 1, "source_sequence": None, "frame_received_ms": 900, "emitted_ms": 950, "expires_ms": 1_800,
                         "status": "candidate_visible", "reason": "candidate", "context_version": None, "evidence_id": None,
                         "evidence_status": "unverified", "observation": None,
                         "advice": {"advice_id": "tail-advice", "category": "lineup", "title_zh": "标题", "action_zh": "动作", "reason_zh": "理由"}}
                with path.open("ab") as output: output.write(json.dumps(valid).encode() + b"\n")
                tail.poll()
                self.assertFalse(store.view()["visible"])
                with path.open("ab") as output: output.write(json.dumps(valid | {"frame_id":"frame-2", "sequence":1, "advice":{**valid["advice"], "advice_id":"tail-advice-2"}}).encode() + b"\n")
                tail.poll()
                self.assertTrue(store.view()["visible"])
            finally:
                tail.close()

    def test_same_inode_recent_prefix_rewrite_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o700)
            path = Path(root) / "tail.jsonl"; path.touch()
            clock = Clock(1_000); store = HumanCoachStore(stream_id="tail-stream", clock=clock)
            tail = JsonlTail(path, store)
            try:
                record = {"schema_version": SCHEMA, "source_kind": "synthetic", "stream_id": "tail-stream", "frame_id": "frame-1",
                          "sequence": 1, "source_sequence": None, "frame_received_ms": 900, "emitted_ms": 950, "expires_ms": 1_800,
                          "status": "candidate_visible", "reason": "candidate", "context_version": None, "evidence_id": None,
                          "evidence_status": "unverified", "observation": None,
                          "advice": {"advice_id": "rewrite-advice", "category": "lineup", "title_zh": "标题", "action_zh": "动作", "reason_zh": "理由"}}
                raw = json.dumps(record).encode() + b"\n"
                with path.open("ab") as output: output.write(raw)
                tail.poll(); self.assertTrue(store.view()["visible"])
                with path.open("wb") as output: output.write(b"x" * len(raw) + raw)
                tail.poll()
                self.assertTrue(tail.failed)
                self.assertEqual(store.view()["status"], "disconnected")
            finally:
                tail.close()


if __name__ == "__main__":
    unittest.main()
