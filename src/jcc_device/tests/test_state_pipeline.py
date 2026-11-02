"""Synthetic provenance/guard tests, not real game recognition evaluation."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import io
import json
import threading
import unittest

from PIL import Image

from jcc_device.game_state_contract import (ContractViolation, iter_observations,
                                           make_validator, semantic_errors, validate_state)
from jcc_device.latest import Frame, FrameRef, LatestFrameStream
from jcc_device.recognition import FrameResult
from jcc_device.state_pipeline import (FrameAdmissionRejected, InvalidatingFrameSource,
                                       SceneEvidence, SceneRecognizer, StatePipeline,
                                       history_evidence, image_metadata, run_probe)


def jpeg(size=(800, 500)):
    with Image.new("RGB", size, "#333333") as image, io.BytesIO() as encoded:
        image.save(encoded, format="JPEG")
        return encoded.getvalue()


IMAGE = jpeg()
SECRET = "private-account-and-nickname-never-emit"


def frame(sequence=1, received_ms=1000, stream="test-stream", data=IMAGE):
    return Frame(FrameRef(stream, sequence, int(received_ms * 1_000_000),
                          "2026-11-02T00:00:00+00:00"), data)


def item(text, x=50, y=30, w=100, h=30):
    return {"text": text, "x": x, "y": y, "width": w, "height": h, "confidence": 0.99}


HISTORY = [item("对局记录"), item("最近对局", y=100), item(SECRET, y=200)]


def evidence():
    return history_evidence(HISTORY, image_metadata(frame()))


def result(context, decision=None, *, started_ms=1050, finished_ms=1050):
    return FrameResult(context.source, int(started_ms * 1_000_000), int(finished_ms * 1_000_000),
                       evidence() if decision is None else decision)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = make_validator()

    def setUp(self):
        self.now_ms = 1050
        self.pipeline = StatePipeline(stream_id="test-stream", sample_kind="synthetic",
                                      host_clock_id="test-clock", scene_ttl_ms=500,
                                      clock_ns=lambda: int(self.now_ms * 1_000_000))

    def assert_valid(self, state, previous=None):
        self.assertEqual(list(self.validator.iter_errors(state)), [])
        self.assertEqual(semantic_errors(state, previous), [])
        validate_state(state, previous, validator=self.validator)

    def test_no_state_before_first_actual_frame(self):
        self.assertIsNone(self.pipeline.snapshot())
        self.pipeline.disconnect()
        self.assertIsNone(self.pipeline.snapshot())

    def test_ttl_is_read_only_within_the_stream(self):
        self.assertEqual(self.pipeline.scene_ttl_ms, 500)
        with self.assertRaises(AttributeError):
            self.pipeline.scene_ttl_ms = 10000

    def test_unknown_snapshot_has_real_source_dimensions_and_thirteen_fields(self):
        context = self.pipeline.advance(frame())
        state = self.pipeline.snapshot()
        self.assert_valid(state)
        self.assertEqual(context.host_clock_id, "test-clock")
        self.assertEqual(context.context_epoch, 0)
        self.assertEqual(context.image_size, (800, 500))
        self.assertEqual(state["stream_id"], "test-stream")
        source = state["frames"][0]
        self.assertEqual(source["frame_id"], "test-stream:1")
        self.assertEqual(source["host_received_monotonic_ms"], 1000)
        self.assertIsNone(source["device_capture_time"])
        self.assertEqual(source["image"]["orientation"], "landscape")
        self.assertIsNone(source["image"]["game_viewport"])
        self.assertIsNone(source["image"]["calibration_id"])
        self.assertEqual([slot["slot"] for slot in state["shop"]], [1, 2, 3, 4, 5])
        self.assertEqual(len(list(iter_observations(state))), 13)
        for _, observation in iter_observations(state):
            self.assertEqual(observation["status"], "unknown")
            self.assertIsNone(observation["value"])
            self.assertEqual(observation["source_frame_id"], source["frame_id"])
            self.assertEqual(observation["observed_host_monotonic_ms"], 1000)
            self.assertEqual(observation["completed_host_monotonic_ms"], 1050)
            self.assertIsNone(observation["expires_host_monotonic_ms"])

    def test_history_is_only_positive_field_numbers_and_shop_are_not_applicable(self):
        context = self.pipeline.advance(frame())
        previous = self.pipeline.snapshot()
        self.assertTrue(self.pipeline.publish(context, result(context)))
        state = self.pipeline.snapshot()
        self.assert_valid(state, previous)
        self.assertEqual(state["scene"]["status"], "recognized")
        self.assertEqual(state["scene"]["value"], "history")
        self.assertEqual(state["scene"]["expires_host_monotonic_ms"], 1500)
        self.assertEqual(state["scene"]["completed_host_monotonic_ms"], 1050)
        self.assertNotIn("confidence", state["scene"])
        for key in ("phase", "round", "hp", "gold", "level"):
            self.assertEqual(state[key]["applicability"], "not_applicable")
            self.assertEqual(state[key]["status"], "unknown")
            self.assertIsNone(state[key]["value"])
        for slot in state["shop"]:
            self.assertEqual(slot["content"]["applicability"], "not_applicable")
            self.assertIsNone(slot["content"]["region"])
            self.assertIsNone(slot["content"]["value"])
        for key in ("mode", "game_version"):
            self.assertEqual(state[key]["applicability"], "undetermined")
            self.assertIsNone(state[key]["value"])

    def test_unknown_result_remains_unknown_without_guessed_values(self):
        context = self.pipeline.advance(frame())
        self.assertTrue(self.pipeline.publish(context, result(context, SceneEvidence(reason="ocr_failed"))))
        state = self.pipeline.snapshot()
        self.assert_valid(state)
        self.assertEqual(state["scene"]["reason"], "ocr_failed")
        self.assertTrue(all(o["status"] == "unknown" for _, o in iter_observations(state)))

    def test_scene_expires_at_exact_deadline_and_revokes_derived_non_applicability(self):
        context = self.pipeline.advance(frame())
        self.pipeline.publish(context, result(context))
        previous = self.pipeline.snapshot()
        self.now_ms = 1499.999
        self.assertEqual(self.pipeline.snapshot()["scene"]["status"], "recognized")
        self.now_ms = 1500
        state = self.pipeline.snapshot()
        self.assert_valid(state, previous)
        scene = state["scene"]
        self.assertEqual(scene["status"], "stale")
        self.assertIsNone(scene["value"])
        self.assertEqual(scene["last_known_value"], "history")
        self.assertEqual(scene["reason"], "expired")
        for key in ("source_frame_id", "observed_host_monotonic_ms", "completed_host_monotonic_ms",
                    "expires_host_monotonic_ms", "method", "region"):
            self.assertEqual(scene[key], previous["scene"][key])
        self.assertEqual(state["gold"]["applicability"], "undetermined")
        self.assertEqual(state["shop"][0]["content"]["applicability"], "undetermined")
        self.assertEqual(self.pipeline.snapshot(), state)
        self.assertEqual(self.pipeline.stats()["expiration_updates"], 1)

    def test_expired_late_result_never_gets_a_new_deadline(self):
        context = self.pipeline.advance(frame())
        self.now_ms = 1500
        self.assertFalse(self.pipeline.publish(context, result(context, finished_ms=1500)))
        self.assertEqual(self.pipeline.snapshot()["scene"]["status"], "unknown")
        self.assertEqual(self.pipeline.stats()["rejections"]["expired_result"], 1)

    def test_new_frame_unknown_barrier_rejects_both_known_and_unknown_old_results(self):
        old = self.pipeline.advance(frame())
        self.pipeline.publish(old, result(old))
        previous = self.pipeline.snapshot()
        self.now_ms = 1150
        newest = self.pipeline.advance(frame(2, 1100))
        barrier = self.pipeline.snapshot()
        self.assert_valid(barrier, previous)
        self.assertEqual(newest.context_epoch, old.context_epoch + 1)
        self.assertEqual(barrier["scene"]["status"], "unknown")
        for decision in (evidence(), SceneEvidence(reason="ocr_failed")):
            self.assertFalse(self.pipeline.publish(old, result(old, decision, finished_ms=1140)))
        self.assertEqual(self.pipeline.snapshot(), barrier)
        self.assertTrue(self.pipeline.publish(newest, result(newest, started_ms=1150, finished_ms=1150)))
        self.assert_valid(self.pipeline.snapshot(), barrier)

    def test_foreign_stream_clock_and_epoch_results_cannot_replace_current(self):
        context = self.pipeline.advance(frame())
        original = self.pipeline.snapshot()
        for changed in (replace(context, host_clock_id="other-clock"),
                        replace(context, context_epoch=99),
                        replace(context, source=replace(context.source, stream_id="other-stream"))):
            self.assertFalse(self.pipeline.publish(changed, result(changed)))
        self.assertFalse(self.pipeline.publish(context, replace(result(context), source=frame(stream="other").source)))
        self.assertEqual(self.pipeline.snapshot(), original)

    def test_duplicate_or_contradictory_second_result_is_never_a_second_publication(self):
        context = self.pipeline.advance(frame())
        self.pipeline.publish(context, result(context))
        original = self.pipeline.snapshot()
        self.assertFalse(self.pipeline.publish(context, result(context)))
        self.assertFalse(self.pipeline.publish(context, result(context, SceneEvidence(reason="not_attempted"))))
        self.assertEqual(self.pipeline.snapshot(), original)

    def test_invalid_processing_times_are_rejected(self):
        context = self.pipeline.advance(frame())
        for started, finished in ((999, 1020), (1030, 1020), (1010, 1060)):
            self.assertFalse(self.pipeline.publish(context, result(context, started_ms=started, finished_ms=finished)))

    def test_completion_before_unknown_barrier_is_rejected_without_retimestamping(self):
        context = self.pipeline.advance(frame())
        self.assertFalse(self.pipeline.publish(context, result(context, started_ms=1010, finished_ms=1020)))
        self.assertEqual(self.pipeline.stats()["rejections"]["completion_precedes_input_barrier"], 1)
        self.assertEqual(self.pipeline.snapshot()["scene"]["completed_host_monotonic_ms"], 1050)

    def test_changed_geometry_gets_new_context_and_actual_orientation(self):
        self.pipeline.advance(frame())
        previous = self.pipeline.snapshot()
        self.now_ms = 1150
        self.pipeline.advance(frame(2, 1100, data=jpeg((500, 800))))
        state = self.pipeline.snapshot()
        self.assert_valid(state, previous)
        self.assertEqual(state["context_epoch"], 1)
        self.assertEqual(state["frames"][0]["image"]["orientation"], "portrait")
        self.assertEqual(state["frames"][0]["image"]["rotation_applied_degrees"], 0)

    def test_invalid_frames_and_time_regressions_never_rewrite_sources(self):
        self.pipeline.advance(frame())
        original = self.pipeline.snapshot()
        for bad in (frame(), frame(2, 999), frame(2, 1051), frame(2, 1000, stream="other")):
            with self.assertRaises(FrameAdmissionRejected):
                self.pipeline.advance(bad)
        self.assertEqual(self.pipeline.snapshot(), original)
        self.now_ms = 1049
        with self.assertRaises(ValueError):
            self.pipeline.snapshot()

    def test_bad_image_revokes_a_prior_recognized_scene(self):
        context = self.pipeline.advance(frame())
        self.pipeline.publish(context, result(context))
        previous = self.pipeline.snapshot()
        self.now_ms = 1150
        with self.assertRaises(Exception):
            self.pipeline.advance(frame(2, 1100, data=b"not-an-image"))
        state = self.pipeline.snapshot()
        self.assert_valid(state, previous)
        self.assertEqual(state["scene"]["status"], "stale")
        self.assertEqual(state["frame_id"], previous["frame_id"])
        self.assertFalse(self.pipeline.publish(context, result(context)))

    def test_explicit_context_invalidation_seals_pending_results_until_next_frame(self):
        context = self.pipeline.advance(frame())
        self.pipeline.invalidate_context()
        self.assertFalse(self.pipeline.publish(context, result(context)))
        self.now_ms = 1150
        next_context = self.pipeline.advance(frame(2, 1100))
        self.assertEqual(next_context.context_epoch, 1)

    def test_ingress_notification_invalidates_before_latest_mailbox_delivery(self):
        context = self.pipeline.advance(frame())
        self.pipeline.publish(context, result(context))
        previous = self.pipeline.snapshot()
        self.now_ms = 1150
        self.pipeline.note_ingress(2)
        state = self.pipeline.snapshot()
        self.assert_valid(state, previous)
        self.assertEqual(state["frame_id"], "test-stream:1")
        self.assertEqual(state["scene"]["status"], "stale")
        self.assertFalse(self.pipeline.publish(context, result(context)))
        with self.assertRaises(FrameAdmissionRejected):
            self.pipeline.advance(frame(1, 1100))
        self.pipeline.advance(frame(2, 1100))
        self.assertEqual(self.pipeline.snapshot()["frame_id"], "test-stream:2")

    def test_ingress_before_old_delivery_cannot_be_undone_by_advancing_old_frame(self):
        self.pipeline.note_ingress(2)
        with self.assertRaises(FrameAdmissionRejected):
            self.pipeline.advance(frame())
        self.assertIsNone(self.pipeline.snapshot())

    def test_disconnect_immediately_revokes_scene_and_all_pending_work(self):
        context = self.pipeline.advance(frame())
        self.pipeline.publish(context, result(context))
        previous = self.pipeline.snapshot()
        self.pipeline.disconnect()
        state = self.pipeline.snapshot()
        self.assert_valid(state, previous)
        self.assertEqual(state["scene"]["reason"], "disconnected")
        self.assertFalse(self.pipeline.publish(context, result(context)))
        with self.assertRaises(FrameAdmissionRejected):
            self.pipeline.advance(frame(2, 1020))
        self.pipeline.disconnect()
        self.assertEqual(self.pipeline.snapshot(), state)

    def test_snapshots_and_input_regions_are_not_mutable_accepted_state(self):
        context = self.pipeline.advance(frame())
        decision = evidence()
        self.pipeline.publish(context, result(context, decision))
        expected = self.pipeline.snapshot()
        decision.region["x_min"] = 0.9
        copy = self.pipeline.snapshot()
        copy["scene"]["value"] = "battle"
        copy["frames"][0]["image"]["width_px"] = 1
        self.assertEqual(self.pipeline.snapshot(), expected)

    def test_schema_or_semantic_corruption_is_detected_by_owning_contract(self):
        self.pipeline.advance(frame())
        state = self.pipeline.snapshot()
        for mutation in (lambda s: s["shop"].pop(),
                         lambda s: s["gold"].update(value=42),
                         lambda s: s["gold"].update(observed_host_monotonic_ms=1001),
                         lambda s: s["frames"][0]["image"].update(orientation="portrait")):
            broken = deepcopy(state)
            mutation(broken)
            with self.assertRaises(ContractViolation):
                validate_state(broken, validator=self.validator)


class HistoryExperimentTests(unittest.TestCase):
    def setUp(self):
        self.image = image_metadata(frame())

    def test_positive_synthetic_cues_keep_only_region_and_fixed_evidence_codes(self):
        decision = history_evidence(HISTORY, self.image)
        self.assertEqual(decision.scene, "history")
        self.assertEqual(decision.ocr_items_count, 3)
        self.assertAlmostEqual(decision.region["x_min"], 50 / 800)
        self.assertNotIn(SECRET, repr(decision))
        self.assertNotIn("对局记录", repr(decision))

    def test_single_cue_wrong_location_duplicates_dialogs_and_portrait_abstain(self):
        cases = ([item("对局记录")], [item("最近对局", y=100)],
                 [item("对局记录", y=300), item("最近对局", y=400)],
                 [item("对局记录"), item("最近对局", y=10)],
                 HISTORY + [item("历史战绩", y=10)],
                 HISTORY + [item("确定", y=250)])
        for items in cases:
            with self.subTest(items=items):
                self.assertIsNone(history_evidence(items, self.image).scene)
        portrait = image_metadata(frame(data=jpeg((500, 800))))
        self.assertEqual(history_evidence(HISTORY, portrait).reason, "layout_unsupported")

    def test_bad_ocr_boxes_never_support_a_positive_result(self):
        for box in ({"width": 0}, {"x": -1}, {"height": float("nan")}, {"y": True}, {"width": 10000}):
            self.assertIsNone(history_evidence([{**HISTORY[0], **box}, HISTORY[1]], self.image).scene)

    def test_history_season_mode_and_numeric_labels_do_not_become_current_fields(self):
        extra = [item("S18"), item("排位赛"), item("金币50"), item("等级8"), item("血量80")]
        decision = history_evidence(HISTORY + extra, self.image)
        self.assertEqual(decision.scene, "history")
        self.assertEqual(set(decision.__dict__),
                         {"scene", "reason", "region", "evidence_codes", "ocr_items_count"})

    def test_disabled_recognizer_never_calls_ocr_and_failure_is_unknown(self):
        class FailOCR:
            def recognize(self, _):
                raise RuntimeError(SECRET)

        self.assertEqual(SceneRecognizer(FailOCR())(frame()).reason, "scene_recognizer_disabled")
        decision = SceneRecognizer(FailOCR(), experimental_history=True)(frame())
        self.assertEqual(decision.reason, "ocr_failed")
        self.assertNotIn(SECRET, repr(decision))


class RuntimeIntegrationTests(unittest.TestCase):
    def test_ingress_adapter_counts_match_latest_refs_and_rejects_slow_old_work(self):
        release_second, delivered_second = threading.Event(), threading.Event()

        # The source uses an event, not timing guesses, to deliver the newer
        # frame while a single synthetic OCR job is still pending.
        class TwoFrames:
            sent = 0

            def read_frame(self, timeout):
                if self.sent == 0:
                    self.sent += 1
                    return IMAGE
                if self.sent == 1:
                    if not release_second.wait(timeout):
                        raise TimeoutError("awaiting second")
                    self.sent += 1
                    delivered_second.set()
                    return IMAGE
                threading.Event().wait(timeout)
                raise TimeoutError("no frame")

            def close(self):
                pass

        wrapped = InvalidatingFrameSource(TwoFrames())
        stream = LatestFrameStream(wrapped, read_timeout=0.02)
        pipeline = StatePipeline(stream_id=stream.stream_id, sample_kind="synthetic", scene_ttl_ms=5000)
        wrapped.bind(pipeline)
        unblock_ocr, ocr_started = threading.Event(), threading.Event()

        def slow(old):
            ocr_started.set()
            self.assertTrue(unblock_ocr.wait(3))
            return FrameResult(old.source, old.source.host_received_monotonic_ns,
                               old.source.host_received_monotonic_ns, evidence())

        with stream, ThreadPoolExecutor(max_workers=1) as executor:
            old = stream.read_latest(timeout=1)
            ticket = pipeline.advance(old)
            pending = executor.submit(slow, old)
            self.assertTrue(ocr_started.wait(1))
            release_second.set()
            try:
                self.assertTrue(delivered_second.wait(1))
                newest = stream.read_latest(timeout=1)
                # Do NOT admit new metadata yet. Receipt already sealed the old
                # result, demonstrating the gap before latest-slot delivery.
                self.assertEqual(newest.source.sequence, 2)
                unblock_ocr.set()
                self.assertFalse(pipeline.publish(ticket, pending.result(1)))
                next_ticket = pipeline.advance(newest)
                self.assertEqual(next_ticket.context_epoch, 1)
            finally:
                unblock_ocr.set()
        state = pipeline.snapshot()
        validate_state(state)
        self.assertTrue(pipeline.stats()["disconnected"])
        self.assertEqual(pipeline.stats()["ingress_invalidations"], 2)
        self.assertFalse(stream.stats()["receiver_thread_alive"])

    def test_terminal_source_failure_revokes_state_without_printing_source_message(self):
        class BrokenSource:
            def read_frame(self, timeout):
                raise RuntimeError(SECRET)

            def close(self):
                pass

        pipeline = StatePipeline(stream_id="test-stream", sample_kind="synthetic")
        wrapped = InvalidatingFrameSource(BrokenSource())
        wrapped.bind(pipeline)
        with self.assertRaises(RuntimeError):
            wrapped.read_frame()
        self.assertTrue(pipeline.stats()["disconnected"])
        self.assertIsNone(pipeline.snapshot())

    def test_synthetic_cli_probe_is_structurally_valid_and_explicitly_not_current(self):
        report = run_probe(source_name="synthetic", experimental_history=True)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["validation"], "schema_and_semantics_passed")
        self.assertEqual(report["sample_kind"], "synthetic")
        self.assertFalse(report["observation_is_runtime_current"])
        observation = report["single_frame_observations"][0]
        self.assertTrue(observation["synthetic_ocr"])
        self.assertEqual(observation["scene_observed"], "history")
        self.assertEqual(report["state"]["sample_kind"], "synthetic")
        self.assertNotEqual(report["state"]["scene"]["status"], "recognized")
        self.assertTrue(report["pipeline_stats"]["disconnected"])
        self.assertEqual(report["images_saved"], 0)
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("对局记录", serialized)
        self.assertNotIn("最近对局", serialized)
        self.assertNotIn(SECRET, serialized)
        validate_state(report["state"])


if __name__ == "__main__":
    unittest.main()
