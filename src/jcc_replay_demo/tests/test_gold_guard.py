"""Generated pixels only: correlated OCR mistakes must not reach economy."""

from dataclasses import replace
import copy
import io
import unittest

from PIL import Image, ImageDraw

from jcc_replay_demo.inspector import inspect_image, load_profile, load_source, structured_frame, safe_summary
from jcc_replay_demo.render import render_page
from jcc_replay_demo.integrations import economy_comparison
from jcc_replay_demo.tests import test_inspector as fixtures


class SameGoldOCR(fixtures.FakeOCR):
    def __init__(self, value):
        super().__init__()
        self.value = str(value)

    def recognize(self, data):
        index = self.calls % 12
        result = super().recognize(data)
        if index in (6, 10):
            result[0]["text"] = self.value
        return result


def draw_six(image, box):
    """An invented connected glyph with a lower loop; no captured font/assets."""
    x, y, _, _ = box
    draw = ImageDraw.Draw(image)
    draw.ellipse((x + 40, y + 23, x + 64, y + 47), outline="white", width=5)
    draw.line(((x + 42, y + 36), (x + 42, y + 19),
               (x + 49, y + 10), (x + 61, y + 10)), fill="white", width=5)


def draw_nine(image, box):
    draw_six(image, box)
    with image.crop(box).transpose(Image.Transpose.ROTATE_180) as flipped:
        image.paste(flipped, box)


def draw_six_with_boundary_stroke(image, box):
    draw_six(image, box)
    x, y, _, _ = box
    ImageDraw.Draw(image).line((x, y+10, x, y+47), fill="white", width=4)


class GoldGuardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InspectorTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.profile = load_profile(self.fixture.profile_path)
        self.source = load_source(self.profile).images[0]
        self.box = next(f.roi.box for f in self.profile.number_layouts[0].fields
                        if f.field_name == "gold")

    def inspect_glyph(self, drawing, value):
        with Image.new("RGB", self.profile.key.geometry.image_size, "#101820") as image:
            drawing(image, self.box)
            with io.BytesIO() as out:
                image.save(out, format="PNG")
                source = replace(self.source, data=out.getvalue())
        self.render_image = source
        return inspect_image(source, self.profile, SameGoldOCR(value))

    def render(self, frame):
        report = {"frames": [frame], "summary": safe_summary([frame]), "generated_utc": "generated-test"}
        return render_page(report, [self.render_image], self.profile)

    def test_correlated_nines_on_lower_loop_pixels_block_economy(self):
        frame = self.inspect_glyph(draw_six, 9)
        gold = structured_frame(frame)["fields"]["gold"]
        self.assertIsNone(gold["candidate"])
        self.assertEqual(gold["status"], "unknown")
        self.assertEqual([v["candidate"] for v in gold["variants"]], [9, 9])
        self.assertEqual(frame["economy"]["result"]["status"], "blocked")
        self.assertTrue(all(r["gold_after_hypothetical_purchase"] is None
                            and r["interest_delta"] is None
                            for r in frame["economy"]["result"]["comparisons"]))

    def test_correlated_sixes_on_upper_loop_pixels_block_economy(self):
        frame = self.inspect_glyph(draw_nine, 6)
        gold = structured_frame(frame)["fields"]["gold"]
        self.assertIsNone(gold["candidate"])
        self.assertEqual(gold["reason"], "gold_glyph_disagreement")
        self.assertEqual([v["candidate"] for v in gold["variants"]], [6, 6])
        self.assertEqual(frame["economy"]["result"]["status"], "blocked")

    def test_consistent_six_and_nine_remain_development_candidates(self):
        for value, drawing, position in ((6, draw_six, "lower"), (9, draw_nine, "upper")):
            with self.subTest(value=value):
                frame = self.inspect_glyph(drawing, value)
                gold = structured_frame(frame)["fields"]["gold"]
                self.assertEqual(gold["candidate"], value)
                self.assertEqual(frame["economy"]["result"]["status"], "conditional")
                self.assertTrue(all(m["positions"] == [position] for m in
                                    structured_frame(frame)["gold_glyph_check"]["loop_observations"]))
                self.assertFalse(structured_frame(frame)["advice_eligible"])
                self.assertIsNone(structured_frame(frame)["accuracy"])
                self.assertFalse(frame["economy"]["result"]["execution_eligible"])

    def test_boundary_stroke_beside_complete_six_blocks_economy(self):
        frame = self.inspect_glyph(draw_six_with_boundary_stroke, 6)
        gold = structured_frame(frame)["fields"]["gold"]
        self.assertEqual(gold["status"], "unknown")
        self.assertIsNone(gold["candidate"])
        self.assertEqual(gold["reason"], "gold_roi_boundary_foreground")
        self.assertEqual([v["candidate"] for v in gold["variants"]], [6, 6])
        self.assertFalse(gold["conflict"])
        self.assertEqual([record["items"][0]["text"] for record in frame["raw_ocr"]
                          if record["group"].startswith("numeric_")][2::4], ["6", "6"])
        self.assertEqual(frame["economy"]["result"]["status"], "blocked")
        self.assertEqual(len(frame["economy"]["result"]["comparisons"]), 5)
        self.assertTrue(all(row[key] is None
                            for row in frame["economy"]["result"]["comparisons"]
                            for key in ("gold_after_hypothetical_purchase", "interest_before",
                                        "interest_after", "interest_delta")))

    def test_boundary_stroke_on_any_edge_blocks_matching_six_and_nine(self):
        for value, glyph in ((6, draw_six), (9, draw_nine)):
            for side in ("left", "right", "top", "bottom"):
                with self.subTest(value=value, side=side):
                    def drawing(image, box):
                        glyph(image, box)
                        x, y, right, bottom = box
                        lines = {"left": (x, y+10, x, y+47),
                                 "right": (right-1, y+10, right-1, y+47),
                                 "top": (x+10, y, x+30, y),
                                 "bottom": (x+10, bottom-1, x+30, bottom-1)}
                        ImageDraw.Draw(image).line(lines[side], fill="white", width=4)
                    frame = self.inspect_glyph(drawing, value)
                    gold = structured_frame(frame)["fields"]["gold"]
                    self.assertIsNone(gold["candidate"])
                    self.assertEqual(gold["reason"], "gold_roi_boundary_foreground")
                    self.assertEqual([v["candidate"] for v in gold["variants"]], [value, value])
                    self.assertEqual(frame["economy"]["result"]["status"], "blocked")

    def test_even_one_boundary_pixel_blocks_complete_glyph(self):
        x, y, right, bottom = self.box
        for point in ((x, y), (right-1, y), (x, bottom-1), (right-1, bottom-1), (x, y+30)):
            with self.subTest(point=point):
                def drawing(image, box):
                    draw_six(image, box)
                    ImageDraw.Draw(image).point(point, fill="white")
                frame = self.inspect_glyph(drawing, 6)
                self.assertEqual(frame["gold_glyph_check"]["reason"], "gold_roi_boundary_foreground")
                self.assertEqual([m["boundary_foreground_pixels"]
                                  for m in frame["gold_glyph_check"]["measurements"]], [1] * 4)
                self.assertEqual(frame["economy"]["result"]["status"], "blocked")

    def test_boundary_signal_at_one_threshold_cannot_be_cleared_by_others(self):
        for brightness, present in ((128, [True, False, False, False]),
                                    (160, [True, True, False, False]),
                                    (192, [True, True, True, False])):
            with self.subTest(brightness=brightness):
                def drawing(image, box):
                    draw_six(image, box)
                    ImageDraw.Draw(image).point((box[0], box[1]+30), fill=(brightness,) * 3)
                frame = self.inspect_glyph(drawing, 6)
                self.assertEqual(frame["gold_glyph_check"]["reason"], "gold_roi_boundary_foreground")
                self.assertEqual([m["boundary_foreground_present"] for m in
                                  structured_frame(frame)["gold_glyph_check"]["loop_observations"]], present)
                self.assertEqual(frame["economy"]["result"]["status"], "blocked")

    def test_boundary_icon_is_not_whitelisted(self):
        def drawing(image, box):
            draw_six(image, box)
            x, y, _, _ = box
            ImageDraw.Draw(image).ellipse((x-8, y+15, x+20, y+40), outline="white", width=4)
        frame = self.inspect_glyph(drawing, 6)
        self.assertEqual(frame["gold_glyph_check"]["reason"], "gold_roi_boundary_foreground")
        self.assertIsNone(structured_frame(frame)["fields"]["gold"]["candidate"])
        self.assertEqual(frame["economy"]["result"]["status"], "blocked")

    def test_tiny_interior_fragment_does_not_count_as_boundary(self):
        def drawing(image, box):
            draw_six(image, box)
            ImageDraw.Draw(image).point((box[0]+1, box[1]+30), fill="white")
        frame = self.inspect_glyph(drawing, 6)
        self.assertEqual(structured_frame(frame)["fields"]["gold"]["candidate"], 6)
        self.assertEqual([m["boundary_foreground_pixels"]
                          for m in frame["gold_glyph_check"]["measurements"]], [0] * 4)
        self.assertEqual(frame["economy"]["result"]["status"], "conditional")

    def test_non_six_nine_candidates_are_not_all_rejected(self):
        for value in (0, 1, 5, 31):
            with self.subTest(value=value):
                frame = self.inspect_glyph(lambda *_: None, value)
                self.assertEqual(structured_frame(frame)["fields"]["gold"]["candidate"], value)
                self.assertFalse(frame["gold_glyph_check"]["required"])
                self.assertNotEqual(frame["economy"]["input_mapping"]["to_status"], "unknown")

    def test_missing_pixels_do_not_validate_correlated_nines(self):
        frame = self.inspect_glyph(lambda *_: None, 9)
        self.assertIsNone(structured_frame(frame)["fields"]["gold"]["candidate"])
        self.assertEqual(frame["gold_glyph_check"]["reason"], "gold_glyph_inconclusive")
        self.assertEqual(frame["economy"]["result"]["status"], "blocked")

    def test_centered_or_multiple_loops_are_inconclusive(self):
        def zero(image, box):
            x, y, _, _ = box
            ImageDraw.Draw(image).ellipse((x+40, y+10, x+64, y+47), outline="white", width=5)
        def eight(image, box):
            x, y, _, _ = box
            draw = ImageDraw.Draw(image)
            for top, bottom in ((10, 30), (27, 47)):
                draw.ellipse((x+40, y+top, x+64, y+bottom), outline="white", width=5)
        for drawing in (zero, eight):
            for value in (6, 9):
                with self.subTest(drawing=drawing.__name__, value=value):
                    frame = self.inspect_glyph(drawing, value)
                    self.assertEqual(frame["gold_glyph_check"]["reason"], "gold_glyph_inconclusive")
                    self.assertIsNone(structured_frame(frame)["fields"]["gold"]["candidate"])

    def test_threshold_instability_cannot_support_a_candidate(self):
        def dim_six(image, box):
            draw_six(image, box)
            with image.point(lambda v: 150 if v == 255 else v) as dim:
                image.paste(dim)
        frame = self.inspect_glyph(dim_six, 6)
        self.assertEqual(frame["gold_glyph_check"]["status"], "unknown")
        self.assertEqual(frame["gold_glyph_check"]["reason"], "gold_glyph_inconclusive")
        self.assertEqual(frame["economy"]["result"]["status"], "blocked")

    def test_near_white_stroke_break_cannot_be_ignored(self):
        def gray_six(image, box):
            draw_six(image, box)
            with image.point(lambda v: 210 if v == 255 else v) as gray:
                image.paste(gray)
        frame = self.inspect_glyph(gray_six, 6)
        self.assertIsNone(structured_frame(frame)["fields"]["gold"]["candidate"])
        self.assertEqual(frame["gold_glyph_check"]["reason"], "gold_glyph_inconclusive")
        self.assertEqual(frame["economy"]["result"]["status"], "blocked")

    def test_clipped_or_extra_components_do_not_support_a_candidate(self):
        def clipped(image, box):
            draw_six(image, box)
            x, y, right, bottom = box
            with image.crop((x+45, y, right, bottom)) as cut:
                ImageDraw.Draw(image).rectangle(box, fill="#101820")
                image.paste(cut, (x, y))
        def extra(image, box):
            draw_six(image, box)
            x, y, _, _ = box
            ImageDraw.Draw(image).rectangle((x+85, y+10, x+90, y+45), fill="white")
        for drawing in (clipped, extra):
            with self.subTest(drawing=drawing.__name__):
                frame = self.inspect_glyph(drawing, 6)
                self.assertIsNone(structured_frame(frame)["fields"]["gold"]["candidate"])
                self.assertEqual(frame["economy"]["result"]["status"], "blocked")

    def test_multidigit_glyphs_match_left_to_right_without_digit_repair(self):
        def sixty_nine(image, box):
            draw_six(image, box)
            with Image.new("RGB", image.size, "#101820") as other:
                draw_nine(other, box)
                with other.crop(box) as nine:
                    image.paste(nine.crop((80, 0, 115, 60)), (box[0]+80, box[1]))
        for value, status in ((69, "consistent"), (96, "unknown"), (169, "unknown")):
            with self.subTest(value=value):
                frame = self.inspect_glyph(sixty_nine, value)
                self.assertEqual(frame["gold_glyph_check"]["status"], status)
                self.assertEqual(structured_frame(frame)["fields"]["gold"]["candidate"],
                                 value if status == "consistent" else None)

    def test_original_ocr_conflict_is_not_resolved_by_shape(self):
        frame = self.inspect_glyph(draw_nine, 9)
        # One OCR variant changes to 6: the new check must not choose a peer.
        next(p for p in frame["variants"][0]["fields"] if p["field"] == "gold")["candidate"] = 6
        self.assertEqual(economy_comparison(frame)["result"]["status"], "blocked")

    def test_missing_or_cross_frame_checks_cannot_enable_economy(self):
        frame = self.inspect_glyph(draw_six, 6)
        for key, value in (("input_frame_ref", "other-generated-frame"),
                           ("input_source_sha256", "0"*64), ("input_roi", [1, 2, 3, 4]),
                           ("checked_value", 9), ("method", "same-ocr-two-scales")):
            with self.subTest(key=key):
                bad = copy.deepcopy(frame)
                bad["gold_glyph_check"][key] = value
                self.assertEqual(economy_comparison(bad)["result"]["status"], "blocked")
        frame.pop("gold_glyph_check")
        self.assertEqual(economy_comparison(frame)["result"]["status"], "blocked")

    def test_previous_method_cannot_enable_economy(self):
        frame = self.inspect_glyph(draw_six, 6)
        frame["gold_glyph_check"]["method"] = "six-nine-loop-topology-v1"
        self.assertEqual(economy_comparison(frame)["result"]["status"], "blocked")

    def test_missing_or_nonfinite_hole_diagnostics_cannot_enable_economy(self):
        frame = self.inspect_glyph(draw_six, 6)
        for center in (None, True, float("inf"), float("nan"), "0.7"):
            with self.subTest(center=center):
                bad = copy.deepcopy(frame)
                bad["gold_glyph_check"]["measurements"][0]["components"][0]["holes"][0]["center_y"] = center
                self.assertEqual(economy_comparison(bad)["result"]["status"], "blocked")
        for measurements in ([], [{}], [{"threshold": t, "components": []} for t in (128, 160, 192, 224)]):
            bad = copy.deepcopy(frame)
            bad["gold_glyph_check"]["measurements"] = measurements
            self.assertEqual(economy_comparison(bad)["result"]["status"], "blocked")

    def test_missing_or_invalid_boundary_evidence_cannot_enable_economy(self):
        frame = self.inspect_glyph(draw_six, 6)
        for index in range(4):
            for value in (None, True, False, -1, 0.0, float("inf"), float("nan"), "0", [], {}):
                with self.subTest(index=index, value=value):
                    bad = copy.deepcopy(frame)
                    bad["gold_glyph_check"]["measurements"][index]["boundary_foreground_pixels"] = value
                    self.assertEqual(economy_comparison(bad)["result"]["status"], "blocked")
            bad = copy.deepcopy(frame)
            bad["gold_glyph_check"]["measurements"][index].pop("boundary_foreground_pixels")
            self.assertEqual(economy_comparison(bad)["result"]["status"], "blocked")

    def test_consistent_flag_cannot_override_boundary_measurement(self):
        frame = self.inspect_glyph(draw_six, 6)
        for index in range(4):
            with self.subTest(index=index):
                bad = copy.deepcopy(frame)
                bad["gold_glyph_check"]["measurements"][index]["boundary_foreground_pixels"] = 1
                self.assertEqual(economy_comparison(bad)["result"]["status"], "blocked")

    def test_structured_check_has_no_private_roi_or_pixel_data(self):
        frame = self.inspect_glyph(draw_six, 9)
        result = structured_frame(frame)["gold_glyph_check"]
        self.assertEqual(result["input_source_sha256"], frame["source_sha256"])
        self.assertEqual(result["input_frame_ref"], frame["frame_ref"])
        for key in ("input_roi", "measurements", "pixels", "box", "raw_ocr", "path"):
            self.assertNotIn(key, result)

    def test_boundary_evidence_is_visible_without_exporting_roi(self):
        for drawing, present in ((draw_six, False), (draw_six_with_boundary_stroke, True)):
            with self.subTest(present=present):
                frame = self.inspect_glyph(drawing, 6)
                observations = structured_frame(frame)["gold_glyph_check"]["loop_observations"]
                self.assertEqual([m["boundary_foreground_present"] for m in observations],
                                 [present] * 4)
                self.assertTrue(all(set(m) == {"threshold", "positions", "boundary_foreground_present"}
                                    for m in observations))
                if present:
                    page = self.render(frame)
                    self.assertIn("gold_roi_boundary_foreground", page)
                    self.assertIn("无法区分截断笔画与图标", page)
                    self.assertIn("边界亮色像素", page)
                    self.assertNotIn('<section class="economy conditional">', page)

    def test_summary_counts_pixel_vetoes_without_calling_them_accuracy(self):
        frames = [self.inspect_glyph(draw_six, 6), self.inspect_glyph(draw_six, 9),
                  self.inspect_glyph(lambda *_: None, 9), self.inspect_glyph(lambda *_: None, 1)]
        summary = safe_summary(frames)
        self.assertEqual(summary.get("gold_glyph_required_frames"), 3)
        self.assertEqual(summary.get("gold_glyph_vetoed_frames"), 2)
        self.assertEqual(summary.get("gold_glyph_consistent_frames"), 1)
        self.assertIsNone(summary["accuracy"])
        self.assertEqual(summary["real_acceptance_samples"], 0)

    def test_page_explains_same_frame_pixel_veto(self):
        frame = self.inspect_glyph(draw_six, 9)
        page = self.render(frame)
        self.assertIn("字形", page)
        self.assertIn("gold_glyph_disagreement", page)
        self.assertIn("six-nine-loop-topology-v2", page)
        self.assertIn('data-field="gold" data-status="unknown"', page)
        self.assertNotIn('<section class="economy conditional">', page)

    def test_renderer_rejects_cross_frame_glyph_check(self):
        frame = self.inspect_glyph(draw_six, 9)
        for key in ("input_source_sha256", "input_frame_ref"):
            with self.subTest(key=key):
                bad = copy.deepcopy(frame)
                bad["gold_glyph_check"][key] = "generated-other-frame"
                with self.assertRaisesRegex(ValueError, "replay_render_binding_mismatch"):
                    self.render(bad)

    def test_renderer_rejects_forced_economy_after_veto(self):
        frame = self.inspect_glyph(draw_six, 9)
        frame["economy"]["input_mapping"]["to_status"] = "candidate"
        frame["economy"]["result"]["status"] = "conditional"
        with self.assertRaisesRegex(ValueError, "replay_render_economy_mismatch"):
            self.render(frame)

    def test_renderer_rechecks_claimed_glyph_evidence_against_actual_pixels(self):
        frame = self.inspect_glyph(draw_six, 9)
        original_image = self.render_image
        donor = self.inspect_glyph(draw_nine, 9)
        frame["gold_glyph_check"] = copy.deepcopy(donor["gold_glyph_check"])
        frame["gold_glyph_check"].update(input_frame_ref=frame["frame_ref"],
                                          input_source_sha256=frame["source_sha256"])
        next(p for p in frame["fields"] if p["field"] == "gold").update(
            candidate=9, status="development_candidate", reason=None)
        frame["economy"] = economy_comparison(frame)
        self.assertEqual(frame["economy"]["result"]["status"], "conditional")
        self.render_image = original_image
        with self.assertRaisesRegex(ValueError, "replay_render_gold_evidence_mismatch"):
            self.render(frame)

    def test_renderer_rechecks_tampered_boundary_counts(self):
        frame = self.inspect_glyph(draw_six_with_boundary_stroke, 6)
        frame["gold_glyph_check"].update(status="consistent", reason=None)
        for measurement in frame["gold_glyph_check"]["measurements"]:
            measurement["boundary_foreground_pixels"] = 0
        next(p for p in frame["fields"] if p["field"] == "gold").update(
            candidate=6, status="development_candidate", reason=None)
        frame["economy"] = economy_comparison(frame)
        self.assertEqual(frame["economy"]["result"]["status"], "conditional")
        with self.assertRaisesRegex(ValueError, "replay_render_gold_evidence_mismatch"):
            self.render(frame)

    def test_relabelled_gold_cannot_reuse_a_rejected_check(self):
        frame = self.inspect_glyph(draw_six, 9)
        for group in (frame["fields"], *(v["fields"] for v in frame["variants"])):
            next(p for p in group if p["field"] == "gold").update(
                candidate=5, status="development_candidate", reason=None)
        self.assertEqual(economy_comparison(frame)["result"]["status"], "blocked")

    def test_consistent_flag_cannot_override_contradictory_hole_measurement(self):
        frame = self.inspect_glyph(draw_six, 6)
        self.assertEqual(frame["economy"]["result"]["status"], "conditional")
        for measurement in frame["gold_glyph_check"]["measurements"]:
            measurement["components"][0]["holes"][0]["center_y"] = 0.3
        self.assertEqual(economy_comparison(frame)["result"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
