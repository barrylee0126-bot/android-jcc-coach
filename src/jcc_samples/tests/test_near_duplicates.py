"""Generated images and fictional metadata only; no game samples or devices.

Some fixtures declare ``real`` only to exercise split-validation wire shapes.
Their pixels, identities, sessions, review metadata and labels are synthetic.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings

import numpy as np
from PIL import Image, ImageDraw, PngImagePlugin

from src.jcc_samples import SampleStore
from src.jcc_samples import near_duplicates as nd


def synthetic_scene(seed=1, size=(320, 192)):
    width, height = size
    y, x = np.indices((height, width))
    pixels = np.stack(((x * (seed + 1) + y) % 256, (y * 2 + seed * 17) % 256,
                       (x // 2 + y + seed * 31) % 256), axis=2).astype(np.uint8)
    image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image)
    draw.rectangle((width // 5, height // 4, width // 2, height // 2), fill=(35, 150, 90))
    draw.ellipse((width // 2, height // 3, width * 4 // 5, height * 3 // 4), fill=(180, 50, 110))
    return image


class NearDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jcc-synthetic-near-duplicates-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.output = self.base / "reports"
        self.output.mkdir(mode=0o700)
        self.counter = 0

    def source(self, images, *, kind="synthetic", split="quarantine", session="session", group="dedupe",
               metadata=None):
        self.counter += 1
        root = self.base / f"store-{self.counter}"
        store = SampleStore.create(root, dataset_id=f"synthetic-test-{self.counter}")
        for index, item in enumerate(images):
            image, image_format = item if isinstance(item, tuple) else (item, "PNG")
            path = self.base / f"generated-{self.counter}-{index}.input"
            options = {"quality": 72, "subsampling": 2} if image_format == "JPEG" else {}
            if metadata is not None:
                options["pnginfo"] = metadata
            image.save(path, format=image_format, **options)
            path.chmod(0o600)
            imported = store.import_image(path, sample_kind=kind, recording_session_id=f"{session}-{index}",
                                          dedupe_group_id=f"{group}-{index}", state_group_id=f"state-{index}",
                                          annotator_id="synthetic-person-a")
            if split != "quarantine":
                reviewed = store.review(imported["sample_id"], reviewer_id="synthetic-person-b",
                                        expected_revision=imported["revision"], evidence_ref="synthetic-only-not-a-real-review")
                store.assign_split([f"{session}-{index}"], split=split, expected_revision=reviewed["revision"])
        store.export_manifest("manifests/frozen.json")
        return nd.FrozenSource(root, "manifests/frozen.json")

    def snapshot(self, source):
        return {path.relative_to(source.private_dir).as_posix(): path.read_bytes()
                for path in Path(source.private_dir).rglob("*") if path.is_file()}

    def workspace(self, source):
        return json.loads((Path(source.private_dir) / "workspace.json").read_bytes())

    def cli(self, sources, *extra):
        args = []
        for source in sources:
            args.extend(("--source", str(source.private_dir), source.manifest))
        args.extend(("--output-dir", str(self.output), *extra))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = nd.main(args)
        return status, out.getvalue(), err.getvalue()

    def fail(self, code, sources, *, config=None):
        before = [self.snapshot(source) for source in sources]
        with self.assertRaises(nd.NearDuplicateError) as caught:
            nd.review_frozen_sources(sources, config=config)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual([self.snapshot(source) for source in sources], before)

    def test_lossy_jpeg_resize_and_local_change_produce_review_leads(self):
        image = synthetic_scene()
        local = image.copy()
        ImageDraw.Draw(local).rectangle((230, 120, 285, 165), fill=(250, 245, 5))
        source = self.source([image, (image, "JPEG"), image.resize((304, 182)), local])
        report = nd.review_frozen_sources([source])
        pairs = {(item["left"], item["right"]) for item in report["candidates"]}
        for ordinal in (2, 3, 4):
            self.assertIn(("entry-000001", f"entry-{ordinal:06d}"), pairs)
        self.assertEqual(report["coverage"]["unordered_pairs_considered"], 6)
        self.assertEqual(report["status"], "complete_candidate_scan")
        self.assertIsNone(report["coverage"]["effective_acceptance_samples"])
        self.assertIsNone(report["coverage"]["accuracy"])

    def test_local_tile_path_handles_a_changed_quadrant(self):
        image = synthetic_scene()
        changed = image.copy()
        ImageDraw.Draw(changed).rectangle((0, 0, 159, 95), fill=(255, 255, 255))
        source = self.source([image, changed])
        report = nd.review_frozen_sources([source], config=nd.ReviewConfig(global_mae=0, dhash_hamming=0,
                                                                         local_global_mae=100))
        self.assertEqual(len(report["candidates"]), 1)
        self.assertEqual(report["candidates"][0]["reasons"], ["mostly_matching_tiles"])

    def test_candidates_are_not_transitive_groups_or_representatives(self):
        source = self.source([Image.new("RGB", (80, 48), (value,) * 3) for value in (0, 18, 36)])
        report = nd.review_frozen_sources([source])
        self.assertEqual([(item["left"], item["right"]) for item in report["candidates"]],
                         [("entry-000001", "entry-000002"), ("entry-000002", "entry-000003")])
        self.assertTrue(all(item["low_information_warning"] for item in report["candidates"]))
        self.assertTrue(all(item["decision"] is None for item in report["candidates"]))
        self.assertNotIn("groups", report)
        self.assertNotIn("representatives", report)

    def test_different_colors_can_have_same_hash_without_a_candidate(self):
        source = self.source([Image.new("RGB", (80, 48), color) for color in ((250, 5, 5), (5, 5, 250))])
        report = nd.review_frozen_sources([source])
        self.assertEqual(report["candidates"], [])
        self.assertIn("non_candidates_do_not_establish_independence_or_truth", report["notice_codes"])
        self.assertEqual(report["manual_review_status"], "pending")

    def test_small_local_changes_stay_pending_without_merging_declared_states(self):
        image = synthetic_scene()
        changed = image.copy()
        ImageDraw.Draw(changed).rectangle((110, 110, 112, 112), fill=(255, 255, 0))
        source = self.source([image, changed])
        report = nd.review_frozen_sources([source])
        self.assertEqual(len(report["candidates"]), 1)
        self.assertNotEqual(report["entries"][0]["state_group_alias"], report["entries"][1]["state_group_alias"])
        self.assertIsNone(report["candidates"][0]["decision"])
        self.assertIsNone(report["coverage"]["distinct_game_states"])

    def test_aspect_exclusions_are_counted_not_called_independent(self):
        source = self.source([Image.new("RGB", (80, 48), (20, 30, 40)),
                              Image.new("RGB", (48, 80), (20, 30, 40))])
        report = nd.review_frozen_sources([source])
        self.assertEqual(report["coverage"]["pairs_skipped_by_aspect_filter"], 1)
        self.assertEqual(report["coverage"]["pairs_scored"], 0)
        self.assertIsNone(report["coverage"]["independent_recordings"])

    def test_cross_split_candidates_are_prioritized_with_original_boundaries(self):
        # Every pixel is generated here; "real" only enables split wire-shape tests.
        image = synthetic_scene()
        dev = self.source([image], kind="real", split="development")
        acceptance = self.source([(image, "JPEG")], kind="real", split="acceptance")
        quarantine = self.source([image.resize((304, 182))])
        before = [self.snapshot(source) for source in (dev, acceptance, quarantine)]
        report = nd.review_frozen_sources([dev, acceptance, quarantine])
        first = report["candidates"][0]
        self.assertEqual(first["priority"], "cross_development_acceptance")
        self.assertTrue(first["boundaries"]["cross_development_acceptance"])
        self.assertEqual(first["boundaries"]["cross_source_identity"], "unverified")
        self.assertNotEqual(report["entries"][0]["recording_session_alias"], report["entries"][1]["recording_session_alias"])
        self.assertTrue(first["boundaries"]["declared_code_equal"]["recording_session_id"])
        self.assertEqual([item["split"] for item in report["entries"]], ["development", "acceptance", "quarantine"])
        self.assertEqual([self.snapshot(source) for source in (dev, acceptance, quarantine)], before)

    def test_declared_cross_source_overlap_warns_even_for_dissimilar_images(self):
        dev = self.source([Image.new("RGB", (80, 48), (255, 0, 0))], kind="real", split="development")
        acceptance = self.source([Image.new("RGB", (80, 48), (0, 0, 255))], kind="real", split="acceptance")
        report = nd.review_frozen_sources([dev, acceptance])
        self.assertEqual(report["candidates"], [])
        self.assertEqual(len(report["declared_boundary_alerts"]), 1)

    def test_exact_bytes_and_pixels_across_sources_are_explicit_review_leads(self):
        image = synthetic_scene()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Description", "sensitive-synthetic-metadata-do-not-echo")
        sources = [self.source([image]), self.source([image]), self.source([image], metadata=metadata)]
        report = nd.review_frozen_sources(sources)
        first, second = report["candidates"][:2]
        self.assertIn("identical_image_bytes", first["reasons"])
        self.assertIn("identical_decoded_pixels", second["reasons"])
        self.assertNotIn("identical_image_bytes", second["reasons"])

    def test_report_contains_only_anonymous_indices_not_labels_paths_or_identifiers(self):
        source = self.source([synthetic_scene(), (synthetic_scene(), "JPEG")],
                             session="sensitive-session", group="sensitive-dedupe")
        workspace = self.workspace(source)
        for record in workspace["annotations"]:
            record["labels"]["gold"]["reason"] = "sensitive-ocr-like-label-must-not-appear"
        root = Path(source.private_dir)
        content = json.dumps(workspace)
        (root / "workspace.json").write_text(content)
        (root / source.manifest).write_text(json.dumps(workspace["annotations"]))
        serialized = json.dumps(nd.review_frozen_sources([source]))
        for forbidden in (str(self.base), "sensitive-session", "sensitive-dedupe", "sensitive-ocr-like-label",
                          "synthetic-person-a", workspace["dataset_id"], workspace["annotations"][0]["sample_id"],
                          workspace["annotations"][0]["stream_id"], workspace["assets"][workspace["annotations"][0]["sample_id"]]["asset_ref"]):
            self.assertNotIn(forbidden, serialized)
        for forbidden_key in ('"rgb":', '"thumbnail":', '"labels":', '"path":', '"dhash":'):
            self.assertNotIn(forbidden_key, serialized)

    def test_repeated_run_is_deterministic_and_source_bytes_do_not_change(self):
        source = self.source([synthetic_scene(), (synthetic_scene(), "JPEG")])
        before = self.snapshot(source)
        first, second = nd.review_frozen_sources([source]), nd.review_frozen_sources([source])
        self.assertEqual(first, second)
        self.assertEqual(self.snapshot(source), before)
        self.assertEqual(len(first["method"]["module_sha256"]), 64)
        self.assertEqual(len(first["provenance"]["ordered_asset_binding_sha256"]), 64)

    def test_success_cli_creates_only_one_private_report(self):
        source = self.source([synthetic_scene(), (synthetic_scene(), "JPEG")])
        before = self.snapshot(source)
        status, out, err = self.cli([source])
        self.assertEqual(status, 0, err)
        self.assertEqual(err, "")
        self.assertNotIn(str(self.base), out)
        self.assertEqual(json.loads(out)["manual_review_status"], "pending")
        report_path = self.output / "near-duplicate-review.json"
        self.assertEqual(stat.S_IMODE(report_path.stat().st_mode), 0o600)
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report["entries"][0]["manifest_ordinal_1based"], 1)
        self.assertEqual(self.snapshot(source), before)

    def test_output_collision_and_symlink_are_not_overwritten(self):
        source = self.source([synthetic_scene()])
        report_path = self.output / "near-duplicate-review.json"
        report_path.write_bytes(b"existing private recovery material")
        self.assertEqual(self.cli([source])[0], 2)
        self.assertEqual(report_path.read_bytes(), b"existing private recovery material")
        report_path.unlink()
        report_path.symlink_to(self.base / "nonexistent")
        self.assertEqual(self.cli([source])[0], 2)
        self.assertTrue(report_path.is_symlink())
        self.assertFalse((self.base / "nonexistent").exists())

    def test_output_cannot_mutate_source_or_use_traversal(self):
        source = self.source([synthetic_scene()])
        before = self.snapshot(source)
        status, _, err = self.cli([source], "--output-dir", str(source.private_dir))
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(err)["error"]["code"], "source_write_forbidden")
        for name in ("../escape.json", "/sensitive/private.json", "sensitive name.json"):
            status, out, err = self.cli([source], "--output-name", name)
            self.assertEqual(status, 2)
            self.assertNotIn(name, out + err)
        self.assertEqual(self.snapshot(source), before)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_output_source_git_and_cloud_paths_are_rejected(self):
        source = self.source([synthetic_scene()])
        git_dir = self.base / "git-directory"
        git_dir.mkdir(mode=0o700)
        (git_dir / ".git").write_text("synthetic worktree marker")
        cloud = self.base / "Dropbox"
        cloud.mkdir(mode=0o700)
        for directory in (nd.wf.PROJECT_ROOT, git_dir, cloud):
            status, out, err = self.cli([source], "--output-dir", str(directory))
            self.assertEqual(status, 2)
            self.assertNotIn(str(directory), out + err)

    def test_bad_cli_argument_never_echoes_input(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            nd.main(["--private-person-account-sensitive-path"])
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn("private-person-account", out.getvalue() + err.getvalue())

    def test_subset_stale_and_reordered_exports_are_not_silently_scanned(self):
        source = self.source([synthetic_scene(), (synthetic_scene(), "JPEG")])
        root = Path(source.private_dir)
        records = self.workspace(source)["annotations"]
        for replacement in (records[:1], list(reversed(records))):
            (root / source.manifest).write_text(json.dumps(replacement))
            self.fail("manifest_snapshot_mismatch", [source])
        (root / source.manifest).write_text(json.dumps(records))
        store = SampleStore(root)
        store.review(records[0]["sample_id"], reviewer_id="synthetic-person-b", expected_revision=2,
                     evidence_ref="synthetic-only-not-a-real-review")
        self.fail("manifest_snapshot_mismatch", [source])

    def test_manifest_must_be_an_explicit_safe_export(self):
        source = self.source([synthetic_scene()])
        for relative in ("workspace.json", "../private.json", "manifests/../workspace.json", "/absolute/private.json"):
            with self.assertRaises(nd.NearDuplicateError):
                nd.review_frozen_sources([replace(source, manifest=relative)])
        link = Path(source.private_dir) / "manifests/alias.json"
        link.symlink_to(Path(source.private_dir) / source.manifest)
        self.fail("path_escape", [replace(source, manifest="manifests/alias.json")])

    def test_duplicate_keys_nonfinite_and_bad_manifest_shapes_fail(self):
        source = self.source([synthetic_scene()])
        for data in (b'{"x":1,"x":2}', b'[NaN]', b'[1e999]', b'{"annotations":[]}'):
            (Path(source.private_dir) / source.manifest).write_bytes(data)
            with self.assertRaises(nd.NearDuplicateError):
                nd.review_frozen_sources([source])

    def test_manifest_bool_cannot_match_a_numeric_workspace_field(self):
        source = self.source([synthetic_scene()])
        records = self.workspace(source)["annotations"]
        records[0]["frame"]["sequence"] = False  # False == 0 in Python, illegal in the schema.
        (Path(source.private_dir) / source.manifest).write_text(json.dumps(records))
        self.fail("invalid_annotations", [source])

    def test_malformed_workspace_errors_are_content_free(self):
        source = self.source([synthetic_scene()])
        root = Path(source.private_dir)
        workspace = self.workspace(source)
        for assets in ({workspace["annotations"][0]["sample_id"]: None}, "sensitive-invalid-private-shape"):
            workspace["assets"] = assets
            (root / "workspace.json").write_text(json.dumps(workspace))
            status, out, err = self.cli([source])
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(err)["status"], "no_report")
            self.assertNotIn("sensitive-invalid-private-shape", out + err)
            self.assertEqual(list(self.output.iterdir()), [])

    def test_decoder_warning_is_sanitized_without_a_partial_report(self):
        source = self.source([synthetic_scene()])
        def warning():
            warnings.warn("sensitive-private-metadata-warning", UserWarning)
        with patch.object(nd.wf.SampleStore, "_load", side_effect=warning):
            status, out, err = self.cli([source])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(err)["error"]["code"], "decoder_warning")
        self.assertNotIn("sensitive-private-metadata-warning", out + err)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_review_does_not_call_network_or_subprocess(self):
        source = self.source([synthetic_scene(), (synthetic_scene(), "JPEG")])
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("subprocess.run", side_effect=AssertionError("external process forbidden")):
            report = nd.review_frozen_sources([source])
        self.assertEqual(report["status"], "complete_candidate_scan")

    def test_orientation_multiframe_and_decode_limits_use_existing_validator(self):
        source = self.source([synthetic_scene()])
        with patch.object(nd.wf, "MAX_PIXELS", 1):
            self.fail("image_too_large", [source])
        with patch.object(nd.wf, "MAX_IMAGE_BYTES", 1):
            self.fail("input_too_large", [source])
        root = Path(source.private_dir)
        asset = next(iter(self.workspace(source)["assets"].values()))
        path = root / asset["path"]
        exif = Image.Exif()
        exif[274] = 6
        synthetic_scene().save(path, format="JPEG", exif=exif)
        self.fail("image_orientation", [source])
        image = synthetic_scene()
        image.save(path, format="PNG", save_all=True, append_images=[synthetic_scene(seed=2)])
        self.fail("unsupported_image", [source])

    def test_missing_corrupt_and_substituted_assets_stop_with_no_report(self):
        source = self.source([synthetic_scene()])
        asset = next(iter(self.workspace(source)["assets"].values()))
        path = Path(source.private_dir) / asset["path"]
        original = path.read_bytes()
        for content in (b"bad-image-with-sensitive-source-name", original[:48]):
            path.write_bytes(content)
            status, out, err = self.cli([source])
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(err)["status"], "no_report")
            self.assertNotIn("sensitive-source-name", out + err)
            self.assertEqual(list(self.output.iterdir()), [])
        path.unlink()
        self.assertEqual(self.cli([source])[0], 2)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_unsafe_permissions_hardlinks_and_missing_locks_fail(self):
        source = self.source([synthetic_scene()])
        root = Path(source.private_dir)
        manifest = root / source.manifest
        manifest.chmod(0o644)
        self.fail("unsafe_permissions", [source])
        manifest.chmod(0o600)
        extra = self.base / "hardlink"
        os.link(manifest, extra)
        self.fail("unsafe_permissions", [source])
        extra.unlink()
        (root / ".lock").unlink()
        self.fail("workspace_unreadable", [source])

    def test_concurrent_store_lock_is_respected_without_source_write(self):
        source = self.source([synthetic_scene()])
        with (Path(source.private_dir) / ".lock").open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fail("workspace_busy", [source])

    def test_changed_asset_or_manifest_during_comparison_cannot_return_a_report(self):
        for change in ("asset", "manifest"):
            with self.subTest(change=change):
                source = self.source([synthetic_scene(), (synthetic_scene(), "JPEG")])
                root = Path(source.private_dir)
                asset = next(iter(self.workspace(source)["assets"].values()))
                path = root / asset["path"] if change == "asset" else root / source.manifest
                original_compare = nd._compare
                def mutate(left, right, config):
                    path.write_bytes(path.read_bytes() + b" ")
                    return original_compare(left, right, config)
                with patch.object(nd, "_compare", side_effect=mutate), self.assertRaises(nd.NearDuplicateError) as caught:
                    nd.review_frozen_sources([source])
                self.assertEqual(caught.exception.code, "input_changed")

    def test_no_partial_file_after_entry_pair_or_candidate_limit(self):
        source = self.source([Image.new("RGB", (80, 48), (value,) * 3) for value in (0, 5, 10)])
        before = self.snapshot(source)
        for flag, value, code in (("--max-entries", "2", "scan_limit_exceeded"),
                                  ("--max-pairs", "2", "scan_limit_exceeded"),
                                  ("--max-candidates", "1", "candidate_limit_exceeded")):
            status, out, err = self.cli([source], flag, value)
            self.assertEqual(status, 2)
            self.assertEqual(out, "")
            self.assertEqual(json.loads(err)["error"]["code"], code)
            self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(self.snapshot(source), before)

    def test_invalid_thresholds_do_not_relax_checks(self):
        source = self.source([synthetic_scene()])
        for config in (nd.ReviewConfig(global_mae=float("nan")), nd.ReviewConfig(tile_mae=float("inf")),
                       nd.ReviewConfig(dhash_hamming=True), nd.ReviewConfig(min_matching_tiles=0),
                       nd.ReviewConfig(max_entries=2001), nd.ReviewConfig(aspect_tolerance=0.2),
                       nd.ReviewConfig(global_mae=-1)):
            self.fail("invalid_config", [source], config=config)
        self.assertEqual(self.cli([source], "--global-mae", "nan")[0], 2)

    def test_empty_and_repeated_sources_cannot_look_like_clean_review(self):
        source = self.source([])
        self.fail("empty_corpus", [source])
        self.fail("repeated_source", [source, source])
        with self.assertRaises(nd.NearDuplicateError):
            nd.review_frozen_sources([])

    def test_transparency_is_rejected_instead_of_guessing_a_background(self):
        image = synthetic_scene().convert("RGBA")
        image.putalpha(128)
        source = self.source([image])
        self.fail("unsupported_transparency", [source])

    def test_existing_exact_duplicate_and_split_isolation_checks_still_apply(self):
        source = self.source([synthetic_scene(), (synthetic_scene(), "JPEG")], kind="real")
        root = Path(source.private_dir)
        workspace = self.workspace(source)
        workspace["annotations"][1]["recording_session_id"] = workspace["annotations"][0]["recording_session_id"]
        workspace["annotations"][1]["split"] = "development"
        (root / "workspace.json").write_text(json.dumps(workspace))
        (root / source.manifest).write_text(json.dumps(workspace["annotations"]))
        self.fail("split_isolation", [source])

    def test_report_write_failure_cleans_only_its_new_file(self):
        source = self.source([synthetic_scene()])
        preserved = self.output / "unrelated.json"
        preserved.write_bytes(b"preexisting private material")
        with patch.object(nd.wf.os, "fsync", side_effect=OSError("sensitive-fake-disk-error")):
            status, out, err = self.cli([source])
        self.assertEqual(status, 2)
        self.assertNotIn("sensitive-fake-disk-error", out + err)
        self.assertEqual(list(self.output.iterdir()), [preserved])
        self.assertEqual(preserved.read_bytes(), b"preexisting private material")

    def test_report_link_failure_leaves_no_partial_final_or_staging_file(self):
        source = self.source([synthetic_scene()])
        with patch.object(nd.os, "link", side_effect=OSError("synthetic-link-failure")):
            status, _, err = self.cli([source])
        self.assertEqual(status, 2)
        self.assertNotIn("synthetic-link-failure", err)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_output_rechecked_after_scan_and_race_collision_preserved(self):
        source = self.source([synthetic_scene()])
        review = nd.review_frozen_sources
        def permission_change(sources, *, config):
            report = review(sources, config=config)
            self.output.chmod(0o755)
            return report
        with patch.object(nd, "review_frozen_sources", side_effect=permission_change):
            status, _, err = self.cli([source])
        self.output.chmod(0o700)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(err)["error"]["code"], "unsafe_permissions")
        self.assertEqual(list(self.output.iterdir()), [])
        report_path = self.output / "near-duplicate-review.json"
        real_link = nd.os.link
        def collision(*args, **kwargs):
            report_path.write_bytes(b"preserved concurrent report")
            return real_link(*args, **kwargs)
        with patch.object(nd.os, "link", side_effect=collision):
            status, _, err = self.cli([source])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(err)["error"]["code"], "output_exists")
        self.assertEqual(list(self.output.iterdir()), [report_path])
        self.assertEqual(report_path.read_bytes(), b"preserved concurrent report")


if __name__ == "__main__":
    unittest.main()
