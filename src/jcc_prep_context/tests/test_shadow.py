from io import BytesIO
import unittest

from PIL import Image, ImageDraw

from jcc_prep_context import Layout, ShadowTracker


SIZE = (400, 240)
BOXES = ((10, 10, 70, 40), (10, 60, 70, 90), (10, 110, 70, 140),
         (100, 10, 180, 50), (200, 10, 230, 40))
LAYOUT = Layout(SIZE, *BOXES, marker_max_mean_delta=6, scale=1)


def image(phase="none", *, marker=50, round_number=5, scene=True):
    pic = Image.new("RGB", SIZE, (0, 0, 0))
    draw = ImageDraw.Draw(pic)
    colors = ((255, 0, 0) if round_number == 5 else (254, 0, 0),
              (0, 255, 0) if scene else (0, 0, 0), (0, 0, 255),
              {"prep": (255, 255, 0), "combat": (255, 0, 255), "none": (90, 90, 90)}[phase],
              (marker, marker, marker))
    for box, color in zip(BOXES, colors):
        left, top, right, bottom = box
        draw.rectangle((left, top, right - 1, bottom - 1), fill=color)
    out = BytesIO()
    pic.save(out, format="PNG")
    return out.getvalue()


class PixelOCR:
    def recognize(self, data):
        with Image.open(BytesIO(data)) as im:
            color = im.convert("RGB").getpixel((0, 0))
            w, h = im.size
        text = {(255, 0, 0): "2-5", (254, 0, 0): "2-6", (0, 255, 0): "购买经验",
                (0, 0, 255): "刷新", (255, 255, 0): "准备阶段", (255, 0, 255): "战斗开始"}.get(color)
        if text is None:
            return []
        return [{"text": text, "x": 3, "y": 3, "width": w - 6, "height": h - 6,
                 "confidence": 1.0}]


