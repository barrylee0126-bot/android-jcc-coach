"""CLI for explicit hypotheses or inline JSON; no file/device/network access."""

import argparse
import json

from .assumptions import source_catalog, ten_gold_five_cap_hypothesis
from .core import GoldCandidate, analyze_purchases, analyze_request
from .demo import demo_cases


def _reject_constant(value: str):
    raise ValueError("Non-finite JSON numbers are unsupported")


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="金币取舍条件计算；所有数字均未确认为本局实况。")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="展示纯合成用例")
    mode.add_argument("--gold", type=int, help="显式提供一个手工假设金币值")
    mode.add_argument("--request-json", help="内联JSON候选请求；不读取文件")
    parser.add_argument("--prices", type=int, nargs="+", help="逐项独立比较的假设购买价格")
    parser.add_argument("--rule", choices=["historical-10-5"], help="显式选择有来源但未绑定本局的假设；省略则不算利息")
    parser.add_argument("--json", action="store_true", help="输出结构化JSON")
    args = parser.parse_args(argv)
    if args.gold is None and (args.prices is not None or args.rule is not None):
        parser.error("--prices 与 --rule 仅供 --gold 使用；JSON请求必须自行声明规则。")
    if args.gold is not None and args.prices is None:
        parser.error("--gold 需要 --prices。")
    if args.demo:
        cases = demo_cases()
    elif args.gold is not None:
        rule = ten_gold_five_cap_hypothesis() if args.rule else None
        cases = [{"label": "手工输入假设；非已确认实况",
                  "analysis": analyze_purchases(
                      GoldCandidate(args.gold, "manual_hypothesis", "manual:cli"), args.prices, rule)}]
    else:
        try:
            request = json.loads(args.request_json, parse_constant=_reject_constant,
                                 object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            parser.error("内联JSON无效；重复字段、NaN和Infinity均不接受。")
        cases = [{"label": "外部候选条件计算；非已确认实况", "analysis": analyze_request(request)}]

    output = {"label": "条件计算，不判断最优买卖，不执行操作。", "cases": cases,
              "source_catalog": source_catalog()}
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(output["label"])
        for case in cases:
            print("\n" + case["label"])
            for row in case["analysis"]["comparisons"]:
                print("  " + row["text_zh"])
            if not case["analysis"]["comparisons"]:
                for issue in case["analysis"]["issues"]:
                    print("  " + issue["message_zh"])
        print("\n共同前提：规则、商店价格及回合结算均未与本局绑定；其他收支与战力价值未计入。")
        print("10/5假设来源（历史社区用户发帖，非官方）：" + source_catalog()[0]["url"])
    return 0 if any(case["analysis"]["status"] in ("conditional", "cash_only", "partial")
                    for case in cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
