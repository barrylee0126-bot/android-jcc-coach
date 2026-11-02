"""Meaningful refusal/lifecycle cases, all using invented states and records."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from jcc_strategy import (
    Condition, EvidenceReport, FieldValue, Literal, Rule, Scope, StrategyEngine,
)
from jcc_strategy.fixtures import (
    RUNTIME_REF, FixtureClock, fixture_case, fixture_evidence, fixture_rule, fixture_state, next_fixture_frame,
)


def codes(result):
    return {reason.code for reason in result.reasons}


def unknown(field, *, applicability="applicable"):
    field.update(status="unknown", value=None, expires_host_monotonic_ms=None,
                 applicability=applicability,
                 reason="not_applicable" if applicability == "not_applicable" else "not_attempted")
    field.pop("candidates", None)
    field.pop("last_known_value", None)


class StrategyCase(unittest.TestCase):
    def setUp(self):
        self.case = fixture_case()
        self.engine = self.case.engine()

    def observe(self, state=None, engine=None):
        result = (engine or self.engine).observe(state if state is not None else self.case.state)
        self.assertTrue(result.valid, result.to_dict())

    def issue(self):
        self.observe()
        decision = self.engine.evaluate(self.case.rule.rule_id)
        self.assertEqual(decision.status, "conditional_fixture", decision.to_dict())
        self.assertIsNotNone(decision.advice)
        return decision.advice

    def refused(self, expected, *, state=None, engine=None):
        engine = engine or self.engine
        self.observe(state, engine)
        decision = engine.evaluate(self.case.rule.rule_id)
        self.assertEqual(decision.status, "refused", decision.to_dict())
        self.assertIsNone(decision.advice)
        self.assertIn(expected, codes(decision), decision.to_dict())
        return decision

    def change_evidence(self, reference="fixture:price", purpose="price", **changes):
        report = self.case.evidence.get(reference, purpose)
        self.case.evidence.set_report(replace(report, **changes))

    def test_default_empty_rulebook_has_no_bundled_tactic(self):
        engine = StrategyEngine(clock=self.case.clock)
        self.assertEqual(engine.rules(), [])
        self.assertIn("rule_unknown", codes(engine.evaluate(self.case.rule.rule_id)))

    def test_default_evidence_port_rejects_even_recognized_version_reference(self):
        engine = StrategyEngine(clock=self.case.clock, rules=(self.case.rule,))
        self.refused("evidence_unverifiable", engine=engine)

    def test_fixture_success_is_explicitly_conditional_traceable_and_not_executable(self):
        advice = self.issue()
        output = advice.to_dict()
        self.assertFalse(output["execution_eligible"])
        self.assertFalse(output["real_rules_verified"])
        self.assertEqual(output["sample_kind"], "synthetic")
        self.assertEqual(output["intent"], "consider_buy")
        self.assertEqual(output["target"]["field"], "/shop/1/content")
        self.assertEqual(output["scope"], self.case.rule.scope.to_dict())
        self.assertEqual(output["state_binding"]["frame_id"], "fixture-frame-0")
        self.assertTrue(output["invalidates_when"])
        self.assertTrue(output["rationale"])
        self.assertTrue(output["conditions_satisfied"])
        self.assertEqual(len(output["basis"]), 5)
        self.assertTrue(all(report["citations"] for report in output["basis"]))
        self.assertIn("/gold", {item["pointer"] for item in output["field_dependencies"]})
        self.assertEqual(advice.evaluated_host_monotonic_ms, 1100)
        self.assertEqual(advice.expires_host_monotonic_ms, 1400)
        self.assertTrue(self.engine.revalidate(advice).valid)

    def test_real_and_replay_states_cannot_activate_fixtures(self):
        for kind in ("live", "replay"):
            with self.subTest(kind=kind):
                state = fixture_state()
                state["sample_kind"] = kind
                self.refused("real_integration_unavailable", state=state, engine=self.case.engine())

    def test_unreviewed_rule_cannot_gain_verification_from_fixture_state(self):
        rule = replace(self.case.rule, material_kind="unreviewed")
        engine = StrategyEngine(clock=self.case.clock, rules=(rule,), evidence=fixture_evidence(rule))
        self.refused("rule_unreviewed", engine=engine)

    def test_three_categories_share_container_and_dependency_contract(self):
        for category, intent in (("lineup", "consider_lineup"), ("trade", "consider_keep"),
                                 ("equipment", "consider_hold_item")):
            with self.subTest(category=category):
                rule = replace(self.case.rule, category=category, intent=intent)
                engine = StrategyEngine(clock=self.case.clock, rules=(rule,))
                self.assertEqual(engine.rules()[0]["category"], category)
                self.assertEqual(engine.rules()[0]["dependencies"], list(rule.dependencies))
        # No extra rule was installed into the ordinary engine by this test.
        self.assertEqual(len(self.engine.rules()), 1)

    def test_mode_and_each_version_dimension_require_exact_match(self):
        for dimension in ("region", "season", "patch", "mode"):
            with self.subTest(dimension=dimension):
                state = fixture_state()
                if dimension == "mode":
                    state["mode"]["value"] = "wrong-mode"
                else:
                    state["game_version"]["value"][dimension] = "wrong-version"
                self.refused("mode_mismatch" if dimension == "mode" else "version_mismatch",
                             state=state, engine=self.case.engine())

    def test_unknown_key_fields_never_use_old_or_guessed_values(self):
        for pointer in ("scene", "phase", "round", "mode", "game_version", "gold"):
            with self.subTest(field=pointer):
                state = fixture_state()
                unknown(state[pointer])
                result = self.refused("field_unknown", state=state, engine=self.case.engine())
                self.assertIn("/" + pointer, {r.pointer for r in result.reasons})

    def test_conflicting_key_field_refuses_to_choose_last_or_highest_score(self):
        state = deepcopy(self.case.state)
        gold = state["gold"]
        candidate = {key: deepcopy(gold[key]) for key in
                     ("value", "source_frame_id", "observed_host_monotonic_ms",
                      "completed_host_monotonic_ms", "expires_host_monotonic_ms", "method", "region")}
        gold.update(status="conflict", value=None, expires_host_monotonic_ms=None, reason="methods_disagree",
                    candidates=[candidate, {**deepcopy(candidate), "value": 10}])
        self.refused("field_conflict", state=state)

    def test_stale_key_field_cannot_use_last_known_value(self):
        state = deepcopy(self.case.state)
        state["gold"].update(status="stale", value=None, last_known_value=9,
                             expires_host_monotonic_ms=1025, reason="expired")
        self.refused("field_stale", state=state)

    def test_not_applicable_is_not_true_or_zero(self):
        state = deepcopy(self.case.state)
        unknown(state["gold"], applicability="not_applicable")
        self.refused("field_not_applicable", state=state)

    def test_missing_extension_is_not_an_empty_bench_or_default_capacity(self):
        state = deepcopy(self.case.state)
        del state["extensions"]["fixture:free-bench-slots"]
        self.refused("field_missing", state=state)

    def test_missing_purchase_affordance_is_not_inferred_from_preparation(self):
        state = deepcopy(self.case.state)
        del state["affordances"]
        self.refused("field_missing", state=state)

    def test_unknown_unneeded_hp_does_not_block_independent_fixture(self):
        state = deepcopy(self.case.state)
        unknown(state["hp"])
        self.observe(state)
        self.assertIsNotNone(self.engine.evaluate(self.case.rule.rule_id).advice)

    def test_negative_conditions_are_not_applicable_and_never_recommend(self):
        cases = (
            lambda s: s["gold"].update(value=6),
            lambda s: s["phase"].update(value="combat"),
            lambda s: s["affordances"]["shop_purchase"].update(value=False),
            lambda s: s["shop"][1]["content"]["value"].update(catalog_ref=None),
            lambda s: s["extensions"]["fixture:held-copies"].update(value=True),
        )
        for change in cases:
            with self.subTest(change=change):
                state = fixture_state()
                change(state)
                engine = self.case.engine()
                self.observe(state, engine)
                result = engine.evaluate(self.case.rule.rule_id)
                self.assertEqual(result.status, "not_applicable", result.to_dict())
                self.assertIsNone(result.advice)
                self.assertIn("condition_not_met", codes(result))

    def test_number_conditions_do_not_coerce_booleans_or_strings(self):
        for value in (True, "1"):
            with self.subTest(value=value):
                state = fixture_state()
                state["extensions"]["fixture:free-bench-slots"]["value"] = value
                self.refused("condition_type_mismatch", state=state, engine=self.case.engine())

    def test_current_clock_not_snapshot_assembly_controls_field_expiry(self):
        self.case.clock.ms = 2000
        self.refused("field_expired")  # Valid at assembly=1030, expired at evaluation=2000.

    def test_exact_field_deadline_is_expired(self):
        self.case.state["gold"]["expires_host_monotonic_ms"] = 1100
        self.refused("field_expired")

    def test_advice_deadline_is_minimum_of_rule_field_and_evidence_deadlines(self):
        self.case.state["gold"]["expires_host_monotonic_ms"] = 1300
        self.change_evidence(expires_host_monotonic_ms=1200)
        advice = self.issue()
        self.assertEqual(advice.expires_host_monotonic_ms, 1200)

    def test_revalidation_never_renews_advice_ttl(self):
        advice = self.issue()
        self.case.clock.ms = 1399.9
        self.assertTrue(self.engine.revalidate(advice).valid)
        self.assertEqual(advice.expires_host_monotonic_ms, 1400)
        self.case.clock.ms = 1400
        result = self.engine.revalidate(advice)
        self.assertFalse(result.valid)
        self.assertIn("advice_expired", codes(result))
        # New evaluation is distinct from reviving the old advice.
        self.assertIsNotNone(self.engine.evaluate(self.case.rule.rule_id).advice)

    def test_clock_domain_mismatch_is_rejected_before_comparing_times(self):
        self.case.state["host_clock_id"] = "another-host-clock"
        result = self.engine.observe(self.case.state)
        self.assertFalse(result.valid)
        self.assertIn("clock_domain_mismatch", codes(result))
        self.assertIsNone(self.engine.evaluate(self.case.rule.rule_id).advice)

    def test_snapshot_assembly_in_future_is_rejected(self):
        self.case.clock.ms = 1029
        result = self.engine.observe(self.case.state)
        self.assertFalse(result.valid)
        self.assertIn("state_from_future", codes(result))

    def test_clock_regression_poison_cannot_be_cleared_by_later_larger_number(self):
        advice = self.issue()
        self.case.clock.ms = 1099
        self.assertIn("clock_regressed", codes(self.engine.revalidate(advice)))
        self.case.clock.ms = 1200
        self.assertIn("clock_regressed", codes(self.engine.revalidate(advice)))

    def test_clock_domain_change_requires_new_session(self):
        advice = self.issue()
        self.case.clock.host_clock_id = "restarted-clock"
        self.assertIn("clock_domain_changed", codes(self.engine.revalidate(advice)))

    def test_nonfinite_clock_returns_refusal_without_leaking_exception(self):
        self.case.clock.ms = float("nan")
        self.assertIn("clock_unavailable", codes(self.engine.observe(self.case.state)))

    def test_bad_state_structure_or_source_rejects_and_revokes_old_advice(self):
        changes = (
            lambda s: s["shop"].pop(),
            lambda s: s["gold"].update(observed_host_monotonic_ms=999),
            lambda s: s["game_version"]["value"].update(verified=True),
            lambda s: s.update(snapshot_revision=0),
        )
        for change in changes:
            with self.subTest(change=change):
                case = fixture_case()
                engine = case.engine()
                self.observe(case.state, engine)
                old = engine.evaluate(case.rule.rule_id).advice
                state = deepcopy(case.state)
                change(state)
                self.assertIn("invalid_state", codes(engine.observe(state)))
                self.assertFalse(engine.revalidate(old).valid)
                self.assertIsNone(engine.evaluate(case.rule.rule_id).advice)

    def test_evidence_unknown_candidate_conflict_stale_missing_and_unverifiable_all_refuse(self):
        for status in ("unknown", "candidate", "conflict", "stale", "missing", "unverifiable"):
            with self.subTest(status=status):
                case = fixture_case()
                report = case.evidence.get("fixture:price", "price")
                case.evidence.set_report(replace(report, status=status, reason="fictional test"))
                self.refused("evidence_" + status, state=case.state, engine=case.engine())

    def test_unresolved_version_reference_is_not_proof(self):
        self.case.state["game_version"]["value"]["verification_ref"] = "unresolvable-reference"
        self.refused("evidence_missing")

    def test_wrong_scope_stream_or_clock_in_evidence_refuses(self):
        cases = (
            ({"scope": replace(self.case.rule.scope, patch="wrong")}, "evidence_scope_mismatch"),
            ({"scope": replace(self.case.rule.scope, mode="wrong")}, "evidence_scope_mismatch"),
            ({"stream_id": "earlier-stream"}, "evidence_binding_mismatch"),
            ({"host_clock_id": "unix-or-device-clock"}, "evidence_clock_mismatch"),
        )
        for changes, expected in cases:
            with self.subTest(changes=changes):
                case = fixture_case()
                report = case.evidence.get("fixture:price", "price")
                case.evidence.set_report(replace(report, **changes))
                self.refused(expected, state=case.state, engine=case.engine())

    def test_evidence_checks_expiry_at_now_and_rejects_future_or_invalid_intervals(self):
        cases = (
            ({"expires_host_monotonic_ms": 1100}, "evidence_expired"),
            ({"assessed_host_monotonic_ms": 1200}, "evidence_time_invalid"),
            ({"expires_host_monotonic_ms": None}, "evidence_time_invalid"),
            ({"expires_host_monotonic_ms": float("nan")}, "evidence_time_invalid"),
            ({"expires_host_monotonic_ms": True}, "evidence_time_invalid"),
        )
        for changes, expected in cases:
            with self.subTest(changes=changes):
                case = fixture_case()
                report = case.evidence.get("fixture:price", "price")
                case.evidence.set_report(replace(report, **changes))
                self.refused(expected, state=case.state, engine=case.engine())

    def test_evidence_requires_locator_digest_conditions_and_non_null_assertion(self):
        original = self.case.evidence.get("fixture:price", "price")
        cases = (
            {"citations": ()}, {"conditions": None}, {"value": Literal(None)},
            {"citations": (replace(original.citations[0], source_sha256="not-a-hash"),)},
            {"citations": (replace(original.citations[0], locator=""),)},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                case = fixture_case()
                report = case.evidence.get("fixture:price", "price")
                case.evidence.set_report(replace(report, **changes))
                self.refused("evidence_incomplete", state=case.state, engine=case.engine())

    def test_available_third_party_or_claimed_review_material_is_not_fixture_or_proof(self):
        self.change_evidence(material_kind="claimed-verified-by-user")
        self.refused("evidence_not_fixture")

    def test_runtime_evidence_must_assert_full_scope_not_just_verified_boolean(self):
        self.change_evidence(RUNTIME_REF, "runtime_binding", value=Literal({"verified": True}))
        self.refused("runtime_binding_mismatch")

    def test_rule_basis_binds_current_rule_revision_and_digest(self):
        self.change_evidence("fixture:rule-basis", "rule_basis",
                             value=Literal({"rule_id": self.case.rule.rule_id, "verified": True}))
        self.refused("rule_basis_mismatch")

    def test_plain_verified_true_payload_is_not_an_evidence_report(self):
        class BooleanPort:
            def revision(self):
                return "caller-claim"

            def resolve(self, query, *, at):
                return {"verified": True, "value": 2}

        engine = StrategyEngine(clock=self.case.clock, rules=(self.case.rule,), evidence=BooleanPort())
        self.refused("evidence_malformed", engine=engine)

    def test_port_failure_is_a_refusal_and_does_not_print_source_exception(self):
        secret = "fixture-exception-details-must-not-leak"

        class FailingPort:
            def revision(self):
                return "revision"

            def resolve(self, query, *, at):
                raise RuntimeError(secret)

        engine = StrategyEngine(clock=self.case.clock, rules=(self.case.rule,), evidence=FailingPort())
        result = self.refused("evidence_unavailable", engine=engine)
        self.assertNotIn(secret, json.dumps(result.to_dict()))

    def test_port_without_revision_barrier_is_unavailable(self):
        engine = StrategyEngine(clock=self.case.clock, rules=(self.case.rule,), evidence=object())
        self.refused("evidence_unavailable", engine=engine)

    def test_changed_evidence_revision_during_evaluation_cannot_issue(self):
        store = self.case.evidence

        class ChangingPort:
            def revision(self):
                return store.revision()

            def resolve(self, query, *, at):
                report = store.resolve(query, at=at)
                if query.purpose == "toy_threshold":
                    store.set_report(report)
                return report

        engine = StrategyEngine(clock=self.case.clock, rules=(self.case.rule,), evidence=ChangingPort())
        self.refused("evidence_changed_during_evaluation", engine=engine)

    def test_evidence_revocation_is_checked_again_when_reading_cached_advice(self):
        advice = self.issue()
        self.change_evidence(status="conflict")
        result = self.engine.revalidate(advice)
        self.assertFalse(result.valid)
        self.assertIn("evidence_conflict", codes(result))

    def test_evidence_content_change_revokes_even_if_new_conditions_still_match(self):
        advice = self.issue()
        self.change_evidence("fixture:minimum-gold", "toy_threshold", value=Literal(6))
        result = self.engine.revalidate(advice)
        self.assertFalse(result.valid)
        self.assertIn("dependency_changed", codes(result))

    def test_evidence_disappearance_revokes_cached_advice(self):
        advice = self.issue()
        self.case.evidence.drop("fixture:price", "price")
        self.assertIn("evidence_missing", codes(self.engine.revalidate(advice)))

    def test_evaluation_cannot_outlive_rule_ttl_while_waiting_for_evidence(self):
        store, clock = self.case.evidence, self.case.clock

        class SlowPort:
            def revision(self):
                return store.revision()

            def resolve(self, query, *, at):
                if query.purpose == "toy_threshold":
                    clock.advance(300)
                return store.resolve(query, at=at)

        engine = StrategyEngine(clock=clock, rules=(self.case.rule,), evidence=SlowPort())
        self.refused("expired_during_evaluation", engine=engine)

    def test_final_publication_time_catches_delay_in_end_revision_check(self):
        store, clock = self.case.evidence, self.case.clock

        class DelayedBarrier:
            calls = 0

            def revision(self):
                self.calls += 1
                if self.calls == 2:
                    clock.advance(300)
                return store.revision()

            def resolve(self, query, *, at):
                return store.resolve(query, at=at)

        engine = StrategyEngine(clock=clock, rules=(self.case.rule,), evidence=DelayedBarrier())
        self.refused("expired_during_evaluation", engine=engine)

    def test_output_sealing_time_is_included_in_expiry_check(self):
        class EndClock(FixtureClock):
            calls = 0

            def now(self):
                self.calls += 1
                if self.calls == 5:  # Admission, start, completion, publication, final sealed-output check.
                    self.ms = 1400
                return super().now()

        engine = StrategyEngine(clock=EndClock(), rules=(self.case.rule,), evidence=self.case.evidence)
        self.refused("expired_during_evaluation", engine=engine)

    def test_next_frame_revokes_even_if_values_and_context_look_identical(self):
        advice = self.issue()
        self.observe(next_fixture_frame(self.case.state))
        self.assertIn("state_changed", codes(self.engine.revalidate(advice)))

    def test_scene_phase_transition_revokes_old_advice(self):
        advice = self.issue()
        state = next_fixture_frame(self.case.state, context_changed=True)
        state["phase"]["value"] = "combat"
        self.observe(state)
        self.assertIn("state_changed", codes(self.engine.revalidate(advice)))
        self.assertEqual(self.engine.evaluate(self.case.rule.rule_id).status, "not_applicable")

    def test_same_snapshot_reread_does_not_fake_new_observation_or_extend_ttl(self):
        advice = self.issue()
        self.observe()
        self.assertTrue(self.engine.revalidate(advice).valid)
        self.assertEqual(advice.expires_host_monotonic_ms, 1400)

    def test_ingress_disconnect_barrier_revokes_before_new_state_is_available(self):
        advice = self.issue()
        self.engine.invalidate("合成断连事件。")
        self.assertIn("state_changed", codes(self.engine.revalidate(advice)))
        self.assertIn("state_invalidated", codes(self.engine.evaluate(self.case.rule.rule_id)))
        # The old snapshot cannot undo a later ingress/disconnect barrier.
        self.assertIn("invalid_state", codes(self.engine.observe(self.case.state)))
        self.assertIsNone(self.engine.evaluate(self.case.rule.rule_id).advice)
        self.observe(next_fixture_frame(self.case.state))
        self.assertIsNotNone(self.engine.evaluate(self.case.rule.rule_id).advice)

    def test_newer_state_published_during_evaluation_supersedes_pending_result(self):
        store = self.case.evidence
        newer = next_fixture_frame(self.case.state)

        class PublishingPort:
            engine = None

            def revision(self):
                return store.revision()

            def resolve(self, query, *, at):
                if query.purpose == "toy_threshold":
                    self.engine.observe(newer)
                return store.resolve(query, at=at)

        port = PublishingPort()
        engine = StrategyEngine(clock=self.case.clock, rules=(self.case.rule,), evidence=port)
        port.engine = engine
        self.refused("evaluation_superseded", engine=engine)

    def test_explicit_barrier_during_evaluation_supersedes_pending_result(self):
        store = self.case.evidence

        class InvalidatingPort:
            engine = None

            def revision(self):
                return store.revision()

            def resolve(self, query, *, at):
                if query.purpose == "toy_threshold":
                    self.engine.invalidate()
                return store.resolve(query, at=at)

        port = InvalidatingPort()
        engine = StrategyEngine(clock=self.case.clock, rules=(self.case.rule,), evidence=port)
        port.engine = engine
        self.refused("evaluation_superseded", engine=engine)

    def test_final_adapter_callback_cannot_publish_over_a_new_invalidation_barrier(self):
        store = self.case.evidence

        class LastBarrierPort:
            engine = None
            calls = 0

            def revision(self):
                self.calls += 1
                if self.calls == 3:
                    self.engine.invalidate("合成的输出封装期失效事件。")
                return store.revision()

            def resolve(self, query, *, at):
                return store.resolve(query, at=at)

        port = LastBarrierPort()
        engine = StrategyEngine(clock=self.case.clock, rules=(self.case.rule,), evidence=port)
        port.engine = engine
        self.refused("evaluation_superseded", engine=engine)

    def test_late_snapshot_cannot_restore_old_scene_after_new_frame(self):
        advice = self.issue()
        self.observe(next_fixture_frame(self.case.state))
        self.assertIn("invalid_state", codes(self.engine.observe(self.case.state)))
        self.assertFalse(self.engine.revalidate(advice).valid)
        self.assertIsNone(self.engine.evaluate(self.case.rule.rule_id).advice)

    def test_reconnect_requires_new_session_not_reusing_old_stream_evidence(self):
        self.issue()
        state = next_fixture_frame(self.case.state)
        state["stream_id"] = "reconnected-stream"
        self.assertIn("invalid_state", codes(self.engine.observe(state)))
        engine = self.case.engine()
        self.refused("evidence_binding_mismatch", state=state, engine=engine)

    def test_another_session_cannot_validate_prior_advice(self):
        advice = self.issue()
        other = self.case.engine()
        self.observe(engine=other)
        self.assertIn("session_changed", codes(other.revalidate(advice)))

    def test_caller_mutations_of_input_output_or_rule_view_do_not_change_accepted_state(self):
        advice = self.issue()
        self.case.state["gold"]["value"] = 0
        output = advice.to_dict()
        output["basis"][0]["value"] = {"verified": True}
        output["target"]["value"]["catalog_ref"]["entity_id"] = "different"
        view = self.engine.rules()
        view[0]["conditions"].clear()
        self.assertTrue(self.engine.revalidate(advice).valid)
        self.assertEqual(advice.to_dict()["target"]["value"]["catalog_ref"]["entity_id"],
                         "fictional-paper-square")

    def test_modified_advice_payload_or_extended_expiry_is_rejected(self):
        advice = self.issue()
        for changed in (replace(advice, payload=Literal({"verified": True})),
                        replace(advice, expires_host_monotonic_ms=9999)):
            with self.subTest(changed=changed):
                self.assertIn("advice_modified", codes(self.engine.revalidate(changed)))
        self.assertTrue(self.engine.revalidate(advice).valid)

    def test_literal_is_frozen_and_does_not_hold_caller_containers(self):
        source = {"values": [1, 1, False]}
        value = Literal(source)
        source["values"].clear()
        decoded = value.value()
        decoded["values"].append(5)
        self.assertEqual(value.value(), {"values": [1, 1, False]})
        with self.assertRaises(FrozenInstanceError):
            value.encoded = "{}"

    def test_declarative_rules_reject_hidden_dependencies_unknown_actions_and_missing_basis(self):
        with self.assertRaises(ValueError):
            Condition(FieldValue("/gold"), "eval", Literal(0), "no dynamic evaluation")
        with self.assertRaises(ValueError):
            replace(self.case.rule, intent="tap")
        with self.assertRaises(ValueError):
            replace(self.case.rule, evidence=())
        with self.assertRaises(ValueError):
            replace(self.case.rule, material_kind="verified")
        with self.assertRaises(ValueError):
            FieldValue("/bad~2pointer")
        with self.assertRaises(ValueError):
            StrategyEngine(clock=self.case.clock, rules=(self.case.rule, self.case.rule))

    def test_finite_json_only_and_no_string_evaluation(self):
        for value in (float("nan"), {"x": float("inf")}, {1: "bad key"}, ("not", "json")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Literal(value)

    def test_cli_runs_only_self_contained_fixture_and_demonstrates_withdrawal(self):
        project = Path(__file__).resolve().parents[3]
        env = dict(os.environ, PYTHONPATH=str(project / "src"), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-B", "-m", "jcc_strategy"],
                                cwd=project, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["device_integration"])
        self.assertFalse(report["real_rules_verified"])
        self.assertTrue(report["valid_before_change"]["valid"])
        self.assertFalse(report["valid_after_scene_change"]["valid"])
        self.assertEqual(report["decision"]["status"], "conditional_fixture")


if __name__ == "__main__":
    unittest.main()
