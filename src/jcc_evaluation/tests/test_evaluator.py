"""Only fabricated in-memory inputs; no real files, humans, images or devices.

Some gate tests deliberately construct legal *real-branch metadata* from the
synthetic fixture. This tests schema/selection behavior, not actual human review.
Those dictionaries/reports are never exported or claimed as real measurements.
"""

from copy import deepcopy
import unittest

from src.jcc_device.game_state_contract import validate_annotations, validate_state
from src.jcc_evaluation import EvaluationInputError, evaluate, render_markdown
from src.jcc_evaluation.evaluator import FIELDS, OUTCOMES, _rate_target
from src.jcc_evaluation.synthetic import binding, demo_inputs, label, prediction, sample, set_status


def fake_real_branch(number=1, *, split="acceptance", review_status="independently_reviewed"):
    """Fabricated metadata only; never a real acceptance sample or export."""
    record = sample(number)
    record.update(sample_kind="real", split=split, annotation_method="human", review_status=review_status,
                  reviewer_id=None if review_status == "draft" else "synthetic-reviewer",
                  asset={"asset_ref": f"synthetic-in-memory-asset-{number}", "sha256": f"{number:064x}",
                         "access": "local_only", "redaction": "none"})
    return record


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.record = sample()
        self.state = prediction(self.record)

    def run_one(self, *, record=None, state=None, predictions=None):
        record = self.record if record is None else record
        state = self.state if state is None else state
        return evaluate([record], [{"sample_id": record["sample_id"], "state": state}] if predictions is None else predictions,
                        cohort="synthetic")

    def assert_partition(self, report):
        for counts in (*report["fields"].values(), report["shop_all"]):
            self.assertEqual(counts["applicable"], sum(counts[key] for key in OUTCOMES))
            self.assertLessEqual(counts["protocol_errors"], counts["unknown"])

    def test_fixture_itself_satisfies_both_contracts(self):
        annotations, predictions, _ = demo_inputs()
        validate_annotations(annotations)
        for item in predictions:
            validate_state(item["state"])

    def test_perfect_synthetic_result_has_exactly_eleven_atomic_fields(self):
        report = self.run_one()
        self.assertEqual(set(report["fields"]), set(FIELDS))
        for field in FIELDS:
            self.assertEqual(report["fields"][field]["correct"], 1)
            self.assertEqual(report["fields"][field]["rates"]["correct"], 1)
        self.assertEqual(report["shop_all"]["correct"], 5)
        self.assertEqual(report["coverage"]["actual"]["deduplicated_frames"], 1)
        self.assertEqual(report["targets"]["phase"]["status"], "report_only")
        self.assertIsNone(report["targets"]["real_acceptance_verdict"])
        self.assertFalse(report["real_result_available"])
        self.assertIn("NOT_REAL", report["result_kind"])
        self.assert_partition(report)

    def test_demo_denominator_includes_unknown_truth_and_missing_output(self):
        annotations, predictions, metadata = demo_inputs()
        report = evaluate(annotations, predictions, cohort="synthetic", run_metadata=metadata)
        counts = report["fields"]["gold"]
        self.assertEqual({key: counts[key] for key in OUTCOMES}, {
            "correct": 1, "error": 1, "unknown": 2, "stale": 1, "conflict": 1, "unverifiable": 1})
        self.assertEqual(counts["applicable"], 7)
        self.assertEqual(counts["known_answers"], 6)
        self.assertEqual(counts["protocol_errors"], 1)
        self.assertEqual(counts["rates"]["correct"], 1 / 7)
        self.assertEqual(counts["rates"]["worst_case_error"], 2 / 7)
        self.assertTrue(counts["observed_error_is_partial"])
        self.assert_partition(report)

    def test_missing_and_null_outputs_count_as_unknown_with_protocol_error(self):
        for entries in ([], [{"sample_id": self.record["sample_id"], "state": None}]):
            with self.subTest(entries=bool(entries)):
                report = self.run_one(predictions=entries)
                for counts in report["fields"].values():
                    self.assertEqual((counts["applicable"], counts["unknown"], counts["protocol_errors"]), (1, 1, 1))
                self.assertEqual(report["prediction_inventory"]["missing_selected_predictions"], 1)
                self.assert_partition(report)

    def test_illegal_snapshot_never_gets_partial_credit(self):
        mutations = [
            lambda state: state.pop("gold"),
            lambda state: state["gold"].update(value="50"),
            lambda state: state["gold"].update(value=True),
            lambda state: state["gold"].update(status="guessed"),
            lambda state: state.update(uncontracted_key=True),
            lambda state: state["gold"].update(expires_host_monotonic_ms=1200),
            lambda state: state["gold"].update(completed_host_monotonic_ms=999),
            lambda state: state["shop"].pop(),
            lambda state: state["shop"][1].update(slot=1),
            lambda state: state.update(shop=list(reversed(state["shop"]))),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                state = deepcopy(self.state)
                mutate(state)
                report = self.run_one(state=state)
                for counts in report["fields"].values():
                    self.assertEqual((counts["correct"], counts["unknown"], counts["protocol_errors"]), (0, 1, 1))
                self.assertEqual(report["samples"][0]["sample_protocol_issues"], ["invalid_game_state"])

    def test_non_object_prediction_is_an_invalid_output(self):
        for state in (False, 1, [], "model failed"):
            with self.subTest(state=state):
                self.assertEqual(self.run_one(state=state)["fields"]["gold"]["unknown"], 1)

    def test_all_nonrecognized_statuses_work_for_every_scored_field(self):
        for field in FIELDS:
            for status in ("unknown", "stale", "conflict"):
                with self.subTest(field=field, status=status):
                    state = deepcopy(self.state)
                    set_status(state, field, status)
                    validate_state(state)
                    counts = self.run_one(state=state)["fields"][field]
                    self.assertEqual(counts[status], 1)
                    self.assertEqual(counts["protocol_errors"], 0)

    def test_unknown_truth_with_recognized_is_unverifiable(self):
        self.record["labels"]["gold"] = label(reason="synthetic_occlusion")
        counts = self.run_one()["fields"]["gold"]
        self.assertEqual((counts["applicable"], counts["known_answers"], counts["unverifiable"], counts["error"]), (1, 0, 1, 0))
        self.assertEqual(counts["rates"]["worst_case_error"], 1)
        self.assertEqual(counts["unknown_truth_reasons"], {"synthetic_occlusion": 1})

    def test_unknown_truth_does_not_turn_abstention_into_unverifiable(self):
        self.record["labels"]["gold"] = label()
        for status in ("unknown", "stale", "conflict"):
            with self.subTest(status=status):
                state = deepcopy(self.state)
                set_status(state, "gold", status)
                counts = self.run_one(state=state)["fields"]["gold"]
                self.assertEqual((counts[status], counts["unverifiable"], counts["applicable"]), (1, 0, 1))

    def test_unverifiable_invalid_source_is_unknown_instead(self):
        self.record["labels"]["gold"] = label()
        self.state["stream_id"] = "synthetic-other-stream"
        counts = self.run_one()["fields"]["gold"]
        self.assertEqual((counts["unverifiable"], counts["unknown"], counts["protocol_errors"]), (0, 1, 1))

    def test_no_applicable_labels_has_null_rates_and_explicit_exclusions(self):
        for key in ("scene", "round", "hp", "level"):
            self.record["labels"][key] = label(applicability="undetermined", reason="synthetic_applicability_unclear")
        for key in ("phase", "gold"):
            self.record["labels"][key] = label(applicability="not_applicable")
        for slot in self.record["labels"]["shop"]:
            slot["content"] = label(applicability="not_applicable")
        report = self.run_one(predictions=[])
        for counts in report["fields"].values():
            self.assertEqual(counts["applicable"], 0)
            self.assertTrue(all(value is None for value in counts["rates"].values()))
            self.assertEqual(counts["protocol_errors_outside_denominator"], 1)
        self.assertEqual(report["fields"]["gold"]["not_applicable_reasons"], {"not_applicable": 1})
        self.assertEqual(report["fields"]["hp"]["undetermined_reasons"], {"synthetic_applicability_unclear": 1})
        self.assertFalse(report["accuracy_result_available"])
        self.assertIn("null（无适用样本）", render_markdown(report))

    def test_empty_manifest_is_insufficient_not_perfect(self):
        report = evaluate([], [], cohort="real_acceptance")
        self.assertFalse(report["real_result_available"])
        self.assertFalse(report["accuracy_result_available"])
        self.assertEqual(report["targets"]["real_acceptance_verdict"]["m1_numerical_targets"], "insufficient_data")
        self.assertEqual(report["coverage"]["shortfalls"]["deduplicated_frames"], 100)
        self.assert_partition(report)

    def test_valid_expired_and_disconnected_stale_are_stale(self):
        set_status(self.state, "gold", "stale")
        for reason in ("expired", "disconnected"):
            with self.subTest(reason=reason):
                self.state["gold"].update(reason=reason, expires_host_monotonic_ms=1200)
                validate_state(self.state)
                self.assertEqual(self.run_one()["fields"]["gold"]["stale"], 1)

    def test_value_equality_is_exact_and_json_numbers_compare_numerically(self):
        self.state["round"]["value"] = {"step": 2.0, "stage": 3.0}
        self.state["gold"]["value"] = 50.0
        report = self.run_one()
        self.assertEqual(report["fields"]["round"]["correct"], 1)
        self.assertEqual(report["fields"]["gold"]["correct"], 1)
        self.state["round"]["value"]["step"] = 3
        self.assertEqual(self.run_one()["fields"]["round"]["error"], 1)

    def test_shop_compares_name_empty_and_entire_catalog_reference(self):
        ref = {"dataset_id": "synthetic-catalog-v1", "entity_kind": "unit", "entity_id": "synthetic-unit", "variant": "base"}
        truth = self.record["labels"]["shop"][1]["content"]["value"]
        truth["catalog_ref"] = deepcopy(ref)
        for change in ({"catalog_ref": None}, {"catalog_ref": {**ref, "variant": "other"}},
                       {"catalog_ref": {**ref, "dataset_id": "synthetic-catalog-v2"}},
                       {"display_name": "合成别名", "catalog_ref": ref}):
            with self.subTest(change=change):
                self.state["shop"][1]["content"]["value"] = {**truth, **change}
                self.assertEqual(self.run_one()["fields"]["shop.2"]["error"], 1)
        self.state["shop"][1]["content"]["value"] = {"kind": "empty"}
        self.assertEqual(self.run_one()["fields"]["shop.2"]["error"], 1)
        self.state["shop"][1]["content"]["value"] = deepcopy(truth)
        self.assertEqual(self.run_one()["fields"]["shop.2"]["correct"], 1)

    def test_recognized_empty_is_not_unknown(self):
        set_status(self.state, "shop.1", "unknown")
        counts = self.run_one()["fields"]["shop.1"]
        self.assertEqual((counts["unknown"], counts["correct"], counts["error"]), (1, 0, 0))

    def test_wrong_stream_clock_and_sample_kind_never_score(self):
        for key, value in (("stream_id", "synthetic-wrong-stream"), ("host_clock_id", "synthetic-wrong-clock"), ("sample_kind", "replay")):
            with self.subTest(key=key):
                state = deepcopy(self.state)
                state[key] = value
                validate_state(state)
                report = self.run_one(state=state)
                self.assertEqual(report["fields"]["gold"]["unknown"], 1)
                self.assertIn(key + "_mismatch", report["samples"][0]["sample_protocol_issues"])

    def test_wrong_anchor_is_not_repaired_by_old_field_matching(self):
        newer = deepcopy(self.state["frames"][0])
        newer.update(frame_id="synthetic-newer-frame", sequence=2, host_received_monotonic_ms=1150)
        self.state["frames"].append(newer)
        self.state["frame_id"] = newer["frame_id"]
        validate_state(self.state)
        report = self.run_one()
        self.assertIn("anchor_frame_mismatch", report["samples"][0]["sample_protocol_issues"])
        self.assertEqual(report["fields"]["gold"]["correct"], 0)

    def test_same_frame_id_with_modified_metadata_is_not_aligned(self):
        for change in ({"sequence": 2}, {"image": {**self.record["frame"]["image"], "calibration_id": "synthetic-other-calibration"}},
                       {"device_capture_time": {"clock_id": "synthetic-device-clock", "ticks_ms": 12, "provenance": "synthetic-metadata"}}):
            with self.subTest(change=change):
                state = deepcopy(self.state)
                state["frames"][0].update(change)
                validate_state(state)
                report = self.run_one(state=state)
                self.assertIn("target_frame_metadata_mismatch", report["samples"][0]["sample_protocol_issues"])
                self.assertEqual(report["fields"]["gold"]["unknown"], 1)

    def old_frame(self):
        older = deepcopy(self.record["frame"])
        older.update(frame_id="synthetic-older-frame", sequence=0, host_received_monotonic_ms=900)
        self.state["frames"].insert(0, older)
        return older

    def test_field_old_frame_inheritance_is_rejected_only_for_that_field(self):
        old = self.old_frame()
        self.state["gold"].update(source_frame_id=old["frame_id"], observed_host_monotonic_ms=900, completed_host_monotonic_ms=950)
        validate_state(self.state)
        report = self.run_one()
        self.assertEqual(report["fields"]["gold"]["unknown"], 1)
        self.assertEqual(report["fields"]["hp"]["correct"], 1)
        self.assertEqual(report["samples"][0]["fields"]["gold"]["protocol_issues"], ["field_source_frame_mismatch"])

    def test_cross_frame_stale_is_unknown_in_strict_frame_evaluation(self):
        old = self.old_frame()
        set_status(self.state, "gold", "stale")
        self.state["gold"].update(source_frame_id=old["frame_id"], observed_host_monotonic_ms=900, completed_host_monotonic_ms=950)
        validate_state(self.state)
        counts = self.run_one()["fields"]["gold"]
        self.assertEqual((counts["unknown"], counts["stale"], counts["protocol_errors"]), (1, 0, 1))

    def test_cross_frame_conflict_candidate_is_unknown(self):
        old = self.old_frame()
        set_status(self.state, "gold", "conflict")
        self.state["gold"]["candidates"][0].update(source_frame_id=old["frame_id"], observed_host_monotonic_ms=900, completed_host_monotonic_ms=950)
        validate_state(self.state)
        report = self.run_one()
        self.assertEqual((report["fields"]["gold"]["conflict"], report["fields"]["gold"]["unknown"]), (0, 1))
        self.assertIn("candidate_source_frame_mismatch", report["samples"][0]["fields"]["gold"]["protocol_issues"])

    def test_duplicate_prediction_cannot_choose_the_best_result(self):
        wrong = deepcopy(self.state)
        wrong["gold"]["value"] = 49
        entries = [binding(self.record, self.state), binding(self.record, wrong)]
        report = self.run_one(predictions=entries)
        self.assertEqual(report["fields"]["gold"]["unknown"], 1)
        self.assertEqual(report["prediction_inventory"]["duplicate_selected_bindings"], 1)

    def test_unbound_prediction_is_a_run_protocol_problem_and_not_a_new_sample(self):
        extra = {"sample_id": "synthetic-unbound", "state": self.state}
        report = self.run_one(predictions=[binding(self.record, self.state), extra])
        self.assertEqual(report["fields"]["gold"]["applicable"], 1)
        self.assertEqual(report["prediction_inventory"]["unbound_prediction_records"], 1)

    def test_synthetic_is_excluded_from_default_real_report(self):
        report = evaluate([self.record], [binding(self.record)])
        self.assertEqual(report["inventory"]["selected_samples"], 0)
        self.assertEqual(report["inventory"]["exclusions"], {"synthetic_excluded_from_real": 1})
        self.assertFalse(report["real_result_available"])

    def test_real_branch_and_synthetic_never_share_denominators(self):
        fake = fake_real_branch(2)
        state = prediction(fake)
        state["sample_kind"] = "replay"
        annotations = [self.record, fake]
        predictions = [binding(self.record), binding(fake, state)]
        for cohort in ("synthetic", "real_acceptance"):
            with self.subTest(cohort=cohort):
                report = evaluate(annotations, predictions, cohort=cohort)
                self.assertEqual(report["inventory"]["selected_samples"], 1)
                self.assertEqual(report["fields"]["gold"]["applicable"], 1)
                self.assertEqual(report["fields"]["gold"]["correct"], 1)
                self.assertEqual(report["prediction_inventory"]["ignored_records_for_excluded_samples"], 1)

    def test_real_development_quarantine_and_draft_do_not_enter_acceptance(self):
        records = [fake_real_branch(1, split="development", review_status="draft"),
                   fake_real_branch(2, split="development"), fake_real_branch(3, split="quarantine")]
        report = evaluate(records, [])
        self.assertEqual(report["inventory"]["selected_samples"], 0)
        self.assertEqual(report["inventory"]["exclusions"], {"not_acceptance_split": 3})

    def test_review_gate_and_synthetic_acceptance_fail_before_scoring(self):
        cases = [fake_real_branch(review_status="draft"), sample()]
        cases[1]["split"] = "acceptance"
        same_person = fake_real_branch()
        same_person["reviewer_id"] = same_person["annotator_id"]
        cases.append(same_person)
        for record in cases:
            with self.subTest(kind=record["sample_kind"], status=record["review_status"]):
                with self.assertRaises(EvaluationInputError) as caught:
                    evaluate([record], [], cohort="synthetic")
                self.assertEqual(caught.exception.code, "invalid_annotations")

    def test_recording_and_dedupe_split_leaks_fail_before_selection(self):
        for group in ("recording_session_id", "dedupe_group_id"):
            with self.subTest(group=group):
                first = fake_real_branch(1, split="development")
                second = fake_real_branch(2)
                second[group] = first[group]
                with self.assertRaises(EvaluationInputError):
                    evaluate([self.record, first, second], [], cohort="synthetic")

    def test_duplicate_acceptance_representatives_and_hidden_image_duplicates_rejected(self):
        for key in ("dedupe_group_id", "sha256"):
            with self.subTest(key=key):
                first, second = fake_real_branch(1), fake_real_branch(2)
                if key == "sha256":
                    second["asset"][key] = first["asset"][key]
                else:
                    second[key] = first[key]
                with self.assertRaises(EvaluationInputError):
                    evaluate([first, second], [])

    def test_mixed_dataset_revision_and_duplicate_source_frames_fail(self):
        first, second = sample(1), sample(2)
        second["dataset_id"] = "synthetic-other-revision"
        with self.assertRaises(EvaluationInputError) as caught:
            evaluate([first, second], [], cohort="synthetic")
        self.assertEqual(caught.exception.code, "mixed_dataset_revisions")
        second = deepcopy(first)
        second["sample_id"] = "synthetic-another-id-for-same-frame"
        with self.assertRaises(EvaluationInputError) as caught:
            evaluate([first, second], [], cohort="synthetic")
        self.assertEqual(caught.exception.code, "duplicate_target_frame")

    def test_malformed_prediction_bindings_reject_instead_of_guessing_correspondence(self):
        for predictions in ({}, [self.state], [{"sample_id": "synthetic-1"}],
                            [{"sample_id": "synthetic-1", "state": None, "extra": 1}]):
            with self.subTest(predictions=type(predictions).__name__):
                with self.assertRaises(EvaluationInputError):
                    evaluate([self.record], predictions, cohort="synthetic")

    def test_coverage_uses_group_ids_and_distinct_complete_shops(self):
        records = [sample(number) for number in range(1, 5)]
        for record in records:
            record["recording_session_id"] = "synthetic-one-session"
            record["state_group_id"] = "synthetic-one-state"
        records[1]["labels"]["shop"][0]["content"] = label()
        records[2]["labels"]["shop"][0]["content"]["value"] = {"kind": "unit", "display_name": "合成新棋子", "catalog_ref": None}
        report = evaluate(records, [], cohort="synthetic")
        self.assertEqual(report["coverage"]["actual"], {"recording_sessions": 1, "deduplicated_frames": 4,
                         "distinct_state_groups": 1, "applicable_shop_slots": 20, "known_complete_shop_states": 2})
        self.assertEqual(report["coverage"]["core_applicable_shortfalls"]["gold"], 96)

    def test_conditions_and_unknown_mode_patch_report_without_inferred_events(self):
        self.record["conditions"] = ["synthetic_popup", "synthetic_occlusion"]
        report = self.run_one()
        self.assertTrue(report["conditions"]["groups_overlap"])
        for counts in report["conditions"]["groups"].values():
            self.assertEqual(counts["fields"]["gold"]["applicable"], 1)
        self.assertEqual(report["applicability_context"]["game_version"][0]["label"]["truth_status"], "unknown")
        self.assertEqual(report["event_coverage"]["status"], "not_measured")
        self.assertIsNone(report["event_coverage"]["boundary_events"]["occlusion"]["count"])
        self.assertIsNone(report["latency"]["warmed_p95_ms"])

    def test_inclusive_percentage_targets_and_worst_case_error(self):
        for correct, error, unverifiable, minimum, status in (
            (95, 2, 0, 100, "met"), (95, 2, 1, 100, "not_met"), (94, 0, 0, 100, "not_met"),
            (95, 0, 2, 100, "met"), (95, 0, 3, 100, "not_met"), (95, 0, 0, 101, "insufficient_data"),
        ):
            with self.subTest(correct=correct, error=error, v=unverifiable, minimum=minimum):
                result = _rate_target({"applicable": 100, "correct": correct, "error": error, "unverifiable": unverifiable}, 95, minimum)
                self.assertEqual(result["status"], status)
        self.assertEqual(_rate_target({"applicable": 100, "correct": 90, "error": 2, "unverifiable": 0}, 90, 100)["status"], "met")
        self.assertIsNone(_rate_target({"applicable": 0, "correct": 0, "error": 0, "unverifiable": 0}, 95, 100)["observed_rates_meet_target"])

    def test_output_determinism_no_mutation_and_revision_fingerprints(self):
        records, predictions, metadata = demo_inputs()
        before = deepcopy((records, predictions, metadata))
        first = evaluate(records, predictions, cohort="synthetic", run_metadata=metadata)
        second = evaluate(records, predictions, cohort="synthetic", run_metadata=metadata)
        self.assertEqual(first, second)
        self.assertEqual((records, predictions, metadata), before)
        records[0]["labels"]["gold"]["value"] = 49
        third = evaluate(records, predictions, cohort="synthetic", run_metadata=metadata)
        self.assertNotEqual(first["reproducibility"]["canonical_annotations_sha256"], third["reproducibility"]["canonical_annotations_sha256"])

    def test_invalid_metadata_cohort_and_nonfinite_inputs_fail_without_scores(self):
        for options in ({"run_metadata": {"unapproved_key": "x"}}, {"cohort": "all"},
                        {"run_metadata": {"model_revision": ""}}):
            with self.subTest(options=options):
                with self.assertRaises(EvaluationInputError):
                    evaluate([self.record], [], **options)
        self.state["gold"]["value"] = float("nan")
        with self.assertRaises(EvaluationInputError) as caught:
            self.run_one()
        self.assertEqual(caught.exception.code, "non_json_input")

    def test_python_api_rejects_non_json_types_and_non_string_keys(self):
        for invalid in ({1: "synthetic"}, (1, 2), {1, 2}, float("inf")):
            with self.subTest(invalid=type(invalid).__name__):
                state = deepcopy(self.state)
                state["extensions"] = invalid
                with self.assertRaises(EvaluationInputError) as caught:
                    self.run_one(state=state)
                self.assertEqual(caught.exception.code, "non_json_input")


if __name__ == "__main__":
    unittest.main()
