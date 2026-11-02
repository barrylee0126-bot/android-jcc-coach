"""Small offline synthetic comparison; never accepts game observations."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from jcc_economy_lab import GoldCandidate, HypotheticalPrice, MAX_EXACT_INTEGER, analyze_purchases


MODEL_NAME = "qwen3-vl:4b-instruct"
LOCAL_API = "http://127.0.0.1:11434"
CHOICES = ("buy", "hold", "sell", "unknown")
MODEL_FORMAT = {
    "type": "object",
    "properties": {
        "choice": {"type": "string", "enum": list(CHOICES)},
        "reason": {"type": "string", "enum": ["meets_reserve_and_maximizes_copies", "unknown"]},
    },
    "required": ["choice", "reason"],
    "additionalProperties": False,
}


def demo_case() -> dict:
    """Fresh, entirely imaginary input; no current game rules or identities."""
    return {
        "source_kind": "synthetic",
        "gold": 31,
        "unit": {"label": "虚构棋子甲", "owned": 2, "buy_price": 2, "sell_value": 1},
        "objective": {"minimum_gold": 30, "priority": "maximize_owned", "basis": "synthetic_hypothesis"},
    }


def _number(value) -> bool:
    return type(value) is int and 0 <= value <= MAX_EXACT_INTEGER


def _base() -> dict:
    return {
        "schema_version": "synthetic-decision-bridge-v1",
        "status": "input_unknown",
        "synthetic_only": True,
        "real_context": {
            "mode": "unknown", "game_version": "unknown", "unit_identity_and_ownership": "unknown",
            "actual_unit_price_and_sale_value": "unknown", "applicable_strategy_source": "unknown",
        },
        "real_rule_verified": False,
        "formal_strategy_recommendation": None,
        "action": None,
        "execution_eligible": False,
        "program": None,
        "model": {"status": "not_called"},
    }


def _unknown(result: dict, reason: str) -> dict:
    result["reason"] = reason
    return result


def evaluate(case) -> dict:
    """Validate synthetic facts and compare options without invoking a model.

    All prices, quantities, ownership, and goals are explicit fake inputs.
    Missing, inconsistent, out-of-range, or non-synthetic inputs fail closed.
    """
    result = _base()
    if type(case) is not dict or set(case) != {"source_kind", "gold", "unit", "objective"}:
        return _unknown(result, "case_shape_unknown")
    if case["source_kind"] != "synthetic":
        return _unknown(result, "real_input_not_supported")
    unit, objective = case["unit"], case["objective"]
    if type(unit) is not dict or set(unit) != {"label", "owned", "buy_price", "sell_value"}:
        return _unknown(result, "unit_ownership_or_prices_unknown")
    if (type(unit["label"]) is not str or not 0 < len(unit["label"].strip()) <= 60
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in unit["label"])):
        return _unknown(result, "unit_identity_unknown")
    if type(objective) is not dict or set(objective) != {"minimum_gold", "priority", "basis"}:
        return _unknown(result, "objective_or_source_unknown")
    if objective["priority"] != "maximize_owned" or objective["basis"] != "synthetic_hypothesis":
        return _unknown(result, "objective_or_source_unknown")
    if not all(_number(x) for x in (case["gold"], unit["owned"], unit["buy_price"],
                                    unit["sell_value"], objective["minimum_gold"])):
        return _unknown(result, "gold_ownership_price_or_reserve_unknown")
    gold, owned, price, sale, reserve = (case["gold"], unit["owned"], unit["buy_price"],
                                         unit["sell_value"], objective["minimum_gold"])
    if gold + sale > MAX_EXACT_INTEGER or owned == MAX_EXACT_INTEGER:
        return _unknown(result, "arithmetic_out_of_range")
    cash = analyze_purchases(GoldCandidate(gold, "synthetic", "synthetic:decision-bridge"),
                             [HypotheticalPrice(price, label="虚构买入价")], rule=None)
    row = cash["comparisons"][0]
    if row["status"] not in ("cash_only", "insufficient_candidate_gold"):
        return _unknown(result, "economy_input_unknown")
    after_buy = row["gold_after_hypothetical_purchase"]

    def option(key, post_gold, post_copies, available):
        meets_reserve = post_gold >= reserve if available else None
        return {"id": key, "available_under_hypothesis": available, "gold_after": post_gold,
                "owned_after": post_copies, "meets_synthetic_reserve": meets_reserve,
                "eligible_under_synthetic_objective": bool(available and meets_reserve)}

    options = [
        option("buy", after_buy, owned + 1 if after_buy is not None else None, after_buy is not None),
        option("hold", gold, owned, True),
        option("sell", gold + sale if owned else None, owned - 1 if owned else None, owned > 0),
    ]
    eligible = [x for x in options if x["eligible_under_synthetic_objective"]]
    winners = ([x["id"] for x in eligible if x["owned_after"] == max(y["owned_after"] for y in eligible)]
               if eligible else [])
    result["status"] = "synthetic_computed" if winners else "no_feasible_synthetic_option"
    result["reason"] = None if winners else "reserve_cannot_be_met"
    result["program"] = {
        "unit_label": unit["label"], "starting_gold": gold, "starting_owned": owned,
        "buy_price_assumption": price, "sell_value_assumption": sale,
        "synthetic_minimum_gold": reserve, "objective": "maximize_owned_after_reserve",
        "objective_source": "synthetic_hypothesis_not_a_game_strategy",
        "options": options, "best_under_synthetic_objective": winners,
        "buy_calculation_source": "jcc_economy_lab.analyze_purchases(rule=None)",
        "interest_before": None, "interest_after": None,
    }
    return result


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _local_json(path: str, payload=None, timeout=60):
    if path not in ("/api/tags", "/api/chat"):
        raise ValueError("unsupported endpoint")
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(LOCAL_API + path, data=data,
                                     headers={"Content-Type": "application/json"})
    # A literal loopback address and a disabled proxy prevent a remote proxy route.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.load(response, object_pairs_hook=_json_object)


def local_model_comparison(prompt: dict, model: str = MODEL_NAME) -> dict:
    """Call an already-installed model at the fixed local endpoint only."""
    if type(model) is not str or not model or len(model) > 120:
        raise ValueError("invalid model name")
    if (type(prompt) is not dict or set(prompt) != {"source", "unit", "minimum_gold", "objective", "options"}
            or prompt["source"] != "synthetic" or prompt["objective"] != "maximize_owned_after_reserve"
            or not _number(prompt["minimum_gold"]) or type(prompt["unit"]) is not str
            or not 0 < len(prompt["unit"].strip()) <= 60 or type(prompt["options"]) is not list
            or not 1 <= len(prompt["options"]) <= 3):
        raise ValueError("invalid synthetic model input")
    option_ids = []
    for option in prompt["options"]:
        if (type(option) is not dict or set(option) != {"id", "available_under_hypothesis", "gold_after",
                                                        "owned_after", "meets_synthetic_reserve",
                                                        "eligible_under_synthetic_objective"}
                or option["id"] not in CHOICES[:3] or option["id"] in option_ids
                or option["available_under_hypothesis"] is not True
                or option["meets_synthetic_reserve"] is not True
                or option["eligible_under_synthetic_objective"] is not True
                or not _number(option["gold_after"]) or not _number(option["owned_after"])
                or option["gold_after"] < prompt["minimum_gold"]):
            raise ValueError("ineligible synthetic model option")
        option_ids.append(option["id"])
    inventory = _local_json("/api/tags")
    models = inventory.get("models") if isinstance(inventory, dict) else None
    if not isinstance(models, list):
        raise ValueError("local model inventory unavailable")
    installed = next((x for x in models if isinstance(x, dict) and x.get("name") == model), None)
    if installed is None:
        raise ValueError("model not installed locally")
    permitted_format = {**MODEL_FORMAT, "properties": {**MODEL_FORMAT["properties"],
                        "choice": {"type": "string", "enum": option_ids + ["unknown"]}}}
    payload = {
        "model": model, "stream": False, "think": False, "format": permitted_format,
        "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 100}, "keep_alive": "5m",
        "messages": [
            {"role": "system", "content": "所有输入都是虚构假设。先仅考虑满足最低金币的选项，"
             "再选择持有棋子数最多的选项。事实不足或无满足者填unknown。只返回指定JSON。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))},
        ],
    }
    start = time.perf_counter()
    body = _local_json("/api/chat", payload)
    wall = time.perf_counter() - start
    if not isinstance(body, dict):
        raise ValueError("invalid local response")
    text = body.get("message", {}).get("content") if isinstance(body.get("message"), dict) else None
    if type(text) is not str or len(text) > 4096:
        raise ValueError("invalid local response")
    reply = json.loads(text, object_pairs_hook=_json_object)
    return {
        "reply": reply, "model": model, "model_digest": installed.get("digest"),
        "wall_seconds": round(wall, 3),
        "model_load_seconds": round(body.get("load_duration", 0) / 1e9, 3),
        "prompt_seconds": round(body.get("prompt_eval_duration", 0) / 1e9, 3),
        "generation_seconds": round(body.get("eval_duration", 0) / 1e9, 3),
        "generated_tokens": body.get("eval_count"),
    }


def run_comparison(case, *, model_call=local_model_comparison) -> dict:
    """Compare a model's bounded selection against independently computed facts."""
    started = time.perf_counter()
    result = evaluate(case)
    result["program_seconds"] = round(time.perf_counter() - started, 6)
    if result["status"] != "synthetic_computed":
        return result
    program = result["program"]
    # The program's expected winner is withheld. Ineligible options remain in
    # the program report but are not presented as model choices.
    eligible = [x for x in program["options"] if x["eligible_under_synthetic_objective"]]
    prompt = {"source": "synthetic", "unit": program["unit_label"],
              "minimum_gold": program["synthetic_minimum_gold"],
              "objective": program["objective"], "options": eligible}
    try:
        measured = model_call(prompt)
    except (OSError, ValueError, TypeError, KeyError, urllib.error.URLError):
        result["model"] = {"status": "unavailable_or_invalid"}
        return result
    if not isinstance(measured, dict):
        result["model"] = {"status": "invalid_response"}
        return result
    answer = measured.get("reply")
    if (type(answer) is not dict or set(answer) != {"choice", "reason"}
            or answer.get("choice") not in CHOICES
            or answer.get("reason") not in ("meets_reserve_and_maximizes_copies", "unknown")):
        result["model"] = {"status": "invalid_response"}
        return result
    report = {k: measured.get(k) for k in ("model", "model_digest", "wall_seconds", "model_load_seconds",
                                             "prompt_seconds", "generation_seconds", "generated_tokens")}
    report["choice"] = answer["choice"]
    if answer["choice"] == "unknown" or answer["reason"] == "unknown":
        report["status"] = "unknown"
    elif answer["choice"] not in program["best_under_synthetic_objective"]:
        report["status"] = "conflicts_with_program_facts"
    else:
        report["status"] = "matches_synthetic_objective"
        chosen = next(x for x in program["options"] if x["id"] == answer["choice"])
        label = {"buy": "买入", "hold": "保留", "sell": "卖出"}[answer["choice"]]
        report["explanation_zh"] = (f"在虚构目标下，{label}后假设有{chosen['gold_after']}金、"
                                    f"{chosen['owned_after']}张，满足至少{program['synthetic_minimum_gold']}金，"
                                    "且在可选项中保留的张数最多。")
    result["model"] = report
    return result
