"""Generated pixels and fictional metadata only; these are not real samples."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from src.jcc_device.game_state_contract import CORE_FIELDS, validate_annotations
from src.jcc_samples import SampleStore, WorkflowError, read_annotation_manifest
from src.jcc_samples.__main__ import main
from src.jcc_samples import workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jcc-synthetic-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "private-store"
        self.store = SampleStore.create(self.root, dataset_id="synthetic-workflow-v1")

    def image(self, name="input.png", *, color=(30, 70, 110), size=(32, 20), metadata=None):
        path = self.base / name
        options = {} if metadata is None else {"pnginfo": metadata}
        Image.new("RGB", size, color).save(path, **options)
        return path

    def imported(self, *, kind="synthetic", session="session-a", group="group-a", color=(30, 70, 110), size=(32, 20)):
        return self.store.import_image(self.image(color=color, size=size), sample_kind=kind,
                                       recording_session_id=session, dedupe_group_id=group,
                                       state_group_id="state-a", annotator_id="annotator-a")

    def workspace(self):
        return json.loads((self.root / "workspace.json").read_text())

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def fails(self, code, call):
        before = self.snapshot()
        with self.assertRaises(WorkflowError) as caught:
            call()
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(self.snapshot(), before, "Rejected operation changed the workspace")

    def edit(self, sample_id, *, update=None, output="edits/draft.json"):
        self.store.edit_template(sample_id, output)
        path = self.root / output
        edit = json.loads(path.read_text())
        if update:
            update(edit)
        path.write_text(json.dumps(edit), encoding="utf-8")
        return output

    def review(self, sample_id, *, reviewer="reviewer-b"):
        return self.store.review(sample_id, reviewer_id=reviewer, evidence_ref="synthetic-review-evidence",
                                 expected_revision=self.workspace()["revision"])

    def test_import_synthetic_full_unknown_and_private_index(self):
        image = self.image()
        result = self.store.import_image(image, sample_kind="synthetic", recording_session_id="session-a",
                                         dedupe_group_id="group-a", state_group_id="state-a", annotator_id="annotator-a")
        data = self.workspace()
        record = data["annotations"][0]
        validate_annotations([record])
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["real_samples"], 0)
        self.assertEqual(record["split"], "quarantine")
        self.assertIsNone(record["asset"])
        self.assertEqual(record["annotation_method"], "synthetic")
        self.assertIsNone(record["frame"]["device_capture_time"])
        self.assertIsNone(record["frame"]["image"]["game_viewport"])
        self.assertEqual(record["frame"]["image"]["width_px"], 32)
        labels = [record["labels"][key] for key in CORE_FIELDS]
        labels += [slot["content"] for slot in record["labels"]["shop"]]
        self.assertEqual(len(labels), 13)
        self.assertTrue(all(label == workflow._unknown() for label in labels))
        asset = data["assets"][result["sample_id"]]
        self.assertEqual(asset["sha256"], hashlib.sha256(image.read_bytes()).hexdigest())
        self.assertEqual((self.root / asset["path"]).read_bytes(), image.read_bytes())
        self.assertNotIn(str(image), json.dumps(data))
        self.assertEqual(data["events"][0]["time_basis"], "host_local_import_decode")

    def test_real_shape_is_explicit_and_not_a_real_game_fixture(self):
        # The pixels are generated in this test. "real" tests the wire shape,
        # not the provenance or existence of any actual game recording.
        self.imported(kind="real", size=(20, 32))
        record = self.workspace()["annotations"][0]
        self.assertEqual(record["annotation_method"], "human")
        self.assertEqual(record["asset"]["access"], "local_only")
        self.assertEqual(record["review_status"], "draft")
        self.assertEqual(record["frame"]["image"]["orientation"], "portrait")
        validate_annotations([record])

    def test_permissions_and_init_never_adopts_existing_directory(self):
        for path in (self.root, self.root / "assets", self.root / "edits", self.root / "manifests"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.imported()
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.fails("output_exists", lambda: SampleStore.create(self.root, dataset_id="other"))

    def test_manifest_export_read_and_validate_exact_assets(self):
        self.imported()
        output = "manifests/frozen-1.json"
        result = self.store.export_manifest(output)
        records = read_annotation_manifest(self.root / output)
        self.assertEqual(records, self.workspace()["annotations"])
        self.assertEqual(result["manifest_sha256"], hashlib.sha256((self.root / output).read_bytes()).hexdigest())
        self.assertEqual(self.store.validate(output)["samples"], 1)
        self.fails("output_exists", lambda: self.store.export_manifest(output))

    def test_exact_duplicate_renamed_input_rejected(self):
        self.imported()
        copy = self.base / "renamed-with-sensitive-name.png"
        copy.write_bytes(self.image().read_bytes())
        self.fails("duplicate_image", lambda: self.store.import_image(copy, sample_kind="synthetic",
                   recording_session_id="session-b", dedupe_group_id="group-b", state_group_id="state-b", annotator_id="annotator-a"))

    def test_same_pixels_reencoded_with_other_metadata_rejected(self):
        self.imported()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Description", "synthetic metadata only")
        copy = self.image("other.png", metadata=metadata)
        self.assertNotEqual(hashlib.sha256(copy.read_bytes()).hexdigest(), self.workspace()["assets"][self.workspace()["annotations"][0]["sample_id"]]["sha256"])
        self.fails("duplicate_image", lambda: self.store.import_image(copy, sample_kind="synthetic",
                   recording_session_id="session-b", dedupe_group_id="group-b", state_group_id="state-b", annotator_id="annotator-a"))

    def test_import_corrupt_missing_truncated_and_unsupported_inputs_roll_back(self):
        corrupt = self.base / "corrupt.png"
        corrupt.write_bytes(b"not an image")
        truncated = self.base / "truncated.png"
        truncated.write_bytes(self.image().read_bytes()[:48])
        unsupported = self.base / "input.gif"
        Image.new("RGB", (2, 2)).save(unsupported)
        for path, code in ((corrupt, "image_unreadable"), (truncated, "image_unreadable"),
                           (self.base / "missing.png", "file_unreadable"), (unsupported, "unsupported_image")):
            with self.subTest(code=code, name=path.name):
                self.fails(code, lambda: self.store.import_image(path, sample_kind="synthetic",
                           recording_session_id="session-a", dedupe_group_id="group-a", state_group_id="state-a", annotator_id="annotator-a"))
        self.assertEqual(self.store.validate()["revision"], 0)

    def test_exif_rotation_requires_explicit_preprocessing(self):
        source = self.base / "rotated.jpg"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (32, 20)).save(source, exif=exif)
        self.fails("image_orientation", lambda: self.store.import_image(source, sample_kind="synthetic",
                   recording_session_id="session-a", dedupe_group_id="group-a", state_group_id="state-a", annotator_id="annotator-a"))

    def test_input_size_and_pixel_limits(self):
        source = self.image()
        with patch.object(workflow, "MAX_IMAGE_BYTES", 2):
            self.fails("input_too_large", lambda: self.store.import_image(source, sample_kind="synthetic",
                       recording_session_id="session-a", dedupe_group_id="group-a", state_group_id="state-a", annotator_id="annotator-a"))
        with patch.object(workflow, "MAX_PIXELS", 2):
            self.fails("image_too_large", lambda: self.store.import_image(source, sample_kind="synthetic",
                       recording_session_id="session-a", dedupe_group_id="group-a", state_group_id="state-a", annotator_id="annotator-a"))

    def test_failed_commit_removes_installed_image_and_temporary(self):
        source = self.image()
        original_replace = workflow.os.replace
        def disk_failure(src, dst):
            if Path(dst).name == "workspace.json":
                raise OSError("synthetic disk failure; do not echo this")
            return original_replace(src, dst)
        before = self.snapshot()
        with patch.object(workflow.os, "replace", side_effect=disk_failure):
            with self.assertRaises(OSError):
                self.store.import_image(source, sample_kind="synthetic", recording_session_id="session-a",
                                         dedupe_group_id="group-a", state_group_id="state-a", annotator_id="annotator-a")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store.validate()["samples"], 0)

    def test_destination_collision_is_not_deleted_or_overwritten(self):
        fixed = "a" * 32
        collision = self.root / "assets" / ("asset-" + fixed + ".png")
        collision.write_bytes(b"pre-existing private file")
        collision.chmod(0o600)
        with patch.object(workflow.uuid, "uuid4") as mock_id:
            mock_id.return_value.hex = fixed
            self.fails("output_exists", lambda: self.imported())
        self.assertEqual(collision.read_bytes(), b"pre-existing private file")

    def test_atomic_temporary_collision_preserves_previous_file_and_rolls_back_import(self):
        fixed = "b" * 32
        collision = self.root / (".workspace-" + fixed + ".tmp")
        collision.write_bytes(b"pre-existing private recovery material")
        collision.chmod(0o600)
        with patch.object(workflow.uuid, "uuid4") as mock_id:
            mock_id.return_value.hex = fixed
            self.fails("output_exists", lambda: self.imported())
        self.assertEqual(collision.read_bytes(), b"pre-existing private recovery material")

    def test_local_private_directory_guards(self):
        cases = [(Path("relative-output"), "unsafe_private_dir"),
                 (workflow.PROJECT_ROOT / "src" / "private-samples", "source_output_forbidden"),
                 (self.base / "Dropbox" / "samples", "cloud_output_forbidden"),
                 (self.base / "CloudStorage" / "Drive" / "samples", "cloud_output_forbidden")]
        repo = self.base / "other-repo"
        repo.mkdir()
        (repo / ".git").write_text("synthetic worktree marker")
        cases.append((repo / "samples", "source_output_forbidden"))
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        cases.append((alias / "new-store", "unsafe_private_dir"))
        for path, code in cases:
            with self.subTest(code=code):
                self.fails(code, lambda: SampleStore.create(path, dataset_id="synthetic"))

    def test_private_directory_permission_regression_rejected(self):
        self.root.chmod(0o755)
        self.addCleanup(self.root.chmod, 0o700)
        self.fails("unsafe_permissions", lambda: self.store.validate())

    def test_export_traversal_absolute_and_protected_paths_rejected(self):
        for output, code in (("../escaped.json", "invalid_output"), ("/tmp/escaped.json", "invalid_output"),
                             ("manifests/../workspace.json", "invalid_output"), ("manifests/..", "invalid_output"),
                             ("workspace.json", "invalid_output"), ("assets/replace.json", "invalid_output")):
            with self.subTest(output=output):
                self.fails(code, lambda: self.store.export_manifest(output))
        self.assertFalse((self.base / "escaped.json").exists())

    def test_symlink_destination_parent_and_read_escape_rejected(self):
        link = self.root / "manifests" / "link.json"
        outside = self.base / "outside.json"
        outside.write_text("[]")
        link.symlink_to(outside)
        self.fails("path_escape", lambda: self.store.export_manifest("manifests/link.json"))
        self.fails("path_escape", lambda: self.store.validate("manifests/link.json"))
        link.unlink()
        (self.root / "manifests").rmdir()
        (self.root / "manifests").symlink_to(self.base, target_is_directory=True)
        self.fails("path_escape", lambda: self.store.export_manifest("manifests/outside.json"))
        self.assertEqual(outside.read_text(), "[]")

    def test_schema_rejects_unknown_as_empty_and_known_without_region(self):
        result = self.imported()
        sample_id = result["sample_id"]
        for index, mutation in enumerate((
                lambda edit: edit["labels"]["gold"].update(value=0),
                lambda edit: edit["labels"]["shop"][0]["content"].update(value={"kind": "empty"}),
                lambda edit: edit["labels"]["shop"][0]["content"].update(applicability="applicable", truth_status="known", value={"kind": "empty"}, reason=None))):
            output = self.edit(sample_id, update=mutation, output=f"edits/invalid-{index}.json")
            self.fails("invalid_annotations", lambda: self.store.annotate(output, annotator_id="annotator-a"))

    def test_annotation_changes_only_labels_and_resets_review(self):
        sample_id = self.imported()["sample_id"]
        self.review(sample_id)
        previous = deepcopy(self.workspace()["annotations"][0])
        def update(edit):
            edit["labels"]["gold"].update(applicability="applicable", truth_status="known", value=12, reason=None)
            edit["labels"]["shop"][0]["content"].update(applicability="applicable", truth_status="known", value={"kind": "empty"}, reason=None,
                                                          region={"x_min": 0.1, "y_min": 0.7, "x_max": 0.2, "y_max": 0.9})
            edit["conditions"] = ["synthetic_normal"]
        output = self.edit(sample_id, update=update)
        self.store.annotate(output, annotator_id="annotator-c")
        record = self.workspace()["annotations"][0]
        self.assertEqual(record["labels"]["gold"]["value"], 12)
        self.assertEqual(record["review_status"], "draft")
        self.assertIsNone(record["reviewer_id"])
        for key in previous.keys() - {"labels", "conditions", "annotator_id", "review_status", "reviewer_id"}:
            self.assertEqual(record[key], previous[key])

    def test_review_requires_independent_actor_and_fresh_revision(self):
        sample_id = self.imported()["sample_id"]
        self.fails("independent_review_required", lambda: self.review(sample_id, reviewer="annotator-a"))
        self.fails("revision_conflict", lambda: self.store.review(sample_id, reviewer_id="reviewer-b", expected_revision=0, evidence_ref="review-a"))
        original = deepcopy(self.workspace()["annotations"][0])
        self.review(sample_id)
        record = self.workspace()["annotations"][0]
        self.assertEqual(record["labels"], original["labels"])
        self.assertEqual(record["split"], original["split"])
        self.assertEqual(record["review_status"], "independently_reviewed")
        self.assertIn("labels_sha256", self.workspace()["events"][-1])

    def test_stale_edit_template_and_invalid_json_cannot_overwrite(self):
        sample_id = self.imported()["sample_id"]
        output = self.edit(sample_id)
        self.review(sample_id)
        self.fails("revision_conflict", lambda: self.store.annotate(output, annotator_id="annotator-a"))
        for content in ('{"x":NaN}', '{"x":1,"x":2}', 'not-json', '[1e999]'):
            path = self.root / "edits" / "bad.json"
            path.write_text(content)
            path.chmod(0o600)
            self.fails("invalid_json", lambda: self.store.annotate("edits/bad.json", annotator_id="annotator-a"))

    def test_synthetic_cannot_enter_real_splits(self):
        self.imported()
        self.fails("synthetic_split_forbidden", lambda: self.store.assign_split(["session-a"], split="acceptance", expected_revision=1))
        self.fails("synthetic_split_forbidden", lambda: self.store.assign_split(["session-a"], split="development", expected_revision=1))

    def test_acceptance_requires_separate_review_and_whole_session_move(self):
        sample_a = self.imported(kind="real")["sample_id"]
        sample_b = self.imported(kind="real", group="group-b", color=(50, 70, 110))["sample_id"]
        self.fails("acceptance_review_required", lambda: self.store.assign_split(["session-a"], split="acceptance", expected_revision=2))
        self.review(sample_a)
        self.review(sample_b)
        result = self.store.assign_split(["session-a"], split="acceptance", expected_revision=4)
        self.assertEqual(result["affected_samples"], 2)
        self.assertEqual(result["splits"]["acceptance"], 2)
        self.assertEqual(len({record["recording_session_id"] for record in self.workspace()["annotations"]}), 1)
        output = self.edit(sample_a)
        self.fails("acceptance_frozen", lambda: self.store.annotate(output, annotator_id="annotator-a"))

    def test_dedupe_connected_sessions_move_together_and_representative_rule_blocks_acceptance(self):
        for session, color in (("session-a", (20, 30, 40)), ("session-b", (25, 35, 45))):
            self.imported(kind="real", session=session, group="near-duplicate-a", color=color)
        result = self.store.assign_split(["session-a"], split="development", expected_revision=2)
        self.assertEqual(result["affected_sessions"], 2)
        self.assertEqual(result["affected_samples"], 2)
        for record in self.workspace()["annotations"]:
            self.review(record["sample_id"])
        self.fails("acceptance_duplicate", lambda: self.store.assign_split(["session-b"], split="acceptance", expected_revision=5))

    def test_import_does_not_silently_join_a_preassigned_split(self):
        self.imported(kind="real")
        self.store.assign_split(["session-a"], split="development", expected_revision=1)
        self.fails("split_isolation", lambda: self.imported(kind="real", group="group-b", color=(1, 2, 3)))

    def test_external_manifest_cannot_leak_session_or_dedupe_groups(self):
        self.imported(kind="real")
        self.imported(kind="real", group="group-b", color=(1, 2, 3))
        records = self.workspace()["annotations"]
        records[0]["split"] = "development"
        path = self.root / "manifests" / "leak.json"
        path.write_text(json.dumps(records))
        path.chmod(0o600)
        self.fails("split_isolation", lambda: self.store.validate("manifests/leak.json"))
        records[1]["recording_session_id"] = "session-b"
        records[1]["dedupe_group_id"] = records[0]["dedupe_group_id"]
        path.write_text(json.dumps(records))
        self.fails("split_isolation", lambda: self.store.validate("manifests/leak.json"))

    def test_tampered_asset_hash_geometry_binding_and_index_rejected(self):
        self.imported(kind="real")
        original = self.workspace()
        mutations = [
            ("asset_integrity", lambda data: data["assets"][data["annotations"][0]["sample_id"]].update(sha256="0" * 64)),
            ("asset_geometry", lambda data: data["annotations"][0]["frame"]["image"].update(width_px=33)),
            ("asset_binding", lambda data: data["annotations"][0]["asset"].update(sha256="0" * 64)),
            ("asset_mapping", lambda data: data["assets"][data["annotations"][0]["sample_id"]].update(path="../outside.png")),
        ]
        for code, mutation in mutations:
            data = deepcopy(original)
            mutation(data)
            (self.root / "workspace.json").write_text(json.dumps(data))
            self.fails(code, lambda: self.store.validate())
        (self.root / "workspace.json").write_text(json.dumps(original))
        asset = next(iter(original["assets"].values()))
        (self.root / asset["path"]).write_bytes(self.image(color=(4, 5, 6)).read_bytes())
        self.fails("asset_integrity", lambda: self.store.validate())

    def test_file_permissions_and_hardlink_aliases_rejected(self):
        self.imported()
        workspace = self.root / "workspace.json"
        workspace.chmod(0o644)
        self.fails("unsafe_permissions", lambda: self.store.validate())
        workspace.chmod(0o600)
        alias = self.base / "hardlinked-workspace.json"
        os.link(workspace, alias)
        self.fails("unsafe_permissions", lambda: self.store.validate())

    def test_nonblocking_lock_rejects_concurrent_mutation(self):
        with (self.root / ".lock").open("r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fails("workspace_busy", lambda: self.store.validate())

    def test_cli_end_to_end_reports_only_counts_and_codes(self):
        image = self.image("sensitive-player-and-device-name.png")
        out, err = io.StringIO(), io.StringIO()
        args = ["--private-dir", str(self.root), "import", "--image", str(image), "--sample-kind", "synthetic",
                "--recording-session-id", "session-a", "--dedupe-group-id", "group-a",
                "--state-group-id", "state-a", "--annotator-id", "annotator-a"]
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(args), 0)
        self.assertEqual(json.loads(out.getvalue())["samples"], 1)
        self.assertNotIn(image.name, out.getvalue() + err.getvalue())
        self.assertNotIn("labels", out.getvalue())
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(main(args), 2)
        self.assertEqual(json.loads(err.getvalue())["error"]["code"], "duplicate_image")

    def test_cli_parser_does_not_echo_sensitive_invalid_arguments(self):
        error = io.StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as caught:
            main(["--private-dir", str(self.root), "import", "--sample-kind", "sensitive-player-device-text"])
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn("sensitive-player", error.getvalue())
        self.assertEqual(json.loads(error.getvalue())["error"]["code"], "invalid_arguments")

    def test_module_entrypoint_and_no_default_private_directory(self):
        result = subprocess.run([sys.executable, "-m", "src.jcc_samples", "--private-dir", str(self.root), "validate"],
                                cwd=workflow.PROJECT_ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["samples"], 0)
        absent = subprocess.run([sys.executable, "-m", "src.jcc_samples", "validate"],
                                cwd=workflow.PROJECT_ROOT, capture_output=True, text=True)
        self.assertEqual(absent.returncode, 2)
        self.assertEqual(json.loads(absent.stderr)["error"]["code"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
