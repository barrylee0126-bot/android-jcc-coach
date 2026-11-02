"""Create a private, standalone human-coach card demo."""

from __future__ import annotations

import argparse
import json

from .core import CoachContractError, build_demo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成私有回放例子玩家提示演示；不读取平板或输出游戏动作。")
    parser.add_argument("--demo-output", required=True, help="项目外、权限受限且尚不存在的输出目录")
    args = parser.parse_args(argv)
    try:
        result = build_demo(args.demo_output)
    except CoachContractError as error:
        print(json.dumps({"status": "blocked", "reason": str(error), "execution_eligible": False}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "private_demo_created", **result, "execution_eligible": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
