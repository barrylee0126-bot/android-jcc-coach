"""Foreground launcher; explicit workspace and explicit sample-kind required."""

import argparse
import sys
import webbrowser

from src.jcc_samples import WorkflowError
from .server import AnnotationServer
from .synthetic import make_synthetic
from .workspace import AnnotationWorkspace


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "参数不完整或无效。请使用 --help 查看用法。\n")


def main(argv=None) -> int:
    parser = SafeParser(description="私有样本工作台：仅在本机运行，不采集画面、不识别、不改变分集。")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    demo = sub.add_parser("make-synthetic", help="在明确的新私有目录创建两张自生成合成图，全标签未知")
    demo.add_argument("--private-dir", required=True)
    serve = sub.add_parser("serve", help="打开已有的私有 SampleStore 工作区")
    serve.add_argument("--private-dir", required=True)
    serve.add_argument("--sample-kind", choices=("real", "synthetic"), required=True)
    serve.add_argument("--port", type=int, default=0, help="本机端口；默认 0 自动分配，不支持其他监听地址")
    serve.add_argument("--open", action="store_true", help="在浏览器打开本实例的专用会话链接")
    args = parser.parse_args(argv)
    try:
        if args.command == "make-synthetic":
            summary = make_synthetic(args.private_dir)
            print(f"已创建 {summary['synthetic_samples']} 张合成图；所有标签未知，真实样本为 0。")
            return 0
        if not 0 <= args.port <= 65535:
            parser.error("invalid port")
        server = AnnotationServer(AnnotationWorkspace(args.private_dir, sample_kind=args.sample_kind), port=args.port)
    except WorkflowError as error:
        print("工作区未打开（" + error.code + "）。请检查私有目录、类型、权限和完整性。", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError):
        print("本机工作台无法启动，请检查目录、依赖和端口。", file=sys.stderr)
        return 2
    try:
        if args.open:
            if webbrowser.open(server.session_url):
                print("已在浏览器打开专用会话。", flush=True)
            else:
                print("请在本机浏览器打开：" + server.session_url, flush=True)
        else:
            print("请在本机浏览器打开：" + server.session_url, flush=True)
        print("前台服务；页面内“关闭服务”或 Ctrl+C 停止。页面断开心跳 90 秒后自动退出。", flush=True)
        server.run()
    except KeyboardInterrupt:
        server.stopping.set()
    finally:
        server.server_close()
    print("私有样本工作台已停止。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
