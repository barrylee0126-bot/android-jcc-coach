from __future__ import annotations

from http.client import HTTPConnection
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from jcc_timed_overlay.server import TimedOverlayServer
from jcc_timed_overlay.source import JsonlTail, SyntheticSource, decode_event
from jcc_timed_overlay.tests.test_lifecycle import Fixture


class SourceTests(Fixture):
    def test_tail_discards_suffix_of_existing_unterminated_line(self):
        for prefix in (b"unfinished-old-record", b"\xe4\xb8", b"\r"):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as directory:
                self.store = type(self.store)(clock_domain_id="test-clock", stream_id="test-stream", clock=self.clock)
                path = Path(directory) / "input.jsonl"
                path.write_bytes(prefix)
                tail = JsonlTail(path, self.store)
                self.addCleanup(tail.close)
                suffix = json.dumps(self.event("must-be-ignored")).encode()
                with path.open("ab") as file:
                    file.write(suffix[:30])
                tail.poll()
                self.assertEqual(self.store.report()["opportunities"], 0)
                with path.open("ab") as file:
                    file.write(suffix[30:] + b"\n")
                tail.poll()
                self.assertEqual(self.store.report()["opportunities"], 0)
                self.assertEqual(self.store.report()["counters"].get("rejected_inputs", 0), 0)
                self.clock.advance(1)
                with path.open("ab") as file:
                    file.write(json.dumps(self.event("fresh-after-boundary")).encode() + b"\n")
                tail.poll()
                self.assertEqual(self.store.view()["sample_id"], "fresh-after-boundary")
                self.assertEqual(self.store.report()["opportunities"], 1)

    def test_tail_waiting_for_old_line_boundary_closes_on_truncation_or_rotation(self):
        for mutation in ("truncate", "rotate"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                self.store = type(self.store)(clock_domain_id="test-clock", stream_id="test-stream", clock=self.clock)
                path = Path(directory) / "input.jsonl"
                path.write_bytes(b"unfinished-old-record")
                tail = JsonlTail(path, self.store)
                self.addCleanup(tail.close)
                with path.open("ab") as file:
                    file.write(b"unfinished-suffix")
                tail.poll()
                if mutation == "truncate":
                    path.write_bytes(b"")
                else:
                    replacement = path.with_name("replacement.jsonl")
                    replacement.write_text(json.dumps(self.event()) + "\n")
                    replacement.replace(path)
                tail.poll()
                self.assertTrue(tail.failed)
                with path.open("ab") as file:
                    file.write(json.dumps(self.event()).encode() + b"\n")
                tail.poll()
                self.assertEqual(self.store.report()["opportunities"], 0)
                self.assertIsNone(self.store.view()["message"])

    def test_tail_skips_existing_and_waits_for_complete_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.jsonl"
            path.write_text("ignored old input\n")
            tail = JsonlTail(path, self.store)
            self.addCleanup(tail.close)
            tail.poll()
            self.assertEqual(self.store.report()["opportunities"], 0)
            data = json.dumps(self.event()).encode()
            with path.open("ab") as file:
                file.write(data[:30])
            tail.poll()
            self.assertEqual(self.store.report()["opportunities"], 0)
            with path.open("ab") as file:
                file.write(data[30:] + b"\n")
            tail.poll()
            self.assertEqual(self.store.view()["status"], "ready")
            path.write_bytes(b"")
            tail.poll()
            self.assertTrue(tail.failed)
            self.assertIsNone(self.store.view()["message"])

    def test_replaced_file_does_not_replay_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.jsonl"
            path.touch()
            tail = JsonlTail(path, self.store)
            self.addCleanup(tail.close)
            replacement = path.with_name("new.jsonl")
            replacement.write_text(json.dumps(self.event()) + "\n")
            replacement.replace(path)
            tail.poll()
            self.assertTrue(tail.failed)
            self.assertEqual(self.store.report()["opportunities"], 0)

    def test_nonregular_and_symlink_inputs_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.jsonl"
            path.touch()
            link = path.with_name("link")
            link.symlink_to(path)
            fifo = path.with_name("fifo")
            os.mkfifo(fifo)
            for target in (link, fifo, Path(directory)):
                with self.subTest(target=target.name), self.assertRaises((OSError, ValueError)):
                    JsonlTail(target, self.store)

    def test_invalid_lines_and_oversize_line_revoke_without_raw_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.jsonl"
            path.touch()
            tail = JsonlTail(path, self.store)
            self.addCleanup(tail.close)
            with path.open("ab") as file:
                file.write(json.dumps(self.event()).encode() + b"\n")
            tail.poll()
            with path.open("ab") as file:
                file.write(b'{"x":NaN}\n' + b"x" * 5000)
            tail.poll()
            self.assertIsNone(self.store.view()["message"])
            self.assertEqual(self.store.report()["counters"]["rejected_inputs"], 2)
            with path.open("ab") as file:
                file.write(b"trailing\n")
            tail.poll()
            self.assertEqual(self.store.report()["counters"]["rejected_inputs"], 2)

    def test_duplicate_keys_and_nonfinite_json_are_refused(self):
        for line in (b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff', b'{"a":Infinity}'):
            with self.subTest(line=line), self.assertRaises((ValueError, UnicodeError)):
                decode_event(line)

    def test_synthetic_scheduler_once_includes_failure_denominator(self):
        source = SyntheticSource(self.store)
        source.choose("play")
        for _ in range(230):
            self.clock.advance(25)
            source.poll()
            self.store.tick()
        report = self.store.report()
        self.assertEqual(report["opportunities"], 5)
        self.assertEqual(report["outcomes"].get("no_result"), 1)
        self.assertEqual(report["outcomes"].get("result_timeout"), 1)
        self.assertFalse(source.playing)
        self.assertIsNone(self.store.view()["message"])


class HttpTests(Fixture):
    def setUp(self):
        super().setUp()
        self.source = SyntheticSource(self.store)
        self.server = TimedOverlayServer(0, self.store, self.source)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        value = response.read()
        result = response.status, value, dict(response.getheaders())
        connection.close()
        return result

    def test_assets_view_report_and_no_arbitrary_file_or_upload_route(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for path in ("/", "/app.js", "/style.css", "/api/view", "/api/report"):
            status, _, headers = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
        for path in ("/../AGENTS.md", "/events.jsonl", "/api/input", "/unknown"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_wrong_host_cross_site_and_missing_origin_are_denied(self):
        for headers in ({"Host": "foreign.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request("GET", "/api/view", headers=headers)[0], 403)
        self.assertEqual(self.request("POST", "/api/demo", "{}")[0], 403)
        self.assertEqual(self.request("POST", "/api/demo", "{}", {"Origin": "http://foreign.example"})[0], 403)

    def test_demo_and_browser_reports_use_strict_json_boundary(self):
        headers = {"Origin": self.origin, "Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/demo", json.dumps({"scenario": "show"}), headers)[0], 200)
        self.clock.advance(80)
        self.source.poll()
        self.assertEqual(self.store.view()["status"], "ready")
        self.assertEqual(self.request("POST", "/api/browser-event", json.dumps(self.browser()), headers)[0], 200)
        self.assertEqual(self.request("POST", "/api/demo", '{"scenario":"show","scenario":"play"}', headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/browser-event", '{"image":"rejected"}', headers)[0], 409)
        self.assertEqual(self.request("POST", "/api/input", "{}", headers)[0], 404)

    def test_jsonl_mode_does_not_allow_demo_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.jsonl"
            path.touch()
            tail = JsonlTail(path, self.store)
            self.addCleanup(tail.close)
            self.server.source = tail
            headers = {"Origin": self.origin, "Content-Type": "application/json"}
            self.assertEqual(self.request("POST", "/api/demo", '{"scenario":"show"}', headers)[0], 405)
            status, body, _ = self.request("GET", "/api/view")
            self.assertEqual(json.loads(body)["input_mode"], "jsonl_tail")


if __name__ == "__main__":
    unittest.main()
