from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from jcc_decision_bridge import demo_case, evaluate, run_comparison
from jcc_decision_bridge.bridge import _local_json, local_model_comparison


def response(choice="hold", reason="meets_reserve_and_maximizes_copies"):
    return {"reply": {"choice": choice, "reason": reason}, "model": "synthetic-test"}


class BridgeTests(unittest.TestCase):
    def test_synthetic_case_uses_existing_cash_lab_and_computes_all_options(self):
        from jcc_economy_lab import analyze_purchases
        with patch("jcc_decision_bridge.bridge.analyze_purchases", wraps=analyze_purchases) as cash:
            result = evaluate(demo_case())
        self.assertIsNone(cash.call_args.kwargs["rule"])
        self.assertEqual(result["status"], "synthetic_computed")
        options = {x["id"]: x for x in result["program"]["options"]}
        self.assertEqual((options["buy"]["gold_after"], options["buy"]["owned_after"]), (29, 3))
        self.assertFalse(options["buy"]["eligible_under_synthetic_objective"])
        self.assertEqual((options["hold"]["gold_after"], options["hold"]["owned_after"]), (31, 2))
        self.assertEqual((options["sell"]["gold_after"], options["sell"]["owned_after"]), (32, 1))
        self.assertEqual(result["program"]["best_under_synthetic_objective"], ["hold"])
        self.assertIsNone(result["program"]["interest_before"])
        self.assertIsNone(result["program"]["interest_after"])
        self.assertTrue(all(value == "unknown" for value in result["real_context"].values()))
        self.assertIsNone(result["action"])
        self.assertIsNone(result["formal_strategy_recommendation"])
        self.assertFalse(result["execution_eligible"])

    def test_unknown_real_or_invalid_input_never_calls_model(self):
        original = demo_case()
        variants = []
        for mutation in (lambda x: x.update(source_kind="replay"),
                         lambda x: x.update(gold=None),
                         lambda x: x["unit"].update(owned=None),
                         lambda x: x["unit"].update(buy_price=None),
                         lambda x: x["unit"].update(sell_value=None),
                         lambda x: x["unit"].update(buy_price=True),
                         lambda x: x["objective"].update(basis="unknown"),
                         lambda x: x["objective"].update(minimum_gold=-1),
                         lambda x: x.update(game_version="claimed")):
            case = deepcopy(original)
            mutation(case)
            variants.append(case)
        with patch("jcc_decision_bridge.bridge.local_model_comparison", side_effect=AssertionError("must not call")):
            for case in variants:
                with self.subTest(case=case):
                    output = run_comparison(case, model_call=lambda prompt: self.fail("model called"))
                    self.assertEqual(output["status"], "input_unknown")
                    self.assertEqual(output["model"]["status"], "not_called")
                    self.assertIsNone(output["action"])

    def test_no_eligible_option_and_unavailable_sell(self):
        case = demo_case()
        case["objective"]["minimum_gold"] = 34
        result = run_comparison(case, model_call=lambda prompt: self.fail("model called"))
        self.assertEqual(result["status"], "no_feasible_synthetic_option")
        self.assertEqual(result["model"]["status"], "not_called")
        case = demo_case()
        case["unit"]["owned"] = 0
        options = {x["id"]: x for x in evaluate(case)["program"]["options"]}
        self.assertFalse(options["sell"]["available_under_hypothesis"])
        self.assertIsNone(options["sell"]["gold_after"])

    def test_unaffordable_buy_never_gets_negative_cash(self):
        case = demo_case()
        case["unit"]["buy_price"] = 32
        options = {x["id"]: x for x in evaluate(case)["program"]["options"]}
        self.assertFalse(options["buy"]["available_under_hypothesis"])
        self.assertIsNone(options["buy"]["gold_after"])

    def test_model_comparison_sees_computed_options_not_expected_answer(self):
        seen = []
        def model(prompt):
            seen.append(prompt)
            return response()
        result = run_comparison(demo_case(), model_call=model)
        self.assertNotIn("best_under_synthetic_objective", seen[0])
        self.assertEqual([x["id"] for x in seen[0]["options"]], ["hold", "sell"])
        self.assertEqual(result["model"]["status"], "matches_synthetic_objective")
        self.assertEqual(result["model"]["choice"], "hold")
        self.assertIn("31金", result["model"]["explanation_zh"])
        self.assertIsNone(result["action"])

    def test_disagreeing_unknown_and_malformed_model_are_not_promoted(self):
        cases = [(response("buy"), "conflicts_with_program_facts"),
                 (response("unknown", "unknown"), "unknown"),
                 ({"reply": {"choice": "hold", "reason": "unverified assertion"}}, "invalid_response"),
                 ({"reply": {"choice": "hold", "reason": "unknown", "extra": "x"}}, "invalid_response")]
        for answer, expected in cases:
            with self.subTest(answer=answer):
                result = run_comparison(demo_case(), model_call=lambda prompt: answer)
                self.assertEqual(result["model"]["status"], expected)
                self.assertNotIn("explanation_zh", result["model"])
                self.assertIsNone(result["formal_strategy_recommendation"])
                self.assertIsNone(result["action"])

    def test_model_error_does_not_erase_program_facts(self):
        def unavailable(prompt):
            raise OSError("sensitive local error text")
        result = run_comparison(demo_case(), model_call=unavailable)
        self.assertEqual(result["model"]["status"], "unavailable_or_invalid")
        self.assertEqual(result["program"]["best_under_synthetic_objective"], ["hold"])
        self.assertNotIn("sensitive", json.dumps(result))

    def test_only_fixed_local_endpoints_and_preinstalled_models_are_used(self):
        with self.assertRaises(ValueError):
            _local_json("https://example.invalid")
        calls = []
        def local(path, payload=None, timeout=60):
            calls.append((path, payload))
            if path == "/api/tags":
                return {"models": [{"name": "local-test", "digest": "synthetic"}]}
            return {"message": {"content": json.dumps(response()["reply"])}, "eval_count": 2}
        program = evaluate(demo_case())["program"]
        prompt = {"source": "synthetic", "unit": program["unit_label"],
                  "minimum_gold": program["synthetic_minimum_gold"], "objective": program["objective"],
                  "options": [x for x in program["options"] if x["eligible_under_synthetic_objective"]]}
        with patch("jcc_decision_bridge.bridge._local_json", side_effect=local):
            result = local_model_comparison(prompt, model="local-test")
        self.assertEqual([x[0] for x in calls], ["/api/tags", "/api/chat"])
        self.assertEqual(calls[1][1]["format"]["properties"]["choice"]["enum"], ["hold", "sell", "unknown"])
        self.assertEqual(result["reply"]["choice"], "hold")
        with patch("jcc_decision_bridge.bridge._local_json", return_value={"models": []}) as request:
            with self.assertRaisesRegex(ValueError, "not installed"):
                local_model_comparison(prompt, model="missing")
            self.assertEqual(request.call_count, 1)
        prompt["options"].append(program["options"][0])
        with patch("jcc_decision_bridge.bridge._local_json") as request:
            with self.assertRaisesRegex(ValueError, "ineligible"):
                local_model_comparison(prompt, model="local-test")
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
