"""Synthetic pixels and fake OCR only; no real samples or truth labels."""

import hashlib
import io
import json
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw

from jcc_shop_probe import (ExperimentalPriceProcessing, LexiconEntry, Rect, ShopLayout,
                            SlotLayout, probe_shop)
from jcc_shop_probe.private_cli import _external


def layout():
    return ShopLayout("synthetic-layout", (520, 110), tuple(
        SlotLayout(i + 1, Rect((x, 5, x + 100, 105)),
                   Rect((x + 5, 75, x + 65, 100)),
                   Rect((x + 75, 75, x + 95, 100)),
                   Rect((x + 5, 10, x + 95, 65)))
        for i, x in enumerate(range(0, 520, 104))
    ), occlusion_terms=("synthetic-cover",))


def image(color=(80, 70, 60), size=(520, 110), orientation=None):
    im = Image.new("RGB", size, color)
    out = io.BytesIO()
    if orientation is None:
        im.save(out, format="PNG")
    else:
        exif = Image.Exif()
        exif[274] = orientation
        im.save(out, format="PNG", exif=exif)
    return out.getvalue()


def image_with_gold():
    im = Image.new("RGB", (520, 110), (80, 70, 60))
    draw = ImageDraw.Draw(im)
    for x in range(0, 520, 104):
        draw.rectangle((x + 71, 79, x + 73, 85), fill=(220, 190, 40))
        draw.rectangle((x + 20, 20, x + 24, 24), fill=(220, 190, 40))
    output = io.BytesIO()
    im.save(output, format="PNG")
    return output.getvalue()


def box(text, scale, *, kind="name", confidence=1.0):
    x, y, w, h = (10, 74, 40, 12) if kind == "name" else (4, 4, 6, 12)
    return {"text": text, "confidence": confidence,
            "x": x * scale, "y": y * scale, "width": w * scale, "height": h * scale}


def script():
    return [raw for _ in range(5) for scale in (1, 2)
            for raw in ([box("虚构甲", scale)], [box("3", scale, kind="price")])]


