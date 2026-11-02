"""Loopback-only HTTP delivery of packaged assets and named synthetic fixtures."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
import webbrowser

from .demo import DemoController, SCENARIOS


ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/overlay.css": ("overlay.css", "text/css; charset=utf-8"),
    "/overlay.js": ("overlay.js", "text/javascript; charset=utf-8"),
}
WEB = Path(__file__).with_name("web")


class OverlayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, controller: DemoController | None = None) -> None:
        self.controller = controller if controller is not None else DemoController()
        super().__init__(("127.0.0.1", port), OverlayHandler)


class OverlayHandler(BaseHTTPRequestHandler):
    server: OverlayServer
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, value: dict) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), "application/json; charset=utf-8")

    def _allowed(self, *, mutation: bool = False) -> bool:
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._json(403, {"message": "请在本机演示页面操作。"})
            return False
        if mutation and self.headers.get("Origin") not in {f"http://{host}" for host in hosts}:
            self._json(403, {"message": "请在本机演示页面操作。"})
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
            try:
                self._json(200, self.server.controller.view())
            except Exception:
                self._json(503, {"message": "演示暂时不可用，请重新开始。"})
        else:
            self._json(404, {"message": "没有这个演示页面。"})

    def do_POST(self) -> None:
        if not self._allowed(mutation=True):
            return
        if self.path != "/api/demo":
            self._json(404, {"message": "没有这个演示入口。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_024 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if type(payload) is not dict or set(payload) != {"scenario"} or payload["scenario"] not in {*SCENARIOS, "play"}:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            self._json(400, {"message": "请选择页面中的一种演示状态。"})
            return
        try:
            self._json(200, self.server.controller.choose(payload["scenario"]))
        except Exception:
            self._json(503, {"message": "演示暂时不可用，请重新开始。"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机短提示原型：仅使用虚构的模拟演示数据。")
    parser.add_argument("--port", type=int, default=8765, help="本机端口，默认 8765；0 代表自动选择空闲端口")
    parser.add_argument("--open", action="store_true", help="启动后在浏览器打开本机演示页面")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65_535:
        parser.error("端口需在 0 到 65535 之间。")
    try:
        server = OverlayServer(args.port)
    except OSError:
        print("无法启动本机演示。可换一个端口，或用 --port 0 自动选择。", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"模拟演示已启动：{url}\n小窗：{url}?compact=1\n只含虚构数据；按 Ctrl+C 停止。", flush=True)
    try:
        if args.open:
            webbrowser.open(url)
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        print("\n模拟演示已停止。", flush=True)
    finally:
        server.server_close()
    return 0
