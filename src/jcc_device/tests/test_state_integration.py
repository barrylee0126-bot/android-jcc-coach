"""End-to-end synthetic receive/check/merge tests; no real images or device IO."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import queue
import sys
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops

from jcc_device.game_state_contract import ContractViolation, iter_observations, validate_state
from jcc_device.latest import (Frame, FrameRef, FrameShutdownTimeout, FrameSourceFailed,
                               LatestFrameStream)
from jcc_device.recognition import FrameProcessor, FrameResult, PixelROI
from jcc_device.stability import ContextEvidence, RegionCalibration
from jcc_device.state_pipeline import (FrameAdmissionRejected, SceneEvidence, StatePipeline,
                                       SceneRecognizer, run_probe, state_summary)


SIZE = (64, 48)
SECRET = "private-device-account-ocr-content"


def image_bytes(*, changed=None, size=SIZE, mode="RGB"):
    with Image.new(mode, size, "#202020") as image, io.BytesIO() as encoded:
        if changed is not None:
            image.putpixel(changed, (33, 32, 32))
        image.save(encoded, format="PNG")
        return encoded.getvalue()


IMAGE = image_bytes()


def calibration(*, partial=False):
    return RegionCalibration("synthetic-calibration", "synthetic-fixture:coverage-v1", SIZE,
                             (PixelROI("recognizer", SIZE, (0, 0, 32 if partial else 64, 48)),),
                             (PixelROI("context", SIZE, (0, 0, 64, 48)),))


def context_for(frame, **changes):
    return replace(ContextEvidence(frame.source, "synthetic-clock", 0, "synthetic-layout",
                                   "synthetic-calibration", "synthetic-fixture:context-v1",
                                   "landscape", 0, True), **changes)


def history():
    # Sanitized fake recognizer output, not actual text read from the RGB image.
    return SceneEvidence("history", "synthetic_cues", {"x_min": .1, "y_min": .1,
                         "x_max": .3, "y_max": .2},
                         ("history_header_upper_half", "history_support_below_header"), 2)


class SyntheticPipelineTests(unittest.TestCase):
    def setUp(self):
        self.now_ns = 1_000_000_000
        self.provider = context_for
        self.pipeline = self.make_pipeline()

    def make_pipeline(self, **changes):
        options = dict(stream_id="synthetic-stream", sample_kind="synthetic", scene_ttl_ms=2000,
                       host_clock_id="synthetic-clock", clock_ns=lambda: self.now_ns,
                       continuity_enabled=True, calibration=calibration(),
                       context_provider=lambda frame: self.provider(frame), max_frame_age_seconds=.5)
        options.update(changes)
        return StatePipeline(**options)

    def frame(self, sequence, *, data=IMAGE, received_ns=None, stream_id="synthetic-stream"):
        return Frame(FrameRef(stream_id, sequence, self.now_ns if received_ns is None else received_ns,
                              "2026-11-02T00:00:00+00:00"), data)

    def receive(self, sequence, *, data=IMAGE, advance_ns=0):
        self.now_ns += advance_ns
        return self.pipeline.observe_frame(self.frame(sequence, data=data))

    def result(self, context, evidence=None, *, finished_ns=None, started_ns=None):
        return FrameResult(context.source,
                           context.source.host_received_monotonic_ns if started_ns is None else started_ns,
                           self.now_ns if finished_ns is None else finished_ns,
                           history() if evidence is None else evidence)

    def assert_state(self, previous=None):
        state = self.pipeline.snapshot()
        validate_state(state, previous=previous)
        return state

    def test_4fps_slow_result_reaches_game_state_with_original_source_and_deadline(self):
        original = self.receive(1)
        for sequence in range(2, 7):
            before = self.pipeline.snapshot()
            self.receive(sequence, advance_ns=250_000_000)
            self.assert_state(before)
        self.now_ns = original.source.host_received_monotonic_ns + 1_450_000_000
        before = self.pipeline.snapshot()
        result = self.result(original)
        self.assertTrue(self.pipeline.publish(original, result))
        state = self.assert_state(before)
        self.assertEqual(state["frame_id"], "synthetic-stream:6")
        self.assertEqual(state["context_epoch"], 0)
        self.assertEqual([record["sequence"] for record in state["frames"]], [1, 6])
        scene = state["scene"]
        self.assertEqual((scene["status"], scene["value"]), ("recognized", "history"))
        self.assertEqual(scene["source_frame_id"], "synthetic-stream:1")
        self.assertEqual(scene["observed_host_monotonic_ms"], 1000)
        self.assertEqual(scene["completed_host_monotonic_ms"], 2450)
        self.assertEqual(scene["expires_host_monotonic_ms"], 3000)
        for key in ("mode", "game_version", "gold", "hp", "level"):
            self.assertIsNone(state[key]["value"])
        self.assertEqual(self.pipeline.stats()["continuity"]["frames_checked"], 6)

    def test_new_checked_frames_advance_anchor_without_retimestamping_a_value(self):
        original = self.receive(1)
        self.assertTrue(self.pipeline.publish(original, self.result(original)))
        scene = deepcopy(self.pipeline.snapshot()["scene"])
        for sequence in range(2, 6):
            before = self.pipeline.snapshot()
            self.receive(sequence, advance_ns=250_000_000)
            state = self.assert_state(before)
            self.assertEqual(state["scene"], scene)
            self.assertEqual(state["frame_id"], f"synthetic-stream:{sequence}")
            self.assertLessEqual(len(state["frames"]), 2)

    def test_local_single_channel_change_revokes_pending_and_published_values(self):
        for published in (False, True):
            with self.subTest(published=published):
                self.setUp()
                original = self.receive(1)
                if published:
                    self.pipeline.publish(original, self.result(original))
                before = self.pipeline.snapshot()
                self.receive(2, data=image_bytes(changed=(63, 47)), advance_ns=250_000_000)
                state = self.assert_state(before)
                self.assertEqual(state["scene"]["status"], "unknown")
                self.assertEqual(state["scene"]["source_frame_id"], "synthetic-stream:2")
                self.assertFalse(self.pipeline.publish(original, self.result(original)))

    def test_a_b_a_never_resurrects_old_a_but_new_a_may_be_recognized(self):
        original = self.receive(1)
        self.receive(2, data=image_bytes(changed=(20, 20)), advance_ns=250_000_000)
        current = self.receive(3, advance_ns=250_000_000)
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        self.assertEqual(self.pipeline.snapshot()["scene"]["status"], "unknown")
        self.assertTrue(self.pipeline.publish(current, self.result(current)))
        self.assertEqual(self.assert_state()["scene"]["source_frame_id"], "synthetic-stream:3")

    def test_unchecked_intermediate_sequence_is_unknown_not_renumbered(self):
        original = self.receive(1)
        self.receive(3, advance_ns=250_000_000)
        state = self.assert_state()
        self.assertEqual(state["frame_id"], "synthetic-stream:3")
        self.assertEqual(state["scene"]["reason"], "sequence_gap")
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        current = self.receive(4, advance_ns=250_000_000)
        self.assertTrue(self.pipeline.publish(current, self.result(current)))
        self.assertFalse(self.pipeline.publish(original, self.result(original)))

    def test_duplicate_or_reordered_authoritative_frame_revokes_old_proof(self):
        for sequence in (1, 0):
            with self.subTest(sequence=sequence):
                self.setUp()
                original = self.receive(1)
                self.pipeline.publish(original, self.result(original))
                with self.assertRaises(FrameAdmissionRejected):
                    self.receive(sequence, advance_ns=100_000_000)
                self.assertEqual(self.assert_state()["scene"]["status"], "stale")
                self.assertFalse(self.pipeline.publish(original, self.result(original)))
                self.receive(2, advance_ns=100_000_000)
                self.assertFalse(self.pipeline.publish(original, self.result(original)))

    def test_context_and_visibility_must_be_proven_again_for_every_frame(self):
        modifications = (None, {"context_key": None}, {"unobscured": None}, {"unobscured": False},
                         {"verification_ref": None}, {"calibration_id": "other"},
                         {"host_clock_id": "other"}, {"source": self.frame(99).source})
        for modification in modifications:
            with self.subTest(modification=modification):
                self.setUp()
                original = self.receive(1)
                self.pipeline.publish(original, self.result(original))
                self.provider = (lambda frame: None) if modification is None else (
                    lambda frame: context_for(frame, **modification))
                self.receive(2, advance_ns=250_000_000)
                self.assertEqual(self.assert_state()["scene"]["status"], "unknown")
                self.assertFalse(self.pipeline.publish(original, self.result(original)))

    def test_semantic_context_changes_even_with_identical_pixels_create_new_epoch(self):
        original = self.receive(1)
        self.provider = lambda frame: context_for(frame, context_key="synthetic-other-layout", context_epoch=1)
        current = self.receive(2, advance_ns=250_000_000)
        self.assertGreater(current.context_epoch, original.context_epoch)
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        self.assertTrue(self.pipeline.publish(current, self.result(current)))
        self.assert_state()

    def test_no_calibration_is_safe_unknown_even_for_positive_same_frame_result(self):
        self.pipeline = self.make_pipeline(calibration=None)
        original = self.receive(1)
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        state = self.assert_state()
        self.assertEqual(state["scene"]["reason"], "regions_unconfigured")
        self.assertTrue(all(observation["status"] == "unknown" for _, observation in iter_observations(state)))
        self.assertIsNone(state["frames"][0]["image"]["calibration_id"])
        self.assertIsNone(state["frames"][0]["image"]["game_viewport"])

    def test_title_box_only_calibration_does_not_cover_full_frame_recognizer(self):
        self.pipeline = self.make_pipeline(calibration=calibration(partial=True))
        original = self.receive(1)
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        self.assertEqual(self.assert_state()["scene"]["reason"], "recognizer_coverage_unproven")

    def test_decode_failure_preserves_real_old_metadata_and_can_recover(self):
        original = self.receive(1)
        self.pipeline.publish(original, self.result(original))
        before = self.pipeline.snapshot()
        self.assertIsNone(self.receive(2, data=b"not-an-image", advance_ns=250_000_000))
        state = self.assert_state(before)
        self.assertEqual(state["frame_id"], "synthetic-stream:1")
        self.assertEqual(state["scene"]["status"], "stale")
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        with self.assertRaises(FrameAdmissionRejected):
            self.pipeline.advance(Frame(original.source, IMAGE))
        current = self.receive(3, advance_ns=250_000_000)
        self.assertEqual(self.pipeline.snapshot()["frame_id"], "synthetic-stream:3")
        self.assertTrue(self.pipeline.publish(current, self.result(current)))
        self.assert_state()

    def test_geometry_or_image_encoding_failure_cannot_inherit(self):
        for data in (image_bytes(size=(65, 48)), image_bytes(mode="RGBA")):
            with self.subTest(data_size=len(data)):
                self.setUp()
                original = self.receive(1)
                self.receive(2, data=data, advance_ns=250_000_000)
                self.assertFalse(self.pipeline.publish(original, self.result(original)))
                state = self.assert_state()
                self.assertEqual(state["scene"]["status"], "unknown")
                self.assertIsNone(state["frames"][-1]["image"]["calibration_id"])

    def test_original_ttl_expires_exactly_despite_new_unchanged_frames(self):
        original = self.receive(1)
        self.pipeline.publish(original, self.result(original))
        original_scene = deepcopy(self.pipeline.snapshot()["scene"])
        for sequence in range(2, 9):
            self.receive(sequence, advance_ns=250_000_000)
        self.now_ns = 2_999_999_999
        self.assertEqual(self.pipeline.snapshot()["scene"]["status"], "recognized")
        before = self.pipeline.snapshot()
        self.now_ns = 3_000_000_000
        state = self.assert_state(before)
        self.assertEqual(state["scene"]["status"], "stale")
        self.assertEqual(state["scene"]["reason"], "expired")
        for key in ("source_frame_id", "observed_host_monotonic_ms", "completed_host_monotonic_ms",
                    "expires_host_monotonic_ms", "region", "method"):
            self.assertEqual(state["scene"][key], original_scene[key])
        self.assertEqual(state["gold"]["applicability"], "undetermined")

    def test_late_result_at_original_deadline_is_rejected(self):
        original = self.receive(1)
        for sequence in range(2, 10):
            self.receive(sequence, advance_ns=250_000_000)
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        self.assertEqual(self.assert_state()["scene"]["status"], "unknown")

    def test_silence_timeout_rechecks_on_read_and_does_not_restore_old_ticket(self):
        original = self.receive(1)
        self.pipeline.publish(original, self.result(original))
        self.now_ns += 500_000_000
        self.assertEqual(self.assert_state()["scene"]["status"], "stale")
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        current = self.receive(2)
        self.assertGreater(current.context_epoch, original.context_epoch)
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        self.assertTrue(self.pipeline.publish(current, self.result(current)))

    def test_check_budget_overrun_revokes_even_an_unchanged_segment(self):
        self.pipeline = self.make_pipeline(max_check_seconds=.01)
        original = self.receive(1)
        self.pipeline.publish(original, self.result(original))

        def slow(frame):
            time.sleep(.03)
            return context_for(frame)

        self.provider = slow
        self.receive(2, advance_ns=250_000_000)
        self.assertEqual(self.assert_state()["scene"]["reason"], "check_timeout")
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        self.provider = context_for
        current = self.receive(3, advance_ns=250_000_000)
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        self.assertTrue(self.pipeline.publish(current, self.result(current)))
        self.assertEqual(self.pipeline.stats()["check_timeouts"], 1)

    def test_context_verifier_failure_stays_unknown_and_redacts_detail(self):
        original = self.receive(1)

        def fail(frame):
            raise RuntimeError(SECRET)

        self.provider = fail
        self.receive(2, advance_ns=250_000_000)
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        self.assertNotIn(SECRET, json.dumps(self.assert_state()))
        self.assertNotIn(SECRET, json.dumps(self.pipeline.stats()))
        self.assertEqual(self.pipeline.stats()["context_provider_errors"], 1)

    def test_newer_unknown_or_recognized_result_is_a_field_watermark(self):
        for decision in (history(), SceneEvidence(reason="synthetic_unreadable")):
            with self.subTest(decision=decision):
                self.setUp()
                original = self.receive(1)
                newer = self.receive(2, advance_ns=250_000_000)
                self.assertTrue(self.pipeline.publish(newer, self.result(newer, decision)))
                before = self.pipeline.snapshot()
                self.assertFalse(self.pipeline.publish(original, self.result(original)))
                self.assertEqual(self.pipeline.snapshot(), before)
                self.assertEqual(self.pipeline.stats()["rejections"]["field_watermark"], 1)

    def test_completion_barrier_and_duplicate_rules_still_apply(self):
        self.now_ns = 1_050_000_000
        frame = self.frame(1, received_ns=1_000_000_000)
        original = self.pipeline.observe_frame(frame)
        self.assertFalse(self.pipeline.publish(original, self.result(original, finished_ns=1_020_000_000)))
        self.assertTrue(self.pipeline.publish(original, self.result(original)))
        before = self.pipeline.snapshot()
        self.assertFalse(self.pipeline.publish(original, self.result(original, SceneEvidence(reason="different"))))
        self.assertEqual(self.pipeline.snapshot(), before)

    def test_bad_result_cannot_bypass_contract_and_failed_merge_is_atomic(self):
        original = self.receive(1)
        before = self.pipeline.snapshot()
        bad = replace(history(), region={"x_min": .1, "y_min": .1, "x_max": 2, "y_max": .2})
        with self.assertRaises(ContractViolation):
            self.pipeline.publish(original, self.result(original, bad))
        self.assertEqual(self.pipeline.snapshot(), before)
        self.assertTrue(self.pipeline.publish(original, self.result(original)))

    def test_pruned_pending_source_record_is_restored_exactly_when_merged(self):
        first = self.receive(1)
        self.pipeline.publish(first, self.result(first))
        pending = self.receive(2, advance_ns=250_000_000)
        original_record = deepcopy(self.pipeline.snapshot()["frames"][-1])
        self.receive(3, advance_ns=250_000_000)
        self.assertNotIn(pending.source.frame_id, [record["frame_id"] for record in self.pipeline.snapshot()["frames"]])
        before = self.pipeline.snapshot()
        self.assertTrue(self.pipeline.publish(pending, self.result(pending)))
        state = self.assert_state(before)
        restored = next(record for record in state["frames"] if record["frame_id"] == pending.source.frame_id)
        self.assertEqual(restored, original_record)
        self.assertEqual(state["scene"]["source_frame_id"], pending.source.frame_id)
        self.assertEqual(state["frame_id"], "synthetic-stream:3")

    def test_foreign_clock_stream_and_epoch_contexts_cannot_use_a_valid_ticket(self):
        original = self.receive(1)
        for changes in ({"host_clock_id": "other"}, {"context_epoch": 999},
                        {"source": replace(original.source, stream_id="other")},
                        {"image_size": (1, 1)}):
            with self.subTest(changes=changes):
                modified = replace(original, **changes)
                self.assertFalse(self.pipeline.publish(modified, self.result(modified)))
        self.assertEqual(self.pipeline.snapshot()["scene"]["status"], "unknown")

    def test_clock_regression_terminates_proof_even_if_clock_later_recovers(self):
        original = self.receive(1)
        self.pipeline.publish(original, self.result(original))
        self.now_ns -= 1
        with self.assertRaises(ValueError):
            self.pipeline.snapshot()
        self.now_ns += 2
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        self.assertEqual(self.assert_state()["scene"]["reason"], "disconnected")
        self.assertTrue(self.pipeline.stats()["disconnected"])

    def test_existing_contract_conflict_cannot_be_silently_resolved_by_scene_path(self):
        original = self.receive(1)
        # Seed a VALID conflict as though another contract-aware merger had
        # supplied it. The scene-only API has no multi-method review authority.
        state = self.pipeline.snapshot()
        observation = state["scene"]
        candidate = {key: deepcopy(observation[key]) for key in
                     ("source_frame_id", "observed_host_monotonic_ms", "completed_host_monotonic_ms", "region")}
        candidate.update(value="history", expires_host_monotonic_ms=3000,
                         method={"kind": "manual", "name": "synthetic-review-a", "version": "v1"})
        other = deepcopy(candidate)
        other.update(value="lobby", method={"kind": "template", "name": "synthetic-review-b", "version": "v1"})
        observation.update(status="conflict", applicability="applicable", reason="synthetic_disagreement",
                           method={"kind": "fusion", "name": "synthetic-conflict", "version": "v1"},
                           candidates=[candidate, other])
        self.pipeline._commit(state, self.now_ns / 1_000_000)
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        self.assertEqual(self.assert_state()["scene"]["status"], "conflict")
        self.now_ns = 3_000_000_000
        # A conflict whose candidates expire is withdrawn, never resolved by
        # completion order; no expired candidate reaches a new valid snapshot.
        self.assertEqual(self.assert_state()["scene"]["status"], "unknown")

    def test_explicit_invalidation_and_disconnect_seal_all_pending_work(self):
        original = self.receive(1)
        self.pipeline.invalidate_context()
        self.assertFalse(self.pipeline.publish(original, self.result(original)))
        current = self.receive(2, advance_ns=250_000_000)
        self.assertGreater(current.context_epoch, original.context_epoch)
        self.pipeline.publish(current, self.result(current))
        self.pipeline.disconnect()
        state = self.assert_state()
        self.assertEqual(state["scene"]["reason"], "disconnected")
        self.assertFalse(self.pipeline.publish(current, self.result(current)))
        with self.assertRaises(FrameAdmissionRejected):
            self.receive(3, advance_ns=250_000_000)

    def test_lossy_deliveries_cannot_be_used_as_the_checking_entry_point(self):
        with self.assertRaises(FrameAdmissionRejected):
            self.pipeline.advance(self.frame(1))
        self.assertIsNone(self.pipeline.snapshot())
        original = self.receive(1)
        self.receive(2, advance_ns=250_000_000)
        with self.assertRaises(FrameAdmissionRejected):
            self.pipeline.advance(Frame(original.source, IMAGE))


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Expected event did not arrive")
        time.sleep(.002)


class QueueSource:
    def __init__(self):
        self.items = queue.Queue()
        self.close_calls = 0

    def read_frame(self, timeout):
        try:
            item = self.items.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.close_calls += 1


class ReceiverIntegrationTests(unittest.TestCase):
    def make_runtime(self, provider=None, **stream_options):
        source = QueueSource()
        stream = LatestFrameStream(source, read_timeout=.01, **stream_options)
        pipeline = StatePipeline(stream_id=stream.stream_id, sample_kind="synthetic", scene_ttl_ms=5000,
                                 host_clock_id="synthetic-clock", continuity_enabled=True,
                                 calibration=calibration(), context_provider=provider or context_for,
                                 max_check_seconds=1, max_frame_age_seconds=1)
        stream.bind_observer(pipeline)
        self.addCleanup(stream.close)
        stream.start()
        return source, stream, pipeline

    def test_every_authoritative_ref_is_checked_before_unread_a_b_a_replacement(self):
        checked = []

        def provider(frame):
            checked.append(frame.source)
            return context_for(frame)

        source, stream, pipeline = self.make_runtime(provider)
        source.items.put(IMAGE)
        original_frame = stream.read_latest(1)
        original = pipeline.advance(original_frame)
        source.items.put(image_bytes(changed=(20, 20)))
        source.items.put(IMAGE)
        eventually(lambda: stream.stats()["frames_replaced"] == 1)
        newest = stream.read_latest(1)
        self.assertEqual([ref.sequence for ref in checked], [1, 2, 3])
        self.assertEqual(checked[0], original_frame.source)
        self.assertEqual(checked[-1], newest.source)
        self.assertEqual(stream.stats()["frames_replaced"], 1)
        result = FrameResult(original.source, original.source.host_received_monotonic_ns,
                             time.monotonic_ns(), history())
        self.assertFalse(pipeline.publish(original, result))
        state = pipeline.snapshot()
        validate_state(state)
        self.assertEqual(state["frame_id"], newest.source.frame_id)
        self.assertEqual(state["scene"]["status"], "unknown")

    def test_result_and_snapshot_wait_for_new_frame_check_then_see_revocation(self):
        entered, release = threading.Event(), threading.Event()

        def provider(frame):
            if frame.source.sequence == 2:
                entered.set()
                if not release.wait(2):
                    raise TimeoutError(SECRET)
                return context_for(frame, unobscured=None)
            return context_for(frame)

        source, stream, pipeline = self.make_runtime(provider)
        source.items.put(IMAGE)
        original = pipeline.advance(stream.read_latest(1))
        result = FrameResult(original.source, original.source.host_received_monotonic_ns,
                             time.monotonic_ns(), history())
        source.items.put(IMAGE)
        self.assertTrue(entered.wait(1))
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending_result = executor.submit(pipeline.publish, original, result)
            pending_read = executor.submit(pipeline.snapshot)
            try:
                self.assertEqual(stream.stats()["frames_received"], 2)
                self.assertEqual(stream.stats()["pending_frames"], 0)
                time.sleep(.02)
                self.assertFalse(pending_result.done())
                self.assertFalse(pending_read.done())
            finally:
                release.set()
            self.assertFalse(pending_result.result(1))
            state = pending_read.result(1)
        validate_state(state)
        self.assertEqual(state["scene"]["status"], "unknown")
        self.assertEqual(state["frame_id"].split(":")[-1], "2")

    def test_recognition_worker_can_finish_while_unchanged_frames_keep_arriving(self):
        source, stream, pipeline = self.make_runtime()
        source.items.put(IMAGE)
        original_frame = stream.read_latest(1)
        original = pipeline.advance(original_frame)
        entered, release = threading.Event(), threading.Event()

        def recognize(frame):
            entered.set()
            if not release.wait(2):
                raise TimeoutError(SECRET)
            return history()

        processor = FrameProcessor(recognize)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(processor.process, original_frame)
            self.assertTrue(entered.wait(1))
            for _ in range(5):
                source.items.put(IMAGE)
            eventually(lambda: stream.stats()["frames_received"] == 6)
            # No latest-slot delivery/admission is needed to establish anchor 6.
            self.assertEqual(pipeline.snapshot()["frame_id"].split(":")[-1], "6")
            release.set()
            self.assertTrue(pipeline.publish(original, future.result(1)))
        state = pipeline.snapshot()
        validate_state(state)
        self.assertEqual(state["scene"]["status"], "recognized")
        self.assertEqual(state["scene"]["source_frame_id"], original.source.frame_id)

    def test_terminal_source_failure_revokes_without_waiting_for_consumer(self):
        source, stream, pipeline = self.make_runtime()
        source.items.put(IMAGE)
        original = pipeline.advance(stream.read_latest(1))
        result = FrameResult(original.source, original.source.host_received_monotonic_ns,
                             time.monotonic_ns(), history())
        self.assertTrue(pipeline.publish(original, result))
        source.items.put(RuntimeError(SECRET))
        eventually(lambda: stream.stats()["source_errors"] == 1)
        with self.assertRaises(FrameSourceFailed) as failure:
            stream.read_latest(0)
        self.assertNotIn(SECRET, str(failure.exception))
        self.assertEqual(pipeline.snapshot()["scene"]["reason"], "disconnected")
        self.assertFalse(pipeline.publish(original, result))
        eventually(lambda: source.close_calls == 1)

    def test_uncooperative_check_has_bounded_close_and_cannot_publish_on_return(self):
        entered, release = threading.Event(), threading.Event()

        def provider(frame):
            if frame.source.sequence == 2:
                entered.set()
                release.wait(2)
            return context_for(frame)

        source, stream, pipeline = self.make_runtime(provider, shutdown_timeout=.02)
        source.items.put(IMAGE)
        original = pipeline.advance(stream.read_latest(1))
        result = FrameResult(original.source, original.source.host_received_monotonic_ns,
                             time.monotonic_ns(), history())
        source.items.put(IMAGE)
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(FrameShutdownTimeout):
                stream.close()
            self.assertTrue(stream.stats()["receiver_thread_alive"])
            self.assertTrue(stream.stats()["closed"])
        finally:
            release.set()
        eventually(lambda: not stream.stats()["receiver_thread_alive"])
        stream.close()
        self.assertFalse(pipeline.publish(original, result))
        self.assertTrue(pipeline.stats()["disconnected"])
        self.assertEqual(stream.stats()["pending_frames"], 0)
        self.assertEqual(source.close_calls, 1)

    def test_unexpected_observer_error_is_terminal_and_closes_the_source(self):
        source, stream, pipeline = self.make_runtime()
        with patch.object(pipeline, "observe_frame", side_effect=RuntimeError(SECRET)):
            source.items.put(IMAGE)
            eventually(lambda: stream.stats()["source_errors"] == 1)
        with self.assertRaises(FrameSourceFailed) as failure:
            stream.read_latest(0)
        self.assertNotIn(SECRET, str(failure.exception))
        self.assertTrue(pipeline.stats()["disconnected"])
        eventually(lambda: source.close_calls == 1)
        counts = stream.stats()
        self.assertEqual(counts["frames_received"], counts["frames_discarded"])


class ProbeIntegrationTests(unittest.TestCase):
    def test_real_clock_4fps_slow_fixture_reaches_valid_game_state(self):
        report = run_probe(source_name="synthetic", experimental_history=True,
                           continuity_mode="synthetic", synthetic_delay_seconds=1.45)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["calibration_basis"], "synthetic_fixture_only")
        self.assertTrue(report["single_frame_observations"][0]["accepted_by_runtime"])
        state = report["runtime_before_close_state"]
        validate_state(state)
        self.assertEqual(state["scene"]["status"], "recognized")
        source = next(record for record in state["frames"] if record["frame_id"] == state["scene"]["source_frame_id"])
        anchor = next(record for record in state["frames"] if record["frame_id"] == state["frame_id"])
        self.assertGreaterEqual(anchor["sequence"] - source["sequence"], 4)
        self.assertEqual(state["scene"]["expires_host_monotonic_ms"], source["host_received_monotonic_ms"] + 2000)
        self.assertGreaterEqual(report["pipeline_stats"]["continuity"]["frames_checked"], 5)
        validate_state(report["state"], previous=state)
        self.assertNotEqual(report["state"]["scene"]["status"], "recognized")
        self.assertFalse(report["capture_stats"]["receiver_thread_alive"])
        self.assertEqual(report["images_saved"], 0)

    def test_unconfigured_probe_cannot_promote_positive_fake_ocr(self):
        report = run_probe(source_name="synthetic", experimental_history=True, continuity_mode="unconfigured")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["single_frame_observations"][0]["scene_observed"], "history")
        self.assertFalse(report["single_frame_observations"][0]["accepted_by_runtime"])
        self.assertEqual(report["runtime_before_close_state"]["scene"]["status"], "unknown")
        self.assertIsNone(report["runtime_before_close_state"]["frames"][-1]["image"]["calibration_id"])

    def test_fixture_calibration_is_rejected_before_any_tablet_access(self):
        with patch("jcc_device.stream.TabletStream") as tablet:
            report = run_probe(source_name="tablet", continuity_mode="synthetic")
        tablet.assert_not_called()
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["error_type"], "ValueError")


class _PixelDiagnostics:
    """Exact full-RGB diagnostics ONLY; always returns unknown context evidence.

    Holds one previous image in memory. No calibration or visibility assertion
    is created, and no image, identifier, hash or OCR text is written out.
    """

    def __init__(self):
        self.previous = None
        self.counts = dict(frames=0, comparisons=0, equal_pairs=0, changed_pairs=0,
                           changed_pixels_total=0, changed_pixels_max=0, unknown=0,
                           check_ns_total=0, check_ns_max=0)
        self.sizes = set()

    def __call__(self, frame):
        started = time.perf_counter_ns()
        self.counts["frames"] += 1
        try:
            with Image.open(io.BytesIO(frame.jpeg)) as image:
                image.load()
                if image.mode != "RGB":
                    raise ValueError("Only RGB diagnostics are supported")
                self.sizes.add(image.size)
                if self.previous is not None and self.previous.size == image.size:
                    self.counts["comparisons"] += 1
                    with ImageChops.difference(image, self.previous) as difference:
                        if difference.getbbox() is None:
                            self.counts["equal_pairs"] += 1
                        else:
                            self.counts["changed_pairs"] += 1
                            lut = [0] + [255] * 255
                            mask = Image.new("L", image.size, 0)
                            try:
                                for channel in ("R", "G", "B"):
                                    with difference.getchannel(channel) as band, band.point(lut) as nonzero:
                                        next_mask = ImageChops.lighter(mask, nonzero)
                                    mask.close()
                                    mask = next_mask
                                changed = image.width * image.height - mask.histogram()[0]
                            finally:
                                mask.close()
                            self.counts["changed_pixels_total"] += changed
                            self.counts["changed_pixels_max"] = max(self.counts["changed_pixels_max"], changed)
                if self.previous is not None:
                    self.previous.close()
                self.previous = image.copy()
        except Exception:
            self.counts["unknown"] += 1
            self.close()
        elapsed = time.perf_counter_ns() - started
        self.counts["check_ns_total"] += elapsed
        self.counts["check_ns_max"] = max(self.counts["check_ns_max"], elapsed)
        return None  # Pixel equality cannot establish scene or unobscured visibility.

    def stats(self):
        return {**self.counts, "image_sizes_px": sorted(self.sizes), "semantic_evidence": "unavailable"}

    def close(self):
        if self.previous is not None:
            self.previous.close()
            self.previous = None


class PixelDiagnosticTests(unittest.TestCase):
    def test_diagnostic_counts_exact_single_channel_pixels_without_context_assertions(self):
        diagnostic = _PixelDiagnostics()
        self.addCleanup(diagnostic.close)
        reference = FrameRef("synthetic", 1, 1, "synthetic")
        self.assertIsNone(diagnostic(Frame(reference, IMAGE)))
        self.assertIsNone(diagnostic(Frame(replace(reference, sequence=2), IMAGE)))
        self.assertIsNone(diagnostic(Frame(replace(reference, sequence=3), image_bytes(changed=(63, 47)))))
        self.assertEqual(diagnostic.stats()["equal_pairs"], 1)
        self.assertEqual(diagnostic.stats()["changed_pairs"], 1)
        self.assertEqual(diagnostic.stats()["changed_pixels_total"], 1)


def tablet_smoke():
    """Explicit opt-in read-only smoke; not called by unittest discovery.

    Uses tools/scrcpy's default device, no-control/no-power-on, no saved logs.
    The diagnostic callback deliberately returns None on EVERY live frame.
    """
    from contextlib import ExitStack
    from jcc_device.stream import TabletStream
    from jcc_device.vision_ocr import VisionOCR

    started = time.monotonic_ns()
    diagnostic = _PixelDiagnostics()
    report = {"source": "tablet", "sample_kind": "live", "calibration": None,
              "context_visibility_evidence": "unavailable", "images_saved": 0,
              "ocr_text_emitted": False, "start_utc": datetime.now(timezone.utc).isoformat(),
              "start_monotonic_ns": started, "observations": []}
    pipeline = stream = processor = None
    try:
        with ExitStack() as stack:
            raw = stack.enter_context(TabletStream(capture_diagnostics=False))
            stream = LatestFrameStream(raw)
            pipeline = StatePipeline(stream_id=stream.stream_id, sample_kind="live", continuity_enabled=True,
                                     calibration=None, context_provider=diagnostic)
            stream.bind_observer(pipeline)
            stack.enter_context(stream)
            ocr = stack.enter_context(VisionOCR(timeout=8))
            processor = FrameProcessor(SceneRecognizer(ocr, experimental_history=True))
            executor = stack.enter_context(ThreadPoolExecutor(max_workers=1))
            future = context = None
            deadline = time.monotonic() + 15
            while len(report["observations"]) < 3:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Smoke observation budget elapsed")
                if future is not None and future.done():
                    result = future.result()
                    accepted = pipeline.publish(context, result)
                    report["observations"].append({"source_sequence": result.source.sequence,
                        "accepted": accepted, "scene_observed": result.value.scene,
                        "reason": result.value.reason, "ocr_items_count": result.value.ocr_items_count,
                        "received_monotonic_ns": result.source.host_received_monotonic_ns,
                        "started_monotonic_ns": result.processing_started_monotonic_ns,
                        "finished_monotonic_ns": result.processing_finished_monotonic_ns})
                    future = None
                    if len(report["observations"]) >= 3:
                        break
                try:
                    frame = stream.read_latest(.05, max_age_seconds=1)
                except TimeoutError:
                    pipeline.snapshot()
                    continue
                try:
                    next_context = pipeline.advance(frame)
                except FrameAdmissionRejected:
                    continue
                if future is None:
                    context = next_context
                    future = executor.submit(processor.process, frame)
            runtime = pipeline.snapshot()
            validate_state(runtime)
            report["runtime"] = state_summary(runtime)
            report["runtime_schema_and_semantics_passed"] = True
            report["status"] = "ok"
    except Exception as error:
        report.update(status="error", error_type=type(error).__name__)
    finally:
        diagnostic.close()
        report["pixel_diagnostics"] = diagnostic.stats()
        if pipeline is not None:
            pipeline.disconnect()
            final = pipeline.snapshot()
            if final is not None:
                validate_state(final)
            report["host_clock_id"] = pipeline.host_clock_id
            report["pipeline"] = pipeline.stats()
        if stream is not None:
            report["capture"] = stream.stats()
        if processor is not None:
            report["processor"] = processor.stats()
        report["end_utc"] = datetime.now(timezone.utc).isoformat()
        report["end_monotonic_ns"] = time.monotonic_ns()
    return report


if __name__ == "__main__":
    if sys.argv[1:] == ["--tablet-smoke"]:
        smoke = tablet_smoke()
        print(json.dumps(smoke, ensure_ascii=False, allow_nan=False))
        raise SystemExit(0 if smoke["status"] == "ok" else 1)
    unittest.main()
