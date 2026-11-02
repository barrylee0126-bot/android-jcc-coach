"""Offline JSON entry point. No frame loader, device, model or network access."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .engine import HypothesisSession, ReplayReviewClock, _base
from .contract import issue
from .fixtures import demo, example_request


MAX_INPUT_BYTES = 256 * 1024


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError("Non-finite JSON number")


def _read_request(path: str) -> dict:
    if path == "-":
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        with Path(path).open("rb") as source:
            raw = source.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Input too large")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="假设／仅供复盘的隔离条件推理；不生成实战指令。")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--demo", action="store_true", help="运行纯合成正例、反例、遮挡与过期演示（默认）")
    group.add_argument("--example", choices=("synthetic_fixture", "replay_candidate"), help="打印纯构造的输入模板")
    group.add_argument("--input", metavar="JSON", help="显式读取本地候选 JSON；- 表示标准输入")
    parser.add_argument("--review-at-ms", type=float, help="显式离线复盘截点，不是局面组装时间或当前对局时间")
    parser.add_argument("--max-frame-age-ms", type=int, help="显式复盘帧年龄上限；1..30000，未经过实机校准")
    parser.add_argument("--max-result-ms", type=int, help="本次结果寿命上限；1..30000，不能延长原依赖期限")
    args = parser.parse_args(argv)
    limits = (args.review_at_ms, args.max_frame_age_ms, args.max_result_ms)
    if args.input is None:
        if any(value is not None for value in limits):
            parser.error("复盘时间和期限参数只用于 --input。")
        result = example_request(replay_candidate=args.example == "replay_candidate") if args.example else demo()
    else:
        if any(value is None for value in limits):
            parser.error("--input 必须显式提供 --review-at-ms、--max-frame-age-ms 和 --max-result-ms。")
        try:
            request = _read_request(args.input)
            # This is explicitly a replay coordinate selected by the caller. It
            # is never inferred from assembled_review_ms or historical wall time.
            clock_id = request.get("binding", {}).get("review_clock_id") if type(request) is dict else None
            clock = ReplayReviewClock(clock_id, args.review_at_ms)
            session = HypothesisSession(clock=clock, max_frame_age_ms=args.max_frame_age_ms,
                                        max_result_ms=args.max_result_ms)
            result = session.submit(request)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError, OverflowError):
            result = _base("refused", [issue("local_input_unreadable", "本地输入无法读取或解析，或复盘时钟/期限无效；未执行候选分析。")])
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
