"""Only fixed counts and codes on stdout; private paths stay in local files."""

import argparse
import json
from pathlib import Path

from .core import run_review
from .private import ReviewError


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读生成本局私有离线回放页；不接设备，不运行 OCR，不联网。")
    parser.add_argument("--manifest", type=Path, required=True, help="项目外的固定私有输入清单")
    args = parser.parse_args(argv)
    try:
        _, summary = run_review(args.manifest)
    except ReviewError as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}))
        return 2
    except Exception:
        print(json.dumps({"status": "rejected", "reason": "private_review_failed"}))
        return 2
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
