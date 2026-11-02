"""Explicit-input JSON CLI. No catalog lookup, device reads, or file writes."""

import argparse
import json
from pathlib import Path
import sys

from .core import _issue, _unknown, analyze_json, analyze_request
from .synthetic import synthetic_request


def main() -> int:
    parser = argparse.ArgumentParser(description="按显式配方及库存列出各自可行的备选，不作装备推荐。")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true", help="使用明确虚构的共用库存示例")
    source.add_argument("--input", type=Path, help="读取调用方明确指定的 JSON 请求")
    source.add_argument("--stdin", action="store_true", help="从标准输入读取 JSON 请求")
    source.add_argument("--print-synthetic-request", action="store_true", help="仅输出虚构请求，供接口联调")
    args = parser.parse_args()
    if args.print_synthetic_request:
        print(json.dumps(synthetic_request(), ensure_ascii=False, indent=2))
        return 0
    if args.demo:
        result = analyze_request(synthetic_request())
    else:
        try:
            text = sys.stdin.read() if args.stdin else args.input.read_text(encoding="utf-8")
            result = analyze_json(text)
        except (OSError, UnicodeError):
            result = _unknown([_issue("input_unreadable", "$", "指定输入无法读取；不替换为空库存。")])
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result["status"] == "evaluated" else 2


if __name__ == "__main__":
    raise SystemExit(main())
