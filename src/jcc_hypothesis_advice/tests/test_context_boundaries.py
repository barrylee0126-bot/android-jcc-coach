"""Constructed context history only; no device, source assets or game rules."""

from copy import deepcopy
import unittest

from jcc_hypothesis_advice.contract import digest
from jcc_hypothesis_advice.fixtures import (
    FixtureClock, example_request, fixture_session, next_frame, rebind, set_claim,
)


def codes(result):
    return {reason["code"] for reason in result["reasons"]}


class ContextBoundaryTests(unittest.TestCase):
    def assert_withdrawn(self, session, result, code):
        self.assertIn(code, codes(result), result["reasons"])
        for view in (result, session.view()):
            self.assertIsNone(view["analysis"])
            self.assertIsNone(view["advice"])
            self.assertIsNone(view["action"])
            self.assertEqual(view["evidence_updates"], [])
            self.assertFalse(view["production_eligible"])
            self.assertFalse(view["execution_eligible"])
            self.assertFalse(view["real_rules_verified"])
            self.assertFalse(view["historical_game_freshness_established"])

    def test_bound_unknown_survives_unrelated_hard_refusal(self):
        case = example_request()
        session = fixture_session(FixtureClock(1300))
        self.assertEqual(session.submit(case)["status"], "hypothesis_only")
        unknown = next_frame(case)
        unknown["claims"]["phase"].update(status="unknown", value=None, expires_review_ms=None)
        unknown["claims"]["gold"]["binding"]["frame_id"] = "wrong-constructed-frame"
        rejected = session.submit(unknown)
        self.assertEqual(rejected["status"], "refused")
        self.assert_withdrawn(session, rejected, "field_binding_mismatch")
        self.assertIn("field_unknown", codes(rejected))
        restored = next_frame(next_frame(case))
        self.assert_withdrawn(session, session.submit(restored), "context_change_unannounced")
        restored = rebind(restored, snapshot_revision=4, context_epoch=1)
        self.assertEqual(session.submit(restored)["status"], "hypothesis_only")

    def test_misbound_unknown_cannot_create_loss_boundary(self):
        changes = {
            "stream_id": "foreign-stream", "review_clock_id": "foreign-clock",
            "frame_id": "other-frame", "frame_sha256": "b" * 64, "sequence": 20,
            "snapshot_revision": 20, "context_epoch": 20,
            "received_review_ms": 999, "assembled_review_ms": 1029,
        }
        for name in ("scene", "phase", "perspective", "scope_binding"):
            for key, value in changes.items():
                with self.subTest(name=name, binding=key):
                    case = example_request()
                    session = fixture_session(FixtureClock(1300))
                    session.submit(case)
                    unknown = next_frame(case)
                    unknown["claims"][name].update(status="unknown", value=None, expires_review_ms=None)
                    unknown["claims"][name]["binding"][key] = value
                    self.assert_withdrawn(session, session.submit(unknown), "field_binding_mismatch")
                    restored = next_frame(next_frame(case))
                    self.assertEqual(session.submit(restored)["status"], "hypothesis_only")

    def test_illegal_candidate_cannot_become_a_known_context_anchor(self):
        for fault in ("role", "unit", "missing_basis", "field_expired", "basis_expired"):
            with self.subTest(fault=fault):
                case = example_request()
                session = fixture_session(FixtureClock(1300))
                session.submit(case)
                bad = next_frame(case, context_changed=True)
                set_claim(bad, "phase", "combat")
                claim = bad["claims"]["phase"]
                if fault == "role":
                    claim["role"] = "other_context"
                elif fault == "unit":
                    claim["unit"] = "gold"
                elif fault == "missing_basis":
                    claim["basis"] = []
                elif fault == "field_expired":
                    claim["expires_review_ms"] = 1300
                else:
                    claim["basis"][0]["expires_review_ms"] = 1300
                self.assertEqual(session.submit(bad)["status"], "insufficient_evidence")
                restored = next_frame(next_frame(case, context_changed=True))
                self.assertEqual(session.submit(restored)["status"], "hypothesis_only")

    def test_expired_frame_cannot_create_loss_boundary(self):
        case = example_request()
        clock = FixtureClock()
        session = fixture_session(clock, max_frame_age_ms=200)
        self.assertEqual(session.submit(case)["status"], "hypothesis_only")
        clock.advance(150)
        unknown = next_frame(case)
        unknown["claims"]["phase"].update(status="unknown", value=None, expires_review_ms=None)
        self.assert_withdrawn(session, session.submit(unknown), "frame_expired")
        self.assertEqual(session.submit(next_frame(next_frame(case)))["status"], "hypothesis_only")

    def test_invalid_frame_envelope_cannot_create_loss_boundary(self):
        for fault in ("future", "reversed", "source_material"):
            with self.subTest(fault=fault):
                case = example_request()
                session = fixture_session(FixtureClock(1300))
                session.submit(case)
                unknown = next_frame(case)
                unknown["claims"]["phase"].update(status="unknown", value=None, expires_review_ms=None)
                if fault == "future":
                    unknown = rebind(unknown, assembled_review_ms=1400)
                elif fault == "reversed":
                    unknown = rebind(unknown, assembled_review_ms=1049)
                else:
                    unknown["source"]["capture_ref"] = "private:wrong-material-kind"
                code = "source_kind_mismatch" if fault == "source_material" else "frame_time_invalid"
                self.assert_withdrawn(session, session.submit(unknown), code)
                self.assertEqual(session.submit(next_frame(next_frame(case)))["status"], "hypothesis_only")

    def test_unknown_with_invalid_local_metadata_cannot_create_loss_boundary(self):
        for fault in ("role", "unit", "observed", "completed", "basis_binding", "basis_scope",
                      "basis_material", "basis_time", "basis_expired"):
            with self.subTest(fault=fault):
                case = example_request()
                session = fixture_session(FixtureClock(1300))
                session.submit(case)
                unknown = next_frame(case)
                claim = unknown["claims"]["phase"]
                claim.update(status="unknown", value=None, expires_review_ms=None)
                basis = claim["basis"][0]
                if fault == "role":
                    claim["role"] = "other_context"
                elif fault == "unit":
                    claim["unit"] = "gold"
                elif fault == "observed":
                    claim["observed_review_ms"] = 999
                elif fault == "completed":
                    claim["completed_review_ms"] = 1049
                elif fault == "basis_binding":
                    basis["binding"]["frame_id"] = "wrong-constructed-frame"
                elif fault == "basis_scope":
                    basis["scope"]["mode"] = "other-constructed-mode"
                elif fault == "basis_material":
                    basis["source_ref"] = "private:wrong-material-kind"
                elif fault == "basis_time":
                    basis["assessed_review_ms"] = 1049
                else:
                    basis["expires_review_ms"] = 1300
                self.assert_withdrawn(session, session.submit(unknown), "field_unknown")
                self.assertEqual(session.submit(next_frame(next_frame(case)))["status"], "hypothesis_only")

    def test_unannounced_context_cannot_retire_other_evidence(self):
        case = example_request()
        session = fixture_session(FixtureClock(1300))
        session.submit(case)
        bad = next_frame(case)
        set_claim(bad, "phase", "combat")
        bad["claims"]["gold"]["basis"][0]["status"] = "revoked"
        self.assert_withdrawn(session, session.submit(bad), "context_change_unannounced")
        restored = rebind(next_frame(case, context_changed=True), snapshot_revision=3)
        self.assertEqual(session.submit(restored)["status"], "hypothesis_only")

    def test_all_context_losses_survive_unrelated_refusal(self):
        for name in ("scene", "phase", "perspective", "scope_binding"):
            for loss in ("unknown", "conflict", "stale", "not_applicable", "missing", "visibility"):
                with self.subTest(name=name, loss=loss):
                    case = example_request()
                    session = fixture_session(FixtureClock(1300))
                    session.submit(case)
                    lost = next_frame(case)
                    if loss == "missing":
                        del lost["claims"][name]
                    elif loss == "visibility":
                        lost["claims"][name]["visibility"] = "unknown"
                    else:
                        lost["claims"][name].update(status=loss, value=None, expires_review_ms=None)
                    lost["claims"]["gold"]["binding"]["frame_id"] = "wrong-constructed-frame"
                    self.assert_withdrawn(session, session.submit(lost), "field_binding_mismatch")
                    restored = next_frame(next_frame(case))
                    self.assert_withdrawn(session, session.submit(restored), "context_change_unannounced")
                    restored = rebind(restored, snapshot_revision=4, context_epoch=1)
                    self.assertEqual(session.submit(restored)["status"], "hypothesis_only")

    def test_scope_unknown_visibility_preserves_loss_instead_of_an_unqualified_value(self):
        for other_failure in (False, True):
            with self.subTest(other_failure=other_failure):
                case = example_request()
                session = fixture_session(FixtureClock(1300))
                session.submit(case)
                lost = next_frame(case)
                lost["scope"]["mode"] = "unqualified-constructed-mode"
                set_claim(lost, "scope_binding", lost["scope"])
                for name, claim in lost["claims"].items():
                    for basis in claim["basis"]:
                        basis["scope"] = deepcopy(lost["scope"])
                        basis["source_sha256"] = digest({"constructed": name, "scope": lost["scope"],
                                                         "value": basis["asserted_value"]})
                lost["claims"]["scope_binding"]["visibility"] = "unknown"
                if other_failure:
                    lost["claims"]["gold"]["binding"]["frame_id"] = "wrong-constructed-frame"
                self.assert_withdrawn(session, session.submit(lost), "field_visibility_unestablished")
                restored = next_frame(next_frame(case))
                self.assert_withdrawn(session, session.submit(restored), "context_change_unannounced")
                restored = rebind(restored, snapshot_revision=4, context_epoch=1)
                self.assertEqual(session.submit(restored)["status"], "hypothesis_only")

    def test_epoch_advanced_during_unknown_allows_a_new_frame_in_that_epoch(self):
        for other_failure in (False, True):
            with self.subTest(other_failure=other_failure):
                case = example_request()
                session = fixture_session(FixtureClock(1300))
                session.submit(case)
                lost = next_frame(case, context_changed=True)
                lost["claims"]["phase"].update(status="unknown", value=None, expires_review_ms=None)
                if other_failure:
                    lost["claims"]["gold"]["binding"]["frame_id"] = "wrong-constructed-frame"
                self.assert_withdrawn(session, session.submit(lost), "field_unknown")
                restored = next_frame(next_frame(case, context_changed=True))
                set_claim(restored, "phase", "combat")
                result = session.submit(restored)
                self.assertEqual(result["status"], "conditions_not_met")
                self.assertIsNone(result["analysis"]["arithmetic"]["after_gold"])
                self.assertIsNone(result["action"])
                self.assertFalse(result["real_rules_verified"])

    def test_invalid_other_context_does_not_hide_a_bound_loss(self):
        for fault in ("role", "unit", "time", "basis"):
            with self.subTest(fault=fault):
                case = example_request()
                session = fixture_session(FixtureClock(1300))
                session.submit(case)
                lost = next_frame(case)
                lost["claims"]["phase"].update(status="unknown", value=None, expires_review_ms=None)
                set_claim(lost, "scene", "invalid-candidate-context")
                scene = lost["claims"]["scene"]
                if fault == "role":
                    scene["role"] = "other_context"
                elif fault == "unit":
                    scene["unit"] = "gold"
                elif fault == "time":
                    scene["completed_review_ms"] = 1049
                else:
                    scene["basis"][0]["binding"]["frame_id"] = "wrong-constructed-frame"
                self.assert_withdrawn(session, session.submit(lost), "field_unknown")
                self.assert_withdrawn(session, session.submit(next_frame(next_frame(case))),
                                      "context_change_unannounced")
                restored = rebind(next_frame(next_frame(case)), snapshot_revision=4, context_epoch=1)
                self.assertEqual(session.submit(restored)["status"], "hypothesis_only")

    def test_source_history_refusal_does_not_hide_a_bound_loss(self):
        case = example_request()
        session = fixture_session(FixtureClock(1300))
        session.submit(case)
        lost = next_frame(case)
        lost["claims"]["phase"].update(status="unknown", value=None, expires_review_ms=None)
        lost["claims"]["gold"]["value"] = 12
        lost["claims"]["gold"]["basis"][0]["asserted_value"] = 12
        self.assert_withdrawn(session, session.submit(lost), "evidence_content_changed")
        self.assert_withdrawn(session, session.submit(next_frame(next_frame(case))), "context_change_unannounced")
        restored = rebind(next_frame(next_frame(case)), snapshot_revision=4, context_epoch=1)
        self.assertEqual(session.submit(restored)["status"], "hypothesis_only")

    def test_pending_capture_barrier_does_not_hide_a_bound_loss(self):
        case = example_request()
        session = fixture_session(FixtureClock(1300))
        session.submit(case)
        session.invalidate("frame_changed")
        lost = rebind(case, snapshot_revision=2)
        lost["claims"]["phase"].update(status="unknown", value=None, expires_review_ms=None)
        self.assert_withdrawn(session, session.submit(lost), "frame_refresh_required")
        restored = rebind(next_frame(case), snapshot_revision=3)
        self.assert_withdrawn(session, session.submit(restored), "context_change_unannounced")
        restored = rebind(restored, snapshot_revision=4, context_epoch=1)
        self.assertEqual(session.submit(restored)["status"], "hypothesis_only")

    def test_unrelated_refusal_cannot_promote_a_different_candidate_to_known_context(self):
        case = example_request()
        session = fixture_session(FixtureClock(1300))
        session.submit(case)
        bad = next_frame(case, context_changed=True)
        set_claim(bad, "phase", "combat")
        bad["claims"]["gold"]["binding"]["frame_id"] = "wrong-constructed-frame"
        self.assert_withdrawn(session, session.submit(bad), "field_binding_mismatch")
        restored = next_frame(next_frame(case, context_changed=True))
        self.assertEqual(session.submit(restored)["status"], "hypothesis_only")

    def test_out_of_order_or_foreign_attempt_cannot_create_loss_boundary(self):
        faults = ("revision", "sequence", "epoch", "received", "frame_reuse", "same_frame_identity",
                  "stream", "clock", "recording", "material")
        for fault in faults:
            with self.subTest(fault=fault):
                case = example_request()
                session = fixture_session(FixtureClock(1300))
                session.submit(case)
                second = next_frame(case, context_changed=True)
                session.submit(second)
                unknown = next_frame(second)
                unknown["claims"]["phase"].update(status="unknown", value=None, expires_review_ms=None)
                changes = {"revision": {"snapshot_revision": 2}, "sequence": {"sequence": 0},
                           "epoch": {"context_epoch": 0}, "received": {"received_review_ms": 1049},
                           "frame_reuse": {"frame_id": case["binding"]["frame_id"]},
                           "same_frame_identity": {"frame_id": second["binding"]["frame_id"]},
                           "stream": {"stream_id": "foreign-stream"},
                           "clock": {"review_clock_id": "foreign-clock"}}
                if fault in changes:
                    unknown = rebind(unknown, **changes[fault])
                elif fault == "recording":
                    unknown["source"]["recording_ref"] = "synthetic:different-recording"
                else:
                    unknown["sample_kind"] = "replay_candidate"
                result = session.submit(unknown)
                self.assertEqual(result["status"], "refused")
                self.assertIsNone(result["analysis"])
                restored = rebind(next_frame(next_frame(second)), snapshot_revision=4)
                self.assertEqual(session.submit(restored)["status"], "hypothesis_only")

    def test_malformed_attempt_cannot_create_loss_boundary(self):
        case = example_request()
        session = fixture_session(FixtureClock(1300))
        session.submit(case)
        unknown = next_frame(case)
        unknown["claims"]["phase"].update(status="unknown", value=None, expires_review_ms=None)
        unknown["claims"]["gold"]["value"] = "invalid-amount"
        self.assert_withdrawn(session, session.submit(unknown), "input_malformed")
        self.assertEqual(session.submit(next_frame(next_frame(case)))["status"], "hypothesis_only")

    def test_unusable_frame_cannot_advance_the_context_ordering_anchor(self):
        for fault in ("expired", "future", "reversed", "source_material"):
            with self.subTest(fault=fault):
                case = example_request()
                clock = FixtureClock()
                session = fixture_session(clock, max_frame_age_ms=200 if fault == "expired" else 1000)
                session.submit(case)
                clock.advance(150)
                bad = rebind(next_frame(case), context_epoch=99)
                bad["claims"]["phase"].update(status="unknown", value=None, expires_review_ms=None)
                if fault == "future":
                    bad = rebind(bad, assembled_review_ms=1400)
                elif fault == "reversed":
                    bad = rebind(bad, assembled_review_ms=1049)
                elif fault == "source_material":
                    bad["source"]["capture_ref"] = "private:wrong-material-kind"
                session.submit(bad)
                # A refused revision is still consumed, without trusting epoch 99.
                old_revision = rebind(next_frame(next_frame(case)), snapshot_revision=2)
                self.assert_withdrawn(session, session.submit(old_revision), "snapshot_not_new")
                restored = rebind(next_frame(next_frame(case)), snapshot_revision=3)
                self.assertEqual(session.submit(restored)["status"], "hypothesis_only")

    def test_first_unusable_frame_consumes_revision_without_anchoring_its_epoch(self):
        case = example_request()
        session = fixture_session()
        bad = rebind(case, context_epoch=99, assembled_review_ms=1400)
        self.assert_withdrawn(session, session.submit(bad), "frame_time_invalid")
        self.assert_withdrawn(session, session.submit(case), "snapshot_not_new")
        self.assertEqual(session.submit(rebind(case, snapshot_revision=2))["status"], "hypothesis_only")

    def test_initial_attempt_watermark_is_isolated_by_stream_and_clock(self):
        for fault in ("stream", "clock"):
            with self.subTest(fault=fault):
                case = example_request()
                session = fixture_session()
                changes = {"stream_id": "foreign-stream"} if fault == "stream" else {"review_clock_id": "foreign-clock"}
                bad = rebind(case, **changes, context_epoch=99, assembled_review_ms=1400)
                result = session.submit(bad)
                self.assertEqual(result["status"], "refused")
                self.assertEqual(session.submit(case)["status"], "hypothesis_only")


if __name__ == "__main__":
    unittest.main()
