"""Loopback-only, fixed-assets HTTP service. No device or upload endpoints."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
from urllib.parse import urlsplit
import webbrowser

from .journal import Journal
from .protocol import anonymous_id
from .source import JsonlTail, SCENARIOS, SyntheticSource, decode_event
from .store import TimedOverlayStore

WEB = Path(__file__).with_name("web")
ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}


class TimedOverlayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, store: TimedOverlayStore, source: SyntheticSource | JsonlTail) -> None:
        self.store, self.source = store, source
        self.stop_event = threading.Event()
        super().__init__(("127.0.0.1", port), Handler)
        self.maintenance = threading.Thread(target=self._maintain, name="timed-overlay-local", daemon=True)

    def _maintain(self) -> None:
        while not self.stop_event.wait(.025):
            try:
                self.source.poll()
                self.store.tick()
            except Exception:
                self.store.source_fault("source_worker_unavailable")
                break

    def start_maintenance(self) -> None:
        self.maintenance.start()

    def server_close(self) -> None:
        self.stop_event.set()
        if self.maintenance.is_alive():
            self.maintenance.join(timeout=2)
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: TimedOverlayServer
    protocol_version = "HTTP/1.0"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(2)

    def log_message(self, format: str, *args: object) -> None:
        pass

    def send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, status: int, value: dict) -> None:
        self.send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), "application/json")

    def allowed(self, mutation: bool = False) -> bool:
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_json(403, {"error": "local_origin_required"})
            return False
        if mutation and self.headers.get("Origin") not in {"http://" + host for host in hosts}:
            self.send_json(403, {"error": "same_origin_required"})
            return False
        return True

    def do_GET(self) -> None:
        if not self.allowed():
            return
        path = urlsplit(self.path).path
        if path in ASSETS:
            filename, content_type = ASSETS[path]
            self.send(200, (WEB / filename).read_bytes(), content_type)
        elif path == "/api/view":
            value = self.server.store.view()
            value["input_mode"] = "synthetic" if isinstance(self.server.source, SyntheticSource) else "jsonl_tail"
            value["playing"] = isinstance(self.server.source, SyntheticSource) and self.server.source.playing
            self.send_json(200, value)
        elif path == "/api/report":
            self.send_json(200, self.server.store.report())
        else:
            self.send_json(404, {"error": "unknown_endpoint"})

    def do_POST(self) -> None:
        if not self.allowed(mutation=True):
            return
        if self.path not in {"/api/demo", "/api/browser-event"}:
            self.send_json(404, {"error": "unknown_endpoint"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("body")
            payload = decode_event(self.rfile.read(length))
        except (ValueError, UnicodeError, RecursionError, OSError):
            self.send_json(400, {"error": "invalid_json_body"})
            return
        if self.path == "/api/browser-event":
            try:
                accepted = self.server.store.report_browser(payload)
            except (TypeError, KeyError, ValueError):
                accepted = False
            self.send_json(200 if accepted else 409, {"accepted": accepted})
        elif not isinstance(self.server.source, SyntheticSource):
            self.send_json(405, {"error": "demo_disabled_for_jsonl_source"})
        elif type(payload) is not dict or set(payload) != {"scenario"} or type(payload["scenario"]) is not str or payload["scenario"] not in SCENARIOS:
            self.send_json(400, {"error": "unknown_synthetic_scenario"})
        else:
            self.server.source.choose(payload["scenario"])
            self.send_json(200, {"accepted": True})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="模拟计时：本机短提示、撤回与分段计时；不提供游戏建议。")
    parser.add_argument("--port", type=int, default=0, help="本机端口，默认自动选择空闲端口")
    parser.add_argument("--input", type=Path, help="只跟随本机匿名 JSONL 新增行；已有内容不重放")
    parser.add_argument("--clock-domain-id", help="输入方声明的当前 Mac 同次开机单调时钟域；标签本身不是证明")
    parser.add_argument("--stream-id", help="匿名生产流 ID；变更须重新启动计时会话")
    parser.add_argument("--source-kind", choices=("synthetic", "replay", "device_readonly"), help="JSONL 来源分类，不改变模拟提示标识")
    parser.add_argument("--source-timeout-ms", type=int, default=1500, help="源静默撤回阈值，默认 1500ms；需持续心跳")
    parser.add_argument("--paint-timeout-ms", type=int, default=1200, help="等待浏览器绘制机会回报的阈值，默认 1200ms")
    parser.add_argument("--log", type=Path, help="新建匿名 JSONL 日志；默认在权限受限的临时目录；拒绝覆盖")
    parser.add_argument("--open", action="store_true", help="在浏览器打开本机页面")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("端口须为 0 到 65535。")
    declarations = (args.clock_domain_id, args.stream_id, args.source_kind)
    if args.input is not None and not all(declarations):
        parser.error("JSONL 输入须同时提供 --clock-domain-id、--stream-id 和 --source-kind。")
    if args.input is None and any(declarations):
        parser.error("来源声明仅用于 --input；内置演示自动创建独立合成会话。")
    if args.input is not None and not all(anonymous_id(x) for x in declarations[:2]):
        parser.error("时钟和流 ID 只接受最多 80 个 ASCII 字母、数字、点、冒号、短横线或下划线。")
    if not 250 <= args.source_timeout_ms <= 10_000 or not 100 <= args.paint_timeout_ms <= 10_000:
        parser.error("源静默阈值须在 250–10000ms；绘制回报阈值须在 100–10000ms。")
    log_path = args.log or Path(tempfile.mkdtemp(prefix="jcc-timed-overlay-")) / "events.jsonl"
    journal = None
    source = None
    server = None
    try:
        journal = Journal(log_path)
        store = TimedOverlayStore(
            clock_domain_id=args.clock_domain_id or "synthetic-clock-" + secrets.token_hex(8),
            stream_id=args.stream_id or "synthetic-stream-" + secrets.token_hex(8),
            source_kind=args.source_kind or "synthetic", journal=journal,
            source_timeout_ms=args.source_timeout_ms, paint_timeout_ms=args.paint_timeout_ms,
            in_process_clock=args.input is None,
        )
        source = JsonlTail(args.input, store) if args.input else SyntheticSource(store)
        server = TimedOverlayServer(args.port, store, source)
    except (OSError, ValueError):
        if source is not None:
            source.close()
        if journal is not None:
            journal.close()
        print("无法启动模拟计时。请检查空闲端口、输入声明及文件；日志须是尚不存在的新文件。", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"模拟计时已启动：{url}\n紧凑页面：{url}?compact=1\n匿名日志：{log_path}\n所有提示均为显示测试。按 Ctrl+C 停止。", flush=True)
    server.start_maintenance()
    try:
        if args.open:
            webbrowser.open(url)
        server.serve_forever(poll_interval=.1)
    except KeyboardInterrupt:
        print("\n模拟计时已停止，日志已保留。", flush=True)
    finally:
        server.server_close()
        source.close()
        store.close()
    return 0
