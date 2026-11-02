from __future__ import annotations

import unittest
from pathlib import Path
import threading

from jcc_fast_operator import (
    DEFAULT_ADB_WRAPPER,
    Action,
    CoordinateCalibration,
    FastOperator,
    FrameEvidence,
)


def evidence(**changes):
    value = {
        "frame_id": "frame-8",
        "received_monotonic_ns": 1_000_000_000,
        "context_epoch": 7,
        "width_px": 1280,
        "height_px": 720,
        "orientation": "landscape",
        "applied_rotation_degrees": 0,
        "scene": "in_game",
        "phase": "preparation",
        "source_kind": "live",
    }
    value.update(changes)
    return FrameEvidence(**value)


def calibration(**changes):
    value = {
        "calibration_id": "reviewed-layout-7",
        "context_epoch": 7,
        "width_px": 1280,
        "height_px": 720,
        "orientation": "landscape",
        "applied_rotation_degrees": 0,
        "reviewed": True,
    }
    value.update(changes)
    return CoordinateCalibration(**value)


class FastOperatorTests(unittest.TestCase):
    def setUp(self):
        self.operator = FastOperator(max_frame_age_ms=750, clock_ns=lambda: 1_100_000_000)
        self.action = Action("buy-slot-1", "frame-8", "tap", 900, 500)

    def test_default_command_uses_project_wrapper_not_a_serial(self):
        command = self.operator.adb_command(self.action)
        self.assertEqual(Path(command[0]), DEFAULT_ADB_WRAPPER)
        self.assertEqual(command[1:], ("shell", "input", "tap", "900", "500"))
        self.assertNotIn("-s", command)

    def test_stale_unknown_or_wrong_phase_never_calls_runner(self):
        calls = []
        for frame, reason in (
            (evidence(received_monotonic_ns=0), "frame_stale"),
            (evidence(phase=None), "phase_unknown_or_not_preparation"),
            (evidence(scene="lobby"), "scene_unknown_or_not_in_game"),
            (evidence(source_kind="replay"), "source_not_live"),
        ):
            receipt = self.operator.dispatch(self.action, frame, calibration(), calls.append)
            self.assertEqual(receipt.status, "blocked")
            self.assertEqual(receipt.reason, reason)
        self.assertEqual(calls, [])

    def test_unreviewed_or_geometry_mismatch_blocks_coordinates(self):
        self.assertEqual(
            self.operator.evaluate(self.action, evidence(), calibration(reviewed=False),
                                   now_ns=1_100_000_000).reason,
            "calibration_not_reviewed",
        )
        self.assertEqual(
            self.operator.evaluate(self.action, evidence(), calibration(width_px=1920),
                                   now_ns=1_100_000_000).reason,
            "calibration_geometry_mismatch",
        )
        outside = Action("outside", "frame-8", "tap", 1280, 10)
        self.assertEqual(
            self.operator.evaluate(outside, evidence(), calibration(), now_ns=1_100_000_000).reason,
            "coordinate_outside_calibrated_frame",
        )

    def test_dispatch_is_one_call_and_records_readback_without_retry(self):
        calls = []
        times = iter((1_100_000_000, 1_110_000_000, 1_120_000_000, 1_130_000_000))
        operator = FastOperator(clock_ns=lambda: next(times))
        receipt = operator.dispatch(self.action, evidence(), calibration(), calls.append)
        self.assertEqual(receipt.status, "runner_returned")
        self.assertEqual(receipt.elapsed_ns, 10_000_000)
        self.assertEqual(receipt.runner_returned_monotonic_ns, 1_120_000_000)
        self.assertEqual(len(calls), 1)
        outcome = operator.record_outcome(receipt, readback_frame_id="frame-9",
                                          readback_monotonic_ns=1_130_000_000,
                                          outcome="confirmed", reason="shop_changed")
        self.assertEqual(outcome.outcome, "confirmed")
        self.assertEqual(len(calls), 1)

    def test_runner_failure_is_recorded_without_retry(self):
        def fail(_command):
            raise OSError("mock only")

        with self.assertRaisesRegex(OSError, "mock only"):
            self.operator.dispatch(self.action, evidence(), calibration(), fail)
        receipt = self.operator.attempt_for("buy-slot-1")
        self.assertEqual(receipt.status, "delivery_unknown")
        self.assertEqual(receipt.reason, "runner_raised_after_invocation")
        with self.assertRaisesRegex(ValueError, "only a runner-returned"):
            self.operator.record_outcome(receipt, readback_frame_id="frame-9",
                                         readback_monotonic_ns=1_140_000_000,
                                         outcome="confirmed", reason="impossible")

    def test_interrupt_after_possible_delivery_keeps_conservative_receipt(self):
        def interrupted(_command):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.operator.dispatch(self.action, evidence(), calibration(), interrupted)
        receipt = self.operator.attempt_for("buy-slot-1")
        self.assertEqual(receipt.status, "delivery_unknown")
        retry = self.operator.dispatch(self.action, evidence(), calibration(), lambda _command: None)
        self.assertEqual(retry.reason, "action_id_already_consumed")

    def test_swipe_command_and_causal_readback(self):
        swipe = Action("field", "frame-8", "swipe", 200, 600, 500, 300, 120)
        self.assertEqual(
            self.operator.adb_command(swipe)[-6:],
            ("swipe", "200", "600", "500", "300", "120"),
        )
        receipt = self.operator.dispatch(swipe, evidence(), calibration(), lambda _command: None)
        with self.assertRaisesRegex(ValueError, "must be newer"):
            self.operator.record_outcome(receipt, readback_frame_id="old",
                                         readback_monotonic_ns=receipt.runner_returned_monotonic_ns - 1,
                                         outcome="rejected", reason="old_frame")

    def test_no_long_queue_reentrant_dispatch_is_busy(self):
        nested = []
        operator = FastOperator(clock_ns=lambda: 1_100_000_000)

        def runner(_command):
            nested.append(operator.dispatch(self.action, evidence(), calibration(), lambda _x: None))

        receipt = operator.dispatch(self.action, evidence(), calibration(), runner)
        self.assertEqual(receipt.status, "runner_returned")
        self.assertEqual(nested[0].status, "busy")

    def test_sent_source_frame_and_action_id_are_single_use(self):
        operator = FastOperator(clock_ns=lambda: 1_100_000_000)
        calls = []
        first = operator.dispatch(self.action, evidence(), calibration(), calls.append)
        self.assertEqual(first.status, "runner_returned")
        self.assertEqual(
            operator.dispatch(Action("new-action", "frame-8", "tap", 901, 500), evidence(), calibration(),
                              calls.append).reason,
            "source_frame_already_consumed",
        )
        newer = evidence(frame_id="frame-9", received_monotonic_ns=1_050_000_000)
        self.assertEqual(
            operator.dispatch(self.action, newer, calibration(), calls.append).reason,
            "action_id_already_consumed",
        )
        self.assertEqual(
            operator.dispatch(Action("new-action", "frame-8", "tap", 901, 500), newer, calibration(),
                              calls.append).reason,
            "action_not_relocated_on_current_frame",
        )
        blocked = operator.dispatch(Action("new-action", "frame-9", "tap", 901, 500), newer, calibration(),
                                    calls.append)
        self.assertEqual(blocked.reason, "frame_not_captured_after_prior_action")
        self.assertEqual(len(calls), 1)

    def test_dispatch_uses_current_clock_not_caller_controlled_time(self):
        clocks = iter((2_000_000_000,))
        operator = FastOperator(clock_ns=lambda: next(clocks))
        stale = evidence(received_monotonic_ns=100)
        receipt = operator.dispatch(self.action, stale, calibration(), lambda _command: self.fail("must not call"))
        self.assertEqual(receipt.reason, "frame_stale")

    def test_final_clock_check_blocks_frame_that_expires_before_runner(self):
        clocks = iter((1_100_000_000, 1_800_000_000))
        operator = FastOperator(clock_ns=lambda: next(clocks))
        calls = []
        receipt = operator.dispatch(self.action, evidence(), calibration(), calls.append)
        self.assertEqual(receipt.status, "blocked")
        self.assertEqual(receipt.reason, "frame_stale")
        self.assertEqual(calls, [])

    def test_orientation_must_agree_with_geometry(self):
        with self.assertRaisesRegex(ValueError, "match decoded width"):
            evidence(width_px=720, height_px=1280, orientation="landscape")
        for rotation in (False, 0.0):
            with self.subTest(rotation=rotation), self.assertRaisesRegex(ValueError, "right angle"):
                evidence(applied_rotation_degrees=rotation)
            with self.subTest(calibration_rotation=rotation), self.assertRaisesRegex(ValueError, "right angle"):
                calibration(applied_rotation_degrees=rotation)

    def test_confirmation_requires_newer_different_readback(self):
        times = iter((1_100_000_000, 1_200_000_000, 1_500_000_000))
        operator = FastOperator(clock_ns=lambda: next(times))
        receipt = operator.dispatch(self.action, evidence(), calibration(), lambda _command: None)
        with self.assertRaisesRegex(ValueError, "different newer"):
            operator.record_outcome(receipt, readback_frame_id="frame-8",
                                    readback_monotonic_ns=1_200_000_000,
                                    outcome="confirmed", reason="same")
        with self.assertRaisesRegex(ValueError, "timed newer"):
            operator.record_outcome(receipt, readback_frame_id="frame-9", readback_monotonic_ns=None,
                                    outcome="confirmed", reason="missing")
        with self.assertRaisesRegex(ValueError, "runner return"):
            operator.record_outcome(receipt, readback_frame_id="frame-9",
                                    readback_monotonic_ns=receipt.dispatched_monotonic_ns + 1,
                                    outcome="confirmed", reason="too_early")

    def test_outcome_rejects_forged_or_other_instance_receipts(self):
        operator = FastOperator(clock_ns=lambda: 1_100_000_000)
        receipt = operator.dispatch(self.action, evidence(), calibration(), lambda _command: None)
        forged = type(receipt)(*receipt.__dict__.values())
        with self.assertRaisesRegex(ValueError, "not dispatched"):
            operator.record_outcome(forged, readback_frame_id="frame-9",
                                    readback_monotonic_ns=1_200_000_000,
                                    outcome="confirmed", reason="forged")
        another = FastOperator(clock_ns=lambda: 1_100_000_000)
        with self.assertRaisesRegex(ValueError, "not dispatched"):
            another.record_outcome(receipt, readback_frame_id="frame-9",
                                   readback_monotonic_ns=1_200_000_000,
                                   outcome="confirmed", reason="other")

    def test_next_action_needs_frame_received_after_prior_runner_return(self):
        clocks = iter((1_100_000_000, 1_110_000_000, 1_120_000_000, 1_130_000_000,
                       1_140_000_000, 1_150_000_000, 1_160_000_000))
        operator = FastOperator(clock_ns=lambda: next(clocks))
        first = operator.dispatch(self.action, evidence(), calibration(), lambda _command: None)
        self.assertEqual(first.runner_returned_monotonic_ns, 1_120_000_000)
        old_renamed = evidence(frame_id="frame-9", received_monotonic_ns=1_110_000_000)
        blocked = operator.dispatch(Action("new", "frame-9", "tap", 901, 500), old_renamed,
                                    calibration(), lambda _command: self.fail("must not call"))
        self.assertEqual(blocked.reason, "frame_not_captured_after_prior_action")
        current = evidence(frame_id="frame-10", received_monotonic_ns=1_120_000_001)
        allowed = operator.dispatch(Action("new", "frame-10", "tap", 901, 500), current,
                                    calibration(), lambda _command: None)
        self.assertEqual(allowed.status, "runner_returned")

    def test_two_threads_cannot_invoke_two_runners_on_one_frame(self):
        operator = FastOperator(clock_ns=lambda: 1_100_000_000)
        entered, release = threading.Event(), threading.Event()
        receipts = []

        def first_runner(_command):
            entered.set()
            self.assertTrue(release.wait(1))

        first = threading.Thread(target=lambda: receipts.append(
            operator.dispatch(self.action, evidence(), calibration(), first_runner)))
        first.start()
        self.assertTrue(entered.wait(1))
        receipts.append(operator.dispatch(Action("second", "frame-8", "tap", 901, 500), evidence(), calibration(),
                                           lambda _command: self.fail("must not call")))
        release.set()
        first.join(1)
        self.assertEqual({receipt.status for receipt in receipts}, {"runner_returned", "busy"})


if __name__ == "__main__":
    unittest.main()
