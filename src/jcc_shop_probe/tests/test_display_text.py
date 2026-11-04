"""Synthetic-only tests for the unclassified shop display-text path."""

import io
import math
import unittest

from PIL import Image

from jcc_shop_probe import Rect, ShopLayout, SlotLayout, probe_shop_display_text
from jcc_shop_probe.display_text import CANDIDATE, UNKNOWN, _DisplayTextView, _ViewIdentity, _reconcile


def layout(scales=(1, 2)):
    return ShopLayout("synthetic-display-text", (520, 110), tuple(
        SlotLayout(i + 1, Rect((x, 5, x + 100, 105)), Rect((x + 5, 75, x + 65, 100)),
                   Rect((x + 75, 75, x + 95, 100)), Rect((x + 5, 10, x + 95, 65)))
        for i, x in enumerate(range(0, 520, 104))
    ), scales=scales)


def image():
    out = io.BytesIO()
    Image.new("RGB", (520, 110), (80, 70, 60)).save(out, format="PNG")
    return out.getvalue()


def box(text, scale, *, x=10, y=74, width=40, height=12, confidence=1.0):
    return {"text": text, "confidence": confidence, "x": x * scale, "y": y * scale,
            "width": width * scale, "height": height * scale}


class FakeOCR:
    def __init__(self, responses):
        self.responses, self.calls = responses, 0

    def recognize(self, _data):
        value = self.responses[self.calls]
        self.calls += 1
        if isinstance(value, BaseException):
            raise value
        return value


def responses(first_one, first_two):
    return [value for slot in range(1, 6) for value in (
        first_one if slot == 1 else [], first_two if slot == 1 else []
    )]


class ShopDisplayTextTests(unittest.TestCase):
    def probe(self, first_one, first_two, *, shop_layout=None):
        backend = FakeOCR(responses(first_one, first_two))
        result = probe_shop_display_text(image(), layout=shop_layout or layout(), ocr=backend)
        return result, backend

    def test_exact_two_scale_text_is_unclassified_candidate(self):
        result, backend = self.probe([box("虚 构 显 示", 1)], [box("虚构显示", 2)])
        slot = result.slots[0]
        self.assertEqual(slot.status, CANDIDATE)
        self.assertEqual(slot.display_text_normalized_candidate, "虚构显示")
        self.assertEqual(slot.display_kind, "unclassified")
        self.assertEqual(slot.identity_status, "unknown")
        self.assertEqual(slot.price_status, "unknown")
        self.assertEqual(slot.buyability, "unknown")
        self.assertEqual(slot.trait_binding, "unknown")
        self.assertEqual(backend.calls, 10)
        summary = result.summary()
        self.assertEqual(summary["source_authentication"], "not_authenticated")
        self.assertNotIn("zone", str(summary))
        self.assertFalse(summary["actionable"])

    def test_crossing_box_is_not_ignored_when_other_text_is_inside(self):
        result, _ = self.probe([box("虚构显示", 1), box("越界", 1, x=2)], [box("虚构显示", 2)])
        self.assertEqual(result.slots[0].status, UNKNOWN)
        self.assertIn("text_crosses_roi_boundary", result.slots[0].reasons)

    def test_box_touching_name_zone_edge_is_unknown_even_when_not_at_card_edge(self):
        result, _ = self.probe([box("虚构显示", 1, x=5)], [box("虚构显示", 2, x=5)])
        self.assertEqual(result.slots[0].status, UNKNOWN)
        self.assertIn("text_touches_roi_edge", result.slots[0].reasons)

    def test_regular_ocr_backend_exceptions_become_unknown(self):
        for error in (RuntimeError("synthetic"), IndexError("synthetic"),
                      KeyError("synthetic"), Exception("synthetic")):
            with self.subTest(error=type(error).__name__):
                result, _ = self.probe(error, [box("虚构显示", 2)])
                self.assertEqual(result.slots[0].status, UNKNOWN)
                self.assertIn("ocr_error", result.slots[0].reasons)

    def test_malformed_nonfinite_or_huge_numeric_ocr_is_unknown(self):
        cases = {
            "nan_confidence": box("虚构显示", 1, confidence=math.nan),
            "huge_coordinate": box("虚构显示", 1, x=10**10000),
            "huge_confidence": box("虚构显示", 1, confidence=10**10000),
            "negative_width": {"text": "虚构显示", "confidence": 1, "x": 1, "y": 1,
                               "width": -1, "height": 1},
        }
        for label, bad in cases.items():
            with self.subTest(case=label):
                result, _ = self.probe([bad], [box("虚构显示", 2)])
                self.assertEqual(result.slots[0].status, UNKNOWN)
                self.assertIn("invalid_ocr_response", result.slots[0].reasons)

    def test_reconcile_rejects_same_slot_with_different_zone(self):
        first = _DisplayTextView(_ViewIdentity("a" * 64, (0, 0, 100, 100),
                                               (5, 70, 65, 95), 1, 1),
                                 CANDIDATE, "虚构显示", (), 1.0)
        different_zone = _DisplayTextView(_ViewIdentity("a" * 64, (0, 0, 100, 100),
                                                        (6, 70, 66, 95), 2, 1),
                                          CANDIDATE, "虚构显示", (), 1.0)
        outcome = _reconcile([first, different_zone])
        self.assertEqual(outcome.status, UNKNOWN)
        self.assertEqual(outcome.reasons, ("invalid_related_view_contract",))

    def test_rejects_duplicate_scale_and_does_not_call_ocr(self):
        backend = FakeOCR([])
        result = probe_shop_display_text(image(), layout=layout((1, 2, 3)), ocr=backend)
        self.assertEqual(backend.calls, 0)
        self.assertTrue(all("display_text_requires_exact_scales_1_and_2" in s.reasons
                            for s in result.slots))


if __name__ == "__main__":
    unittest.main()
