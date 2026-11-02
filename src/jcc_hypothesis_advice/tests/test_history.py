"""Synthetic regressions for history that must survive snapshot revisions."""

from copy import deepcopy
import unittest

from jcc_hypothesis_advice import ClockTick
from jcc_hypothesis_advice.contract import SCOPE_KEYS, digest
from jcc_hypothesis_advice.fixtures import (
    FixtureClock, example_request, fixture_session, next_frame, rebind, set_claim,
)


def codes(result):
    return {reason["code"] for reason in result["reasons"]}


def set_scope(case, key, value):
    case["scope"][key] = value
    set_claim(case, "scope_binding", case["scope"])
    for name, claim in case["claims"].items():
        for basis in claim["basis"]:
            basis["scope"] = deepcopy(case["scope"])
            basis["source_sha256"] = digest({"synthetic_scope_source": name, "scope": case["scope"],
                                             "value": basis["asserted_value"]})


class HistoryTests(unittest.TestCase):
    def assert_withdrawn(self, session, result, code):
        self.assertIn(code, codes(result), {"status": result["status"], "reasons": result["reasons"]})
        for view in (result, session.view()):
            self.assertIsNone(view["analysis"])
            self.assertIsNone(view["advice"])
            self.assertIsNone(view["action"])
            self.assertFalse(view["production_eligible"])
            self.assertFalse(view["execution_eligible"])

    def test_revision_only_cannot_cross_any_invalidation_barrier(self):
        for event in ("frame_changed", "occluded", "disconnected", "context_changed", "evidence_changed"):
            with self.subTest(event=event):
                case = example_request()
                session = fixture_session()
                self.assertEqual(session.submit(case)["status"], "hypothesis_only")
                session.invalidate(event)
                result = session.submit(rebind(case, snapshot_revision=2))
                expected = ("context_refresh_required" if event == "context_changed" else
                            "evidence_refresh_required" if event == "evidence_changed" else
                            "frame_refresh_required")
                self.assert_withdrawn(session, result, expected)

    def test_frame_barrier_survives_unknown_intermediate_revision(self):
        case = example_request()
        session = fixture_session()
        session.submit(case)
        session.invalidate("frame_changed")
        unknown = rebind(case, snapshot_revision=2)
        unknown["claims"]["gold"].update(status="unknown", value=None, expires_review_ms=None)
        session.submit(unknown)
        result = session.submit(rebind(case, snapshot_revision=3))
        self.assert_withdrawn(session, result, "frame_refresh_required")

    def test_new_capture_recovers_frame_barriers_with_same_candidate_values(self):
        for event in ("frame_changed", "occluded", "disconnected"):
            with self.subTest(event=event):
                case = example_request()
                session = fixture_session()
                session.submit(case)
                session.invalidate(event)
                result = session.submit(next_frame(case))
                self.assertEqual(result["status"], "hypothesis_only", result)
                self.assertNotEqual(result["binding"]["frame_id"], case["binding"]["frame_id"])

    def test_context_event_needs_new_capture_and_epoch(self):
        case = example_request()
        session = fixture_session()
        session.submit(case)
        session.invalidate("context_changed")
        self.assert_withdrawn(session, session.submit(next_frame(case)), "context_refresh_required")
        recovered = rebind(next_frame(case, context_changed=True), snapshot_revision=3)
        self.assertEqual(session.submit(recovered)["status"], "hypothesis_only")

    def test_revoked_content_stays_retired_across_revisions_ids_and_frames(self):
        for disguise in ("revision", "evidence_id", "source_ref", "new_frame"):
            with self.subTest(disguise=disguise):
                case = example_request()
                session = fixture_session(FixtureClock(1200))
                session.submit(case)
                revoked = rebind(case, snapshot_revision=2)
                revoked["claims"]["gold"]["basis"][0]["status"] = "revoked"
                self.assertEqual(session.submit(revoked)["status"], "insufficient_evidence")
                restored = rebind(next_frame(case) if disguise == "new_frame" else case, snapshot_revision=3)
                basis = restored["claims"]["gold"]["basis"][0]
                if disguise == "evidence_id":
                    basis["evidence_id"] = "renamed-revoked-assertion"
                elif disguise == "source_ref":
                    basis["source_ref"] = "synthetic:renamed-old-content"
                self.assert_withdrawn(session, session.submit(restored), "evidence_version_retired")

    def test_new_source_after_revocation_requires_later_review_and_records_update(self):
        clock = FixtureClock()
        case = example_request()
        session = fixture_session(clock)
        session.submit(case)
        revoked = rebind(case, snapshot_revision=2)
        revoked["claims"]["gold"]["basis"][0]["status"] = "revoked"
        session.submit(revoked)
        new = rebind(case, snapshot_revision=3, assembled_review_ms=1230)
        basis = new["claims"]["gold"]["basis"][0]
        basis.update(source_ref="synthetic:new-source-snapshot", source_sha256=digest({"new_synthetic_fact": 1}),
                     assessed_review_ms=1220)
        clock.advance(200)
        # The source assessment must be in the completed review interval.
        clock.advance(30)
        self.assert_withdrawn(session, session.submit(new), "evidence_update_required")
        new = rebind(new, snapshot_revision=4)
        basis = new["claims"]["gold"]["basis"][0]
        basis["review_ref"] = "synthetic:new-source-review-record"
        result = session.submit(new)
        self.assertEqual(result["status"], "hypothesis_only", result["reasons"])
        update = result["evidence_updates"][0]
        self.assertEqual(update["field"], "gold")
        self.assertEqual(update["previous"][0]["source_sha256"], case["claims"]["gold"]["basis"][0]["source_sha256"])
        self.assertEqual(update["replacement"]["source_sha256"], basis["source_sha256"])
        self.assertEqual(update["replacement"]["review_ref"], basis["review_ref"])
        old = rebind(case, snapshot_revision=5, assembled_review_ms=1230)
        self.assert_withdrawn(session, session.submit(old), "evidence_version_retired")

    def test_review_ref_alone_is_not_new_evidence(self):
        case = example_request()
        session = fixture_session()
        session.submit(case)
        session.invalidate("evidence_changed")
        new = rebind(case, snapshot_revision=2)
        for claim in new["claims"].values():
            for basis in claim["basis"]:
                basis["review_ref"] = "synthetic:metadata-only-review"
        self.assert_withdrawn(session, session.submit(new), "evidence_refresh_required")

    def test_last_known_context_survives_unknown_scope_or_claim(self):
        for component in (*SCOPE_KEYS, "scene", "phase", "perspective"):
            with self.subTest(component=component):
                case = example_request()
                session = fixture_session(FixtureClock(1300))
                session.submit(case)
                unknown = next_frame(case)
                if component in SCOPE_KEYS:
                    set_scope(unknown, component, None)
                else:
                    unknown["claims"][component].update(status="unknown", value=None, expires_review_ms=None)
                self.assertEqual(session.submit(unknown)["status"], "insufficient_evidence")
                changed = next_frame(next_frame(case))
                if component in SCOPE_KEYS:
                    set_scope(changed, component, "another-fictional-context")
                else:
                    set_claim(changed, component, {"scene": "lobby", "phase": "combat", "perspective": "other"}[component])
                self.assert_withdrawn(session, session.submit(changed), "context_change_unannounced")
                changed = rebind(changed, snapshot_revision=4, context_epoch=1)
                expected = "hypothesis_only" if component in SCOPE_KEYS else "conditions_not_met"
                self.assertEqual(session.submit(changed)["status"], expected)

    def test_even_same_context_after_unknown_requires_crossing_loss_boundary(self):
        case = example_request()
        session = fixture_session(FixtureClock(1300))
        session.submit(case)
        unknown = next_frame(case)
        del unknown["claims"]["phase"]
        session.submit(unknown)
        recovered = next_frame(next_frame(case))
        self.assert_withdrawn(session, session.submit(recovered), "context_change_unannounced")
        recovered = rebind(recovered, snapshot_revision=4, context_epoch=1)
        self.assertEqual(session.submit(recovered)["status"], "hypothesis_only")

    def test_source_content_version_cannot_equivocate_by_revision_or_alias(self):
        for disguise in ("revision", "evidence_id", "source_ref", "unknown", "new_frame"):
            with self.subTest(disguise=disguise):
                case = example_request()
                session = fixture_session()
                session.submit(case)
                if disguise == "unknown":
                    unknown = rebind(case, snapshot_revision=2)
                    unknown["claims"]["gold"].update(status="unknown", value=None, expires_review_ms=None)
                    session.submit(unknown)
                changed = rebind(next_frame(case) if disguise == "new_frame" else case, snapshot_revision=3)
                changed["claims"]["gold"]["value"] = 12
                basis = changed["claims"]["gold"]["basis"][0]
                basis["asserted_value"] = 12
                if disguise == "evidence_id":
                    basis["evidence_id"] = "different-name-same-source-content"
                elif disguise == "source_ref":
                    basis["source_ref"] = "synthetic:alias-same-source-content"
                self.assert_withdrawn(session, session.submit(changed), "evidence_content_changed")

    def test_changed_assertion_with_distinct_source_content_is_usable(self):
        case = example_request()
        session = fixture_session()
        session.submit(case)
        changed = rebind(case, snapshot_revision=2)
        set_claim(changed, "gold", 12)
        result = session.submit(changed)
        self.assertEqual(result["status"], "hypothesis_only", result["reasons"])
        self.assertEqual(result["analysis"]["arithmetic"]["after_gold"], 9)

    def test_revision_only_before_expiry_keeps_the_first_fact_deadline(self):
        case = example_request()
        clock = FixtureClock()
        session = fixture_session(clock)
        first = session.submit(case)
        clock.advance(200)
        revised = session.submit(rebind(case, snapshot_revision=2))
        self.assertEqual(revised["status"], "hypothesis_only")
        self.assertEqual(revised["expires_review_ms"], first["expires_review_ms"])

    def test_revision_aliases_unknown_and_reordering_do_not_renew_expired_facts(self):
        for disguise in ("revision", "evidence_id", "source_ref", "source_pointer", "review_ref", "unknown", "duplicate_basis"):
            with self.subTest(disguise=disguise):
                case = example_request()
                clock = FixtureClock()
                session = fixture_session(clock)
                session.submit(case)
                if disguise == "unknown":
                    unknown = rebind(case, snapshot_revision=2)
                    unknown["claims"]["gold"].update(status="unknown", value=None, expires_review_ms=None)
                    session.submit(unknown)
                clock.advance(300)
                # Deliberately do not call view(): submit must also catch expiry.
                revised = rebind(case, snapshot_revision=3, assembled_review_ms=1040)
                basis = revised["claims"]["gold"]["basis"][0]
                if disguise == "evidence_id":
                    basis["evidence_id"] = "alias-of-old-assertion"
                elif disguise == "source_ref":
                    basis["source_ref"] = "synthetic:alias-of-old-content"
                elif disguise == "source_pointer":
                    basis["source_pointer"] = "/different-locator-same-content"
                elif disguise == "review_ref":
                    basis["review_ref"] = "synthetic:new-label-without-new-content"
                elif disguise == "duplicate_basis":
                    duplicate = deepcopy(basis)
                    duplicate["evidence_id"] = "another-copy-of-old-assertion"
                    revised["claims"]["gold"]["basis"].insert(0, duplicate)
                result = session.submit(revised)
                self.assertEqual(result["status"], "expired", result["reasons"])
                self.assert_withdrawn(session, result, "fact_result_expired")

    def test_new_frame_or_new_source_content_gets_its_own_bounded_deadline(self):
        for fact in ("new_frame", "new_source"):
            with self.subTest(fact=fact):
                case = example_request()
                clock = FixtureClock()
                session = fixture_session(clock)
                first = session.submit(case)
                clock.advance(300)
                self.assertEqual(session.view()["status"], "expired")
                new = next_frame(case) if fact == "new_frame" else rebind(case, snapshot_revision=2, assembled_review_ms=1400)
                if fact == "new_source":
                    basis = new["claims"]["gold"]["basis"][0]
                    basis.update(source_sha256=digest({"new_synthetic_source": True}), assessed_review_ms=1390)
                result = session.submit(new)
                self.assertEqual(result["status"], "hypothesis_only", result["reasons"])
                self.assertGreater(result["expires_review_ms"], first["expires_review_ms"])
                self.assertLessEqual(result["expires_review_ms"], 1700)

    def test_subsets_of_already_seen_sources_are_not_another_refresh(self):
        case = example_request()
        clock = FixtureClock()
        session = fixture_session(clock)
        session.submit(case)
        clock.advance(100)
        second = rebind(case, snapshot_revision=2)
        extra = deepcopy(second["claims"]["gold"]["basis"][0])
        extra.update(evidence_id="second-source", source_sha256=digest({"second_source": True}))
        second["claims"]["gold"]["basis"].append(extra)
        self.assertEqual(session.submit(second)["expires_review_ms"], 1500)
        clock.advance(300)
        subset = rebind(second, snapshot_revision=3)
        subset["claims"]["gold"]["basis"] = [subset["claims"]["gold"]["basis"][1]]
        self.assert_withdrawn(session, session.submit(subset), "fact_result_expired")

    def test_source_alias_cannot_extend_original_assessment_or_expiry(self):
        case = example_request()
        session = fixture_session()
        session.submit(case)
        alias = rebind(case, snapshot_revision=2)
        basis = alias["claims"]["gold"]["basis"][0]
        basis.update(source_ref="synthetic:same-bytes-different-ref", assessed_review_ms=1021, expires_review_ms=1900)
        self.assert_withdrawn(session, session.submit(alias), "evidence_deadline_extended")

    def test_one_snapshot_cannot_assign_inconsistent_meaning_to_same_source_location(self):
        case = example_request()
        copied = deepcopy(case["claims"]["gold"]["basis"][0])
        copied.update(evidence_id="second-meaning-same-source", asserted_value=3)
        case["claims"]["hypothetical_spend"]["basis"] = [copied]
        session = fixture_session()
        self.assert_withdrawn(session, session.submit(case), "evidence_content_changed")

    def test_foreign_clock_revocation_does_not_enter_local_source_history(self):
        case = example_request()
        foreign = rebind(case, review_clock_id="foreign-review-clock")
        foreign["claims"]["gold"]["basis"][0]["status"] = "revoked"
        session = fixture_session()
        self.assert_withdrawn(session, session.submit(foreign), "clock_domain_mismatch")
        self.assertEqual(session.submit(case)["status"], "hypothesis_only")

    def test_evidence_event_recovers_only_after_every_affected_source_is_replaced(self):
        case = example_request()
        clock = FixtureClock()
        session = fixture_session(clock)
        session.submit(case)
        session.invalidate("evidence_changed")
        self.assert_withdrawn(session, session.submit(next_frame(case)), "evidence_refresh_required")
        new = rebind(case, snapshot_revision=3, assembled_review_ms=1230)
        clock.advance(130)
        for name, claim in new["claims"].items():
            for basis in claim["basis"]:
                basis.update(source_sha256=digest({"new_synthetic_source": name}), assessed_review_ms=1220,
                             review_ref="synthetic:replacement-review-" + name)
        result = session.submit(new)
        self.assertEqual(result["status"], "hypothesis_only", result["reasons"])
        self.assertEqual(len(result["evidence_updates"]), len(case["claims"]))
        revised = session.submit(rebind(new, snapshot_revision=4))
        self.assertEqual(revised["evidence_updates"], result["evidence_updates"])
        self.assertEqual(revised["expires_review_ms"], result["expires_review_ms"])
        self.assert_withdrawn(session, session.submit(rebind(case, snapshot_revision=5, assembled_review_ms=1230)),
                              "evidence_version_retired")

    def test_new_hash_and_review_without_post_retirement_assessment_are_insufficient(self):
        for assessed in (1020, 1100):
            with self.subTest(assessed=assessed):
                case = example_request()
                session = fixture_session()
                session.submit(case)
                revoked = rebind(case, snapshot_revision=2)
                revoked["claims"]["gold"]["basis"][0]["status"] = "revoked"
                session.submit(revoked)
                candidate = rebind(case, snapshot_revision=3, assembled_review_ms=1100)
                candidate["claims"]["gold"]["basis"][0].update(
                    source_sha256=digest({"different_synthetic_source": assessed}),
                    review_ref="synthetic:new-review-but-no-later-assessment", assessed_review_ms=assessed)
                self.assert_withdrawn(session, session.submit(candidate), "evidence_update_required")

    def test_revocation_is_retained_even_while_a_frame_barrier_is_pending(self):
        case = example_request()
        session = fixture_session()
        session.submit(case)
        session.invalidate("frame_changed")
        revoked = rebind(case, snapshot_revision=2)
        revoked["claims"]["gold"]["basis"][0]["status"] = "revoked"
        session.submit(revoked)
        new = rebind(next_frame(case), snapshot_revision=3)
        self.assert_withdrawn(session, session.submit(new), "evidence_version_retired")

    def test_stacked_frame_and_context_events_require_both_refreshes(self):
        case = example_request()
        session = fixture_session()
        session.submit(case)
        session.invalidate("occluded")
        session.invalidate("context_changed")
        self.assert_withdrawn(session, session.submit(next_frame(case)), "context_refresh_required")
        new = rebind(next_frame(case, context_changed=True), snapshot_revision=3)
        self.assertEqual(session.submit(new)["status"], "hypothesis_only")

    def test_evidence_event_uses_event_time_instead_of_last_submit_time(self):
        case = example_request()
        clock = FixtureClock()
        session = fixture_session(clock)
        session.submit(case)
        clock.advance(100)
        session.invalidate("evidence_changed")
        clock.advance(30)
        candidate = rebind(case, snapshot_revision=2, assembled_review_ms=1230)
        for name, claim in candidate["claims"].items():
            for basis in claim["basis"]:
                basis.update(source_sha256=digest({"replacement_before_event": name}),
                             review_ref="synthetic:checked-before-event-" + name, assessed_review_ms=1150)
        self.assert_withdrawn(session, session.submit(candidate), "evidence_update_required")

    def test_event_during_clock_read_uses_the_completed_tick_as_its_boundary(self):
        case = example_request()
        clock = FixtureClock()
        session = fixture_session(clock)
        session.submit(case)
        calls = 0

        def reading():
            nonlocal calls
            calls += 1
            if calls == 2:
                session.invalidate("evidence_changed")
            return ClockTick("fixture-review-clock", 1250 if calls >= 2 else 1100)

        clock.now = reading
        self.assert_withdrawn(session, session.submit(rebind(case, snapshot_revision=2)), "evaluation_superseded")
        clock.now = lambda: ClockTick("fixture-review-clock", 1300)
        candidate = rebind(case, snapshot_revision=3, assembled_review_ms=1300)
        for name, claim in candidate["claims"].items():
            for basis in claim["basis"]:
                basis.update(source_sha256=digest({"source_before_completed_tick": name}), assessed_review_ms=1200,
                             review_ref="synthetic:new-ref-before-completed-tick-" + name)
        self.assert_withdrawn(session, session.submit(candidate), "evidence_update_required")


if __name__ == "__main__":
    unittest.main()
