"""Explicit synthetic and private offline-replay commands; no tablet access."""

import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="10H→14B独立桥接；合成或私有回放，提示仅用于模拟显示。")
    sub = parser.add_subparsers(dest="mode", required=True)
    synthetic = sub.add_parser("synthetic", help="可控时钟；真实调用两侧公开模块")
    stream = sub.add_parser("stream", help="独立生产进程→桥接→运行中的消费服务及HTTP状态")
    serve = sub.add_parser("serve", help="同一流式链路，提供原14B浏览器页面并自动停止")
    replay = sub.add_parser("replay", help="私有离线连续帧与本机OCR；回放候选和模拟显示")
    replay.add_argument("--manifest", required=True, type=Path, help="项目外连续帧清单")
    replay.add_argument("--profile", required=True, type=Path, help="项目外金币区域配置")
    replay.add_argument("--start-delay", type=float, default=5)
    replay.add_argument("--port", type=int, default=0)
    replay.add_argument("--report", required=True, type=Path, help="项目外新建的私有计时报告")
    producer = sub.add_parser("_producer", help=argparse.SUPPRESS)
    for command in (stream, serve, producer):
        command.add_argument("--cycles", type=int, choices=range(1, 5), default=1)
        command.add_argument("--start-delay", type=float, default=1 if command is serve else .3)
        command.add_argument("--processing-ms", type=int, default=80)
    for command in (synthetic, stream, serve):
        command.add_argument("--report", type=Path, help="新建JSON报告，拒绝覆盖；不写原始画面")
    for command in (stream, serve):
        command.add_argument("--seconds", type=float, default=12)
        command.add_argument("--port", type=int, default=0)
    producer.add_argument("--nonce", required=True)
    producer.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    if args.mode in {"stream", "serve", "_producer"} and not 0 <= args.start_delay <= 10:
        parser.error("start-delay须为0到10秒。")
    if args.mode in {"stream", "serve", "_producer"} and not 20 <= args.processing_ms <= 300:
        parser.error("processing-ms须为20到300毫秒；它是模拟工作时间。")
    if args.mode in {"stream", "serve"} and (not 1 <= args.seconds <= 60 or not 0 <= args.port <= 65535):
        parser.error("seconds须为1到60秒，port须为0到65535。")
    if args.mode == "_producer":
        from .runtime import produce
        return produce(nonce=args.nonce, run_id=args.run_id, cycles=args.cycles,
                       start_delay=args.start_delay, processing_ms=args.processing_ms)
    if args.mode == "replay":
        from jcc_realtime_probe.__main__ import _new_private_output
        try:
            with _new_private_output(args.report) as file:
                from .replay import run_replay
                result = run_replay(args.manifest, args.profile, port=args.port, start_delay=args.start_delay)
                file.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            print(json.dumps({"recordings_read": result["recordings_read"],
                              "opportunities": result["bridge"]["opportunities"],
                              "dom_reports": result["read_start_to_dom_report_received_ms"],
                              "paint_reports": result["read_start_to_paint_report_received_ms"],
                              "error_types": result["error_types"]}, ensure_ascii=False), flush=True)
            return 1 if result["error_types"] or result["bridge"]["fault"] else 0
        except (OSError, ValueError, RuntimeError):
            print("离线回放未通过；未访问平板。", file=sys.stderr)
            return 1
    # Reserve an optional artifact before doing any work; never overwrite it.
    file = None
    try:
        if args.report is not None:
            file = args.report.open("x", encoding="utf-8")
        if args.mode == "synthetic":
            from .synthetic import run_synthetic
            result = run_synthetic()
        else:
            from .runtime import run_stream
            result = run_stream(serve=args.mode == "serve", seconds=args.seconds, port=args.port,
                                cycles=args.cycles, start_delay=args.start_delay, processing_ms=args.processing_ms)
        payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if file is not None:
            file.write(payload + "\n")
        print(payload)
        return 0 if result.get("producer_to_consumer_streaming_verified", True) else 1
    except (OSError, ValueError, RuntimeError):
        print("合成桥接未通过；没有访问平板。请核对公开接口、时钟证明及新报告路径。", file=sys.stderr)
        return 1
    finally:
        if file is not None:
            file.close()


raise SystemExit(main())
