"""Capability-authenticated, loopback-only delivery; no generic file routes."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import re
import secrets
import threading
import time

from src.jcc_samples import WorkflowError
from src.jcc_samples import workflow as wf
from .workspace import AnnotationWorkspace


STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
SAMPLE_ROUTE = re.compile(r"/api/(sample|image)/(sample-[0-9a-f]{32})\Z")
MAX_BODY = 128 * 1024
ERROR_MESSAGES = {
    "revision_conflict": "工作区修订已变化。草稿未保存，请重新载入当前样本并检查。",
    "view_expired": "页面中的画面或标签版本已过期，请重新载入并检查。",
    "workspace_busy": "另一个操作正在使用工作区，请稍后重试。",
    "independent_review_required": "复核者代号不能与标注者相同；不同代号仍不能证明实际人员独立。",
    "acceptance_frozen": "验收样本已冻结，须用原工作流先移回隔离区。",
    "occluded_known_field": "已知字段的证据区域与遮挡重叠，请将受影响字段保留未知。",
    "known_region_required": "每个已知的可编辑字段都需要本图中的证据区域。",
    "sample_kind_boundary": "实例、工作区或请求的真实/合成类型不一致，操作已拒绝。",
    "invalid_annotations": "标签未通过共享 Schema 和语义校验，请检查状态、值、原因和区域。",
    "review_confirmation_required": "请明确确认本次复核的人员及真实/合成边界。",
    "unsafe_permissions": "工作区文件权限不安全，操作已拒绝；请检查私有目录和文件权限。",
    "asset_integrity": "图像完整性校验失败，停止使用此样本并检查私有工作区。",
    "image_unreadable": "图像损坏或不能完整解码，操作已拒绝。",
    "file_unreadable": "所需私有文件不可读或链接不安全，操作已拒绝。",
    "invalid_identifier": "请填写非个人信息代号：小写字母开头，仅小写字母、数字、下划线或短横线，最多 64 位。",
    "invalid_region": "区域需要 0 到 1 之间的整帧归一化坐标，且必须有正面积。",
    "invalid_occlusion": "遮挡区域格式无效、重复或超过 32 个。",
    "invalid_conditions": "画面条件需要互不重复的简短文本，不能包含保留的遮挡前缀。",
    "unsupported_fields": "仅允许编辑六个 M1 字段，其他字段须保持原值。",
    "invalid_json": "请求须为无重复键、无非有限数字的 UTF-8 JSON。",
}


class AnnotationServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = 8

    def __init__(self, workspace: AnnotationWorkspace, *, port: int = 0, lease_seconds: float = 90):
        self.workspace = workspace
        self.session_token = secrets.token_urlsafe(32)
        self.stopping = threading.Event()
        self.lease_seconds = lease_seconds
        self.last_authenticated = time.monotonic()
        super().__init__(("127.0.0.1", port), AnnotationHandler)
        self.timeout = 0.25

    @property
    def origin(self) -> str:
        return "http://127.0.0.1:" + str(self.server_address[1])

    @property
    def session_url(self) -> str:
        # Fragments are not sent in HTTP requests or Referer headers. The page
        # consumes it into memory and removes it from the address bar.
        return self.origin + "/#" + self.session_token

    def run(self) -> None:
        try:
            while not self.stopping.is_set() and time.monotonic() - self.last_authenticated < self.lease_seconds:
                self.handle_request()
        finally:
            self.stopping.set()
            self.server_close()
            self.workspace.close()

    def handle_error(self, request, client_address) -> None:
        # No tracebacks, request strings, private labels or file paths in logs.
        pass


class AnnotationHandler(BaseHTTPRequestHandler):
    server: AnnotationServer
    protocol_version = "HTTP/1.0"
    server_version = "LocalAnnotation"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, format, *args) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self' blob:; base-uri 'none'; frame-ancestors 'none'; "
                         "form-action 'none'; object-src 'none'; worker-src 'none'")
        self.end_headers()
        try:
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _json(self, status: int, value: dict) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, code: str) -> None:
        self._json(status, {"error": {"code": code, "message": ERROR_MESSAGES.get(code, "请求未通过检查，操作已拒绝。")}})

    def send_error(self, code, message=None, explain=None) -> None:
        self._error(code, "invalid_http")

    def _header(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        return values[0] if len(values) == 1 else None

    def _allowed(self, *, authenticated: bool = False, writing: bool = False) -> bool:
        target = self.requestline.split()[1]
        if not target.startswith("/") or target.startswith("//"):
            self._error(404, "route_missing")
            return False
        if self._header("Host") != self.server.origin.removeprefix("http://"):
            self._error(403, "host_rejected")
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and origins[0] != self.server.origin) or (writing and not origins):
            self._error(403, "origin_rejected")
            return False
        fetch = self.headers.get_all("Sec-Fetch-Site", [])
        if len(fetch) > 1 or (fetch and fetch[0] not in ("same-origin", "none")):
            self._error(403, "cross_site_rejected")
            return False
        if authenticated:
            token = self._header("X-JCC-Session")
            if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None or not hmac.compare_digest(token, self.server.session_token):
                self._error(403, "session_required")
                return False
        return True

    def _execute(self, operation) -> None:
        try:
            operation()
        except WorkflowError as error:
            status = 409 if error.code in ("revision_conflict", "view_expired", "workspace_busy", "acceptance_frozen") else 422
            if error.code == "sample_missing":
                status = 404
            self._error(status, error.code)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, RuntimeError):
            self._error(503, "local_io_failed")

    def do_GET(self) -> None:
        if not self._allowed():
            return
        if self.path in STATIC:
            filename, content_type = STATIC[self.path]
            self._execute(lambda: self._send(200, (Path(__file__).with_name("web") / filename).read_bytes(), content_type))
            return
        if not self._allowed(authenticated=True):
            return
        self.server.last_authenticated = time.monotonic()
        if self.path == "/api/samples":
            self._execute(lambda: self._json(200, self.server.workspace.samples()))
            return
        match = SAMPLE_ROUTE.fullmatch(self.path)
        if match is None:
            self._error(404, "route_missing")
            return
        route, sample_id = match.groups()
        if route == "sample":
            self._execute(lambda: self._json(200, self.server.workspace.sample(sample_id)))
        else:
            def show_image():
                revision = self._header("X-JCC-Revision")
                if not isinstance(revision, str) or re.fullmatch(r"0|[1-9][0-9]{0,15}", revision) is None:
                    self._error(400, "invalid_revision")
                    return
                raw, content_type = self.server.workspace.image({"sample_id": sample_id,
                    "sample_kind": self._header("X-JCC-Sample-Kind"), "expected_revision": int(revision),
                    "view_token": self._header("X-JCC-View")})
                self._send(200, raw, content_type)
            self._execute(show_image)

    def do_POST(self) -> None:
        if not self._allowed(authenticated=True, writing=True):
            return
        if self.path not in ("/api/annotate", "/api/review", "/api/verify", "/api/heartbeat", "/api/shutdown"):
            self._error(404, "route_missing")
            return
        if self.headers.get_all("Transfer-Encoding", []) or self.headers.get_all("Content-Encoding", []):
            self._error(400, "invalid_request")
            return
        length = self._header("Content-Length")
        content_type = self._header("Content-Type")
        if (not isinstance(length, str) or re.fullmatch(r"[1-9][0-9]{0,6}", length) is None
                or int(length) > MAX_BODY or content_type not in ("application/json", "application/json; charset=utf-8")):
            self._error(400, "invalid_request")
            return

        def process():
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                self._error(400, "invalid_request")
                return
            payload = wf._parse_json(raw)
            self.server.last_authenticated = time.monotonic()
            if self.path in ("/api/heartbeat", "/api/shutdown"):
                if payload != {}:
                    self._error(400, "invalid_request")
                    return
                self._json(200, {"status": "stopping" if self.path.endswith("shutdown") else "active"})
                if self.path.endswith("shutdown"):
                    self.server.stopping.set()
            elif self.path == "/api/annotate":
                self._json(200, self.server.workspace.annotate(payload))
            elif self.path == "/api/verify":
                self.server.workspace._keys(payload, set())
                self._json(200, self.server.workspace.verify())
            else:
                self._json(200, self.server.workspace.review(payload))
        self._execute(process)

    def _method_rejected(self) -> None:
        if self._allowed():
            self._error(405, "method_rejected")

    do_HEAD = _method_rejected
    do_OPTIONS = _method_rejected
    do_PUT = _method_rejected
    do_PATCH = _method_rejected
    do_DELETE = _method_rejected
