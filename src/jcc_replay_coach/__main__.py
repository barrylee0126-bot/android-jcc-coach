"""CLI for a private replay-only conditional explanation page."""

from __future__ import annotations

import argparse
import json

from .core import ReplayCoachError, build_replay_coach


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Private replay condition explanation; never emits game advice or actions.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        report = build_replay_coach(args.manifest, args.output)
    except ReplayCoachError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc), "automatic_action": None}))
        return 1
    print(json.dumps({"status": "private_page_created", "frames": len(report["frames"]),
                      "states": [frame["state"] for frame in report["frames"]],
                      "automatic_action": None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
