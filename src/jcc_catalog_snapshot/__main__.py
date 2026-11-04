"""JSON-only CLI; deliberately has no URL fetching or cache writes."""

import argparse
import json
from pathlib import Path
import sys

from .core import _issue, _unknown, analyze_json, analyze_request
from .synthetic import synthetic_request


def main() -> int:
    parser = argparse.ArgumentParser(description="从本地显式快照建立候选映射；不联网、不验证客户端版本。")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true")
    source.add_argument("--input", type=Path)
    source.add_argument("--stdin", action="store_true")
    source.add_argument("--print-synthetic-request", action="store_true")
    args = parser.parse_args()
    if args.print_synthetic_request:
        print(json.dumps(synthetic_request(), ensure_ascii=False, indent=2)); return 0
    if args.demo:
        result = analyze_request(synthetic_request())
    else:
        try:
            result = analyze_json(sys.stdin.read() if args.stdin else args.input.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            result = _unknown([_issue("input_unreadable", "$", "指定输入无法读取；未寻找替代来源。")])
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result["status"] == "candidate" else 2


if __name__ == "__main__":
    raise SystemExit(main())
