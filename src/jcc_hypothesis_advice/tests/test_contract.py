from copy import deepcopy
import unittest

from jcc_hypothesis_advice.contract import MAX_INTEGER, SCOPE_KEYS
from jcc_hypothesis_advice.fixtures import example_request, fixture_session, set_claim


def codes(result):
    return {reason["code"] for reason in result["reasons"]}


def change_scope(case, **changes):
    case["scope"].update(changes)
    set_claim(case, "scope_binding", case["scope"])
    for claim in case["claims"].values():
        for basis in claim["basis"]:
            basis["scope"] = deepcopy(case["scope"])


class ContractTests(unittest.TestCase):
    def check_blocked(self, case, expected, status=None):
        result = fixture_session().submit(case)
        if status is not None:
            self.assertEqual(result["status"], status, result)
        self.assertIn(expected, codes(result), result)
        self.assertIsNone(result["analysis"])
        self.assertIsNone(result["advice"])
        self.assertIsNone(result["action"])
        self.assertFalse(result["production_eligible"])
        self.assertFalse(result["execution_eligible"])
        return result

    def test_positive_fixture_has_reason_unknowns_counterexamples_and_no_action(self):
        result = fixture_session().submit(example_request())
        self.assertEqual(result["status"], "hypothesis_only")
        self.assertTrue(result["analysis"]["conditions_hold"])
        self.assertEqual(result["analysis"]["arithmetic"]["after_gold"], 6)
        self.assertIn("假设", result["analysis"]["statement_zh"])
        self.assertIn("不能推出应购买", result["analysis"]["statement_zh"])
        self.assertTrue(result["analysis"]["rationale"])
        self.assertGreaterEqual(len(result["unknowns"]), 4)
        self.assertGreaterEqual(len(result["counterexamples"]), 3)
        self.assertFalse(result["real_rules_verified"])
        self.assertFalse(result["historical_game_freshness_established"])
        self.assertIsNone(result["advice"])
        self.assertIsNone(result["action"])

    def test_numeric_counterexample_actually_breaks_reserve_condition(self):
        case = example_request()
        set_claim(case, "gold", 8)
        result = fixture_session().submit(case)
        self.assertEqual(result["status"], "conditions_not_met")
        self.assertEqual(result["analysis"]["arithmetic"]["after_gold"], 5)
        self.assertFalse(result["analysis"]["arithmetic"]["reserve_met"])
        self.assertFalse(result["analysis"]["conditions_hold"])

    def test_spend_above_balance_does_not_emit_negative_remainder(self):
        case = example_request()
        set_claim(case, "hypothetical_spend", 10)
        result = fixture_session().submit(case)
        self.assertEqual(result["status"], "conditions_not_met")
        arithmetic = result["analysis"]["arithmetic"]
        self.assertFalse(arithmetic["within_candidate_balance"])
        self.assertIsNone(arithmetic["after_gold"])
        self.assertIsNone(arithmetic["reserve_met"])

    def test_zero_and_safe_integer_boundaries_are_arithmetic_only(self):
        for value in (0, MAX_INTEGER):
            with self.subTest(value=value):
                case = example_request()
                for name in ("gold", "hypothetical_spend", "reserve"):
                    set_claim(case, name, 0 if name != "gold" else value)
                result = fixture_session().submit(case)
                self.assertEqual(result["status"], "hypothesis_only")
                self.assertEqual(result["analysis"]["arithmetic"]["after_gold"], value)
                self.assertIsNone(result["analysis"]["arithmetic"]["interest"])
                self.assertIsNone(result["action"])

    def test_replay_shaped_example_retains_constructed_and_unverified_labels(self):
        result = fixture_session().submit(example_request(replay_candidate=True))
        self.assertEqual(result["status"], "hypothesis_only")
        self.assertEqual(result["sample_kind"], "replay_candidate")
        self.assertEqual(result["material_origin"], "constructed_replay_template")
        self.assertTrue(all(item["trust"] == "unverified_caller_report" for item in result["analysis"]["assumptions"]))
        self.assertFalse(result["real_rules_verified"])

    def test_private_replay_candidate_is_only_a_hypothesis_without_reading_assets(self):
        case = example_request(replay_candidate=True)
        case["source"]["kind"] = "private_replay_candidate"
        # This is still a constructed metadata test; no real recording exists.
        result = fixture_session().submit(case)
        self.assertEqual(result["status"], "hypothesis_only")
        self.assertEqual(result["material_origin"], "private_replay_candidate")
        self.assertFalse(result["production_eligible"])
        self.assertFalse(result["historical_game_freshness_established"])

    def test_live_and_production_snapshots_are_not_relabelled(self):
        for kind in ("live", "live_candidate"):
            case = example_request()
            case["sample_kind"] = kind
            self.check_blocked(case, "live_input_forbidden", "refused")
        self.check_blocked({"schema_version": "game-state-v1"}, "production_input_forbidden", "refused")
        case = example_request()
        case["sample_kind"] = "replay"
        self.check_blocked(case, "sample_kind_forbidden", "refused")

    def test_recognized_and_verified_labels_cannot_promote_candidates(self):
        for status in ("recognized", "verified"):
            case = example_request()
            case["claims"]["gold"]["status"] = status
            self.check_blocked(case, "claim_status_forbidden", "refused")
        for container in ("claim", "basis", "root"):
            case = example_request()
            target = case if container == "root" else case["claims"]["gold"]
            if container == "basis":
                target = target["basis"][0]
            target["verified"] = True
            self.check_blocked(case, "input_malformed", "refused")

    def test_high_machine_confidence_and_equal_sources_do_not_establish_verification(self):
        case = example_request(replay_candidate=True)
        claim = case["claims"]["gold"]
        claim["machine_confidence"] = 1.0
        extra = deepcopy(claim["basis"][0])
        extra["evidence_id"] = "another-correlated-candidate"
        claim["basis"].append(extra)
        result = fixture_session().submit(case)
        self.assertEqual(result["status"], "hypothesis_only")
        self.assertFalse(result["real_rules_verified"])
        self.assertIn("recognition_not_validated", {item["code"] for item in result["unknowns"]})
        claim["basis"][1]["asserted_value"] = 999
        self.check_blocked(case, "evidence_value_conflict", "insufficient_evidence")

    def test_recorded_review_is_a_report_and_still_not_authentication(self):
        case = example_request(replay_candidate=True)
        basis = case["claims"]["gold"]["basis"][0]
        basis["provenance"] = "review_recorded"
        self.check_blocked(case, "review_reference_missing", "insufficient_evidence")
        basis["review_ref"] = "private:constructed-review-record"
        result = fixture_session().submit(case)
        self.assertEqual(result["status"], "hypothesis_only")
        self.assertFalse(result["real_rules_verified"])
        self.assertIn("source_not_authenticated", {item["code"] for item in result["unknowns"]})

    def test_each_scope_dimension_is_required_and_not_a_wildcard(self):
        for name in SCOPE_KEYS:
            with self.subTest(name=name):
                case = example_request()
                change_scope(case, **{name: None})
                self.check_blocked(case, "scope_unknown", "insufficient_evidence")

    def test_each_evidence_scope_dimension_must_match_exactly(self):
        for name in SCOPE_KEYS:
            with self.subTest(name=name):
                case = example_request()
                case["claims"]["gold"]["basis"][0]["scope"][name] = "different-fictional-value"
                self.check_blocked(case, "evidence_scope_mismatch", "refused")

    def test_scope_assertion_cannot_bind_a_different_mode(self):
        case = example_request()
        scope = deepcopy(case["scope"])
        scope["mode"] = "another-fictional-mode"
        set_claim(case, "scope_binding", scope)
        self.check_blocked(case, "scope_binding_mismatch", "refused")

    def test_every_claim_is_required_and_missing_is_not_zero_or_empty(self):
        for name in example_request()["claims"]:
            with self.subTest(name=name):
                case = example_request()
                del case["claims"][name]
                self.check_blocked(case, "field_missing", "insufficient_evidence")

    def test_unknown_conflict_stale_and_not_applicable_do_not_carry_values(self):
        for status in ("unknown", "conflict", "stale", "not_applicable"):
            with self.subTest(status=status):
                case = example_request()
                claim = case["claims"]["gold"]
                claim.update(status=status, value=None, expires_review_ms=None)
                self.check_blocked(case, "field_" + status, "insufficient_evidence")
                claim["value"] = 9
                self.check_blocked(case, "input_malformed", "refused")

    def test_occlusion_unknown_visibility_and_wrong_subject_block(self):
        for visibility in ("occluded", "unknown", "not_visual"):
            case = example_request()
            case["claims"]["gold"]["visibility"] = visibility
            self.check_blocked(case, "field_visibility_unestablished", "insufficient_evidence")
        case = example_request()
        case["claims"]["gold"]["role"] = "observed_gold"
        self.check_blocked(case, "field_role_mismatch", "insufficient_evidence")

    def test_cost_and_reserve_must_be_explicit_hypotheses(self):
        for name in ("hypothetical_spend", "reserve", "scope_binding"):
            case = example_request()
            case["claims"][name]["status"] = "candidate"
            self.check_blocked(case, "hypothesis_kind_mismatch", "refused")
        case = example_request()
        case["claims"]["hypothetical_spend"]["role"] = "catalog_base_price"
        self.check_blocked(case, "field_role_mismatch", "insufficient_evidence")
        case = example_request(replay_candidate=True)
        case["claims"]["hypothetical_spend"]["basis"][0]["provenance"] = "machine_candidate"
        self.check_blocked(case, "hypothesis_basis_required", "refused")

    def test_missing_revoked_conflicting_or_stale_sources_block(self):
        case = example_request()
        case["claims"]["gold"]["basis"] = []
        self.check_blocked(case, "evidence_missing", "insufficient_evidence")
        for status in ("unknown", "conflict", "revoked", "stale"):
            case = example_request()
            case["claims"]["gold"]["basis"][0]["status"] = status
            self.check_blocked(case, "evidence_" + status, "insufficient_evidence")

    def test_evidence_conflict_is_not_resolved_by_order_or_majority(self):
        for reverse in (False, True):
            case = example_request()
            claim = case["claims"]["gold"]
            for index in range(3):
                extra = deepcopy(claim["basis"][0])
                extra["evidence_id"] = "other-assertion-" + str(index)
                if index == 0:
                    extra["asserted_value"] = 1
                claim["basis"].append(extra)
            if reverse:
                claim["basis"].reverse()
            self.check_blocked(case, "evidence_value_conflict", "insufficient_evidence")

    def test_synthetic_and_replay_source_provenance_cannot_be_laundered(self):
        case = example_request()
        case["source"]["kind"] = "private_replay_candidate"
        self.check_blocked(case, "source_kind_mismatch", "refused")
        for replay in (False, True):
            case = example_request(replay_candidate=replay)
            basis = case["claims"]["gold"]["basis"][0]
            basis["provenance"] = "synthetic_fixture" if replay else "machine_candidate"
            self.check_blocked(case, "evidence_material_mismatch", "refused")

    def test_duplicate_evidence_ids_do_not_merge_distinct_assertions(self):
        case = example_request()
        case["claims"]["gold"]["basis"].append(deepcopy(case["claims"]["gold"]["basis"][0]))
        self.check_blocked(case, "evidence_id_duplicate", "refused")

    def test_amounts_do_not_coerce_strings_bools_floats_or_nonfinite_values(self):
        for name in ("gold", "hypothetical_spend", "reserve"):
            for value in (True, -1, 1.0, "9", "9l", float("nan"), float("inf"), MAX_INTEGER + 1):
                with self.subTest(name=name, value=value):
                    case = example_request()
                    case["claims"][name]["value"] = value
                    case["claims"][name]["basis"][0]["asserted_value"] = value
                    self.check_blocked(case, "input_malformed", "refused")

    def test_unit_and_evidence_unit_are_not_implicitly_converted(self):
        case = example_request()
        case["claims"]["gold"]["unit"] = "percentage"
        self.check_blocked(case, "field_unit_mismatch", "insufficient_evidence")
        case = example_request()
        case["claims"]["gold"]["basis"][0]["unit"] = "catalog_cost"
        self.check_blocked(case, "evidence_unit_mismatch", "insufficient_evidence")

    def test_missing_digest_pointer_or_opaque_source_is_not_an_evidence_record(self):
        for key, value in (("source_sha256", ""), ("source_pointer", "bad~pointer"),
                           ("source_ref", "https://example.invalid/not-read")):
            case = example_request()
            case["claims"]["gold"]["basis"][0][key] = value
            self.check_blocked(case, "input_malformed", "refused")

    def test_historical_wall_time_is_preserved_but_never_used_as_freshness(self):
        case = example_request(replay_candidate=True)
        case["source"]["historical_capture_time"] = "2001-01-01T00:00:00+08:00"
        case["claims"]["gold"]["basis"][0]["source_time"] = "2000-01-01T00:00:00Z"
        result = fixture_session().submit(case)
        self.assertEqual(result["status"], "hypothesis_only")
        self.assertFalse(result["historical_game_freshness_established"])
        self.assertEqual(result["source"]["historical_capture_time"], "2001-01-01T00:00:00+08:00")
        self.assertEqual(result["source"]["capture_ref"], case["source"]["capture_ref"])
        case["source"]["historical_capture_time"] = "2001-01-01T00:00:00"
        self.check_blocked(case, "input_malformed", "refused")


if __name__ == "__main__":
    unittest.main()
