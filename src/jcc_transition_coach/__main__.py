"""CLI for explicit local transition comparisons."""

import argparse
import json
import sys
from pathlib import Path

from .core import compare_json
from .synthetic import request


def main() -> int:
    parser = argparse.ArgumentParser(description="比较版本绑定的阵容转型候选；不操作设备。")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--demo", action="store_true")
    group.add_argument("--print-synthetic-request", action="store_true")
    group.add_argument("--stdin", action="store_true")
    group.add_argument("--input", type=Path)
    args = parser.parse_args()
    if args.print_synthetic_request:
        print(json.dumps(request(), ensure_ascii=False, sort_keys=True))
        return 0
    if args.demo:
        result = compare_json(json.dumps(request(), ensure_ascii=False))
    elif args.stdin:
        result = compare_json(sys.stdin.read())
    else:
        try:
            result = compare_json(args.input.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            result = {"format_version": "jcc-transition-coach/v1", "status": "unknown", "issues": [
                {"code": "input_unreadable", "path": "$.input", "message_zh": "输入文件无法读取；未比较转型。"}
            ]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "evaluated" else 2


if __name__ == "__main__":
    raise SystemExit(main())
