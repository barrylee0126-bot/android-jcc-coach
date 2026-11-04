import json
from pathlib import Path
import tempfile
import unittest

from jcc_latency_audit.audit import ATTEMPT_SCHEMA, VISIBILITY_SCHEMA, audit_attempts, audit_visibility_attempts, read_attempts
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


def visibility_attempt(identifier, outcome="unknown", **changes):
    row = dict(
        schema_version=VISIBILITY_SCHEMA,
        opportunity_id=identifier,
        source_kind="live_candidate",
        source_stream_id="opaque-stream",
        source_sequence=1,
        execution="none",
        outcome=outcome,
        host_clock_domain_id="host-run-a",
        record_kind="frame_opportunity",
        host_stream_started_ns=None,
        case_id=None,
        case_partition=None,
        source_video_digest=None,
        source_frame_digest=None,
        source_pts_ns=None,
        source_capture_clock_domain_id=None,
        source_capture_ns=None,
        replay_input_read_started_host_ns=None,
        host_frame_received_ns=1_000_000_000,
        recognition_completed_ns=None,
        decision_completed_ns=None,
        overlay_ingested_ns=None,
        browser_dom_report_received_ns=None,
        browser_raf_report_received_ns=None,
        browser_withdrawn_report_received_ns=None,
        presentation_session_id=None,
        presentation_revision=None,
        render_token=None,
        target_client_id=None,
        dom_report_session_id=None,
        dom_report_client_id=None,
        dom_report_client_event_sequence=None,
        dom_report_revision=None,
        dom_report_render_token=None,
        dom_report_opportunity_id=None,
        dom_report_stage=None,
        dom_report_visibility=None,
        dom_report_browser_monotonic_ms=None,
        raf_report_session_id=None,
        raf_report_client_id=None,
        raf_report_client_event_sequence=None,
        raf_report_revision=None,
        raf_report_render_token=None,
        raf_report_opportunity_id=None,
        raf_report_stage=None,
        raf_report_visibility=None,
        raf_report_browser_monotonic_ms=None,
        withdrawn_report_session_id=None,
        withdrawn_report_client_id=None,
        withdrawn_report_client_event_sequence=None,
        withdrawn_report_revision=None,
        withdrawn_report_render_token=None,
        withdrawn_report_opportunity_id=None,
        withdrawn_report_stage=None,
        withdrawn_report_visibility=None,
        withdrawn_report_browser_monotonic_ms=None,
        browser_dom_to_raf_ms=None,
        physical_visible_clock_domain_id=None,
        physical_visible_ns=None,
        user_acknowledged_clock_domain_id=None,
        user_acknowledged_ns=None,
    )
    row.update(changes)
    return row


def matched_raf(identifier, *, client="player-tablet", session="presentation-a", revision=1, token="render-a",
                event_sequence=2, browser_monotonic_ms=100.0, interval_ms=5.5, **changes):
    row = dict(
        overlay_ingested_ns=1_040_000_000,
        browser_raf_report_received_ns=1_080_000_000,
        presentation_session_id=session, presentation_revision=revision, render_token=token, target_client_id=client,
        raf_report_session_id=session, raf_report_client_id=client, raf_report_client_event_sequence=event_sequence,
        raf_report_revision=revision, raf_report_render_token=token, raf_report_opportunity_id=identifier,
        raf_report_stage="paint_opportunity", raf_report_visibility="visible",
        raf_report_browser_monotonic_ms=browser_monotonic_ms, browser_dom_to_raf_ms=interval_ms,
    )
    row.update(changes)
    return row


