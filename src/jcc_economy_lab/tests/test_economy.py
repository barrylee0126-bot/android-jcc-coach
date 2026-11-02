"""Counterexamples and arithmetic invariants on synthetic inputs only."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
import io
import json
import unittest
from unittest.mock import patch

from jcc_economy_lab import (
    GoldCandidate, HypotheticalPrice, InterestAssumption, MAX_EXACT_INTEGER,
    analyze_purchases, analyze_request, source_catalog, ten_gold_five_cap_hypothesis,
)
from jcc_economy_lab.__main__ import main
from jcc_economy_lab.demo import demo_cases


def gold(value=31, **changes):
    return replace(GoldCandidate(value, "synthetic", "synthetic:test"), **changes)


class EconomyTests(unittest.TestCase):
    def setUp(self):
        self.rule = ten_gold_five_cap_hypothesis()

    def row(self, value, price, rule=None):
        return analyze_purchases(gold(value), [price], rule or self.rule)["comparisons"][0]

    def assert_no_interest(self, row):
        for key in ("interest_before", "interest_after", "interest_delta", "interest_effect"):
            self.assertIsNone(row[key])

    def assert_no_calculation(self, result):
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["gold_candidate"])
        for row in result["comparisons"]:
            self.assertIsNone(row["gold_after_hypothetical_purchase"])
            self.assertIsNone(row["within_candidate_balance"])
            self.assert_no_interest(row)

    def test_same_gold_prices_change_or_keep_interest(self):
        result = analyze_purchases(gold(31), [1, 2], self.rule)
        kept, changed = result["comparisons"]
        self.assertEqual((kept["gold_after_hypothetical_purchase"], kept["interest_delta"]), (30, 0))
        self.assertEqual((changed["gold_after_hypothetical_purchase"], changed["interest_delta"]), (29, -1))
        self.assertEqual(kept["interest_effect"], "unchanged")
        self.assertEqual(changed["interest_effect"], "reduced")

    def test_prices_are_alternatives_not_cumulative(self):
        rows = analyze_purchases(gold(31), [1, 2, 1], self.rule)["comparisons"]
        self.assertEqual([row["gold_after_hypothetical_purchase"] for row in rows], [30, 29, 30])
        self.assertEqual([row["hypothetical_price"] for row in rows], [1, 2, 1])

    def test_zero_one_and_exact_thresholds(self):
        cases = [
            (0, 0, 0, 0, 0), (1, 1, 0, 0, 0), (9, 0, 9, 0, 0),
            (10, 0, 10, 1, 0), (10, 1, 9, 0, -1), (11, 1, 10, 1, 0),
            (19, 0, 19, 1, 0), (20, 1, 19, 1, -1), (21, 1, 20, 2, 0),
            (49, 0, 49, 4, 0), (50, 0, 50, 5, 0), (50, 1, 49, 4, -1),
            (51, 1, 50, 5, 0), (60, 10, 50, 5, 0), (60, 11, 49, 4, -1),
        ]
        for value, price, balance, interest, delta in cases:
            with self.subTest(gold=value, price=price):
                row = self.row(value, price)
                self.assertEqual(row["gold_after_hypothetical_purchase"], balance)
                self.assertEqual(row["interest_after"]["interest_gold"], interest)
                self.assertEqual(row["interest_delta"], delta)

    def test_cap_masks_raw_step_change(self):
        row = self.row(61, 2)
        self.assertEqual(row["interest_before"]["uncapped_step_count"], 6)
        self.assertEqual(row["interest_after"]["uncapped_step_count"], 5)
        self.assertEqual(row["interest_delta"], 0)
        self.assertTrue(row["interest_before"]["at_cap"])
        self.assertTrue(row["interest_after"]["at_cap"])

    def test_changed_cap_reverses_no_change_case(self):
        base = self.row(61, 2)
        changed = self.row(61, 2, replace(self.rule, max_interest=7))
        self.assertEqual(base["interest_delta"], 0)
        self.assertEqual(changed["interest_delta"], -1)

    def test_changed_step_and_payout_with_partial_cap(self):
        rule = InterestAssumption("toy", 5, 2, 7, "synthetic_hypothesis", "纯合成参数。", ("synthetic:toy",))
        row = self.row(20, 1, rule)
        self.assertEqual(row["interest_before"]["interest_gold"], 7)
        self.assertEqual(row["interest_after"]["interest_gold"], 6)
        self.assertEqual(row["interest_delta"], -1)

    def test_explicit_zero_cap_is_not_unknown_rule(self):
        row = self.row(20, 20, replace(self.rule, max_interest=0))
        self.assertEqual(row["status"], "conditional")
        self.assertEqual(row["interest_delta"], 0)
        self.assertEqual(row["interest_after"]["interest_gold"], 0)

    def test_spending_entire_balance_is_not_negative(self):
        row = self.row(31, 31)
        self.assertEqual(row["gold_after_hypothetical_purchase"], 0)
        self.assertEqual(row["interest_delta"], -3)

    def test_overspend_has_no_negative_or_interest_projection(self):
        row = self.row(31, 32)
        self.assertEqual(row["status"], "insufficient_candidate_gold")
        self.assertFalse(row["within_candidate_balance"])
        self.assertIsNone(row["gold_after_hypothetical_purchase"])
        self.assert_no_interest(row)

    def test_one_bad_price_does_not_hide_valid_independent_case(self):
        result = analyze_purchases(gold(31), [2, 32, HypotheticalPrice(-1)], self.rule)
        self.assertEqual(result["status"], "partial")
        self.assertEqual([row["status"] for row in result["comparisons"]],
                         ["conditional", "insufficient_candidate_gold", "blocked"])

    def test_gold_states_block_even_if_a_value_is_present(self):
        for status in ("unknown", "conflict", "unreadable", "stale", "not_applicable", "recognized", "verified", None, []):
            with self.subTest(status=status):
                self.assert_no_calculation(analyze_purchases(gold(31, status=status), [1, 2], self.rule))

    def test_unreadable_and_non_integer_gold_never_coerced(self):
        for value in (None, -1, True, False, "31", "3l", "", 31.0, 31.5, float("nan"), float("inf"), [], {}, MAX_EXACT_INTEGER + 1):
            with self.subTest(value=repr(value)):
                result = analyze_purchases(gold(value), [1], self.rule)
                self.assert_no_calculation(result)
                json.dumps(result, allow_nan=False)

    def test_conflicting_alternatives_are_not_selected_or_averaged(self):
        for alternatives in ((31, 37), (31, 31), [31], "31", None):
            with self.subTest(alternatives=alternatives):
                self.assert_no_calculation(analyze_purchases(gold(31, alternatives=alternatives), [1], self.rule))

    def test_gold_needs_explicit_origin_and_source(self):
        for changes in ({"origin": "unknown"}, {"origin": "live"}, {"origin": []},
                        {"source_ref": ""}, {"source_ref": None}, {"source_ref": "a\nb"}):
            with self.subTest(changes=changes):
                self.assert_no_calculation(analyze_purchases(gold(31, **changes), [1], self.rule))

    def test_raw_gold_number_not_accepted_as_confirmed_input(self):
        self.assert_no_calculation(analyze_purchases(31, [1], self.rule))

    def test_invalid_prices_do_not_turn_into_zero_or_negative_spend(self):
        for value in (None, -1, True, False, "2", "?", 2.0, float("nan"), float("inf"), [], {}, MAX_EXACT_INTEGER + 1):
            with self.subTest(value=repr(value)):
                row = self.row(31, HypotheticalPrice(value))
                self.assertEqual(row["status"], "blocked")
                self.assertIsNone(row["hypothetical_price"])
                self.assertIsNone(row["gold_after_hypothetical_purchase"])
                self.assert_no_interest(row)

    def test_unknown_conflicting_or_stale_price_blocks_row(self):
        for status in ("unknown", "conflict", "stale", "recognized", None, []):
            with self.subTest(status=status):
                row = self.row(31, HypotheticalPrice(2, status=status))
                self.assertEqual(row["status"], "blocked")
                self.assert_no_interest(row)

    def test_price_labels_remain_separate_from_price_values(self):
        row = self.row(31, HypotheticalPrice(2, "假设对象A"))
        self.assertEqual(row["label"], "假设对象A")
        self.assertEqual(row["hypothetical_price"], 2)
        row = self.row(31, HypotheticalPrice(2, "\ninvalid"))
        self.assertEqual(row["status"], "blocked")

    def test_missing_rule_only_allows_explicit_cash_arithmetic(self):
        result = analyze_purchases(gold(31), [2])
        self.assertEqual(result["status"], "cash_only")
        row = result["comparisons"][0]
        self.assertEqual(row["gold_after_hypothetical_purchase"], 29)
        self.assert_no_interest(row)
        self.assertIn("不判断利息", row["text_zh"])

    def test_invalid_rule_never_defaults_to_ten_five(self):
        rules = [None, {}, True, replace(self.rule, gold_per_step=0),
                 replace(self.rule, gold_per_step=-10), replace(self.rule, gold_per_step=True),
                 replace(self.rule, gold_per_step="10"), replace(self.rule, interest_per_step=0),
                 replace(self.rule, interest_per_step=1.0), replace(self.rule, max_interest=None),
                 replace(self.rule, max_interest=-1), replace(self.rule, max_interest=True),
                 replace(self.rule, max_interest=float("nan")), replace(self.rule, max_interest=MAX_EXACT_INTEGER + 1)]
        for rule in rules:
            with self.subTest(rule=rule):
                result = analyze_purchases(gold(31), [2], rule)
                self.assertEqual(result["status"], "cash_only")
                self.assertIsNone(result["rule_assumption"]["parameters"])
                self.assert_no_interest(result["comparisons"][0])

    def test_rule_conflict_unknown_or_revoked_stays_unavailable(self):
        for status in ("conflict", "unknown", "revoked", "verified", None, []):
            with self.subTest(status=status):
                result = analyze_purchases(gold(31), [2], replace(self.rule, status=status))
                self.assertEqual(result["status"], "cash_only")
                self.assert_no_interest(result["comparisons"][0])

    def test_rule_without_explicit_basis_not_usable_for_interest(self):
        for changes in ({"assumption_id": ""}, {"basis_note": ""}, {"source_refs": ()},
                        {"source_refs": None}, {"source_refs": (None,)},
                        {"basis_kind": "official_current"}, {"basis_kind": []}):
            with self.subTest(changes=changes):
                result = analyze_purchases(gold(31), [2], replace(self.rule, **changes))
                self.assertEqual(result["status"], "cash_only")
                self.assert_no_interest(result["comparisons"][0])

    def test_empty_or_malformed_price_list_does_not_invent_a_case(self):
        for prices in ([], (), None, 2, "1,2", {"value": 2}):
            with self.subTest(prices=prices):
                result = analyze_purchases(gold(), prices, self.rule)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["comparisons"], [])

    def test_all_origins_preserve_unconfirmed_and_unbound_flags(self):
        labels = {"synthetic": "合成假设", "manual_hypothesis": "手工假设",
                  "replay_candidate": "回放机器候选", "live_candidate": "实时机器候选"}
        for origin, label in labels.items():
            with self.subTest(origin=origin):
                result = analyze_purchases(gold(origin=origin), [2], self.rule)
                self.assertFalse(result["confirmed_game_state"])
                self.assertFalse(result["gold_candidate"]["confirmed"])
                self.assertFalse(result["execution_eligible"])
                self.assertFalse(result["strategy_recommendation"])
                self.assertEqual(result["settlement_binding"], "unbound")
                self.assertEqual(result["match_rule_binding"], "unverified")
                self.assertIn(label, result["comparisons"][0]["text_zh"])
                self.assertIn("未绑定本局结算", result["comparisons"][0]["text_zh"])
                self.assertNotIn("建议购买", result["comparisons"][0]["text_zh"])

    def test_bounded_exhaustive_cash_and_interest_invariants(self):
        for rule in (self.rule, replace(self.rule, max_interest=0),
                     replace(self.rule, gold_per_step=7, interest_per_step=2, max_interest=9)):
            for value in range(0, 102):
                rows = analyze_purchases(gold(value), list(range(value + 1)), rule)["comparisons"]
                prior_interest = None
                for row in rows:
                    after = row["gold_after_hypothetical_purchase"]
                    interest = row["interest_after"]["interest_gold"]
                    self.assertEqual(after + row["hypothetical_price"], value)
                    self.assertGreaterEqual(after, 0)
                    self.assertGreaterEqual(interest, 0)
                    self.assertLessEqual(interest, rule.max_interest)
                    self.assertLessEqual(row["interest_delta"], 0)
                    if prior_interest is not None:
                        self.assertLessEqual(interest, prior_interest)
                    prior_interest = interest

    def test_json_safe_integer_boundary_without_rounding(self):
        rule = replace(self.rule, max_interest=MAX_EXACT_INTEGER,
                       gold_per_step=1, interest_per_step=MAX_EXACT_INTEGER)
        row = self.row(MAX_EXACT_INTEGER, MAX_EXACT_INTEGER - 1, rule)
        self.assertEqual(row["gold_after_hypothetical_purchase"], 1)
        self.assertEqual(row["interest_before"]["interest_gold"], MAX_EXACT_INTEGER)
        self.assertEqual(row["interest_after"]["interest_gold"], MAX_EXACT_INTEGER)
        json.dumps(row, allow_nan=False)

    def test_repeated_calls_and_output_mutation_do_not_modify_inputs(self):
        candidate, prices = gold(), [HypotheticalPrice(2)]
        before = (asdict(candidate), list(prices), asdict(self.rule))
        first = analyze_purchases(candidate, prices, self.rule)
        expected = analyze_purchases(candidate, prices, self.rule)
        self.assertEqual(first, expected)
        first["comparisons"][0]["interest_after"]["interest_gold"] = 999
        first["rule_assumption"]["source_refs"].clear()
        first["gold_candidate"]["value"] = 999
        self.assertEqual(analyze_purchases(candidate, prices, self.rule), expected)
        self.assertEqual((asdict(candidate), prices, asdict(self.rule)), before)

    def test_calculation_performs_no_file_network_or_process_io(self):
        with patch("builtins.open", side_effect=AssertionError("file I/O")), \
             patch("socket.socket", side_effect=AssertionError("network I/O")), \
             patch("subprocess.run", side_effect=AssertionError("process I/O")):
            self.assertEqual(analyze_purchases(gold(), [1, 2], self.rule)["status"], "conditional")

    def test_source_catalog_is_fresh_and_marks_historical_limits(self):
        sources = source_catalog()
        self.assertEqual(len(sources), 3)
        self.assertEqual(sources[0]["source_kind"], "historical_secondary")
        self.assertIn("未核实本局", self.rule.basis_note)
        sources[0]["url"] = "changed"
        self.assertNotEqual(source_catalog()[0]["url"], "changed")


class AdapterAndCliTests(unittest.TestCase):
    def payload(self):
        return {"gold": asdict(gold()), "prices": [1, {"value": 2}],
                "rule": asdict(ten_gold_five_cap_hypothesis())}

    def cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_json_adapter_roundtrip_equals_typed_input(self):
        request = json.loads(json.dumps(self.payload()))
        self.assertEqual(analyze_request(request), analyze_purchases(
            gold(), [1, HypotheticalPrice(2)], ten_gold_five_cap_hypothesis()))

    def test_json_adapter_without_rule_keeps_only_cash(self):
        request = self.payload()
        del request["rule"]
        self.assertEqual(analyze_request(request)["status"], "cash_only")

    def test_json_adapter_does_not_promote_conflict_to_candidate(self):
        request = self.payload()
        request["gold"]["status"] = "conflict"
        self.assertEqual(analyze_request(request)["status"], "blocked")

    def test_json_adapter_rejects_unknown_and_verification_flags(self):
        for path in (None, "gold", "rule"):
            with self.subTest(path=path):
                request = self.payload()
                target = request if path is None else request[path]
                target["verified"] = True
                result = analyze_request(request)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["comparisons"], [])

    def test_json_adapter_rejects_malformed_requests(self):
        requests = [None, [], {}, {"gold": 31, "prices": [1]},
                    {"gold": asdict(gold()), "prices": "1"},
                    {"gold": asdict(gold()), "prices": [{"value": 1, "verified": True}]},
                    {"gold": asdict(gold()), "prices": [1], "rule": {}},
                    {"gold": {}, "prices": [1]}]
        for request in requests:
            with self.subTest(request=request):
                result = analyze_request(request)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["comparisons"], [])

    def test_demo_contains_change_and_no_change_without_real_inputs(self):
        cases = demo_cases()
        self.assertEqual(len(cases), 8)
        first = cases[0]["analysis"]
        self.assertEqual([row["interest_delta"] for row in first["comparisons"][:2]], [0, -1])
        for case in cases:
            self.assertIn("纯合成", case["label"])
            result = case["analysis"]
            self.assertFalse(result["confirmed_game_state"])
            if result["gold_candidate"] is not None:
                self.assertEqual(result["gold_candidate"]["origin"], "synthetic")

    def test_cli_manual_examples_label_inputs_as_hypotheses(self):
        code, output, _ = self.cli(["--gold", "31", "--prices", "1", "2", "--rule", "historical-10-5", "--json"])
        self.assertEqual(code, 0)
        result = json.loads(output)["cases"][0]["analysis"]
        self.assertEqual(result["gold_candidate"]["origin"], "manual_hypothesis")
        self.assertEqual([row["interest_delta"] for row in result["comparisons"]], [0, -1])

    def test_cli_omitted_rule_does_not_select_a_default(self):
        code, output, _ = self.cli(["--gold", "31", "--prices", "2", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["cases"][0]["analysis"]["status"], "cash_only")

    def test_cli_inline_json_supports_future_machine_candidate_mapping(self):
        request = self.payload()
        request["gold"]["origin"] = "replay_candidate"
        request["gold"]["source_ref"] = "synthetic:future-adapter-example"
        code, output, _ = self.cli(["--request-json", json.dumps(request), "--json"])
        self.assertEqual(code, 0)
        result = json.loads(output)["cases"][0]["analysis"]
        self.assertIn("回放机器候选", result["comparisons"][0]["text_zh"])
        self.assertFalse(result["confirmed_game_state"])

    def test_cli_demo_text_is_self_contained_and_conditional(self):
        code, output, _ = self.cli(["--demo"])
        self.assertEqual(code, 0)
        for phrase in ("纯合成", "不判断最优买卖", "未绑定本局结算", "不判断利息", "非官方"):
            self.assertIn(phrase, output)

    def test_cli_demo_json_is_serializable(self):
        code, output, _ = self.cli(["--demo", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output)["cases"]), 8)

    def test_cli_invalid_or_duplicate_json_has_nonzero_exit(self):
        for payload in ('{"gold":1,"gold":2}', '{"gold":NaN}', '{"gold":Infinity}', 'not json'):
            with self.subTest(payload=payload):
                code, output, _ = self.cli(["--request-json", payload])
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")

    def test_cli_rejects_ignored_flags_and_missing_prices(self):
        for argv in (["--demo", "--rule", "historical-10-5"], ["--demo", "--prices", "1"],
                     ["--gold", "31"], ["--request-json", "{}", "--prices", "1"]):
            with self.subTest(argv=argv):
                code, output, _ = self.cli(argv)
                self.assertEqual(code, 2)
                self.assertEqual(output, "")

    def test_cli_blocked_result_has_nonzero_exit_and_no_asserted_balance(self):
        code, output, _ = self.cli(["--gold", "-1", "--prices", "1", "--json"])
        self.assertEqual(code, 1)
        row = json.loads(output)["cases"][0]["analysis"]["comparisons"][0]
        self.assertIsNone(row["gold_after_hypothetical_purchase"])
        self.assertIsNone(row["interest_delta"])


if __name__ == "__main__":
    unittest.main()
