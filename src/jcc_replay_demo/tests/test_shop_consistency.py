"""Shop consumer regressions using generated pixels and scripted OCR only."""

import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from PIL import Image

from jcc_shop_probe import LexiconEntry, Rect, ShopLayout, SlotLayout, probe_shop
from jcc_replay_demo.inspector import attach_shop, digest
from jcc_replay_demo.render import _shop_html
from jcc_replay_demo.shop import match_shop


class ScriptedOCR:
    def __init__(self, responses):
        self.responses = iter(responses)

    def recognize(self, _):
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class ShopConsistencyTests(unittest.TestCase):
    def fixture(self, *, missing_prices=False, dark=False, reference_cost=3, transform=None):
        layout = ShopLayout("generated-shop-layout", (520, 110), tuple(
            SlotLayout(i + 1, Rect((x, 5, x + 100, 105)),
                       Rect((x + 5, 75, x + 65, 100)), Rect((x + 75, 75, x + 95, 100)),
                       Rect((x + 5, 10, x + 95, 65)))
            for i, x in enumerate(range(0, 520, 104))
        ), occlusion_terms=("generated-cover",))
        out = io.BytesIO()
        Image.new("RGB", layout.image_size, (10, 10, 10) if dark else (80, 70, 60)).save(out, format="PNG")
        data = out.getvalue()
        responses = []
        for _ in range(5):
            for scale in (1, 2):
                responses.extend([
                    [{"text": "虚构甲", "confidence": 1.0, "x": 10 * scale,
                      "y": 74 * scale, "width": 40 * scale, "height": 12 * scale}],
                    [] if missing_prices else [{"text": "3", "confidence": 1.0, "x": 4 * scale,
                                                "y": 4 * scale, "width": 6 * scale, "height": 12 * scale}],
                ])
        if transform is not None:
            transform(responses)
        row = probe_shop(data, layout=layout, lexicon=(
            LexiconEntry("generated-a", "虚构甲", reference_cost=reference_cost),
            LexiconEntry("generated-b", "虚构乙", reference_cost=2),
        ), ocr=ScriptedOCR(responses)).summary()
        # Exercise the actual JSON representation rather than Python dataclass tuples.
        row = json.loads(json.dumps(row))
        frame = {"source_sha256": digest(data), "frame_ref": "generated-private-frame"}
        config = {"run_id": "generated-completed-run", "layout_id": layout.layout_id}
        doc = {"revision": 1, "summary": {"revision": 1, "format": "shop-probe-private-run-v1",
                                         "run_id": config["run_id"], "frames": 1}, "frames": [row]}
        return frame, config, doc

    def consume(self, fixture):
        frame, config, doc = fixture
        return match_shop(frame, doc, config, (520, 110), [frame["source_sha256"]], completion_verified=True)

    def assert_unknown_slot(self, result, index=0):
        slot = result["slots"][index]
        self.assertEqual(slot["status"], "unknown")
        self.assertEqual(slot["name_status"], "unknown")
        self.assertEqual(slot["names"], [])
        self.assertEqual(slot["price_status"], "unknown")
        self.assertFalse(result["execution_eligible"])
        self.assertFalse(result["prices_used_for_economy"])

    def test_public_probe_valid_candidate_and_missing_price_remain_usable_as_candidates(self):
        for missing_prices in (False, True):
            with self.subTest(missing_prices=missing_prices):
                result = self.consume(self.fixture(missing_prices=missing_prices))
                self.assertTrue(all(s["names"] == ["虚构甲"] for s in result["slots"]))
                self.assertTrue(all(s["price_status"] == ("unknown" if missing_prices else "candidate")
                                    for s in result["slots"]))

    def test_public_probe_empty_conflict_overlay_and_ocr_failure_keep_safe_states(self):
        def empty(responses):
            for raw in responses: raw.clear()

        def conflict(responses):
            responses[2][0]["text"] = "虚构乙"

        def overlay(responses):
            responses[0].append({"text": "generated-cover", "confidence": 1.0,
                                 "x": 5, "y": 10, "width": 80, "height": 15})

        def ocr_failure(responses):
            responses[0] = TimeoutError("generated failure only")

        for transform, dark, expected in ((empty, True, "empty_candidate"), (conflict, False, "conflict"),
                                          (overlay, False, "occluded_candidate"), (ocr_failure, False, "unknown")):
            with self.subTest(state=expected):
                result = self.consume(self.fixture(dark=dark, transform=transform))
                self.assertEqual(result["slots"][0]["status"], expected)
                self.assertEqual(result["slots"][0]["names"], [])

    def test_unknown_slot_with_overlay_clues_never_displays_name_or_price_candidate(self):
        fixture = self.fixture()
        slot = fixture[2]["frames"][0]["slots"][0]
        slot.update(status="unknown", occlusion="suspected", reasons=["body_overlay_text_cue"])
        result = self.consume(fixture)
        self.assertEqual(result["slots"][0]["status"], "occluded_candidate")
        self.assertEqual(result["slots"][0]["names"], [])
        self.assertEqual(result["slots"][0]["price_status"], "unknown")
        slot["name_options"][0]["name"] = "GENERATED_OVERLAY_NAME_MUST_NOT_RENDER"
        self.assertNotIn("GENERATED_OVERLAY_NAME_MUST_NOT_RENDER", _shop_html({"shop": self.consume(fixture)}))

    def test_each_independent_overlay_indicator_suppresses_identity(self):
        for patch in ({"occlusion": "suspected"}, {"reasons": ["body_overlay_text_cue"]},
                      {"status": "occluded_candidate"}):
            with self.subTest(patch=patch):
                fixture = self.fixture()
                fixture[2]["frames"][0]["slots"][0].update(patch)
                slot = self.consume(fixture)["slots"][0]
                self.assertEqual(slot["status"], "occluded_candidate")
                self.assertEqual(slot["names"], [])
                self.assertEqual(slot["name_status"], "unknown")
                self.assertEqual(slot["price_status"], "unknown")

    def test_name_summary_cannot_hide_conflicting_scale_candidates(self):
        fixture = self.fixture()
        name = fixture[2]["frames"][0]["slots"][0]["name"]
        name["variants"][1].update(candidates=["generated-b"], eligible=["generated-b"])
        result = self.consume(fixture)
        self.assert_unknown_slot(result)
        self.assertEqual(result["slots"][1]["names"], ["虚构甲"])

    def test_price_candidate_requires_a_real_summary_candidate(self):
        fixture = self.fixture()
        fixture[2]["frames"][0]["slots"][0]["price"]["candidates"] = []
        self.assert_unknown_slot(self.consume(fixture))

    def test_whole_slot_candidate_cannot_override_unknown_fields(self):
        fixture = self.fixture(missing_prices=True)
        slot = fixture[2]["frames"][0]["slots"][0]
        slot["name"] = copy.deepcopy(slot["price"])
        slot["name_options"] = []
        slot["status"] = "candidate"
        slot["reasons"] = []
        self.assert_unknown_slot(self.consume(fixture))

    def test_missing_rejected_or_unexplained_scale_cannot_be_summarized_as_candidate(self):
        for field in ("name", "price"):
            for case in ("missing", "low_score", "unexplained", "summary_reason", "wrong_union", "wrong_status"):
                with self.subTest(field=field, case=case):
                    fixture = self.fixture()
                    item = fixture[2]["frames"][0]["slots"][0][field]
                    vote = item["variants"][1]
                    if case == "missing": vote.update(candidates=[], eligible=[], reasons=["no_text_in_roi"])
                    if case == "low_score": vote.update(eligible=[], reasons=["low_ocr_confidence"])
                    if case == "unexplained": vote["reasons"] = ["outside_vocabulary" if field == "name" else "invalid_price_token"]
                    if case == "summary_reason": item["reasons"] = ["low_ocr_confidence"]
                    if case == "wrong_union": item["candidates"] = ["generated-b"] if field == "name" else [4]
                    if case == "wrong_status": item["status"] = "unknown"
                    self.assert_unknown_slot(self.consume(fixture))

    def test_scales_must_be_complete_unique_integer_and_consistent_between_fields(self):
        for case in ("absent", "empty", "single", "duplicate", "bool", "zero", "five", "reversed", "other_scales"):
            with self.subTest(case=case):
                fixture = self.fixture()
                name = fixture[2]["frames"][0]["slots"][0]["name"]
                if case == "absent": name.pop("variants")
                if case == "empty": name["variants"] = []
                if case == "single": name["variants"].pop()
                if case in ("duplicate", "bool", "zero", "five", "other_scales"):
                    name["variants"][1]["scale"] = {"duplicate": 1, "bool": True, "zero": 0, "five": 5, "other_scales": 3}[case]
                if case == "reversed": name["variants"].reverse()
                self.assert_unknown_slot(self.consume(fixture))

    def test_all_five_slots_must_agree_on_scale_sequence(self):
        fixture = self.fixture()
        for field in ("name", "price"):
            fixture[2]["frames"][0]["slots"][0][field]["variants"][1]["scale"] = 3
        result = self.consume(fixture)
        for index in range(5): self.assert_unknown_slot(result, index)

    def test_malformed_evidence_types_or_eligible_outside_candidates_are_unknown(self):
        for field in ("name", "price"):
            for case in ("wrong_candidate_type", "eligible_outside", "duplicate_candidate", "bad_reason_type", "bad_vote_type"):
                with self.subTest(field=field, case=case):
                    fixture = self.fixture()
                    item = fixture[2]["frames"][0]["slots"][0][field]
                    if case == "wrong_candidate_type": item["variants"][0]["candidates"] = [7] if field == "name" else [True]
                    if case == "eligible_outside": item["variants"][0]["eligible"] = ["generated-b"] if field == "name" else [4]
                    if case == "duplicate_candidate": item["candidates"] *= 2
                    if case == "bad_reason_type": item["variants"][0]["reasons"] = {"no_text_in_roi": True}
                    if case == "bad_vote_type": item["variants"][0] = None
                    self.assert_unknown_slot(self.consume(fixture))

    def test_slot_reasons_presence_and_reference_flags_cannot_contradict_fields(self):
        for patch in ({"status": "unknown"}, {"presence": "empty_candidate"}, {"occlusion": "unknown"},
                      {"reference_cost_check": "differs_from_unverified_reference"},
                      {"reasons": ["unverified_reference_cost_disagreement"]},
                      {"status": "empty_candidate", "presence": "empty_candidate",
                       "reasons": ["dark_low_variance_and_all_ocr_blank"]}):
            with self.subTest(patch=patch):
                fixture = self.fixture()
                fixture[2]["frames"][0]["slots"][0].update(patch)
                self.assert_unknown_slot(self.consume(fixture))

    def test_consistent_reference_disagreement_does_not_fill_or_override_price(self):
        result = self.consume(self.fixture(reference_cost=9))
        self.assertEqual(result["slots"][0]["status"], "unknown")
        self.assertEqual(result["slots"][0]["names"], ["虚构甲"])
        self.assertEqual(result["slots"][0]["price_status"], "candidate")
        self.assertNotIn('"reference_cost":', json.dumps(result))
        self.assertFalse(result["prices_used_for_economy"])

    def test_flat_dark_text_contradiction_never_releases_usable_fields(self):
        result = self.consume(self.fixture(dark=True))
        for index in range(5): self.assert_unknown_slot(result, index)

    def test_matching_completed_file_hashes_do_not_override_semantic_checks(self):
        fixture = self.fixture()
        frame, config, document = fixture
        slot = document["frames"][0]["slots"][0]
        slot.update(status="unknown", occlusion="suspected", reasons=["body_overlay_text_cue"])
        slots = document["frames"][0]["slots"]
        slots[1]["name"]["variants"][1].update(candidates=["generated-b"], eligible=["generated-b"])
        slots[2]["price"]["candidates"] = []
        unknown = self.fixture(missing_prices=True)[2]["frames"][0]["slots"][0]["price"]
        slots[3].update(name=copy.deepcopy(unknown), price=copy.deepcopy(unknown), name_options=[])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            os.chmod(root, 0o700)
            files = {"results.private.json": json.dumps(document).encode(),
                     "summary.json": json.dumps(document["summary"]).encode(), "generated.html": b"<p>generated only</p>"}
            for name, data in files.items():
                with os.fdopen(os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                    out.write(data)
            marker = json.dumps({"revision": 1, "run_id": config["run_id"],
                                 "files_sha256": {name: digest(data) for name, data in files.items()}}).encode()
            with os.fdopen(os.open(root / "COMPLETED.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                out.write(marker)
            config.update(directory=str(root), completed_sha256=digest(marker))
            profile = SimpleNamespace(shop_config=config, key=SimpleNamespace(geometry=SimpleNamespace(image_size=(520, 110))))
            attach_shop([frame], profile)
        result = frame["shop"]
        self.assertTrue(result["completion_verified"])
        self.assertEqual(result["slots"][0]["status"], "occluded_candidate")
        self.assertEqual(result["slots"][0]["names"], [])
        self.assertEqual(result["slots"][0]["price_status"], "unknown")
        for index in (1, 2, 3): self.assert_unknown_slot(result, index)
        self.assertEqual(result["slots"][4]["names"], ["虚构甲"])
        self.assertFalse(result["execution_eligible"])


if __name__ == "__main__":
    unittest.main()
