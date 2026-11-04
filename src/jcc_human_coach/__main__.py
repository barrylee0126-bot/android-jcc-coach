"""Create a private, standalone human-coach card demo."""

from __future__ import annotations

import argparse
import json

from .core import CoachContractError, build_demo
from . import server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成离线演示或启动本机动态提示；不读取平板或输出游戏动作。")
    parser.add_argument("--demo-output", help="项目外、权限受限且尚不存在的输出目录")
    parser.add_argument("--input", help="启动动态 JSONL 输入通路（仅追加行）")
    parser.add_argument("--stream-id", help="动态展示流 ID")
    parser.add_argument("--clock-domain-id", help="动态输入本机时钟域")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--source-timeout-ms", type=int, default=1500)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(argv)
    if bool(args.demo_output) == bool(args.input):
        parser.error("必须二选一：--demo-output 或 --input。")
    if args.input:
        if not args.stream_id or not args.clock_domain_id:
            parser.error("动态输入需要 --stream-id 和 --clock-domain-id。")
        return server.main(["--input", args.input, "--stream-id", args.stream_id, "--clock-domain-id", args.clock_domain_id,
                            "--port", str(args.port), "--source-timeout-ms", str(args.source_timeout_ms)]
                           + (["--open"] if args.open else []))
    try:
        result = build_demo(args.demo_output)
    except CoachContractError as error:
        print(json.dumps({"status": "blocked", "reason": str(error), "execution_eligible": False}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "private_demo_created", **result, "execution_eligible": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
