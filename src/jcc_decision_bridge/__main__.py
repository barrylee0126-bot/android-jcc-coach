"""Run the fixed synthetic-only comparison against an installed local model."""

import argparse
import json
import time

from .bridge import demo_case, run_comparison


def main(argv=None):
    parser = argparse.ArgumentParser(description="虚构买卖局面对照；不读取设备或真实对局。")
    parser.add_argument("--demo", action="store_true", required=True)
    parser.add_argument("--runs", type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args(argv)
    overall = time.perf_counter()
    results = [run_comparison(demo_case()) for _ in range(args.runs)]
    print(json.dumps({"synthetic_demo": True, "runs": results,
                      "total_process_seconds": round(time.perf_counter() - overall, 3)},
                     ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if all(x["model"]["status"] == "matches_synthetic_objective" for x in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
