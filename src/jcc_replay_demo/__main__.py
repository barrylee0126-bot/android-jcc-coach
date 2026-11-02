"""User entry point; console output contains only fixed codes and counts."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .inspector import InspectorError, default_profile, run_inspection


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本机只读历史回放识别检查；不操作设备，不提供实战建议。")
    parser.add_argument("--profile", type=Path, default=default_profile(), help="项目外私有配置；含固定开发样本清单与显式区域。")
    parser.add_argument("--no-open", action="store_true", help="生成本机报告后不自动打开。")
    args = parser.parse_args(argv)
    try:
        page, summary = run_inspection(args.profile)
    except KeyboardInterrupt:
        print(json.dumps({"status": "cancelled", "reason": "user_interrupted", "accuracy": None}))
        return 130
    except InspectorError as exc:
        print(json.dumps({"status": "no_results", "reason": str(exc), "accuracy": None}))
        return 2
    except Exception:
        print(json.dumps({"status": "no_results", "reason": "private_inspection_failed", "accuracy": None}))
        return 2
    opened = False
    if not args.no_open and sys.platform == "darwin":
        try:
            opened = subprocess.run(["/usr/bin/open", page.as_uri()], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=10, check=False).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    print(json.dumps({"status": "private_report_created", **summary,
                      "browser_open_requested": opened}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
