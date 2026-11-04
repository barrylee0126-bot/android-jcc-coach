"""No-I/O CLI for the synthetic team-planner example."""

import argparse
import json
import sys

from .core import plan, plan_json, plan_partial_trait_hint_json
from .fixtures import synthetic_request


def main() -> int:
    parser = argparse.ArgumentParser(description="计算调用方明确局面的羁绊/转型/装备候选；不作自动操作。")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--demo", action="store_true", help="使用纯虚构、版本绑定示例")
    group.add_argument("--stdin", action="store_true", help="从标准输入读取明确 JSON")
    group.add_argument("--partial-stdin", action="store_true", help="从标准输入读取局部观察 JSON；仅生成条件提示")
    args = parser.parse_args()
    result = (plan(synthetic_request()) if args.demo else plan_json(sys.stdin.read()) if args.stdin
              else plan_partial_trait_hint_json(sys.stdin.read()))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result["status"] in {"evaluated", "evaluated_partial_observation"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
