"""Controlled RGB images, clocks and workers; never opens a device.

Optional standalone timing: PYTHONPATH=src .venv/bin/python -m
jcc_device.tests.test_stability --benchmark
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import json
import math
import queue
import statistics
import sys
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, PngImagePlugin

from jcc_device.latest import Frame, FrameRef
from jcc_device.recognition import FrameProcessor, FrameResult, PixelROI
from jcc_device.stability import (ContextEvidence, ExactPixelContinuity,
                                  RegionCalibration, StableRecognitionScheduler)


class Clock:
    def __init__(self):
        self.now = 10_000_000_000

    def __call__(self):
        return self.now

    def at(self, seconds):
        self.now = 10_000_000_000 + round(seconds * 1_000_000_000)


def calibration(size=(64, 48)):
    width, height = size
    return RegionCalibration(
        "synthetic-calibration", "synthetic-test-only", size,
        (PixelROI("synthetic-target", size, (width // 4, height // 4, width // 2, height // 2)),),
        (PixelROI("synthetic-context", size, (0, 0, width, height // 8)),))


def encoded(*, size=(64, 48), changed=None, mode="RGB", metadata=None):
    with Image.new(mode, size, "white") as image, io.BytesIO() as output:
        if changed is not None:
            image.putpixel(changed, (254, 255, 255))  # One channel, one unit.
        image.save(output, format="PNG", pnginfo=metadata)
        return output.getvalue()


def frame(clock, sequence=1, *, stream_id="synthetic-stream", jpeg=None, **image_options):
    return Frame(FrameRef(stream_id, sequence, clock(), "2026-11-02T00:00:00+00:00"),
                 encoded(**image_options) if jpeg is None else jpeg)


def evidence(input_frame, **changes):
    value = ContextEvidence(input_frame.source, "synthetic-clock", 0, "synthetic-scene-a",
                            "synthetic-calibration", "synthetic-context-verifier-v1",
                            "landscape", 0, True)
    return replace(value, **changes)


def completed(clock, ticket, value="synthetic-value"):
    return FrameResult(ticket.source, ticket.source.host_received_monotonic_ns, clock(), value)


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Condition did not become true before timeout")
        time.sleep(0.002)


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.tracker = self.make_tracker()

    def make_tracker(self, **changes):
        options = dict(host_clock_id="synthetic-clock", max_result_age_seconds=2,
                       max_frame_age_seconds=0.5, clock_ns=self.clock)
        options.update(changes)
        return ExactPixelContinuity(calibration(), **options)

    def begin(self, **options):
        first = frame(self.clock, **options)
        self.assertEqual(self.tracker.observe(first, evidence(first)).status, "ready")
        ticket = self.tracker.ticket()
        self.assertIsNotNone(ticket)
        return first, ticket

    def observe(self, sequence, seconds, **image_options):
        self.clock.at(seconds)
        item = frame(self.clock, sequence, **image_options)
        return self.tracker.observe(item, evidence(item))

    def test_4fps_1450ms_old_result_keeps_its_source_and_deadline(self):
        first, ticket = self.begin()
        for sequence in range(2, 7):
            self.assertEqual(self.observe(sequence, (sequence - 1) / 4).reason, "unchanged")
        self.clock.at(1.45)
        result = completed(self.clock, ticket)
        decision = self.tracker.validate_result(ticket, result)
        self.assertEqual(decision.status, "proven")
        proof = decision.proof
        self.assertEqual(proof.source, first.source)
        self.assertEqual(proof.verified_through.sequence, 6)
        self.assertEqual(proof.checked_frame_count, 6)
        self.assertEqual(proof.expires_host_monotonic_ns, 12_000_000_000)
        for sequence in (7, 8, 9):
            self.observe(sequence, (sequence - 1) / 4)
        self.assertEqual(self.tracker.validate_result(ticket, result).reason, "result_expired")
        fresh_ticket = self.tracker.ticket()
        self.assertGreater(fresh_ticket.expires_host_monotonic_ns, ticket.expires_host_monotonic_ns)
        self.assertEqual(fresh_ticket.source.sequence, 9)  # A genuinely new OCR input.

    def test_every_relevant_and_context_pixel_change_revokes(self):
        for point in ((16, 12), (31, 23), (0, 0), (63, 5)):
            with self.subTest(point=point):
                self.setUp()
                _, ticket = self.begin()
                self.assertEqual(self.observe(2, 0.25, changed=point).reason, "pixels_changed")
                self.assertEqual(self.tracker.validate_result(ticket, completed(self.clock, ticket)).status,
                                 "unknown")
                self.assertIsNotNone(self.tracker.ticket())  # New OCR may inspect the new pixels.

    def test_a_b_a_never_revives_an_old_ticket(self):
        _, ticket = self.begin()
        self.observe(2, 0.25, changed=(16, 12))
        self.observe(3, 0.5)
        self.assertEqual(self.tracker.validate_result(ticket, completed(self.clock, ticket)).reason,
                         "continuity_revoked")

    def test_changes_outside_coverage_are_not_a_full_screen_claim(self):
        _, ticket = self.begin()
        self.assertEqual(self.observe(2, 0.25, changed=(63, 47)).reason, "unchanged")
        self.assertEqual(self.tracker.proof_for(ticket).status, "proven")

    def test_encoded_metadata_is_not_mistaken_for_pixel_change(self):
        _, ticket = self.begin()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("synthetic-description", "different container, same RGB pixels")
        self.observe(2, 0.25, jpeg=encoded(metadata=metadata))
        self.assertEqual(self.tracker.proof_for(ticket).status, "proven")

    def test_gap_rejects_and_next_frame_can_only_start_new_proof(self):
        _, ticket = self.begin()
        check = self.observe(3, 0.25)
        self.assertEqual((check.status, check.reason), ("unknown", "sequence_gap"))
        self.assertIsNone(self.tracker.ticket())
        self.observe(4, 0.5)
        self.assertEqual(self.tracker.proof_for(ticket).status, "unknown")
        new_ticket = self.tracker.ticket()
        self.assertEqual(self.tracker.proof_for(new_ticket).proof.checked_frame_count, 1)

    def test_duplicate_and_reordered_frames_revoke_not_silently_ignore(self):
        for sequence in (1, 2):
            with self.subTest(sequence=sequence):
                self.setUp()
                _, ticket = self.begin()
                self.observe(2, 0.25)
                check = self.observe(sequence, 0.26)
                self.assertEqual(check.reason, "out_of_order_or_duplicate")
                self.assertIsNone(self.tracker.ticket())
                self.observe(3, 0.5)
                self.assertEqual(self.tracker.proof_for(ticket).status, "unknown")

    def test_no_calibration_is_unknown_and_empty_coverage_is_invalid(self):
        tracker = ExactPixelContinuity(None, host_clock_id="synthetic-clock",
                                       max_result_age_seconds=2, max_frame_age_seconds=0.5,
                                       clock_ns=self.clock)
        item = frame(self.clock)
        self.assertEqual(tracker.observe(item, evidence(item)).reason, "regions_unconfigured")
        self.assertIsNone(tracker.ticket())
        for attribute in ("relevant_regions", "context_regions"):
            with self.subTest(attribute=attribute), self.assertRaises(ValueError):
                replace(calibration(), **{attribute: ()})

    def test_policy_and_calibration_cannot_be_changed_to_renew_a_ticket(self):
        _, ticket = self.begin()
        for attribute, value in (("max_result_age_ns", 99_000_000_000),
                                 ("max_frame_age_ns", 99_000_000_000),
                                 ("calibration", calibration()), ("host_clock_id", "other")):
            with self.subTest(attribute=attribute), self.assertRaises(AttributeError):
                setattr(self.tracker, attribute, value)
        renewed = replace(ticket, expires_host_monotonic_ns=ticket.expires_host_monotonic_ns + 1)
        self.assertEqual(self.tracker.proof_for(renewed).reason, "invalid_ticket_deadline")

    def test_missing_or_wrong_frame_context_and_visibility_reject(self):
        changes = (None, {"context_key": None}, {"verification_ref": None},
                   {"context_epoch": -1}, {"unobscured": None}, {"unobscured": False},
                   {"calibration_id": "not-the-calibration"},
                   {"orientation": "portrait"}, {"applied_rotation_degrees": 180})
        for change in changes:
            with self.subTest(change=change):
                self.setUp()
                _, ticket = self.begin()
                self.clock.at(0.25)
                item = frame(self.clock, 2)
                context = None if change is None else evidence(item, **change)
                self.assertEqual(self.tracker.observe(item, context).status, "unknown")
                self.assertEqual(self.tracker.proof_for(ticket).status, "unknown")
                self.assertIsNone(self.tracker.ticket())
        self.setUp()
        first, ticket = self.begin()
        self.clock.at(0.25)
        self.assertEqual(self.tracker.observe(frame(self.clock, 2), evidence(first)).reason,
                         "context_unproven")
        self.assertEqual(self.tracker.proof_for(ticket).status, "unknown")

    def test_context_changes_and_context_a_b_a_break_old_tickets(self):
        for changes in ({"context_key": "synthetic-scene-b"}, {"context_epoch": 1},
                        {"verification_ref": "synthetic-context-verifier-v2"}):
            with self.subTest(changes=changes):
                self.setUp()
                _, ticket = self.begin()
                self.clock.at(0.25)
                second = frame(self.clock, 2)
                self.assertEqual(self.tracker.observe(second, evidence(second, **changes)).reason,
                                 "context_changed")
                self.clock.at(0.5)
                third = frame(self.clock, 3)
                self.tracker.observe(third, evidence(third))
                self.assertEqual(self.tracker.proof_for(ticket).status, "unknown")

    def test_geometry_decode_and_pixel_encoding_fail_closed(self):
        for options, reason in (({"size": (48, 64)}, "geometry_mismatch"),
                                ({"size": (80, 60)}, "geometry_mismatch"),
                                ({"jpeg": b"not an image"}, "decode_failed"),
                                ({"mode": "RGBA"}, "unsupported_image_encoding")):
            with self.subTest(options=options):
                self.setUp()
                _, ticket = self.begin()
                self.assertEqual(self.observe(2, 0.25, **options).reason, reason)
                self.assertEqual(self.tracker.proof_for(ticket).status, "unknown")

    def test_session_and_clock_domain_changes_are_terminal(self):
        for change in ("session", "clock"):
            with self.subTest(change=change):
                self.setUp()
                _, ticket = self.begin()
                self.clock.at(0.25)
                item = frame(self.clock, 2, stream_id="other" if change == "session" else "synthetic-stream")
                context = evidence(item, host_clock_id="other" if change == "clock" else "synthetic-clock")
                self.assertEqual(self.tracker.observe(item, context).status, "unknown")
                self.observe(3, 0.5)
                self.assertIsNone(self.tracker.ticket())
                self.assertEqual(self.tracker.proof_for(ticket).status, "unknown")

    def test_receive_time_regression_future_time_and_clock_regression(self):
        for received in (self.clock() - 1, self.clock() + 1):
            with self.subTest(received=received):
                self.setUp()
                first, ticket = self.begin()
                invalid = replace(first, source=replace(first.source, sequence=2,
                                                       host_received_monotonic_ns=received))
                self.assertEqual(self.tracker.observe(invalid, evidence(invalid)).reason,
                                 "invalid_receive_time")
                self.assertEqual(self.tracker.proof_for(ticket).status, "unknown")
        self.setUp()
        _, ticket = self.begin()
        self.clock.now -= 1
        self.assertEqual(self.tracker.proof_for(ticket).reason, "clock_regressed_or_invalid")
        self.clock.at(0.25)
        self.observe(2, 0.25)
        self.assertIsNone(self.tracker.ticket())

    def test_frame_silence_timeout_is_not_restored_when_matching_frames_resume(self):
        _, ticket = self.begin()
        self.clock.at(0.5)
        self.assertEqual(self.tracker.proof_for(ticket).reason, "frame_timeout")
        self.observe(2, 0.51)
        self.assertEqual(self.tracker.proof_for(ticket).status, "unknown")
        self.assertIsNotNone(self.tracker.ticket())

    def test_unpolled_silence_also_breaks_the_segment(self):
        _, ticket = self.begin()
        self.observe(2, 0.51)
        self.assertEqual(self.tracker.proof_for(ticket).status, "unknown")

    def test_result_source_processing_time_foreign_ticket_and_disconnect(self):
        first, ticket = self.begin()
        result = completed(self.clock, ticket)
        other = self.make_tracker()
        other.observe(first, evidence(first))
        self.assertEqual(other.proof_for(ticket).reason, "foreign_ticket")
        bad_source = replace(result, source=replace(first.source, sequence=2))
        self.assertEqual(self.tracker.validate_result(ticket, bad_source).reason, "result_source_mismatch")
        for started, finished in ((self.clock() - 1, self.clock()),
                                  (self.clock(), self.clock() - 1),
                                  (self.clock(), self.clock() + 1)):
            invalid = replace(result, processing_started_monotonic_ns=started,
                              processing_finished_monotonic_ns=finished)
            self.assertEqual(self.tracker.validate_result(ticket, invalid).reason,
                             "invalid_processing_time")
        self.tracker.disconnect()
        self.assertEqual(self.tracker.validate_result(ticket, result).reason, "disconnected")
        self.observe(2, 0.25)
        self.assertIsNone(self.tracker.ticket())

    def test_decoding_in_progress_blocks_publication_until_check_finishes(self):
        _, ticket = self.begin()
        self.clock.at(0.25)
        second = frame(self.clock, 2, changed=(16, 12))
        entered, release, validating = threading.Event(), threading.Event(), threading.Event()
        original_open = Image.open

        def blocked_open(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("Test image decoder was not released")
            return original_open(*args, **kwargs)

        def validate():
            validating.set()
            return self.tracker.validate_result(ticket, completed(self.clock, ticket))

        with patch("PIL.Image.open", blocked_open), ThreadPoolExecutor(max_workers=2) as pool:
            checking = pool.submit(self.tracker.observe, second, evidence(second))
            try:
                self.assertTrue(entered.wait(1))
                publication = pool.submit(validate)
                self.assertTrue(validating.wait(1))
                self.assertFalse(publication.done())
            finally:
                release.set()
            self.assertEqual(checking.result(1).reason, "pixels_changed")
            self.assertEqual(publication.result(1).status, "unknown")

    def test_stats_measure_check_work_without_private_payload(self):
        _, ticket = self.begin()
        self.observe(2, 0.25)
        counts = self.tracker.stats()
        self.assertEqual((counts["frames_checked"], counts["frames_decoded"], counts["frames_ready"]),
                         (2, 2, 2))
        self.assertGreater(counts["check_ns_total"], 0)
        self.assertGreater(counts["check_ns_max"], 0)
        self.assertGreater(counts["region_bytes_extracted"], 0)
        self.assertNotIn("jpeg", repr(counts))
        self.assertNotIn("_owner", repr(ticket))


class Work:
    def __init__(self, item, started):
        self.frame, self.started = item, started
        self.release = threading.Event()
        self.error = None


class ControlledProcessor:
    def __init__(self, clock):
        self.clock = clock
        self.requests = queue.Queue()
        self.shutdown = threading.Event()

    def process(self, item):
        work = Work(item, self.clock())
        self.requests.put(work)
        deadline = time.monotonic() + 3
        while not work.release.wait(0.005) and not self.shutdown.is_set():
            if time.monotonic() >= deadline:
                raise AssertionError("Test recognition was not released")
        if work.error is not None:
            raise work.error
        return FrameResult(item.source, work.started, self.clock(), "synthetic-recognition")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.tracker = ExactPixelContinuity(calibration(), host_clock_id="synthetic-clock",
                                            max_result_age_seconds=2, max_frame_age_seconds=0.5,
                                            clock_ns=self.clock)
        self.processor = ControlledProcessor(self.clock)
        self.scheduler = StableRecognitionScheduler(self.tracker, self.processor).start()
        self.addCleanup(self.stop)

    def stop(self):
        self.scheduler.disconnect()
        self.processor.shutdown.set()
        self.scheduler.close()

    def receive(self, sequence, seconds, **image_options):
        self.clock.at(seconds)
        item = frame(self.clock, sequence, **image_options)
        return self.scheduler.observe(item, evidence(item))

    def first_job(self):
        self.receive(1, 0)
        return self.processor.requests.get(timeout=1)

    def test_4fps_slow_worker_publishes_at_1450ms_and_pending_is_latest_only(self):
        work = self.first_job()
        for sequence in range(2, 7):
            self.receive(sequence, (sequence - 1) / 4)
        self.assertEqual(self.scheduler.current().status, "unknown")
        self.assertEqual(self.scheduler.stats()["jobs_started"], 1)
        self.assertEqual(self.scheduler.stats()["pending_frames"], 1)
        self.clock.at(1.45)
        work.release.set()
        next_work = self.processor.requests.get(timeout=1)
        current = self.scheduler.current()
        self.assertEqual(current.status, "available")
        self.assertEqual(current.result.source.sequence, 1)
        self.assertAlmostEqual(current.result.timing()["receive_to_finish_seconds"], 1.45)
        self.assertEqual(current.proof.checked_frame_count, 6)
        self.assertEqual(next_work.frame.source.sequence, 6)
        counts = self.scheduler.stats()
        self.assertEqual((counts["max_active_jobs"], counts["max_pending_frames"],
                          counts["pending_replaced"]), (1, 1, 4))
        for sequence in (7, 8):
            self.receive(sequence, (sequence - 1) / 4)
        self.clock.at(2)
        self.assertEqual(self.scheduler.current().reason, "result_expired")

    def test_changed_intermediate_frame_cannot_be_hidden_by_pending_replacement(self):
        work = self.first_job()
        self.receive(2, 0.25, changed=(16, 12))
        self.receive(3, 0.5)
        self.clock.at(0.6)
        work.release.set()
        next_work = self.processor.requests.get(timeout=1)
        self.assertEqual(next_work.frame.source.sequence, 3)
        self.assertEqual(self.scheduler.current().status, "unknown")
        self.assertEqual(self.scheduler.stats()["results_rejected"], 1)
        next_work.release.set()
        eventually(lambda: self.scheduler.current().status == "available")
        self.assertEqual(self.scheduler.current().result.source.sequence, 3)

    def test_published_value_is_revoked_on_context_change_and_missing_context(self):
        work = self.first_job()
        work.release.set()
        eventually(lambda: self.scheduler.current().status == "available")
        self.clock.at(0.25)
        second = frame(self.clock, 2)
        self.scheduler.observe(second, evidence(second, context_key="synthetic-scene-b"))
        self.assertEqual(self.scheduler.current().status, "unknown")
        self.clock.at(0.5)
        third = frame(self.clock, 3)
        self.scheduler.observe(third, None)
        self.assertEqual(self.scheduler.current().reason, "context_unproven")

    def test_unchecked_middle_frames_from_latest_mailbox_are_rejected(self):
        work = self.first_job()
        self.assertEqual(self.receive(6, 1.25).reason, "sequence_gap")
        work.release.set()
        eventually(lambda: self.scheduler.stats()["jobs_completed"] == 1)
        self.assertEqual(self.scheduler.current().status, "unknown")
        self.assertEqual(self.scheduler.stats()["results_accepted"], 0)

    def test_over_deadline_job_rejected_despite_new_identical_frames(self):
        work = self.first_job()
        for sequence in range(2, 10):
            self.receive(sequence, (sequence - 1) / 4)
        work.release.set()
        self.processor.requests.get(timeout=1)
        self.assertEqual(self.scheduler.current().status, "unknown")
        self.assertEqual(self.scheduler.stats()["results_rejected"], 1)

    def test_processor_failure_is_redacted_and_worker_can_process_latest_pending(self):
        work = self.first_job()
        self.receive(2, 0.25)
        work.error = ValueError("private-ocr-text-and-device-id")
        work.release.set()
        next_work = self.processor.requests.get(timeout=1)
        self.assertEqual(self.scheduler.current().reason, "recognition_failed")
        counts = self.scheduler.stats()
        self.assertEqual(counts["processing_error_type"], "ValueError")
        self.assertNotIn("private-ocr", repr(counts))
        next_work.release.set()
        eventually(lambda: self.scheduler.current().status == "available")

    def test_disconnect_revokes_and_close_timeout_reports_living_worker(self):
        work = self.first_job()
        self.receive(2, 0.25)
        with self.assertRaises(TimeoutError):
            self.scheduler.close(timeout_seconds=0.001)
        self.assertEqual(self.scheduler.current().reason, "disconnected")
        counts = self.scheduler.stats()
        self.assertTrue(counts["worker_alive"])
        self.assertEqual(counts["pending_frames"], 0)
        work.release.set()
        self.scheduler.close()
        self.assertFalse(self.scheduler.stats()["worker_alive"])
        self.assertEqual(self.scheduler.stats()["results_accepted"], 0)
        self.assertEqual(self.receive(3, 0.5).status, "unknown")
        with self.assertRaises(RuntimeError):
            self.scheduler.start()

    def test_original_frame_processor_works_without_adapter_or_device(self):
        self.stop()
        tracker = ExactPixelContinuity(calibration(), host_clock_id="real-host-clock",
                                       max_result_age_seconds=2, max_frame_age_seconds=0.5)
        with StableRecognitionScheduler(tracker, FrameProcessor(lambda item: "synthetic")) as scheduler:
            item = frame(time.monotonic_ns)
            scheduler.observe(item, evidence(item, host_clock_id="real-host-clock"))
            eventually(lambda: scheduler.current().status == "available")
            self.assertEqual(scheduler.current().result.source, item.source)


def benchmark_checks(size=(2560, 1600), frames=120):
    """JPEG decode+crop+exact checks; no sleep, OCR, device or pass threshold."""
    configuration = calibration(size)
    with Image.new("RGB", size, "white") as image, io.BytesIO() as output:
        draw = ImageDraw.Draw(image)
        for index in range(32):
            draw.rectangle((index * size[0] // 32, 0, (index + 1) * size[0] // 32 - 1, size[1] - 1),
                           fill=((index * 37) % 256, (index * 59) % 256, (index * 83) % 256))
        image.save(output, format="JPEG", quality=90)
        payload = output.getvalue()
    tracker = ExactPixelContinuity(configuration, host_clock_id="synthetic-benchmark-clock",
                                   max_result_age_seconds=10, max_frame_age_seconds=10)
    durations = []
    for sequence in range(1, frames + 1):
        item = frame(time.monotonic_ns, sequence, jpeg=payload)
        check = tracker.observe(item, evidence(item, host_clock_id="synthetic-benchmark-clock"))
        if check.status != "ready":
            raise AssertionError(check.reason)
        durations.append(check.check_elapsed_ns / 1_000_000)
    selected_pixels = sum((roi.box[2] - roi.box[0]) * (roi.box[3] - roi.box[1])
                          for roi in configuration.relevant_regions + configuration.context_regions)
    return dict(image_size=list(size), frames=frames, jpeg_bytes=len(payload),
                selected_pixels=selected_pixels, baseline_region_bytes=selected_pixels * 3,
                check_ms_median=round(statistics.median(durations), 3),
                check_ms_p95=round(sorted(durations)[math.ceil(frames * 0.95) - 1], 3),
                check_ms_max=round(max(durations), 3), continuity=tracker.stats())


def timed_stream_demo():
    """Real wall-time 4 Hz synthetic input and an approximately 1.45 s worker."""
    tracker = ExactPixelContinuity(calibration(), host_clock_id="synthetic-wall-clock",
                                   max_result_age_seconds=2, max_frame_age_seconds=0.5)
    shutdown = threading.Event()

    def slow(_):
        shutdown.wait(1.45)
        return "synthetic-slow-result"

    scheduler = StableRecognitionScheduler(tracker, FrameProcessor(slow)).start()
    payload = encoded()
    start = time.monotonic()
    next_frame = start
    sequence = 0
    checks = []
    first = None
    first_deadline = None
    saw_expiration = False
    try:
        while time.monotonic() - start < 3.05:
            now = time.monotonic()
            if now >= next_frame:
                sequence += 1
                item = frame(time.monotonic_ns, sequence, jpeg=payload)
                check = scheduler.observe(item, evidence(item, host_clock_id="synthetic-wall-clock"))
                if check.status != "ready":
                    raise AssertionError(check.reason)
                checks.append(check.check_elapsed_ns / 1_000_000)
                next_frame += 0.25
            read = scheduler.current()
            if first is None and read.status == "available":
                first_deadline = read.proof.expires_host_monotonic_ns
                first = dict(source_sequence=read.result.source.sequence,
                             verified_through_sequence=read.proof.verified_through.sequence,
                             checked_frames=read.proof.checked_frame_count,
                             processing_seconds=round(read.result.timing()["processing_seconds"], 4),
                             receive_to_finish_seconds=round(read.result.timing()["receive_to_finish_seconds"], 4),
                             available_after_receive_seconds=round((time.monotonic_ns() -
                                 read.result.source.host_received_monotonic_ns) / 1e9, 4),
                             original_lifetime_seconds=round((first_deadline -
                                 read.result.source.host_received_monotonic_ns) / 1e9, 4))
            if first_deadline is not None and time.monotonic_ns() >= first_deadline and read.status == "unknown":
                saw_expiration = True
            time.sleep(0.002)
        if first is None or not saw_expiration:
            raise AssertionError("Synthetic result availability or expiry was not observed")
        stats = scheduler.stats()
        return dict(input="synthetic RGB PNG; no device or OCR", target_input_fps=4,
                    frames_supplied=sequence, first_available=first,
                    saw_unknown_after_original_deadline=saw_expiration,
                    check_ms_median=round(statistics.median(checks), 3), scheduler=stats)
    finally:
        scheduler.disconnect()
        shutdown.set()
        scheduler.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--benchmark"]:
        print(json.dumps([benchmark_checks((1280, 800)), benchmark_checks()], indent=2))
    elif sys.argv[1:] == ["--stream-demo"]:
        print(json.dumps(timed_stream_demo(), indent=2))
    else:
        unittest.main()
