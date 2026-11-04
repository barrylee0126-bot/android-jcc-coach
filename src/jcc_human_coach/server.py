"""Loopback-only dynamic rendering endpoint for human coach cards."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit
import webbrowser

from .core import HumanCoachStore
from .source import JsonlTail, decode_json


WEB = Path(__file__).with_name("web")
ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}


class CoachServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, store: HumanCoachStore, source: JsonlTail, source_timeout_ms: int) -> None:
        self.store, self.source, self.source_timeout_ms = store, source, source_timeout_ms
        self.stop_event = threading.Event()
        self.state_lock = threading.RLock()
        super().__init__(("127.0.0.1", port), Handler)
        self.maintenance = threading.Thread(target=self._maintain, name="human-coach-local", daemon=True)

    def _maintain(self) -> None:
        while not self.stop_event.wait(.04):
            try:
                with self.state_lock:
                    self.source.poll()
                    self.store.tick(source_timeout_ms=self.source_timeout_ms)
            except Exception:
                with self.state_lock:
                    self.store.source_fault("source_unavailable")
                break

    def start_maintenance(self) -> None:
        self.maintenance.start()

    def server_close(self) -> None:
        self.stop_event.set()
        if self.maintenance.is_alive():
            self.maintenance.join(timeout=2)
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: CoachServer
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), "application/json")

    def _allowed(self, mutation: bool = False) -> bool:
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._json(403, {"error": "local_origin_required"})
            return False
        if mutation and self.headers.get("Origin") not in {"http://" + host for host in hosts}:
            self._json(403, {"error": "same_origin_required"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._allowed():
            return
        path = urlsplit(self.path).path
        if path in ASSETS:
            filename, content_type = ASSETS[path]
            self._send(200, (WEB / filename).read_bytes(), content_type)
        elif path == "/api/view":
            with self.server.state_lock:
                view = self.server.store.view()
            view["delivery"] = "loopback_browser_only"
            view["tablet_delivery_verified"] = False
            self._json(200, view)
        elif path == "/api/report":
            with self.server.state_lock:
                report = self.server.store.report()
            report["delivery"] = "loopback_browser_only"
            report["tablet_delivery_verified"] = False
            self._json(200, report)
        else:
            self._json(404, {"error": "unknown_endpoint"})

    def do_POST(self) -> None:
        if not self._allowed(mutation=True):
            return
        if urlsplit(self.path).path != "/api/browser-event":
            self._json(404, {"error": "unknown_endpoint"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096 or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError("body")
            payload = decode_json(self.rfile.read(length))
        except (ValueError, UnicodeError, RecursionError, OSError):
            self._json(400, {"error": "invalid_json_body"})
            return
        with self.server.state_lock:
            accepted = self.server.store.report_browser(payload, source_timeout_ms=self.server.source_timeout_ms)
        self._json(200 if accepted else 409, {"accepted": accepted})


def run_server(*, input_path: Path, port: int = 0, stream_id: str, clock_domain_id: str,
               source_timeout_ms: int = 1500) -> CoachServer:
    if not 0 <= port <= 65535 or not 100 <= source_timeout_ms <= 10_000:
        raise ValueError("invalid_server_options")
    store = HumanCoachStore(stream_id=stream_id, clock_domain_id=clock_domain_id)
    source = JsonlTail(input_path, store)
    return CoachServer(port, store, source, source_timeout_ms)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机动态玩家提示通路：仅跟随匿名 JSONL 新行，不读平板、不操作游戏。")
    parser.add_argument("--input", required=True, type=Path, help="已有普通文件；仅处理启动后追加的完整 JSONL 行")
    parser.add_argument("--stream-id", required=True, help="适配器展示流 ID")
    parser.add_argument("--clock-domain-id", required=True, help="本机单调时钟域声明")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--source-timeout-ms", type=int, default=1500)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(argv)
    try:
        server = run_server(input_path=args.input, port=args.port, stream_id=args.stream_id,
                            clock_domain_id=args.clock_domain_id, source_timeout_ms=args.source_timeout_ms)
    except (OSError, ValueError):
        parser.error("无法启动：输入必须是已有普通文件；端口、匿名流 ID、时钟域和超时须有效。")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(json.dumps({"status": "human_coach_loopback_started", "url": url,
                      "delivery": "loopback_browser_only", "tablet_delivery_verified": False,
                      "simulation": False, "execution_eligible": False}, ensure_ascii=False), flush=True)
    server.start_maintenance()
    try:
        if args.open:
            webbrowser.open(url)
        server.serve_forever(poll_interval=.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.source.close()
    return 0
