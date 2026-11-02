import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from jcc_hypothesis_advice.fixtures import example_request


class CliTests(unittest.TestCase):
    def run_cli(self, *args, payload=None):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2]), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, "-B", "-m", "jcc_hypothesis_advice", *args],
                              input=payload, text=True, capture_output=True, env=env, timeout=10)

    def input_cli(self, payload):
        return self.run_cli("--input", "-", "--review-at-ms", "1100", "--max-frame-age-ms", "1000",
                            "--max-result-ms", "300", payload=payload)

    def test_default_demo_exercises_positive_counterexample_occlusion_expiry_and_replay_shape(self):
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["synthetic_positive"]["status"], "hypothesis_only")
        self.assertEqual(result["numeric_counterexample"]["status"], "conditions_not_met")
        self.assertEqual(result["occluded"]["status"], "insufficient_evidence")
        self.assertEqual(result["at_exact_expiry"]["status"], "expired")
        self.assertEqual(result["old_revision_after_expiry"]["status"], "refused")
        self.assertEqual(result["constructed_replay_shape"]["material_origin"], "constructed_replay_template")
        self.assertTrue(result["repeated_read_same_deadline"])

    def test_constructed_replay_template_round_trips_through_isolated_json_input(self):
        template = self.run_cli("--example", "replay_candidate")
        self.assertEqual(template.returncode, 0, template.stderr)
        process = self.input_cli(template.stdout)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "hypothesis_only")
        self.assertEqual(result["sample_kind"], "replay_candidate")
        self.assertEqual(result["material_origin"], "constructed_replay_template")
        self.assertFalse(result["production_eligible"])

    def test_input_needs_explicit_review_cutoff_and_engineering_limits(self):
        process = self.run_cli("--input", "-", payload="{}")
        self.assertEqual(process.returncode, 2)
        self.assertIn("--review-at-ms", process.stderr)

    def test_duplicate_json_keys_nonfinite_and_oversized_inputs_are_rejected(self):
        for payload in ('{"key":1,"key":2}', '{"key":NaN}', '{"key":Infinity}', "[", "x" * (256 * 1024 + 1)):
            process = self.input_cli(payload)
            self.assertEqual(process.returncode, 2, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result["status"], "refused")
            self.assertIsNone(result["analysis"])
            self.assertEqual(result["reasons"][0]["code"], "local_input_unreadable")

    def test_no_private_path_or_exception_text_is_echoed_on_read_failure(self):
        private_path = "/nonexistent/private-example-only/candidate.json"
        process = self.run_cli("--input", private_path, "--review-at-ms", "1100", "--max-frame-age-ms", "1000",
                               "--max-result-ms", "300")
        self.assertEqual(process.returncode, 2)
        self.assertNotIn(private_path, process.stdout + process.stderr)
        self.assertNotIn("Traceback", process.stderr)

    def test_live_candidate_remains_a_refusal_in_cli(self):
        request = example_request(replay_candidate=True)
        request["sample_kind"] = "live_candidate"
        process = self.input_cli(json.dumps(request))
        self.assertEqual(process.returncode, 0)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reasons"][0]["code"], "live_input_forbidden")

    def test_malformed_binding_without_clock_does_not_crash_or_guess_a_domain(self):
        for binding in (None, [], {}, {"review_clock_id": ""}):
            request = example_request()
            request["binding"] = binding
            process = self.input_cli(json.dumps(request))
            self.assertEqual(process.returncode, 2, process.stderr)
            self.assertEqual(json.loads(process.stdout)["status"], "refused")

    def test_importing_isolated_core_does_not_load_production_device_catalog_or_overlay(self):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2]), "PYTHONDONTWRITEBYTECODE": "1"}
        script = (
            "import sys, json; from jcc_hypothesis_advice import HypothesisSession; "
            "print(json.dumps([n for n in sys.modules if n.split('.')[0] in "
            "('jcc_device','jcc_strategy','jcc_catalog','jcc_overlay')]))"
        )
        process = subprocess.run([sys.executable, "-S", "-B", "-c", script], capture_output=True,
                                 text=True, env=env, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), [])


if __name__ == "__main__":
    unittest.main()
