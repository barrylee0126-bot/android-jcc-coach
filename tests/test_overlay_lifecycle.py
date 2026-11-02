"""Synthetic presentation lifetime tests; no device, images, or network models."""

from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jcc_overlay import Advice, OverlayInput, OverlayStore
from jcc_overlay.demo import DemoController


class Clock:
    def __init__(self) -> None:
        self.tick = 1_000.0

    def __call__(self) -> float:
        return self.tick


class OverlayLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.store = OverlayStore(clock=self.clock)

    def message(self, **changes) -> OverlayInput:
        data = OverlayInput(
            sample_kind="synthetic", host_clock_id=self.store.host_clock_id,
            stream_id=self.store.stream_id, source_revision=1,
            context_epoch=1, status="valid",
            advice=Advice("fixture-1", "第 2 格 · 纸片小方", "可考虑保留", "虚构条件：留作配对。", 1_000, 6_000),
        )
        return replace(data, **changes)

    def assert_no_advice(self, status: str) -> None:
        view = self.store.view()
        self.assertEqual(view["status"], status)
        self.assertIsNone(view["advice"])
        self.assertEqual(view["remaining_ms"], 0)
        self.assertEqual(view["lease_ms"], 0)

    def test_initial_state_is_unknown(self) -> None:
        self.assert_no_advice("unknown")

    def test_valid_hint_carries_simulation_and_object_action_reason(self) -> None:
        self.assertTrue(self.store.publish(self.message()))
        view = self.store.view()
        self.assertEqual(view["status"], "valid")
        self.assertEqual(view["remaining_ms"], 5_000)
        self.assertLessEqual(view["lease_ms"], 1_200)
        self.assertTrue(view["simulation"])
        self.assertFalse(view["execution_eligible"])
        self.assertEqual(view["sample_kind"], "synthetic")
        self.assertEqual(view["advice"]["target"], "第 2 格 · 纸片小方")
        self.assertEqual(view["advice"]["action"], "可考虑保留")
        self.assertIn("虚构", view["advice"]["reason"])

    def test_reading_never_renews_the_original_deadline(self) -> None:
        self.store.publish(self.message())
        for tick, remaining in ((1_100, 4_900), (3_000, 3_000), (5_999, 1)):
            self.clock.tick = tick
            self.assertEqual(self.store.view()["remaining_ms"], remaining)
        self.clock.tick = 6_000
        self.assert_no_advice("expired")

    def test_warning_threshold_and_exact_expiry(self) -> None:
        self.store.publish(self.message())
        self.clock.tick = 2_999
        self.assertEqual(self.store.view()["status"], "valid")
        self.clock.tick = 3_000
        self.assertEqual(self.store.view()["status"], "expiring")
        self.clock.tick = 6_000
        self.assert_no_advice("expired")

    def test_already_expired_input_is_never_visible_as_valid(self) -> None:
        self.clock.tick = 8_000
        self.assertTrue(self.store.publish(self.message()))
        self.assert_no_advice("expired")

    def test_unknown_conflict_disconnect_context_change_withdraw_old_hint(self) -> None:
        for state in ("unknown", "conflict", "disconnected", "invalidated", "expired"):
            with self.subTest(state=state):
                self.setUp()
                self.store.publish(self.message())
                self.assertTrue(self.store.publish(self.message(source_revision=2, context_epoch=2, status=state, advice=None)))
                self.assert_no_advice(state)

    def test_bad_input_withdraws_instead_of_preserving_old_advice(self) -> None:
        for bad in (None, {}, self.message(status="invented"), self.message(status="unknown")):
            with self.subTest(bad=type(bad).__name__):
                self.setUp()
                self.store.publish(self.message())
                self.assertFalse(self.store.publish(bad))
                self.assert_no_advice("unknown")

    def test_live_and_replay_are_always_rejected(self) -> None:
        for kind in ("live", "replay", "Synthetic", True):
            with self.subTest(kind=kind):
                self.setUp()
                self.store.publish(self.message())
                self.assertFalse(self.store.publish(self.message(sample_kind=kind, source_revision=2)))
                self.assert_no_advice("unknown")

    def test_other_stream_or_clock_cannot_enter(self) -> None:
        for change in ({"host_clock_id": "other"}, {"stream_id": "other"}):
            with self.subTest(change=change):
                self.assertFalse(self.store.publish(self.message(**change)))
                self.assert_no_advice("unknown")

    def test_old_revision_and_context_cannot_restore_a_hint(self) -> None:
        old = self.message()
        self.store.publish(old)
        self.store.publish(self.message(source_revision=2, context_epoch=2, status="disconnected", advice=None))
        self.assertFalse(self.store.publish(old))
        self.assert_no_advice("unknown")
        new_id = replace(old.advice, advice_id="new-id")
        self.assertFalse(self.store.publish(self.message(source_revision=3, context_epoch=1, advice=new_id)))
        self.assert_no_advice("unknown")

    def test_same_advice_id_cannot_be_republished_with_a_longer_deadline(self) -> None:
        old = self.message()
        self.store.publish(old)
        newer = replace(old.advice, expires_ms=10_000)
        self.assertFalse(self.store.publish(self.message(source_revision=2, advice=newer)))
        self.assert_no_advice("unknown")
        self.assertFalse(self.store.publish(self.message(source_revision=3, advice=newer)))

    def test_a_fresh_synthetic_revision_can_follow_a_barrier(self) -> None:
        self.store.publish(self.message())
        self.store.publish(self.message(source_revision=2, status="conflict", advice=None))
        new = replace(self.message().advice, advice_id="new-fixture")
        self.assertTrue(self.store.publish(self.message(source_revision=3, advice=new)))
        self.assertEqual(self.store.view()["status"], "valid")

    def test_invalid_times_and_boolean_revisions_are_rejected(self) -> None:
        for bad_time in (float("nan"), float("inf"), True, "6000", 10 ** 1000):
            with self.subTest(bad_time=type(bad_time).__name__):
                bad = replace(self.message().advice, expires_ms=bad_time)
                self.assertFalse(self.store.publish(self.message(advice=bad)))
        self.assertFalse(self.store.publish(self.message(source_revision=True)))
        self.assertFalse(self.store.publish(self.message(context_epoch=True)))

    def test_future_issue_time_and_unbounded_ttl_are_rejected(self) -> None:
        for changes in ({"created_ms": 1_001}, {"expires_ms": 31_001}, {"expires_ms": 1_000}):
            with self.subTest(changes=changes):
                self.assertFalse(self.store.publish(self.message(advice=replace(self.message().advice, **changes))))

    def test_clock_rollback_is_latched_and_cannot_revive_old_hint(self) -> None:
        self.store.publish(self.message())
        self.clock.tick = 999
        self.assert_no_advice("unknown")
        self.clock.tick = 2_000
        fresh = replace(self.message().advice, advice_id="new")
        self.assertFalse(self.store.publish(self.message(source_revision=2, advice=fresh)))
        self.assert_no_advice("unknown")

    def test_clock_failure_is_safe(self) -> None:
        self.store.publish(self.message())
        self.clock.tick = float("nan")
        self.assert_no_advice("unknown")

    def test_bad_text_is_rejected_and_output_is_a_copy(self) -> None:
        for target in ("", "x" * 29, "含\n换行", " 纸片"):
            with self.subTest(target=target):
                bad = replace(self.message().advice, target=target)
                self.assertFalse(self.store.publish(self.message(advice=bad)))
        self.assertTrue(self.store.publish(self.message()))
        view = self.store.view()
        view["advice"]["action"] = "tampered"
        self.assertEqual(self.store.view()["advice"]["action"], "可考虑保留")

    def test_demo_plays_once_with_expired_gap_and_no_automatic_renewal(self) -> None:
        demo = DemoController(self.store)
        demo.choose("play")
        self.assertEqual(demo.view()["status"], "valid")
        for tick, expected in ((6_000, "expiring"), (9_000, "expired"), (11_000, "unknown"), (14_000, "conflict"), (17_000, "disconnected"), (50_000, "disconnected")):
            self.clock.tick = tick
            view = demo.view()
            self.assertEqual(view["status"], expected)
            if expected not in {"valid", "expiring"}:
                self.assertIsNone(view["advice"])
        self.assertFalse(demo.view()["playing"])

    def test_manual_choice_cancels_automatic_sequence(self) -> None:
        demo = DemoController(self.store)
        demo.choose("play")
        demo.choose("unknown")
        self.clock.tick = 30_000
        self.assertEqual(demo.view()["status"], "unknown")
        self.assertFalse(demo.view()["playing"])


if __name__ == "__main__":
    unittest.main()
