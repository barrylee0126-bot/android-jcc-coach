"""Synthetic CLI parsing, reproducibility and no-overwrite checks."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.jcc_evaluation import EvaluationInputError
from src.jcc_evaluation.__main__ import main, strict_json
from src.jcc_evaluation.synthetic import demo_inputs


class CLITests(unittest.TestCase):
    def test_strict_parser_rejects_duplicate_keys_and_nonfinite_numbers(self):
        for raw in (b'{"x": 1, "x": 2}', b'{"x": NaN}', b'{"x": Infinity}', b'{"x": 1e999}', b'not-json', b'\xff'):
            with self.subTest(raw=raw):
                with self.assertRaises(EvaluationInputError):
                    strict_json(raw)

    def test_demo_default_cannot_be_mistaken_for_real_accuracy(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["--demo"])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["cohort"], "synthetic")
        self.assertFalse(report["real_result_available"])
        self.assertIsNone(report["targets"]["real_acceptance_verdict"])

    def test_explicit_json_input_output_has_file_hashes_and_markdown(self):
        # Temporary files stay inside the evaluation module and are removed.
        with TemporaryDirectory(prefix="synthetic-cli-", dir=Path(__file__).parent) as directory:
            root = Path(directory)
            records, predictions, metadata = demo_inputs()
            for name, value in (("annotations", records), ("predictions", predictions), ("metadata", metadata)):
                (root / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
            args = ["--annotations", str(root / "annotations.json"), "--predictions", str(root / "predictions.json"),
                    "--run-metadata", str(root / "metadata.json"), "--cohort", "synthetic",
                    "--output", str(root / "report.json"), "--markdown", str(root / "report.md")]
            self.assertEqual(main(args), 0)
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(set(report["reproducibility"]["input_file_sha256"]), {"annotations", "predictions", "run_metadata"})
            self.assertIn("非真实识别成绩", (root / "report.md").read_text(encoding="utf-8"))
            error = StringIO()
            with redirect_stderr(error):
                self.assertEqual(main(args), 2)
            self.assertEqual(json.loads(error.getvalue())["error"], "output_exists")

    def test_cli_contract_error_produces_no_scores(self):
        with TemporaryDirectory(prefix="synthetic-cli-", dir=Path(__file__).parent) as directory:
            root = Path(directory)
            (root / "annotations.json").write_text('[{"invalid": true}]', encoding="utf-8")
            (root / "predictions.json").write_text('[]', encoding="utf-8")
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = main(["--annotations", str(root / "annotations.json"), "--predictions", str(root / "predictions.json")])
            self.assertEqual(code, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(json.loads(errors.getvalue())["status"], "no_scores")

    def test_cannot_overwrite_input_or_output_paths(self):
        with TemporaryDirectory(prefix="synthetic-cli-", dir=Path(__file__).parent) as directory:
            path = str(Path(directory) / "synthetic.json")
            for args in (["--demo", "--output", path, "--markdown", path],
                         ["--annotations", path, "--predictions", path, "--output", path]):
                with self.subTest(args=args):
                    with redirect_stderr(StringIO()):
                        self.assertEqual(main(args), 2)
                    self.assertFalse(Path(path).exists())

    def test_demo_cannot_consume_external_data(self):
        with redirect_stderr(StringIO()):
            self.assertEqual(main(["--demo", "--annotations", "unused.json"]), 2)
        with redirect_stderr(StringIO()):
            self.assertEqual(main([]), 2)


if __name__ == "__main__":
    unittest.main()
