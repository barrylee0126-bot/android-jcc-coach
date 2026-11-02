from copy import deepcopy
from unittest.mock import patch
import unittest

from jcc_hypothesis_advice import ClockTick, HypothesisSession, ReplayReviewClock
from jcc_hypothesis_advice.fixtures import (
    FixtureClock, example_request, fixture_session, next_frame, rebind, set_claim,
)


def codes(result):
    return {reason["code"] for reason in result["reasons"]}


class ScriptedClock:
    def __init__(self, ticks):
        self.ticks = iter(ticks)
        self.last = None

    def now(self):
        self.last = next(self.ticks, self.last)
        return self.last


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.clock = FixtureClock()
        self.session = fixture_session(self.clock)
        self.case = example_request()

    def issued(self):
        result = self.session.submit(self.case)
        self.assertEqual(result["status"], "hypothesis_only", result)
        return result

    def blocked(self, request, code):
        result = self.session.submit(request)
        self.assertIn(code, codes(result), result)
        self.assertIsNone(result["analysis"])
        self.assertIsNone(self.session.view()["analysis"])
        return result

    def test_every_binding_component_is_checked_for_fields_and_evidence(self):
        changes = {
            "frame_id": "other-frame", "frame_sha256": "b" * 64, "stream_id": "other-stream",
            "sequence": 3, "snapshot_revision": 3, "context_epoch": 2,
            "review_clock_id": "other-clock", "received_review_ms": 999, "assembled_review_ms": 1029,
        }
        for target in ("field", "evidence"):
            for key, value in changes.items():
                with self.subTest(target=target, key=key):
                    case = example_request()
                    item = case["claims"]["gold"]
                    if target == "evidence":
                        item = item["basis"][0]
                    item["binding"][key] = value
                    result = fixture_session().submit(case)
                    self.assertEqual(result["status"], "refused")
                    expected = "field_binding_mismatch" if target == "field" else "evidence_binding_mismatch"
                    self.assertIn(expected, codes(result))
                    self.assertIsNone(result["analysis"])

    def test_root_clock_mismatch_is_refused_before_timestamp_comparison(self):
        case = rebind(self.case, review_clock_id="different-domain", assembled_review_ms=9999999)
        result = self.session.submit(case)
        self.assertEqual(codes(result), {"clock_domain_mismatch"})
        self.assertIsNone(result["analysis"])

    def test_future_and_reversed_frame_times_withdraw_previous_result(self):
        for changes in ({"received_review_ms": 1040}, {"assembled_review_ms": 1200}):
            case = rebind(self.case, **changes)
            result = fixture_session().submit(case)
            self.assertIn("frame_time_invalid", codes(result))
            self.assertIsNone(result["analysis"])
        self.issued()
        bad = rebind(self.case, snapshot_revision=2, assembled_review_ms=1200)
        self.blocked(bad, "frame_time_invalid")
        self.blocked(self.case, "snapshot_not_new")

    def test_field_and_evidence_completion_must_belong_to_the_frame_review_interval(self):
        for key, value in (("observed_review_ms", 999), ("completed_review_ms", 999),
                           ("completed_review_ms", 1040), ("expires_review_ms", 1020)):
            case = example_request()
            case["claims"]["gold"][key] = value
            result = fixture_session().submit(case)
            self.assertIn("field_time_invalid", codes(result))
        for key, value in (("assessed_review_ms", 999), ("assessed_review_ms", 1040),
                           ("expires_review_ms", 1020)):
            case = example_request()
            case["claims"]["gold"]["basis"][0][key] = value
            result = fixture_session().submit(case)
            self.assertIn("evidence_time_invalid", codes(result))

    def test_field_evidence_and_frame_age_expire_at_exact_boundary(self):
        for layer in ("field", "evidence", "frame"):
            case = example_request()
            if layer == "field":
                case["claims"]["gold"]["expires_review_ms"] = 1100
            elif layer == "evidence":
                case["claims"]["gold"]["basis"][0]["expires_review_ms"] = 1100
            session = fixture_session(max_frame_age_ms=100 if layer == "frame" else 1000)
            result = session.submit(case)
            self.assertEqual(result["status"], "insufficient_evidence")
            self.assertIn(layer + "_expired", codes(result))
            self.assertIsNone(result["analysis"])

    def test_result_deadline_is_minimum_of_every_dependency_and_own_limit(self):
        case = example_request()
        case["claims"]["gold"]["expires_review_ms"] = 1250
        case["claims"]["reserve"]["basis"][0]["expires_review_ms"] = 1200
        result = self.session.submit(case)
        self.assertEqual(result["expires_review_ms"], 1200)
        short = fixture_session(max_frame_age_ms=150).submit(example_request())
        self.assertEqual(short["expires_review_ms"], 1150)

    def test_duplicate_submit_and_view_do_not_extend_result_or_source_deadlines(self):
        first = self.issued()
        self.clock.advance(200)
        self.assertEqual(self.session.view()["expires_review_ms"], first["expires_review_ms"])
        repeated = self.session.submit(deepcopy(self.case))
        self.assertEqual(repeated, first)
        self.clock.advance(100)
        result = self.session.view()
        self.assertEqual(result["status"], "expired")
        self.assertIsNone(result["analysis"])
        self.assertIsNone(result["expires_review_ms"])
        self.blocked(self.case, "snapshot_not_new")

    def test_calculation_and_serialization_time_can_expire_result_before_publish(self):
        clock = ScriptedClock([ClockTick("fixture-review-clock", 1100), ClockTick("fixture-review-clock", 1400)])
        session = HypothesisSession(clock=clock, max_frame_age_ms=1000, max_result_ms=300)
        result = session.submit(self.case)
        self.assertEqual(result["status"], "expired")
        self.assertIn("expired_during_evaluation", codes(result))
        self.assertIsNone(result["analysis"])

    def test_all_ingress_barriers_withdraw_and_old_revision_cannot_return(self):
        for event in ("frame_changed", "disconnected", "occluded", "evidence_changed", "context_changed"):
            with self.subTest(event=event):
                session = fixture_session()
                self.assertEqual(session.submit(self.case)["status"], "hypothesis_only")
                result = session.invalidate(event)
                self.assertEqual(result["status"], "invalidated")
                self.assertIsNone(result["analysis"])
                self.assertIn("snapshot_not_new", codes(session.submit(self.case)))

    def test_malformed_invalidation_event_also_withdraws_current_explanation(self):
        for event in ("unsupported-event", [], None):
            session = fixture_session()
            session.submit(self.case)
            result = session.invalidate(event)
            self.assertEqual(result["status"], "refused")
            self.assertIn("invalidation_event_unknown", codes(result))
            self.assertIsNone(session.view()["analysis"])

    def test_unknown_new_revision_retires_old_success_and_same_revision_correction(self):
        self.issued()
        unknown = rebind(self.case, snapshot_revision=2)
        unknown["claims"]["gold"].update(status="unknown", value=None, expires_review_ms=None)
        result = self.session.submit(unknown)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.blocked(self.case, "snapshot_not_new")
        corrected = rebind(self.case, snapshot_revision=2)
        self.blocked(corrected, "snapshot_not_new")
        self.assertEqual(self.session.submit(rebind(self.case, snapshot_revision=3))["status"], "hypothesis_only")

    def test_same_frame_cannot_extend_deadline_even_through_unknown_revision(self):
        self.issued()
        unknown = rebind(self.case, snapshot_revision=2)
        unknown["claims"]["gold"].update(status="unknown", value=None, expires_review_ms=None)
        self.session.submit(unknown)
        refreshed = rebind(self.case, snapshot_revision=3)
        refreshed["claims"]["gold"]["expires_review_ms"] = 1900
        self.blocked(refreshed, "field_deadline_extended")

    def test_same_frame_evidence_cannot_extend_deadline_or_reset_assessment(self):
        for key, value in (("expires_review_ms", 1900), ("assessed_review_ms", 1021)):
            self.session = fixture_session(self.clock)
            self.issued()
            refreshed = rebind(self.case, snapshot_revision=2)
            refreshed["claims"]["gold"]["basis"][0][key] = value
            self.blocked(refreshed, "evidence_deadline_extended")

    def test_new_frame_replaces_old_binding_even_when_candidate_values_match(self):
        first = self.issued()
        new = next_frame(self.case)
        result = self.session.submit(new)
        self.assertEqual(result["status"], "hypothesis_only")
        self.assertNotEqual(first["binding"]["frame_id"], result["binding"]["frame_id"])
        self.assertEqual(self.session.view()["input_digest"], result["input_digest"])
        self.blocked(self.case, "snapshot_not_new")

    def test_same_frame_id_cannot_be_paired_with_other_bytes_or_metadata(self):
        for changes in ({"frame_sha256": "b" * 64}, {"sequence": 1}, {"received_review_ms": 1001}, {"context_epoch": 1}):
            self.session = fixture_session(self.clock)
            self.issued()
            self.blocked(rebind(self.case, snapshot_revision=2, **changes), "frame_identity_changed")

    def test_new_frame_cannot_reuse_old_id_or_sequence(self):
        self.issued()
        second = next_frame(self.case)
        self.assertEqual(self.session.submit(second)["status"], "hypothesis_only")
        old_id = next_frame(second)
        old_id = rebind(old_id, frame_id=self.case["binding"]["frame_id"])
        self.blocked(old_id, "frame_identity_reused")

    def test_context_changes_need_new_epoch_and_combat_has_no_spend_explanation(self):
        self.issued()
        bad = next_frame(self.case)
        set_claim(bad, "phase", "combat")
        self.blocked(bad, "context_change_unannounced")
        new = next_frame(self.case, context_changed=True)
        new = rebind(new, snapshot_revision=3)
        set_claim(new, "phase", "combat")
        result = self.session.submit(new)
        self.assertEqual(result["status"], "conditions_not_met")
        self.assertIsNone(result["analysis"]["arithmetic"]["after_gold"])
        self.assertIn("不展示支出推论", result["analysis"]["statement_zh"])

    def test_failed_ordering_revision_is_not_reused_after_editing_the_payload(self):
        self.issued()
        bad = rebind(self.case, snapshot_revision=2, frame_sha256="c" * 64)
        self.blocked(bad, "frame_identity_changed")
        self.blocked(rebind(self.case, snapshot_revision=2), "snapshot_not_new")
        result = self.session.submit(rebind(self.case, snapshot_revision=3))
        self.assertEqual(result["status"], "hypothesis_only")

    def test_stream_or_recording_changes_require_new_session(self):
        for kind in ("stream", "recording"):
            self.session = fixture_session(self.clock)
            self.issued()
            case = next_frame(self.case)
            if kind == "stream":
                case = rebind(case, stream_id="other-stream")
            else:
                case["source"]["recording_ref"] = "synthetic:other-recording"
            self.blocked(case, "review_session_changed" if kind == "stream" else "source_changed")

    def test_revocation_is_an_atomic_replacement_and_no_old_text_survives(self):
        self.issued()
        revoked = rebind(self.case, snapshot_revision=2)
        revoked["claims"]["gold"]["basis"][0]["status"] = "revoked"
        result = self.session.submit(revoked)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIn("evidence_revoked", codes(result))
        self.assertIsNone(result["analysis"])
        self.blocked(self.case, "snapshot_not_new")

    def test_bad_shape_withdraws_current_explanation_before_returning(self):
        self.issued()
        self.blocked({"unexpected": "not-a-case"}, "input_malformed")
        self.blocked(self.case, "snapshot_not_new")

    def test_clock_regression_domain_change_and_failure_remain_closed(self):
        for fault in ("regression", "domain", "failure", "nonfinite"):
            clock = FixtureClock()
            session = fixture_session(clock)
            self.assertEqual(session.submit(self.case)["status"], "hypothesis_only")
            if fault == "regression":
                clock.ms = 1099
            elif fault == "domain":
                clock.review_clock_id = "other-domain"
            elif fault == "nonfinite":
                clock.ms = float("nan")
            else:
                clock.now = lambda: (_ for _ in ()).throw(RuntimeError("never shown"))
            result = session.view()
            self.assertEqual(result["status"], "refused")
            self.assertIsNone(result["analysis"])
            clock.ms = 1100
            clock.review_clock_id = "fixture-review-clock"
            self.assertEqual(session.submit(next_frame(self.case))["status"], "refused")

    def test_invalid_deadlines_are_not_silently_defaulted(self):
        for value in (True, 0, -1, 30001, 1.0):
            for name in ("max_frame_age_ms", "max_result_ms"):
                kwargs = {"max_frame_age_ms": 1000, "max_result_ms": 300, name: value}
                with self.assertRaises(ValueError):
                    HypothesisSession(clock=FixtureClock(), **kwargs)

    def test_mutating_input_or_output_cannot_change_cached_analysis(self):
        first = self.issued()
        self.case["claims"]["gold"]["value"] = 999
        first["analysis"]["arithmetic"]["after_gold"] = 999
        first["execution_eligible"] = True
        current = self.session.view()
        self.assertEqual(current["analysis"]["arithmetic"]["after_gold"], 6)
        self.assertFalse(current["execution_eligible"])

    def test_offline_review_clock_uses_explicit_cutoff_plus_real_elapsed_time(self):
        with patch("jcc_hypothesis_advice.engine.time.monotonic_ns", side_effect=[5000000, 8000000]):
            clock = ReplayReviewClock("explicit-offline-clock", 1234)
            tick = clock.now()
        self.assertEqual(tick, ClockTick("explicit-offline-clock", 1237))

    def test_invalidation_during_calculation_prevents_old_publication(self):
        session = self.session
        calls = 0

        def reading():
            nonlocal calls
            calls += 1
            if calls == 2:
                session.invalidate("evidence_changed")
            return ClockTick("fixture-review-clock", 1100)

        self.clock.now = reading
        result = self.session.submit(self.case)
        self.assertEqual(result["status"], "refused")
        self.assertIn("evaluation_superseded", codes(result))
        self.assertIsNone(result["analysis"])


if __name__ == "__main__":
    unittest.main()