def matched_dom(identifier, *, client="player-tablet", session="presentation-a", revision=1, token="render-a",
                event_sequence=1, browser_monotonic_ms=95.0, **changes):
    row = dict(
        overlay_ingested_ns=1_040_000_000,
        browser_dom_report_received_ns=1_060_000_000,
        presentation_session_id=session, presentation_revision=revision, render_token=token, target_client_id=client,
        dom_report_session_id=session, dom_report_client_id=client, dom_report_client_event_sequence=event_sequence,
        dom_report_revision=revision, dom_report_render_token=token, dom_report_opportunity_id=identifier,
        dom_report_stage="dom_updated", dom_report_visibility="visible",
        dom_report_browser_monotonic_ms=browser_monotonic_ms,
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

    def test_visibility_audit_keeps_raf_and_terminal_failures_in_one_group(self):
        ready = visibility_attempt("raf", "browser_raf_reported", **{
            "source_capture_clock_domain_id": "tablet-run", "source_capture_ns": 900_000_000,
            "recognition_completed_ns": 1_020_000_000, "decision_completed_ns": 1_030_000_000,
            **matched_raf("raf"), **matched_dom("raf")})
        unknown = visibility_attempt("unknown", "unknown", source_sequence=2)
        report = audit_visibility_attempts([ready, unknown])
        group = report["groups"]["frame_opportunity/live_candidate/none/host-run-a/no-case"]
        self.assertEqual(group["opportunities"], 2)
        self.assertEqual(group["outcomes"]["browser_raf_reported"], 1)
        self.assertEqual(group["outcomes"]["unknown"], 1)
        self.assertEqual(group["host_clock_segments"]["host_frame_to_raf_report"]["p50_ms"], 80)
        self.assertEqual(group["host_clock_segments"]["live_source_capture_to_host_frame"]["cross_clock_not_compared"], 1)
        self.assertEqual(group["browser_dom_to_raf"]["p50_ms"], 5.5)
        self.assertEqual(report["evidence_boundary"]["host_raf_report"],
                         "host receipt of a caller-declared, field-matched browser paint-opportunity report; not physical screen visibility")

    def test_visibility_audit_compares_external_stamps_only_in_the_same_clock_domain(self):
        row = visibility_attempt("same-clock", source_capture_clock_domain_id="host-run-a", source_capture_ns=950_000_000,
                                 physical_visible_clock_domain_id="other-display-clock", physical_visible_ns=1_100_000_000,
                                 user_acknowledged_clock_domain_id="host-run-a", user_acknowledged_ns=1_120_000_000)
        report = audit_visibility_attempts([row])
        group = report["groups"]["frame_opportunity/live_candidate/none/host-run-a/no-case"]
        self.assertEqual(group["host_clock_segments"]["live_source_capture_to_host_frame"]["p50_ms"], 50)
        self.assertEqual(group["host_clock_segments"]["host_frame_to_physical_visible"]["cross_clock_not_compared"], 1)
        self.assertEqual(group["host_clock_segments"]["host_frame_to_user_acknowledged"]["p50_ms"], 120)

    def test_visibility_rejects_unbound_raf_and_reversed_host_stages(self):
        with self.assertRaisesRegex(ValueError, "raf_report_receipt_binding"):
            audit_visibility_attempts([visibility_attempt("unbound", "browser_raf_reported",
                                                           browser_raf_report_received_ns=1_050_000_000)])
        with self.assertRaisesRegex(ValueError, "timestamp_order"):
            audit_visibility_attempts([visibility_attempt("reversed", recognition_completed_ns=1_020_000_000,
                                                           overlay_ingested_ns=1_010_000_000)])

    def test_visibility_case_binding_keeps_development_and_holdout_separate(self):
        digest_a, digest_b = "a" * 64, "b" * 64
        dev = visibility_attempt("dev", source_kind="replay_candidate", case_id="25a-dev-2-5-467527-v1", case_partition="development",
                                 source_video_digest=digest_a, source_frame_digest=digest_b, source_pts_ns=467_527_078_000)
        holdout = visibility_attempt("holdout", source_kind="replay_candidate", source_sequence=2, case_id="25a-holdout-example", case_partition="holdout",
                                     source_video_digest=digest_a, source_frame_digest="c" * 64, source_pts_ns=500_000_000_000)
        report = audit_visibility_attempts([dev, holdout])
        self.assertEqual(set(report["groups"]), {"frame_opportunity/replay_candidate/none/host-run-a/development",
                                                  "frame_opportunity/replay_candidate/none/host-run-a/holdout"})
        with self.assertRaisesRegex(ValueError, "case_binding"):
            audit_visibility_attempts([visibility_attempt("bad-case", case_id="case", case_partition="development")])

    def test_visibility_rejects_same_clock_negative_latency_and_binding_conflicts(self):
        with self.assertRaisesRegex(ValueError, "source_capture_after"):
            audit_visibility_attempts([visibility_attempt("late-capture", source_capture_clock_domain_id="host-run-a",
                                                           source_capture_ns=1_100_000_000)])
        with self.assertRaisesRegex(ValueError, "physical_visible_before"):
            audit_visibility_attempts([visibility_attempt("early-display", physical_visible_clock_domain_id="host-run-a",
                                                           physical_visible_ns=900_000_000)])
        with self.assertRaisesRegex(ValueError, "user_acknowledged_before"):
            audit_visibility_attempts([visibility_attempt("early-ack", user_acknowledged_clock_domain_id="host-run-a",
                                                           user_acknowledged_ns=900_000_000)])
        ready = matched_raf("first", client="tablet", session="session", token="token",
                            overlay_ingested_ns=1_020_000_000, browser_raf_report_received_ns=1_030_000_000)
        with self.assertRaisesRegex(ValueError, "raf_report_binding_mismatch"):
            audit_visibility_attempts([visibility_attempt("mismatch", "browser_raf_reported", **{
                **matched_raf("mismatch", client="tablet", session="session", token="token"),
                "raf_report_client_id": "mac"})])
        with self.assertRaisesRegex(ValueError, "presentation_binding_conflict"):
            audit_visibility_attempts([visibility_attempt("first", "browser_raf_reported", **ready),
                                       visibility_attempt("second", "browser_raf_reported", source_sequence=2,
                                                          **matched_raf("second", client="tablet", session="session", token="token"))])

    def test_visibility_rejects_invalid_external_clock_pairs_without_type_errors(self):
        for changes in (
            {"source_capture_clock_domain_id": "host-run-a", "source_capture_ns": "nan"},
            {"physical_visible_clock_domain_id": "bad clock", "physical_visible_ns": 1_020_000_000},
            {"user_acknowledged_clock_domain_id": "host-run-a", "user_acknowledged_ns": True},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "clock_pair"):
                    audit_visibility_attempts([visibility_attempt("bad-clock", **changes)])

    def test_visibility_replay_and_pre_frame_failure_are_separate_and_strict(self):
        digest = "a" * 64
        replay = visibility_attempt("replay", source_kind="replay_candidate", case_id="25a-dev-2-5-467527-v1",
                                    case_partition="development", source_video_digest=digest,
                                    source_frame_digest="b" * 64, source_pts_ns=467_527_078_000,
                                    replay_input_read_started_host_ns=990_000_000)
        start_failure = visibility_attempt("before-frame", source_kind="live_candidate", source_sequence=None,
                                           outcome="disconnected", record_kind="stream_start_failure",
                                           host_stream_started_ns=900_000_000, host_frame_received_ns=None)
        report = audit_visibility_attempts([replay, start_failure])
        self.assertEqual(report["record_kinds"], {"frame_opportunity": 1, "stream_start_failure": 1})
        self.assertIn("stream_start_failure/live_candidate/none/host-run-a/no-case", report["groups"])
        with self.assertRaisesRegex(ValueError, "replay_requires_case"):
            audit_visibility_attempts([visibility_attempt("missing-case", source_kind="replay_candidate")])
        with self.assertRaisesRegex(ValueError, "nonreplay_forbids"):
            audit_visibility_attempts([visibility_attempt("bad-live-case", case_id="case", case_partition="development",
                                                           source_video_digest=digest, source_frame_digest="b" * 64,
                                                           source_pts_ns=1)])
        with self.assertRaisesRegex(ValueError, "replay_capture_clock"):
            audit_visibility_attempts([dict(replay, source_capture_clock_domain_id="host-run-a", source_capture_ns=1)])

    def test_visibility_rejects_partition_overlap_and_malformed_values_but_allows_parallel_reports(self):
        digest = "a" * 64
        dev = visibility_attempt("dev", source_kind="replay_candidate", case_id="case-dev", case_partition="development",
                                 source_video_digest=digest, source_frame_digest="b" * 64, source_pts_ns=1)
        overlap = visibility_attempt("holdout", source_kind="replay_candidate", source_sequence=2, case_id="case-holdout",
                                     case_partition="holdout", source_video_digest=digest, source_frame_digest="b" * 64,
                                     source_pts_ns=1)
        with self.assertRaisesRegex(ValueError, "case_partition_overlap"):
            audit_visibility_attempts([dev, overlap])
        with self.assertRaisesRegex(ValueError, "case_binding"):
            audit_visibility_attempts([dict(dev, case_partition=[])])
        with self.assertRaisesRegex(ValueError, "browser_dom_to_raf"):
            audit_visibility_attempts([visibility_attempt("huge", **matched_raf("huge", browser_dom_to_raf_ms=10 ** 500))])
        with self.assertRaisesRegex(ValueError, "dom_report_receipt_binding"):
            audit_visibility_attempts([visibility_attempt("unknown-bare-dom", overlay_ingested_ns=1_020_000_000,
                                                           browser_dom_report_received_ns=1_030_000_000)])
        report = audit_visibility_attempts([visibility_attempt("parallel", "browser_raf_reported", **{
            **matched_raf("parallel", session="session", client="tablet", token="token",
                          overlay_ingested_ns=1_020_000_000, browser_raf_report_received_ns=1_040_000_000,
                          interval_ms=3.0),
            **matched_dom("parallel", session="session", client="tablet", token="token",
                          overlay_ingested_ns=1_020_000_000, browser_dom_report_received_ns=1_050_000_000)})])
        group = report["groups"]["frame_opportunity/live_candidate/none/host-run-a/no-case"]
        self.assertEqual(group["host_clock_segments"]["overlay_ingest_to_dom_report"]["p50_ms"], 30)
        self.assertEqual(group["host_clock_segments"]["overlay_ingest_to_raf_report"]["p50_ms"], 20)

    def test_visibility_raf_requires_complete_matching_25e_event(self):
        valid = visibility_attempt("raf", "browser_raf_reported", **matched_raf("raf"))
        for field, value, error in (
            ("raf_report_client_event_sequence", None, "raf_report_requires_complete_binding"),
            ("raf_report_opportunity_id", "other", "raf_report_binding_mismatch"),
            ("raf_report_stage", "dom_updated", "raf_report_stage"),
            ("raf_report_visibility", "hidden", "raf_report_visibility"),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, error):
                    audit_visibility_attempts([dict(valid, **{field: value})])
        with self.assertRaisesRegex(ValueError, "browser_dom_to_raf"):
            audit_visibility_attempts([visibility_attempt("bare-interval", browser_dom_to_raf_ms=2.0)])

    def test_visibility_allows_expected_only_timeout_and_independent_dom_report(self):
        timeout = visibility_attempt("timeout", "timeout", overlay_ingested_ns=1_020_000_000,
                                     presentation_session_id="session", presentation_revision=1,
                                     render_token="token", target_client_id="tablet")
        dom_only = visibility_attempt("dom", source_sequence=2, **matched_dom("dom", session="session", client="tablet", token="token",
                                                             overlay_ingested_ns=1_020_000_000,
                                                             browser_dom_report_received_ns=1_040_000_000))
        report = audit_visibility_attempts([timeout, dom_only])
        group = report["groups"]["frame_opportunity/live_candidate/none/host-run-a/no-case"]
        self.assertEqual(group["outcomes"]["timeout"], 1)
        self.assertEqual(group["host_clock_segments"]["overlay_ingest_to_dom_report"]["n"], 1)
        self.assertEqual(group["host_clock_segments"]["overlay_ingest_to_raf_report"]["n"], 0)

    def test_visibility_keeps_withdrawn_browser_event_out_of_raf_success(self):
        withdrawn = visibility_attempt("withdrawn", "withdrawn", overlay_ingested_ns=1_020_000_000,
            browser_withdrawn_report_received_ns=1_030_000_000,
            presentation_session_id="session", presentation_revision=1, render_token="token", target_client_id="tablet",
            withdrawn_report_session_id="session", withdrawn_report_client_id="tablet",
            withdrawn_report_client_event_sequence=9, withdrawn_report_revision=1,
            withdrawn_report_render_token="token", withdrawn_report_opportunity_id="withdrawn",
            withdrawn_report_stage="withdrawn", withdrawn_report_visibility="hidden",
            withdrawn_report_browser_monotonic_ms=100.0)
        report = audit_visibility_attempts([withdrawn])
        group = report["groups"]["frame_opportunity/live_candidate/none/host-run-a/no-case"]
        self.assertEqual(group["outcomes"]["withdrawn"], 1)
        self.assertEqual(group["host_clock_segments"]["host_frame_to_raf_report"]["n"], 0)

    def test_visibility_rejects_non_visible_raf_and_local_dom_raf_order_errors(self):
        hidden = visibility_attempt("hidden", "unknown", **matched_raf("hidden", raf_report_visibility="hidden"))
        with self.assertRaisesRegex(ValueError, "raf_report_visibility"):
            audit_visibility_attempts([hidden])
        equal = visibility_attempt("equal", "browser_raf_reported", **{
            **matched_raf("equal", event_sequence=1, browser_monotonic_ms=100),
            **matched_dom("equal", event_sequence=1, browser_monotonic_ms=99)})
        with self.assertRaisesRegex(ValueError, "browser_local_dom_raf_sequence_order"):
            audit_visibility_attempts([equal])
        reversed_local = visibility_attempt("reversed-local", "browser_raf_reported", **{
            **matched_raf("reversed-local", event_sequence=2, browser_monotonic_ms=99),
            **matched_dom("reversed-local", event_sequence=1, browser_monotonic_ms=100)})
        with self.assertRaisesRegex(ValueError, "browser_local_dom_raf_clock_order"):
            audit_visibility_attempts([reversed_local])

    def test_visibility_rejects_reused_client_event_sequence_and_report_in_start_failure(self):
        first = visibility_attempt("first", "browser_raf_reported", **matched_raf("first", event_sequence=7))
        second = visibility_attempt("second", "browser_raf_reported", source_sequence=2,
                                    **matched_raf("second", session="presentation-b", token="render-b", event_sequence=7))
        with self.assertRaisesRegex(ValueError, "browser_client_event_sequence_conflict"):
            audit_visibility_attempts([first, second])
        start = visibility_attempt("start", source_sequence=None, outcome="disconnected",
                                   record_kind="stream_start_failure", host_stream_started_ns=1,
                                   host_frame_received_ns=None, withdrawn_report_session_id="forged")
        with self.assertRaisesRegex(ValueError, "stream_start_failure"):
            audit_visibility_attempts([start])

    def test_visibility_rejects_cross_partition_case_video_pts_or_frame_overlap(self):
        common = dict(source_kind="replay_candidate", source_video_digest="a" * 64,
                      source_frame_digest="b" * 64, source_pts_ns=42)
        dev = visibility_attempt("dev", case_id="case-dev", case_partition="development", **common)
        cases = (
            visibility_attempt("case", source_kind="replay_candidate", source_sequence=2, case_id="case-dev", case_partition="holdout",
                               source_video_digest="c" * 64, source_frame_digest="d" * 64, source_pts_ns=43),
            visibility_attempt("video-pts", source_kind="replay_candidate", source_sequence=2, case_id="case-holdout", case_partition="holdout",
                               source_video_digest="a" * 64, source_frame_digest="c" * 64, source_pts_ns=42),
            visibility_attempt("frame", source_kind="replay_candidate", source_sequence=2, case_id="case-holdout", case_partition="holdout",
                               source_video_digest="c" * 64, source_frame_digest="b" * 64, source_pts_ns=43),
        )
        for overlap in cases:
            with self.subTest(opportunity=overlap["opportunity_id"]):
                with self.assertRaisesRegex(ValueError, "case_partition_overlap"):
                    audit_visibility_attempts([dev, overlap])

    def test_visibility_audit_is_single_target_client_scope(self):
        one = visibility_attempt("one", "browser_raf_reported", **matched_raf("one", client="tablet"))
        two = visibility_attempt("two", "browser_raf_reported", source_sequence=2,
                                 **matched_raf("two", client="mac", session="presentation-b", token="render-b"))
        with self.assertRaisesRegex(ValueError, "multiple_target_clients_not_supported"):
            audit_visibility_attempts([one, two])
        pending_tablet = visibility_attempt("pending", "timeout", source_sequence=3,
                                            presentation_session_id="session", presentation_revision=1,
                                            render_token="token", target_client_id="tablet")
        actual_mac = visibility_attempt("mac", "browser_raf_reported", source_sequence=4,
                                        **matched_raf("mac", client="mac", session="session-mac", token="token-mac"))
        with self.assertRaisesRegex(ValueError, "multiple_target_clients_not_supported"):
            audit_visibility_attempts([pending_tablet, actual_mac])

    def test_visibility_rejects_one_declared_browser_event_for_two_sources(self):
        first = visibility_attempt("first", "browser_raf_reported", **matched_raf("first"))
        second = visibility_attempt("second", "browser_raf_reported", source_sequence=2,
                                    **matched_raf("second", session="presentation-b", token="render-b",
                                                  event_sequence=3,
                                                  raf_report_opportunity_id="first"))
        # Reuse the complete declared event identity while its expected source
        # binding differs, modeling a replay/misbinding attempt.
        second.update({key: first[key] for key in (
            "raf_report_session_id", "raf_report_client_id", "raf_report_client_event_sequence",
            "raf_report_revision", "raf_report_render_token", "raf_report_opportunity_id", "raf_report_stage")})
        with self.assertRaisesRegex(ValueError, "raf_report_binding_mismatch"):
            audit_visibility_attempts([first, second])

    def test_visibility_cli_uses_the_separate_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path, report_path = root / "visibility.jsonl", root / "visibility-report.json"
            row = visibility_attempt("one", "browser_raf_reported",
                                     **matched_raf("one", overlay_ingested_ns=1_040_000_000,
                                                   browser_raf_report_received_ns=1_050_000_000))
            input_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.assertEqual(main(["--visibility-attempts", str(input_path), "--report", str(report_path)]), 0)
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["schema_version"],
                             "latency-visibility-report-v1")


if __name__ == "__main__":
    unittest.main()