class FakeOCR:
    def __init__(self, responses=None):
        self.responses = script() if responses is None else responses
        self.calls = 0

    def recognize(self, data):
        result = self.responses[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


VOCAB = (LexiconEntry("fiction-a", "虚构甲", reference_cost=3),
         LexiconEntry("fiction-b", "虚构乙", reference_cost=2))


class ShopProbeTests(unittest.TestCase):
    def probe(self, responses=None, *, pixels=None, vocab=VOCAB, **kwargs):
        backend = FakeOCR(responses)
        result = probe_shop(image() if pixels is None else pixels, layout=layout(),
                            lexicon=vocab, ocr=backend, **kwargs)
        return result, backend

    def test_five_fixed_slots_and_exact_anonymous_byte_binding(self):
        data = image()
        result, backend = self.probe(pixels=data)
        self.assertEqual([s.slot for s in result.slots], [1, 2, 3, 4, 5])
        self.assertEqual(result.frame_ref, "sha256:" + hashlib.sha256(data).hexdigest())
        self.assertEqual(result.summary()["source_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(backend.calls, 20)
        self.assertTrue(all(s.status == "candidate" for s in result.slots))
        self.assertFalse(any(s.identity_verified for s in result.slots))

    def test_name_candidate_is_kept_when_one_scale_loses_it(self):
        responses = script()
        responses[2] = []
        result, _ = self.probe(responses)
        self.assertEqual(result.slots[0].name.status, "unknown")
        self.assertEqual(result.slots[0].name.candidates, ("fiction-a",))
        self.assertEqual(result.slots[0].name.variants[1]["candidates"], ())

    def test_price_candidate_is_kept_when_one_scale_loses_it(self):
        responses = script()
        responses[3] = []
        result, _ = self.probe(responses)
        self.assertEqual(result.slots[0].price.status, "unknown")
        self.assertEqual(result.slots[0].price.candidates, (3,))

    def test_dictionary_cost_does_not_fill_missing_price(self):
        responses = script()
        responses[1] = responses[3] = []
        result, _ = self.probe(responses)
        self.assertEqual(result.slots[0].name.status, "candidate")
        self.assertEqual(result.slots[0].price.candidates, ())
        self.assertEqual(result.slots[0].status, "unknown")

    def test_price_disagreement_preserves_both_candidates(self):
        responses = script()
        responses[3] = [box("2", 2, kind="price")]
        result, _ = self.probe(responses)
        self.assertEqual(result.slots[0].price.status, "conflict")
        self.assertEqual(set(result.slots[0].price.candidates), {2, 3})
        self.assertEqual(result.slots[0].status, "conflict")

    def test_name_disagreement_does_not_choose_by_cost(self):
        responses = script()
        responses[2] = [box("虚构乙", 2)]
        result, _ = self.probe(responses)
        self.assertEqual(result.slots[0].name.status, "conflict")
        self.assertEqual(set(result.slots[0].name.candidates), {"fiction-a", "fiction-b"})

    def test_alias_collision_is_a_conflict(self):
        vocab = VOCAB + (LexiconEntry("fiction-c", "另一名称", ("虚构甲",), 3),)
        result, _ = self.probe(vocab=vocab)
        self.assertEqual(result.slots[0].name.status, "conflict")

    def test_duplicate_keys_are_rejected_instead_of_deduped(self):
        with self.assertRaises(ValueError):
            self.probe(vocab=VOCAB + VOCAB[:1])

    def test_unknown_word_is_not_fuzzily_forced_to_dictionary(self):
        responses = script()
        responses[0] = [box("虚构丙", 1)]
        responses[2] = [box("虚构丙", 2)]
        result, _ = self.probe(responses)
        self.assertEqual(result.slots[0].name.candidates, ())
        self.assertIn("outside_vocabulary", result.slots[0].name.reasons)

    def test_low_confidence_keeps_proposal_without_promoting_it(self):
        responses = script()
        responses[0] = [box("虚构甲", 1, confidence=0.2)]
        result, _ = self.probe(responses)
        self.assertEqual(result.slots[0].name.candidates, ("fiction-a",))
        self.assertEqual(result.slots[0].name.status, "unknown")

    def test_body_cover_clue_blocks_combined_candidate(self):
        responses = script()
        responses[0].append({"text": "synthetic-cover", "confidence": 1,
                             "x": 5, "y": 10, "width": 80, "height": 15})
        result, _ = self.probe(responses)
        self.assertEqual(result.slots[0].status, "occluded_candidate")
        self.assertEqual(result.slots[0].occlusion, "suspected")

    def test_dark_blank_is_only_an_empty_candidate(self):
        result, _ = self.probe([[] for _ in range(20)], pixels=image((10, 10, 10)))
        self.assertTrue(all(s.status == "empty_candidate" for s in result.slots))
        self.assertTrue(all(s.visibility == "unverified" for s in result.slots))

    def test_ocr_silence_alone_does_not_prove_empty(self):
        result, _ = self.probe([[] for _ in range(20)])
        self.assertTrue(all(s.status == "unknown" for s in result.slots))

    def test_ocr_failure_on_dark_pixels_does_not_prove_empty(self):
        responses = [[] for _ in range(20)]
        responses[0] = TimeoutError("synthetic failure")
        result, _ = self.probe(responses, pixels=image((10, 10, 10)))
        self.assertEqual(result.slots[0].presence, "unknown")
        self.assertEqual(result.slots[0].status, "unknown")

    def test_price_token_cannot_contain_explanatory_text(self):
        responses = script()
        responses[1] = [box("3 things", 1, kind="price")]
        responses[3] = [box("3 things", 2, kind="price")]
        result, _ = self.probe(responses)
        self.assertEqual(result.slots[0].price.candidates, ())

    def test_text_touching_price_crop_edge_is_kept_but_rejected(self):
        responses = script()
        responses[1][0]["x"] = 0
        result, _ = self.probe(responses)
        self.assertEqual(result.slots[0].price.candidates, (3,))
        self.assertIn("text_touches_crop_edge", result.slots[0].price.reasons)
        self.assertEqual(result.slots[0].price.status, "unknown")

    def test_text_crossing_name_zone_cannot_supply_identity(self):
        responses = script()
        responses[0][0]["x"] = 2
        result, _ = self.probe(responses)
        self.assertIn("text_crosses_roi_boundary", result.slots[0].name.reasons)
        self.assertEqual(result.slots[0].name.status, "unknown")

    def test_reference_disagreement_never_changes_observed_price(self):
        result, _ = self.probe(vocab=(LexiconEntry("fiction-a", "虚构甲", reference_cost=9),))
        self.assertEqual(result.slots[0].price.candidates, (3,))
        self.assertEqual(result.slots[0].reference_cost_check, "differs_from_unverified_reference")
        self.assertEqual(result.slots[0].status, "unknown")

    def test_bad_hash_geometry_orientation_or_bytes_returns_five_unknowns(self):
        cases = [(image(), {"expected_sha256": "0" * 64}),
                 (image(size=(521, 110)), {}),
                 (image(orientation=6), {}), (b"not an image", {})]
        for pixels, kwargs in cases:
            with self.subTest(kwargs=kwargs, length=len(pixels)):
                result, backend = self.probe(pixels=pixels, **kwargs)
                self.assertEqual(len(result.slots), 5)
                self.assertTrue(all(s.status == "unknown" for s in result.slots))
                self.assertEqual(backend.calls, 0)

    def test_malformed_ocr_cannot_leak_partial_valid_candidates(self):
        for bad in ({"text": "not a list"}, [box("虚构甲", 1), {"text": "broken"}]):
            responses = script()
            responses[0] = bad
            result, _ = self.probe(responses)
            self.assertIn("invalid_ocr_response", result.slots[0].name.reasons)
            self.assertEqual(result.slots[0].name.status, "unknown")

    def test_summary_omits_raw_text_coordinates_and_private_diagnostics(self):
        result, _ = self.probe()
        summary = json.dumps(result.summary(), ensure_ascii=False)
        self.assertNotIn("private_diagnostics", summary)
        self.assertNotIn("interior_stddev", summary)
        self.assertNotIn("name_zone", summary)
        self.assertNotIn('"raw"', summary)
        self.assertIn("private_diagnostics", result.private_details())
        self.assertEqual(result.slots[0].name_options[0]["name"], "虚构甲")

    def test_invalid_layouts_are_rejected_before_any_ocr(self):
        original = layout()
        for change in ({"slots": original.slots[:4]}, {"scales": (1,)},
                       {"scales": (1, 1)}, {"image_size": (10, 10)},
                       {"slots": (original.slots[1],) + original.slots[1:]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(original, **change)

    def test_private_paths_cannot_be_inside_project(self):
        project = Path(__file__).resolve().parents[3]
        with self.assertRaises(ValueError):
            _external(project / "would-be-private.json", project)

    def test_experimental_processing_changes_only_price_images_and_records_evidence(self):
        class RecordingOCR(FakeOCR):
            def __init__(self):
                super().__init__()
                self.images = []

            def recognize(self, data):
                self.images.append(data)
                return super().recognize(data)

        source = image_with_gold()
        unmasked, masked = RecordingOCR(), RecordingOCR()
        plain = probe_shop(source, layout=layout(), lexicon=VOCAB, ocr=unmasked,
                           price_processing=ExperimentalPriceProcessing(5, suppress_gold=False))
        result = probe_shop(source, layout=layout(), lexicon=VOCAB, ocr=masked,
                            price_processing=ExperimentalPriceProcessing(5))
        self.assertEqual(plain.frame_ref, result.frame_ref)
        self.assertTrue(result.summary()["experimental_price_processing"])
        self.assertEqual(result.ocr_requests, 20)
        for index in range(0, 20, 2):
            self.assertEqual(unmasked.images[index], masked.images[index])
            self.assertNotEqual(unmasked.images[index + 1], masked.images[index + 1])
        first_price = result.private_details()["private_diagnostics"]["slots"][0]["ocr"][1]["price_processing"]
        self.assertGreater(first_price["mask"]["changed_pixels"], 0)
        self.assertNotEqual(first_price["source_crop_sha256"], first_price["ocr_input_sha256"])
        self.assertFalse(result.summary()["actionable"])

    def test_experimental_price_does_not_promote_missing_or_edge_results(self):
        for kind in ("missing", "edge"):
            with self.subTest(kind=kind):
                responses = script()
                if kind == "missing":
                    responses[3] = []
                else:
                    responses[1][0]["x"] = 0
                result = probe_shop(image_with_gold(), layout=layout(), lexicon=VOCAB,
                                    ocr=FakeOCR(responses),
                                    price_processing=ExperimentalPriceProcessing(5))
                self.assertEqual(result.slots[0].price.status, "unknown")
                self.assertEqual(result.slots[0].status, "unknown")

    def test_experimental_missing_gold_and_unsafe_crop_fail_closed(self):
        class EmptyOCR:
            def __init__(self):
                self.calls = 0

            def recognize(self, data):
                self.calls += 1
                return []

        backend = EmptyOCR()
        result = probe_shop(image(), layout=layout(), lexicon=VOCAB, ocr=backend,
                            price_processing=ExperimentalPriceProcessing(5))
        self.assertEqual(backend.calls, 10)
        self.assertEqual(result.ocr_requests, 10)
        self.assertTrue(all(slot.price.status == "unknown" for slot in result.slots))
        self.assertTrue(all("price_mask_no_gold_pixels" in slot.price.reasons for slot in result.slots))
        with self.assertRaises(ValueError):
            probe_shop(image_with_gold(), layout=layout(), lexicon=VOCAB, ocr=backend,
                       price_processing=ExperimentalPriceProcessing(11))


if __name__ == "__main__":
    unittest.main()
