"""Local JSON CLI. Does not discover data, access images or open a device."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

from .evaluator import COHORTS, EvaluationInputError, evaluate, render_markdown


def strict_json(data: bytes):
    """Reject duplicate JSON keys, NaN/Infinity and numeric overflow."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise EvaluationInputError("invalid_json", "Duplicate object keys are not allowed.")
            result[key] = value
        return result

    def nonfinite(_):
        raise EvaluationInputError("invalid_json", "Non-finite numbers are not JSON values.")

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            nonfinite(value)
        return result

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=nonfinite, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, EvaluationInputError):
            raise
        raise EvaluationInputError("invalid_json", "Input is not valid UTF-8 JSON.") from None


def _read(path: Path):
    try:
        data = path.read_bytes()
    except OSError:
        raise EvaluationInputError("unreadable_input", "A required input file is not readable.") from None
    return strict_json(data), hashlib.sha256(data).hexdigest()


def _write_new(path: Path, content: str):
    # No implicit parent creation and no overwrites, especially not input files.
    with path.open("x", encoding="utf-8") as output:
        output.write(content)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline M1 field evaluation. Synthetic and real results never merge.")
    parser.add_argument("--annotations", type=Path, help="Full frozen sample-annotation-v1 JSON array, including all splits")
    parser.add_argument("--predictions", type=Path, help='JSON array of {"sample_id": ..., "state": game-state-v1 or null}')
    parser.add_argument("--cohort", choices=COHORTS, default="real_acceptance")
    parser.add_argument("--run-metadata", type=Path, help="Optional frozen annotation/split/model/config revision object")
    parser.add_argument("--output", type=Path, help="Create a new JSON report file; otherwise print JSON to stdout")
    parser.add_argument("--markdown", type=Path, help="Also create a new Markdown summary file")
    parser.add_argument("--demo", action="store_true", help="Use only in-memory synthetic fixtures, with a prominent non-real-results label")
    args = parser.parse_args(argv)
    try:
        inputs = [p.resolve() for p in (args.annotations, args.predictions, args.run_metadata) if p is not None]
        outputs = [p.resolve() for p in (args.output, args.markdown) if p is not None]
        if len(set(outputs)) != len(outputs) or set(inputs) & set(outputs):
            raise EvaluationInputError("output_conflicts_with_input", "Output files must differ from all inputs and each other.")
        if any(path.exists() for path in outputs):
            raise EvaluationInputError("output_exists", "Outputs must be new files; existing files will not be overwritten.")
        if args.demo:
            if args.annotations or args.predictions or args.run_metadata or args.cohort != "real_acceptance":
                raise EvaluationInputError("invalid_demo_arguments", "Use --demo by itself (plus optional output paths). It always selects synthetic.")
            from .synthetic import demo_inputs
            annotations, predictions, metadata = demo_inputs()
            report = evaluate(annotations, predictions, cohort="synthetic", run_metadata=metadata)
        else:
            if args.annotations is None or args.predictions is None:
                raise EvaluationInputError("missing_input", "Supply both --annotations and --predictions, or use --demo.")
            annotations, annotations_sha = _read(args.annotations)
            predictions, predictions_sha = _read(args.predictions)
            metadata, metadata_sha = _read(args.run_metadata) if args.run_metadata else (None, None)
            report = evaluate(annotations, predictions, cohort=args.cohort, run_metadata=metadata)
            report["reproducibility"]["input_file_sha256"] = {
                "annotations": annotations_sha, "predictions": predictions_sha, "run_metadata": metadata_sha,
            }
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        # Serialize both artifacts before writing either one. A filesystem failure
        # is still an error; a partially written artifact is never a successful run.
        summary = render_markdown(report) if args.markdown else None
        if args.output:
            _write_new(args.output, encoded)
        else:
            sys.stdout.write(encoded)
        if args.markdown:
            _write_new(args.markdown, summary)
        return 0
    except (EvaluationInputError, RuntimeError, OSError) as error:
        code = error.code if isinstance(error, EvaluationInputError) else "unavailable_dependency_or_output"
        # Paths and raw input values are deliberately not reproduced in errors.
        message = str(error) if isinstance(error, EvaluationInputError) else "Required dependency or output location is unavailable."
        sys.stderr.write(json.dumps({"status": "no_scores", "error": code, "message": message}, ensure_ascii=False) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
