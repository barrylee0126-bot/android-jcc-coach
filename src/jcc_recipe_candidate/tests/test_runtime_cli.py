"""Runtime failures, provenance, and the public JSON/CLI integration boundary."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jcc_recipe_candidate import analyze_json, analyze_request
from jcc_recipe_candidate import core
from jcc_recipe_candidate.synthetic import synthetic_request


PACKAGE = Path(__file__).resolve().parents[1]


class RuntimeFailureTests(unittest.TestCase):
    def assert_unknown(self, code):
        result = analyze_request(synthetic_request())
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["candidates"])
        self.assertIsNone(result["inventory_snapshot"])
        self.assertEqual(result["issues"][0]["code"], code)

    def test_missing_node_does_not_look_like_empty_inventory(self):
        with patch.object(core.shutil, "which", return_value=None):
            self.assert_unknown("upstream_runtime_unavailable")

    def test_unavailable_source_is_unknown(self):
        with patch.object(core.Path, "read_bytes", side_effect=OSError("synthetic:unreadable")):
            self.assert_unknown("upstream_source_unavailable")

    def test_source_digest_change_blocks_execution(self):
        with patch.object(core.Path, "read_bytes", return_value=b"synthetic:changed-code"):
            with patch.object(core.subprocess, "run", side_effect=AssertionError("must not run")):
                self.assert_unknown("upstream_source_integrity")

    def test_timeout_and_start_failure_are_unknown(self):
        for failure, code in ((subprocess.TimeoutExpired("synthetic:node", 5), "upstream_timeout"),
                               (OSError("synthetic:failed"), "upstream_runtime_unavailable")):
            with patch.object(core.subprocess, "run", side_effect=failure):
                self.assert_unknown(code)

    def test_nonzero_exit_does_not_emit_backend_stderr_or_partial_candidates(self):
        completed = subprocess.CompletedProcess([], 1, stdout='[{"item":"fiction_xx"}]',
                                                stderr="synthetic:do-not-echo")
        with patch.object(core.subprocess, "run", return_value=completed):
            result = analyze_request(synthetic_request())
        self.assertEqual(result["issues"][0]["code"], "upstream_failed")
        self.assertIsNone(result["candidates"])
        self.assertNotIn("synthetic:do-not-echo", json.dumps(result))

    def test_malformed_unexpected_duplicated_or_reordered_results_are_unknown(self):
        xx = {"item": "fiction_xx", "name": "fiction_xx", "components": ["piece_x", "piece_x"]}
        xy = {"item": "fiction_xy", "name": "fiction_xy", "components": ["piece_x", "piece_y"]}
        bad = ["", "not-json", "{}", '[{"item":"a","item":"b"}]',
               json.dumps([xx, xx]), json.dumps([xy, xx]), json.dumps([{"item": []}]),
               json.dumps([{**xx, "components": []}]), json.dumps([{**xx, "item": "fiction_missing"}])]
        for stdout in bad:
            completed = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
            with self.subTest(stdout=stdout), patch.object(core.subprocess, "run", return_value=completed):
                self.assert_unknown("upstream_output_invalid")

    def test_validly_shaped_backend_output_still_requires_runtime_trust(self):
        # Fault injection only: the fixture has one Y, so YY is infeasible.
        # Output shape checks do not independently authenticate that computation.
        row = {"item": "fiction_yy", "name": "fiction_yy", "components": ["piece_y", "piece_y"]}
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps([row]), stderr="")
        with patch.object(core.subprocess, "run", return_value=completed):
            evaluated = analyze_request(synthetic_request())
        self.assertEqual(evaluated["status"], "evaluated")
        self.assertEqual([r["item_id"] for r in evaluated["candidates"]], ["fiction_yy"])
        unknown = analyze_request({})
        self.assertEqual(unknown["status"], "unknown")
        for result in (evaluated, unknown):
            notes = "\n".join(result["notes_zh"])
            for dependency in ("PATH", "Node", "bridge.mjs", "本包文件", "宿主", "摘要"):
                self.assertIn(dependency, notes)


class SourceProvenanceTests(unittest.TestCase):
    def test_adopted_source_and_full_mit_license_match_recorded_hashes(self):
        manifest = json.loads((PACKAGE / "vendor" / "manifest.json").read_text())
        self.assertEqual(manifest["commit"], core.UPSTREAM_COMMIT)
        self.assertEqual(manifest["adopted_lines_inclusive"], [31, 44])
        self.assertFalse(manifest["translation"])
        for name, hash_key, size_key in (("craftableFrom.mjs", "adopted_sha256", "adopted_bytes"),
                                         ("tft-advisor-LICENSE.txt", "license_sha256", "license_bytes")):
            data = (PACKAGE / "vendor" / name).read_bytes()
            self.assertEqual(len(data), manifest[size_key])
            self.assertEqual(hashlib.sha256(data).hexdigest(), manifest[hash_key])
        self.assertEqual(manifest["adopted_sha256"], core.UPSTREAM_EXCERPT_SHA256)
        license_text = (PACKAGE / "vendor" / "tft-advisor-LICENSE.txt").read_text()
        self.assertIn("Copyright (c) 2026 ndh0408", license_text)
        self.assertTrue(license_text.endswith("SOFTWARE.\n"))


class JsonCliIntegrationTests(unittest.TestCase):
    def invoke(self, *args, stdin=None, working_directory=PACKAGE, node_search_path=None):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PACKAGE.parent)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if node_search_path is not None:
            env["PATH"] = node_search_path
        return subprocess.run([sys.executable, "-B", "-m", "jcc_recipe_candidate", *args],
                              cwd=working_directory, input=stdin, text=True, encoding="utf-8",
                              capture_output=True, env=env, timeout=10, check=False)

    def test_public_json_api_uses_actual_original_function(self):
        result = analyze_json(json.dumps(synthetic_request()))
        self.assertEqual(result["status"], "evaluated", result["issues"])
        self.assertEqual([r["item_id"] for r in result["candidates"]], ["fiction_xx", "fiction_xy"])

    def test_cli_demo_runs_from_another_working_directory(self):
        completed = self.invoke("--demo")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["data_kind"], "synthetic")
        self.assertEqual(len(result["candidates"]), 2)

    def test_relative_path_node_remains_executable_after_child_cwd_changes(self):
        actual_node = core.shutil.which("node")
        self.assertIsNotNone(actual_node, "Node is required for successful integration tests")
        actual_node = Path(actual_node).resolve()
        with tempfile.TemporaryDirectory(prefix="synthetic-relative-path-", dir=PACKAGE / "tests") as directory:
            caller_directory = Path(directory)
            (caller_directory / "bin").mkdir()
            (caller_directory / "bin" / "node").symlink_to(actual_node)
            (caller_directory / "node").symlink_to(actual_node)
            for search_path in ("bin", "./bin", os.pathsep + os.defpath):
                with self.subTest(search_path=search_path):
                    completed = self.invoke("--demo", working_directory=caller_directory,
                                            node_search_path=search_path)
                    self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
                    result = json.loads(completed.stdout)
                    self.assertEqual(result["status"], "evaluated", result["issues"])
                    self.assertEqual([r["item_id"] for r in result["candidates"]],
                                     ["fiction_xx", "fiction_xy"])

    def test_cli_prints_only_an_explicit_synthetic_request(self):
        completed = self.invoke("--print-synthetic-request")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), synthetic_request())

    def test_cli_stdin_roundtrip_and_unknown_exit_code(self):
        completed = self.invoke("--stdin", stdin=json.dumps(synthetic_request()))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "evaluated")
        completed = self.invoke("--stdin", stdin="{}")
        self.assertEqual(completed.returncode, 2, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["candidates"])

    def test_cli_rejects_c1_reference_in_literal_and_escaped_json(self):
        request = synthetic_request()
        request["catalog"]["evidence"]["refs"] = ["synthetic:before\u0080after"]
        for ensure_ascii in (False, True):
            with self.subTest(ensure_ascii=ensure_ascii):
                completed = self.invoke("--stdin", stdin=json.dumps(request, ensure_ascii=ensure_ascii))
                self.assertEqual(completed.returncode, 2, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["candidates"])
                self.assertEqual(result["issues"][0]["code"], "evidence_insufficient")

    def test_cli_unknown_json_and_unreadable_file_do_not_become_empty(self):
        for args, stdin, code in ((["--stdin"], '{"inventory":null,"inventory":[]}', "json_invalid"),
                                  (["--input", str(PACKAGE / "synthetic-nonexistent.json")], None, "input_unreadable")):
            completed = self.invoke(*args, stdin=stdin)
            self.assertEqual(completed.returncode, 2, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertIsNone(result["candidates"])
            self.assertEqual(result["issues"][0]["code"], code)


if __name__ == "__main__":
    unittest.main()
