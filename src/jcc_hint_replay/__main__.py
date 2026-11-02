"""CLI for a private, offline lifecycle replay page."""

from __future__ import annotations

import argparse
import json

from .core import HintReplayError, build_replay


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Private replay-only capacity withdrawal page; no game advice or action.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = build_replay(args.manifest, args.output)
    except HintReplayError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc), "advice_eligible": False, "automatic_action": None}))
        return 1
    print(json.dumps({"status": "ok", "states": [row["state"] for row in result["frames"]],
                      "advice_eligible": False, "automatic_action": None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