class ShadowTests(unittest.TestCase):
    def tracker(self, *, gap=150, age=400):
        return ShadowTracker(LAYOUT, PixelOCR(), source_id="source-a", max_frame_gap_ns=gap,
                             max_anchor_age_ns=age)

    def test_banner_seeds_continuation_without_certifying_context(self):
        t = self.tracker()
        first = t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)
        second = t.observe(image(), source_id="source-a", sequence=2, pts_ns=200)
        self.assertEqual((first["status"], first["reason"]), ("development_candidate", "preparation_banner"))
        self.assertEqual((second["status"], second["reason"]), ("development_candidate", "shadow_continuation"))
        self.assertEqual(second["anchor_sequence"], 1)
        self.assertEqual(second["anchor_deadline_pts_ns_declared"], 500)
        for key in ("advice_eligible", "execution_eligible", "source_metadata_authenticated", "ocr_authenticated"):
            self.assertFalse(second[key])
        self.assertEqual((second["board_owner"], second["visibility"]), ("unknown", "unknown"))
        self.assertIsInstance(second["round_candidate"], tuple)

    def test_combat_banner_withdraws_even_if_shop_cue_missing(self):
        t = self.tracker()
        t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)
        result = t.observe(image("combat", scene=False), source_id="source-a", sequence=2, pts_ns=200)
        self.assertEqual((result["status"], result["reason"]), ("unknown", "combat_banner"))
        self.assertEqual(t.observe(image(), source_id="source-a", sequence=3, pts_ns=300)["reason"], "preparation_anchor_missing")

    def test_gap_and_expiry_do_not_resurrect_old_anchor(self):
        t = self.tracker()
        t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)
        self.assertEqual(t.observe(image(), source_id="source-a", sequence=3, pts_ns=200)["reason"], "replay_gap")
        self.assertEqual(t.observe(image(), source_id="source-a", sequence=4, pts_ns=300)["reason"], "preparation_anchor_missing")
        t = self.tracker()
        t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)
        for seq in (2, 3, 4):
            self.assertEqual(t.observe(image(), source_id="source-a", sequence=seq, pts_ns=seq * 100)["status"], "development_candidate")
        self.assertEqual(t.observe(image(), source_id="source-a", sequence=5, pts_ns=500)["reason"], "anchor_expired")
        self.assertEqual(t.observe(image(), source_id="source-a", sequence=6, pts_ns=600)["reason"], "preparation_anchor_missing")

    def test_changed_round_marker_or_scene_withdraw(self):
        cases = ((image(round_number=6), "round_changed"), (image(marker=90), "marker_changed"),
                 (image(scene=False), "scene_cues_missing"))
        for case, expected in cases:
            with self.subTest(expected=expected):
                t = self.tracker()
                t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)
                result = t.observe(case, source_id="source-a", sequence=2, pts_ns=200)
                self.assertEqual((result["status"], result["reason"]), ("unknown", expected))

    def test_metadata_failure_and_explicit_close_clear_candidate(self):
        t = self.tracker()
        t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)
        self.assertEqual(t.observe(image(), source_id="source-a", sequence=1, pts_ns=200)["reason"], "duplicate_or_out_of_order")
        self.assertEqual(t.observe(image(), source_id="source-a", sequence=2, pts_ns=200)["reason"], "preparation_anchor_missing")
        t.close()
        self.assertEqual(t.observe(image("prep"), source_id="source-a", sequence=3, pts_ns=300)["reason"], "closed")

    def test_current_withdraws_after_stream_silence_without_a_new_frame(self):
        t = self.tracker()
        t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)
        self.assertEqual(t.current(source_id="source-a", pts_ns=249)["status"], "development_candidate")
        self.assertEqual(t.current(source_id="source-a", pts_ns=251)["reason"], "replay_silence")
        self.assertEqual(t.observe(image(), source_id="source-a", sequence=2, pts_ns=200)["reason"], "replay_clock_regressed")

    def test_unreadable_phase_cue_withdraws_instead_of_carrying(self):
        class FailingBanner(PixelOCR):
            def recognize(self, data):
                with Image.open(BytesIO(data)) as im:
                    color = im.convert("RGB").getpixel((0, 0))
                if color == (90, 90, 90):
                    raise RuntimeError("ocr unavailable")
                return super().recognize(data)
        t = ShadowTracker(LAYOUT, FailingBanner(), source_id="source-a", max_frame_gap_ns=150, max_anchor_age_ns=400)
        t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)
        self.assertEqual(t.observe(image(), source_id="source-a", sequence=2, pts_ns=200)["reason"], "phase_cue_unreadable")

    def test_unrecognized_text_in_one_phase_probe_withdraws(self):
        class InconsistentBanner(PixelOCR):
            count = 0
            def recognize(self, data):
                with Image.open(BytesIO(data)) as im:
                    color, (w, h) = im.convert("RGB").getpixel((0, 0)), im.size
                if color == (90, 90, 90):
                    self.count += 1
                    return ([{"text": "OTHER", "x": 3, "y": 3, "width": w - 6,
                              "height": h - 6, "confidence": 1.0}] if self.count == 1 else [])
                return super().recognize(data)
        t = ShadowTracker(LAYOUT, InconsistentBanner(), source_id="source-a", max_frame_gap_ns=150, max_anchor_age_ns=400)
        t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)
        self.assertEqual(t.observe(image(), source_id="source-a", sequence=2, pts_ns=200)["reason"], "unrecognized_banner_text")

    def test_invalid_picture_or_missing_ocr_cannot_seed(self):
        t = self.tracker()
        self.assertEqual(t.observe(b"bad image", source_id="source-a", sequence=1, pts_ns=100)["reason"], "image_unreadable")
        t = ShadowTracker(LAYOUT, None, source_id="source-a", max_frame_gap_ns=150, max_anchor_age_ns=400)
        self.assertEqual(t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)["status"], "unknown")

    def test_marker_checked_even_while_preparation_banner_remains(self):
        t = self.tracker()
        t.observe(image("prep", marker=50), source_id="source-a", sequence=1, pts_ns=100)
        boundary = t.observe(image("prep", marker=56), source_id="source-a", sequence=2, pts_ns=200)
        self.assertEqual(boundary["status"], "development_candidate")
        self.assertEqual(boundary["anchor_sequence"], 1)
        changed = t.observe(image("prep", marker=90), source_id="source-a", sequence=3, pts_ns=300)
        self.assertEqual((changed["status"], changed["reason"]), ("unknown", "marker_changed"))
        self.assertIsNone(changed["anchor_sequence"])
        self.assertEqual(t.observe(image(marker=90), source_id="source-a", sequence=4, pts_ns=400)["reason"],
                         "marker_conflict_locked")

    def test_marker_conflict_latches_across_persistent_banner_and_other_frames(self):
        t = self.tracker()
        self.assertEqual(t.observe(image("prep", marker=50), source_id="source-a", sequence=1,
                                   pts_ns=100)["status"], "development_candidate")
        conflict = t.observe(image("prep", marker=90), source_id="source-a", sequence=2, pts_ns=200)
        self.assertEqual((conflict["status"], conflict["reason"]), ("unknown", "marker_changed"))
        self.assertTrue(conflict["marker_conflict_locked"])
        for seq, picture in ((3, image("prep", marker=90)),
                             (4, image("prep", marker=90, scene=False)),
                             (5, b"unreadable"),
                             (6, image("prep", marker=90, round_number=6))):
            result = t.observe(picture, source_id="source-a", sequence=seq, pts_ns=seq * 100)
            self.assertEqual((result["status"], result["reason"]), ("unknown", "marker_conflict_locked"))
            self.assertTrue(result["marker_conflict_locked"])
            self.assertIsNone(result["anchor_sequence"])
        self.assertEqual(t.current(source_id="source-a", pts_ns=600)["reason"], "marker_conflict_locked")
        t.close()
        self.assertEqual(t.observe(image("prep", marker=90), source_id="source-a", sequence=7,
                                   pts_ns=700)["reason"], "closed")
        fresh = self.tracker()
        self.assertEqual(fresh.observe(image("prep", marker=90), source_id="source-a", sequence=1,
                                       pts_ns=100)["status"], "development_candidate")

    def test_marker_conflict_is_seen_before_temporary_scene_ocr_failure(self):
        t = self.tracker()
        t.observe(image("prep", marker=50), source_id="source-a", sequence=1, pts_ns=100)
        conflict = t.observe(image("prep", marker=90, scene=False), source_id="source-a",
                             sequence=2, pts_ns=200)
        self.assertEqual((conflict["status"], conflict["reason"]), ("unknown", "marker_changed"))
        self.assertTrue(conflict["marker_conflict_locked"])
        self.assertEqual(t.observe(image("prep", marker=90), source_id="source-a", sequence=3,
                                   pts_ns=300)["reason"], "marker_conflict_locked")

    def test_source_change_requires_a_new_bound_tracker(self):
        t = self.tracker()
        t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)
        switched = t.observe(image("prep"), source_id="source-b", sequence=2, pts_ns=200)
        self.assertEqual((switched["status"], switched["reason"]), ("unknown", "declared_source_changed"))
        self.assertIsNone(switched["anchor_sequence"])
        self.assertFalse(switched["source_metadata_authenticated"])
        self.assertEqual(t.current(source_id="source-b", pts_ns=200)["reason"], "source_rebind_required")
        self.assertEqual(t.observe(image(), source_id="source-a", sequence=2, pts_ns=200)["reason"],
                         "source_rebind_required")
        self.assertEqual(t.observe(image("prep"), source_id="source-a", sequence=2, pts_ns=200)["reason"],
                         "source_rebind_required")
        other = ShadowTracker(LAYOUT, PixelOCR(), source_id="source-b", max_frame_gap_ns=150,
                              max_anchor_age_ns=400)
        self.assertEqual(other.observe(image("prep"), source_id="source-b", sequence=1, pts_ns=100)["status"],
                         "development_candidate")

    def test_source_change_declared_at_current_also_requires_rebinding(self):
        t = self.tracker()
        t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)
        switched = t.current(source_id="source-b", pts_ns=100)
        self.assertEqual((switched["status"], switched["reason"]), ("unknown", "declared_source_changed"))
        self.assertIsNone(switched["anchor_sequence"])
        self.assertEqual(t.current(source_id="source-a", pts_ns=100)["reason"], "source_rebind_required")
        self.assertEqual(t.observe(image("prep"), source_id="source-a", sequence=2, pts_ns=200)["reason"],
                         "source_rebind_required")

    def test_missing_source_declaration_clears_existing_candidate(self):
        for call in (lambda t: t.observe(image(), sequence=2, pts_ns=200),
                     lambda t: t.current(pts_ns=200)):
            with self.subTest(call=call):
                t = self.tracker()
                t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)
                result = call(t)
                self.assertEqual((result["status"], result["reason"]), ("unknown", "invalid_declared_source"))
                self.assertIsNone(result["anchor_sequence"])
                self.assertEqual(t.current(source_id="source-a", pts_ns=100)["status"], "unknown")
        with self.assertRaises(ValueError):
            ShadowTracker(LAYOUT, PixelOCR(), source_id=" ", max_frame_gap_ns=150,
                          max_anchor_age_ns=400)

    def test_expiry_with_persistent_banner_breaks_candidate_before_reanchoring(self):
        t = self.tracker()
        t.observe(image("prep"), source_id="source-a", sequence=1, pts_ns=100)
        for seq in (2, 3, 4):
            result = t.observe(image("prep"), source_id="source-a", sequence=seq, pts_ns=seq * 100)
            self.assertEqual(result["anchor_sequence"], 1)
            self.assertEqual(result["anchor_deadline_pts_ns_declared"], 500)
        expired = t.observe(image("prep"), source_id="source-a", sequence=5, pts_ns=500)
        self.assertEqual((expired["status"], expired["reason"]), ("unknown", "anchor_expired"))
        self.assertIsNone(expired["anchor_sequence"])
        self.assertFalse(expired["marker_conflict_locked"])
        new = t.observe(image("prep"), source_id="source-a", sequence=6, pts_ns=600)
        self.assertEqual((new["status"], new["anchor_sequence"]), ("development_candidate", 6))
        self.assertEqual(new["anchor_deadline_pts_ns_declared"], 1000)
        self.assertFalse(new["marker_conflict_locked"])

    def test_expiry_is_not_reclassified_as_marker_conflict(self):
        t = self.tracker()
        t.observe(image("prep", marker=50), source_id="source-a", sequence=1, pts_ns=100)
        for seq in (2, 3, 4):
            t.observe(image("prep", marker=50), source_id="source-a", sequence=seq, pts_ns=seq * 100)
        expired = t.observe(image("prep", marker=90), source_id="source-a", sequence=5, pts_ns=500)
        self.assertEqual((expired["status"], expired["reason"]), ("unknown", "anchor_expired"))
        self.assertFalse(expired["marker_conflict_locked"])
        new = t.observe(image("prep", marker=90), source_id="source-a", sequence=6, pts_ns=600)
        self.assertEqual((new["status"], new["anchor_sequence"]), ("development_candidate", 6))
        self.assertFalse(new["marker_conflict_locked"])


if __name__ == "__main__":
    unittest.main()
