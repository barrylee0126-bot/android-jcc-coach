import json
from pathlib import Path
import tempfile
import unittest

from jcc_latency_audit.audit import ATTEMPT_SCHEMA, audit_attempts, read_attempts
from jcc_latency_audit.__main__ import main


def attempt(identifier, outcome="unknown", **changes):
    row = dict(
        schema_version=ATTEMPT_SCHEMA,
        opportunity_id=identifier,
        source_kind="replay",
        execution="none",
        outcome=outcome,
        observed_monotonic_ns=1_000_000_000,
        capture_received_ns=1_010_000_000,
        recognition_completed_ns=None,
        decision_completed_ns=None,
        action_sent_ns=None,
        feedback_received_ns=None,
        display_reported_ns=None,
    )
    row.update(changes)
    return row


class LatencyAuditTests(unittest.TestCase):
    def test_full_denominator_keeps_all_terminal_outcomes(self):
        rows = [
            attempt("display", "advice_displayed", recognition_completed_ns=1_020_000_000,
                    decision_completed_ns=1_030_000_000, display_reported_ns=1_040_000_000),
            attempt("action", "action_confirmed", execution="attempted", recognition_completed_ns=1_020_000_000,
                    decision_completed_ns=1_030_000_000, action_sent_ns=1_035_000_000,
                    feedback_received_ns=1_050_000_000),
            attempt("unknown"),
            attempt("timeout", "timeout"),
            attempt("withdrawn", "withdrawn", recognition_completed_ns=1_020_000_000),
            attempt("failed", "failed"),
        ]
        report = audit_attempts(rows)
        self.assertEqual(report["opportunities"], 6)
        self.assertTrue(report["all_supplied_rows_accounted"])
        self.assertEqual(report["outcomes"], {
            "action_confirmed": 1, "advice_displayed": 1, "failed": 1,
            "timeout": 1, "unknown": 1, "withdrawn": 1,
        })
        group = report["groups"]["replay/none"]
        self.assertEqual(group["segments"]["capture_to_recognition"]["n"], 2)
        self.assertEqual(group["segments"]["capture_to_recognition"]["denominator"], 5)
        self.assertEqual(group["segments"]["capture_to_recognition"]["unmeasured"], 3)
        self.assertIsNone(group["segments"]["capture_to_recognition"]["p95_ms"])
        self.assertEqual(report["groups"]["replay/attempted"]["coverage"]["feedback_received_ns"]["observed"], 1)

    def test_p95_only_appears_with_twenty_observed_segments(self):
        rows = [attempt(str(index), recognition_completed_ns=1_010_000_000 + index)
                for index in range(20)]
        report = audit_attempts(rows)
        segment = report["groups"]["replay/none"]["segments"]["capture_to_recognition"]
        self.assertEqual(segment["n"], 20)
        self.assertIsNotNone(segment["p95_ms"])

    def test_rejects_ambiguous_action_and_duplicate_opportunity(self):
        with self.assertRaisesRegex(ValueError, "action_requires"):
            audit_attempts([attempt("x", action_sent_ns=1_020_000_000)])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            audit_attempts([attempt("x"), attempt("x", outcome="failed")])

    def test_keeps_mixed_sources_and_executions_in_separate_timing_groups(self):
        replay = attempt("replay", recognition_completed_ns=1_020_000_000)
        live = attempt("live", source_kind="live", execution="attempted", outcome="action_confirmed",
                       recognition_completed_ns=5_010_000_000, decision_completed_ns=5_020_000_000,
                       action_sent_ns=5_030_000_000, feedback_received_ns=5_040_000_000,
                       observed_monotonic_ns=1_000_000_000, capture_received_ns=1_010_000_000)
        report = audit_attempts([replay, live])
        self.assertNotIn("segments", report)
        self.assertEqual(report["groups"]["replay/none"]["segments"]["capture_to_recognition"]["p50_ms"], 10)
        self.assertEqual(report["groups"]["live/attempted"]["segments"]["capture_to_recognition"]["p50_ms"], 4000)

    def test_display_and_action_branches_can_have_independent_order(self):
        row = attempt("branches", "action_confirmed", source_kind="live", execution="attempted",
                      recognition_completed_ns=1_020_000_000, decision_completed_ns=1_030_000_000,
                      display_reported_ns=1_040_000_000, action_sent_ns=1_050_000_000,
                      feedback_received_ns=1_060_000_000)
        report = audit_attempts([row])
        self.assertEqual(report["groups"]["live/attempted"]["segments"]["decision_to_display"]["p50_ms"], 10)
        self.assertEqual(report["groups"]["live/attempted"]["segments"]["action_to_feedback"]["p50_ms"], 10)

    def test_sparse_live_timing_allows_unmeasured_intermediate_stages(self):
        action_only = attempt("sparse-action", "action_confirmed", source_kind="live", execution="attempted",
                              recognition_completed_ns=None, decision_completed_ns=None,
                              action_sent_ns=1_030_000_000, feedback_received_ns=1_040_000_000)
        display_only = attempt("sparse-display", "advice_displayed", source_kind="live",
                               recognition_completed_ns=None, decision_completed_ns=None,
                               display_reported_ns=1_040_000_000)
        feedback_without_action_stamp = attempt("sparse-feedback", "action_confirmed", source_kind="live",
                                                 execution="attempted", recognition_completed_ns=None,
                                                 decision_completed_ns=None, feedback_received_ns=1_040_000_000)
        report = audit_attempts([action_only, display_only, feedback_without_action_stamp])
        attempted = report["groups"]["live/attempted"]
        self.assertEqual(attempted["segments"]["action_to_feedback"]["p50_ms"], 10)
        self.assertEqual(attempted["segments"]["recognition_to_decision"]["unmeasured"], 2)
        self.assertEqual(report["groups"]["live/none"]["segments"]["decision_to_display"]["unmeasured"], 1)

    def test_sparse_timing_still_rejects_order_regression_across_null_stages(self):
        with self.assertRaisesRegex(ValueError, "display_branch"):
            audit_attempts([attempt("bad-display", "advice_displayed", source_kind="live",
                                   capture_received_ns=1_100_000_000, recognition_completed_ns=None,
                                   decision_completed_ns=None, display_reported_ns=1_050_000_000)])
        with self.assertRaisesRegex(ValueError, "action_branch"):
            audit_attempts([attempt("bad-action", "action_confirmed", source_kind="live", execution="attempted",
                                   capture_received_ns=1_100_000_000, recognition_completed_ns=None,
                                   decision_completed_ns=None, action_sent_ns=1_050_000_000,
                                   feedback_received_ns=1_060_000_000)])

    def test_empty_input_is_not_evidence_that_producer_logged_every_opportunity(self):
        report = audit_attempts([])
        self.assertFalse(report["all_supplied_rows_accounted"])
        self.assertEqual(report["input_opportunity_completeness"], "not_verified_by_audit")

    def test_cli_writes_private_summary_without_echoing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path, report_path = root / "attempts.jsonl", root / "report.json"
            input_path.write_text(json.dumps(attempt("opaque-private-id")) + "\n", encoding="utf-8")
            self.assertEqual(main(["--attempts", str(input_path), "--report", str(report_path)]), 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["opportunities"], 1)
            self.assertNotIn("opaque-private-id", report_path.read_text(encoding="utf-8"))
            self.assertEqual(read_attempts(input_path)[0]["opportunity_id"], "opaque-private-id")

    def test_duplicate_json_key_and_bad_source_fail_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate = root / "duplicate.jsonl"
            report = root / "report.json"
            duplicate.write_text('{"schema_version":"latency-attempt-v1","schema_version":"bad"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate_json_key"):
                read_attempts(duplicate)
            bad_source = root / "bad-source.jsonl"
            bad = attempt("bad", source_kind=[])
            bad_source.write_text(json.dumps(bad) + "\n", encoding="utf-8")
            self.assertEqual(main(["--attempts", str(bad_source), "--report", str(report)]), 1)

    @unittest.skipUnless(hasattr(__import__("os"), "symlink"), "symlinks unavailable")
    def test_report_final_symlink_is_rejected_without_following_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path, report_link, target = root / "attempts.jsonl", root / "report.json", root / "target.json"
            input_path.write_text(json.dumps(attempt("one")) + "\n", encoding="utf-8")
            report_link.symlink_to(target)
            self.assertEqual(main(["--attempts", str(input_path), "--report", str(report_link)]), 1)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
