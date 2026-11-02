"""Offline replay contract: candidate ambiguity must never light the hint."""

from types import SimpleNamespace
import unittest

from jcc_device.latest import Frame, FrameRef
from jcc_realtime_probe.processing import ProcessingResult
from jcc_realtime_probe.profile import load_profile
from jcc_realtime_probe.replay_ocr import ReplayGoldOCR
from jcc_realtime_probe.timeline import png
from jcc_realtime_overlay_bridge.replay import replay_report
from jcc_realtime_overlay_bridge.synthetic import InProcessPipeline


class Answers:
    def __init__(self, answers):
        self.answers = iter(answers)

    def recognize(self, _data):
        text = next(self.answers)
        if text is None:
            return []
        return [dict(text=text, x=2, y=2, width=5, height=5, confidence=.99)]


class ReplayTests(unittest.TestCase):
    def test_numeric_agreement_and_disagreement_keep_actual_ocr_counts(self):
        profile = load_profile(dict(image_size=[160, 100], input_boxes=[[10, 10, 70, 40]],
                                    numeric_fields=[dict(field_name="gold", box=[10, 10, 70, 40])]), "replay")
        job = SimpleNamespace(frame=Frame(FrameRef("replay-fixture", 1, 1, "synthetic"), png()))
        for answers, outcome, status, items in ((["1", "1"], "ocr_text", "development_candidate", 2),
                                                 (["1", "2"], "ocr_unresolved", "unknown", 2),
                                                 ([None, None], "ocr_empty", "unknown", 0)):
            with self.subTest(answers=answers):
                report = ReplayGoldOCR(profile, Answers(answers))(job)
                self.assertEqual((report["processing_outcome"], report["numeric_status"]), (outcome, status))
                self.assertEqual((report["ocr_requests"], report["ocr_items_count"], report["ocr_errors"]),
                                 (2, items, 0))
                self.assertNotIn("candidate_value", report)

    def test_unresolved_after_candidate_withdraws_and_never_becomes_ready(self):
        p = InProcessPipeline(source_kind="replay", processing_kind="ocr")
        p.receive()
        p.complete(p.core.start_job(), report=ProcessingResult("ocr", "ocr_text", 2, 2).fields())
        self.assertEqual(p.bridge.store.view()["status"], "ready")
        p.clock.now += 250_000_000
        p.receive(outside=1)
        self.assertIsNone(p.bridge.store.view()["message"])
        p.complete(p.core.start_job(), report=ProcessingResult("ocr", "ocr_unresolved", 2, 2).fields())
        view = p.bridge.store.view()
        self.assertEqual(view["status"], "no_result")
        self.assertIsNone(view["message"])
        self.assertEqual(p.bridge.report()["processing"]["outcome_counts"]["ocr_unresolved"], 1)
        p.bridge.store.close()

    def test_expired_candidate_withdraws(self):
        p = InProcessPipeline(source_kind="replay", processing_kind="ocr")
        p.receive()
        p.complete(p.core.start_job(), report=ProcessingResult("ocr", "ocr_text", 2, 2).fields())
        p.clock.now += 1_000_000_000
        p.core.tick()
        p.bridge.tick()
        self.assertIsNone(p.bridge.store.view()["message"])
        p.bridge.store.close()

    def test_string_nanoseconds_are_compared_as_integers(self):
        sample = dict(sample_id="replay:1", first_dom_report_received_ns="11000000",
                      first_paint_report_received_ns="13000000")
        service = SimpleNamespace(
            bridge=SimpleNamespace(report=lambda: {}), core=SimpleNamespace(summary=lambda: {}),
            store=SimpleNamespace(report=lambda: {"samples": [sample]}),
            frames=SimpleNamespace(reads=[dict(sequence=1, started_ns=1000000, completed_ns=2000000)], rows=[1]),
            errors=[])
        report = replay_report(service)
        self.assertEqual(report["read_start_to_dom_report_received_ms"]["median_ms"], 10)
        self.assertEqual(report["read_start_to_paint_report_received_ms"]["median_ms"], 12)


if __name__ == "__main__":
    unittest.main()
