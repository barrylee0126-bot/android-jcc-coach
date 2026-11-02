"""Synthetic contract tests.  No tablet, replay, or private image is read."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from jcc_human_coach import CoachAdvice, CoachEvent, HumanCoachStore, build_demo
from jcc_human_coach.core import SCHEMA, replay_example_events


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

    def test_live_candidate_stays_unverified_and_non_executable(self):
        live = CoachEvent(SCHEMA, "live_candidate", "test-stream", "frame-1", 1, 900, 950,
                          1_800, "candidate_visible", "candidate",
                          CoachAdvice("live-advice", "economy", "经济候选", "保留预算", "字段由调用方声明。"))
        self.assertTrue(self.store.ingest(live))
        view = self.store.view()
        self.assertEqual(view["source_kind"], "live_candidate")
        self.assertFalse(view["source_authenticated"])
        self.assertFalse(view["strategy_verified"])
        self.assertFalse(view["execution_eligible"])

    def test_invalid_event_withdraws_current_card(self):
        self.assertTrue(self.store.ingest(event("test-stream", 1, 900, 950, 1_800)))
        bad = CoachEvent(SCHEMA, "synthetic", "test-stream", "frame-2", 2, 1_000, 1_050,
                         1_900, "candidate_visible", "candidate", None)
        self.assertFalse(self.store.ingest(bad))
        self.assertFalse(self.store.view()["visible"])
        self.assertEqual(self.store.view()["status"], "unknown")

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
            self.assertEqual(report["source_kinds"], ["replay_candidate", "synthetic"])
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
        self.assertEqual(frames[0].source_kind, "replay_candidate")
        self.assertIn("自然庇护所", frames[0].advice.title_zh)
        self.assertIn("先补满人口", frames[1].advice.action_zh)
        self.assertIn("3 永恒之森", frames[2].advice.action_zh)
        self.assertIn("巨人杀手", frames[3].advice.action_zh)
        self.assertIn("2 迅捷射手", frames[4].advice.action_zh)
        self.assertIsNone(frames[-1].advice)


if __name__ == "__main__":
    unittest.main()
