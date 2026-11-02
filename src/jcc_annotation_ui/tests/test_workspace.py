from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from unittest.mock import patch

from src.jcc_annotation_ui.workspace import AnnotationWorkspace, EDITABLE_FIELDS, OCCLUSION_PREFIX
from src.jcc_annotation_ui.synthetic import make_synthetic
from src.jcc_samples import WorkflowError
from src.jcc_annotation_ui.tests.helpers import PrivateFixture, REGION


class WorkspaceTests(PrivateFixture):
    def test_read_and_image_are_pure_and_all_unknown(self):
        before = self.snapshot()
        listing = self.ui.samples()
        view = self.view()
        raw, mime = self.ui.image(self.anchor(view))
        self.assertEqual(listing["samples"][0]["known_fields"], 0)
        self.assertEqual(mime, "image/png")
        self.assertTrue(raw.startswith(b"\x89PNG"))
        self.assertIsNone(view["record"]["asset"])
        self.assertTrue(all(view["record"]["labels"][k]["value"] is None for k in EDITABLE_FIELDS))
        self.assertTrue(all(s["content"]["value"] is None for s in view["record"]["labels"]["shop"]))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(list((self.root / "edits").iterdir()), [])

    def test_explicit_save_preserves_sample_identity_and_readonly_labels(self):
        original = deepcopy(self.view()["record"])
        result = self.ui.annotate(self.known(self.edit()))
        current = self.view()["record"]
        self.assertEqual(result["revision"], 2)
        self.assertEqual(current["labels"]["gold"]["value"], 32)
        for key in original.keys() - {"labels", "conditions", "annotator_id", "reviewer_id", "review_status"}:
            self.assertEqual(current[key], original[key])
        for key in ("mode", "game_version", "shop"):
            self.assertEqual(current["labels"][key], original["labels"][key])
        self.assertEqual(list((self.root / "edits").iterdir()), [])
        self.assertEqual(self.data()["events"][-1]["action"], "annotate")

    def test_unknown_applicable_occluded_and_not_applicable_round_trip(self):
        edit = self.edit()
        edit["fields"]["gold"].update(applicability="applicable", reason="occluded", region=deepcopy(REGION))
        edit["fields"]["round"].update(applicability="not_applicable", reason="not_applicable")
        edit["occlusions"] = [deepcopy(REGION)]
        self.ui.annotate(edit)
        view = self.view()
        self.assertEqual(view["occlusions"], [REGION])
        self.assertEqual(view["record"]["labels"]["gold"]["value"], None)
        self.assertEqual(view["record"]["labels"]["round"]["applicability"], "not_applicable")
        self.assertTrue(any(c.startswith(OCCLUSION_PREFIX) for c in self.data()["annotations"][0]["conditions"]))

    def test_known_fields_require_region(self):
        self.rejects("known_region_required", lambda: self.ui.annotate(self.known(self.edit(), region=None)))

    def test_zero_is_explicit_valid_and_unknown_is_never_zero(self):
        self.ui.annotate(self.known(self.edit(), value=0))
        self.assertEqual(self.view()["record"]["labels"]["gold"]["value"], 0)
        edit = self.edit()
        edit["fields"]["gold"].update(truth_status="unknown", value=0, reason="occluded")
        self.rejects("invalid_annotations", lambda: self.ui.annotate(edit))

    def test_overlap_rejects_known_even_partial_but_touching_edge_is_valid(self):
        for covered in (REGION, {"x_min":.29,"y_min":.39,"x_max":.5,"y_max":.5}):
            edit = self.known(self.edit()); edit["occlusions"] = [deepcopy(covered)]
            self.rejects("occluded_known_field", lambda: self.ui.annotate(edit))
        edit = self.known(self.edit()); edit["occlusions"] = [{"x_min":.3,"y_min":.2,"x_max":.5,"y_max":.4}]
        self.ui.annotate(edit)

    def test_bad_geometry_and_injected_fields_reject(self):
        for region in ({**REGION, "x_max": 2}, {**REGION, "x_max": .1}, {**REGION, "x_min": True}):
            edit = self.edit(); edit["occlusions"] = [region]
            self.rejects("invalid_region", lambda: self.ui.annotate(edit))
        for field, value in (("sample_kind", "real"), ("split", "acceptance"), ("asset", {}), ("recording_session_id", "other")):
            edit = self.edit(); edit[field] = value
            self.rejects("sample_kind_boundary" if field == "sample_kind" else "invalid_request", lambda: self.ui.annotate(edit))
        edit = self.edit(); edit["fields"]["shop"] = []
        self.rejects("unsupported_fields", lambda: self.ui.annotate(edit))

    def test_conditions_reject_reserved_prefix_duplicate_or_invalid_types(self):
        for conditions in ([OCCLUSION_PREFIX + "[]"], ["a","a"], [None], ["a\nb"]):
            edit = self.edit(); edit["conditions"] = conditions
            self.rejects("invalid_conditions", lambda: self.ui.annotate(edit))

    def test_changed_workspace_rejects_old_edit_review_and_image(self):
        view = self.view()
        self.ui.annotate(self.edit(view))
        for operation in (lambda: self.ui.annotate(self.edit(view)), lambda: self.ui.review(self.review(view)),
                          lambda: self.ui.image(self.anchor(view))):
            self.rejects("revision_conflict", operation)

    def test_token_cannot_cross_samples_sessions_or_record_changes(self):
        second = self.add_image(color=(80,30,50))
        edit = self.edit(); edit["sample_id"] = second
        self.rejects("view_expired", lambda: self.ui.annotate(edit))

    def test_same_revision_record_rewrite_invalidates_the_signed_view(self):
        edit = self.edit()
        data = self.data()
        data["annotations"][0]["conditions"] = ["synthetic-external-rewrite"]
        (self.root / "workspace.json").write_text(json.dumps(data))
        self.rejects("view_expired", lambda: self.ui.annotate(edit))
        other = AnnotationWorkspace(self.root, sample_kind="synthetic")
        self.rejects("view_expired", lambda: other.annotate(self.edit()))
        edit = self.edit(); edit["view_token"] = "非" * 64
        self.rejects("view_expired", lambda: self.ui.annotate(edit))

    def test_nonblocking_store_lock_is_reused(self):
        with self.store._locked():
            self.rejects("workspace_busy", self.ui.samples)

    def test_simultaneous_old_revision_only_one_save_succeeds(self):
        edit_a = self.known(self.edit(), value=12)
        edit_b = deepcopy(edit_a); edit_b["fields"]["gold"]["value"] = 13
        def save(edit):
            try:
                return self.ui.annotate(edit)["status"]
            except WorkflowError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(save, (edit_a, edit_b)))
        self.assertCountEqual(results, ["annotation_saved", "revision_conflict"])
        self.assertEqual(self.data()["revision"], 2)
        self.assertIn(self.view()["record"]["labels"]["gold"]["value"], (12,13))

    def test_writer_between_adapter_check_and_store_commit_is_rejected_and_edit_removed(self):
        edit = self.known(self.edit())
        annotate = self.ui.store.annotate
        def racing_write(relative, *, annotator_id):
            self.store.review(self.sample_id, reviewer_id="synthetic-b", expected_revision=1, evidence_ref="synthetic-race")
            return annotate(relative, annotator_id=annotator_id)
        with patch.object(self.ui.store, "annotate", side_effect=racing_write):
            self.rejects("revision_conflict", lambda: self.ui.annotate(edit), unchanged=False)
        self.assertEqual(self.data()["revision"], 2)
        self.assertIsNone(self.view()["record"]["labels"]["gold"]["value"])
        self.assertEqual(list((self.root / "edits").iterdir()), [])

    def test_store_write_failure_keeps_truth_and_removes_temp_edit(self):
        with patch("src.jcc_samples.workflow._atomic_save", side_effect=OSError("private-detail-never-print")):
            before = self.snapshot()
            with self.assertRaises(OSError):
                self.ui.annotate(self.edit())
            self.assertEqual(before, self.snapshot())

    def test_review_requires_explicit_type_confirmation_and_different_code(self):
        review = self.review(); review["confirmation"] = "different_human_checked"
        self.rejects("review_confirmation_required", lambda: self.ui.review(review))
        review = self.review(); review["reviewer_id"] = "synthetic-a"
        self.rejects("independent_review_required", lambda: self.ui.review(review))
        review = self.review(); review["evidence_ref"] = ""
        self.rejects("invalid_identifier", lambda: self.ui.review(review))

    def test_synthetic_review_binding_then_annotation_invalidates_it(self):
        before = self.view()["record"]["labels"]
        self.ui.review(self.review())
        current = self.view()
        self.assertTrue(current["review_binding"]["current"])
        self.assertEqual(current["record"]["labels"], before)
        self.assertEqual(current["record"]["annotation_method"], "synthetic")
        self.ui.annotate(self.edit(current))
        after = self.view()
        self.assertEqual(after["record"]["review_status"], "draft")
        self.assertIsNone(after["record"]["reviewer_id"])
        self.assertFalse(after["review_binding"]["current"])

    def test_adjudication_never_invents_unknown_answers(self):
        review = self.review()
        review["status"] = "adjudicated"
        self.ui.review(review)
        current = self.view()
        self.assertEqual(current["record"]["review_status"], "adjudicated")
        self.assertTrue(all(current["record"]["labels"][k]["value"] is None for k in EDITABLE_FIELDS))
        self.assertEqual(current["record"]["split"], "quarantine")

    def test_review_binding_tamper_is_visible_as_stale(self):
        self.ui.review(self.review())
        data = self.data(); data["events"][-1]["labels_sha256"] = "0" * 64
        (self.root / "workspace.json").write_text(json.dumps(data))
        self.assertIn("review_binding_stale", self.view()["warnings"])
        self.assertFalse(self.view()["review_binding"]["current"])

    def test_real_wire_shape_uses_no_real_people_or_game_images(self):
        self.real_fixture()
        original = deepcopy(self.view()["record"])
        self.ui.annotate(self.known(self.edit(), "hp", 78))
        review = self.review(); review["confirmation"] = "synthetic_software_check"
        self.rejects("review_confirmation_required", lambda: self.ui.review(review))
        review["confirmation"] = "different_human_checked"
        self.ui.review(review)  # Fictional attestation tests the interface only.
        current = self.view()["record"]
        self.assertEqual(current["asset"], original["asset"])
        self.assertEqual(current["annotation_method"], "human")
        self.assertEqual(current["split"], "quarantine")
        self.assertTrue(self.view()["review_binding"]["current"])

    def test_acceptance_edit_is_frozen_and_split_never_exposed(self):
        self.real_fixture()
        review = self.review(); review["confirmation"] = "different_human_checked"
        self.ui.review(review)
        self.store.assign_split(["synthetic-session-0"], split="acceptance", expected_revision=2)
        self.rejects("acceptance_frozen", lambda: self.ui.annotate(self.edit()))

    def test_wrong_kind_mixed_workspace_and_type_changes_are_rejected(self):
        self.rejects("sample_kind_boundary", lambda: AnnotationWorkspace(self.root, sample_kind="real"))
        self.add_image(kind="real", color=(1,2,3))
        self.rejects("sample_kind_boundary", self.ui.samples)
        self.rejects("sample_kind_boundary", lambda: AnnotationWorkspace(self.root, sample_kind="synthetic"))

    def test_corrupt_json_image_binding_permission_and_links_fail_closed(self):
        for corruption, expected in (("json", "invalid_json"), ("image", "asset_integrity"),
                                     ("binding", "asset_integrity"), ("permission", "unsafe_permissions")):
            with self.subTest(corruption=corruption):
                backup = self.snapshot()
                data = self.data(); asset = self.root / data["assets"][self.sample_id]["path"]
                if corruption == "json": (self.root / "workspace.json").write_text("broken")
                if corruption == "image": asset.write_bytes(b"not png")
                if corruption == "binding":
                    data["assets"][self.sample_id]["sha256"] = "0" * 64
                    (self.root / "workspace.json").write_text(json.dumps(data))
                if corruption == "permission": asset.chmod(0o644)
                self.rejects(expected, self.ui.samples)
                for name, raw in backup.items():
                    p = self.root / name; p.write_bytes(raw); p.chmod(0o600)
        asset = self.root / self.data()["assets"][self.sample_id]["path"]
        asset.unlink(); asset.symlink_to(self.base / "generated-input.png")
        self.rejects("path_escape", self.ui.samples)

    def test_demo_creates_only_synthetic_unknown_samples_and_never_overwrites(self):
        root = self.base / "demo"
        summary = make_synthetic(root)
        ui = AnnotationWorkspace(root, sample_kind="synthetic")
        self.assertEqual(summary["synthetic_samples"], 2)
        self.assertEqual(summary["real_samples"], 0)
        self.assertEqual(summary["reviewed_samples"], 0)
        self.assertTrue(all(s["known_fields"] == 0 for s in ui.samples()["samples"]))
        with self.assertRaises(WorkflowError) as caught:
            make_synthetic(root)
        self.assertEqual(caught.exception.code, "output_exists")
