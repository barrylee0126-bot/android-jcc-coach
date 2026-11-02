"""Exercise only a temporary 127.0.0.1 fixture server; no external requests."""

import http.client
import json
from pathlib import Path
import sys
import threading
import unittest
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jcc_overlay.server import OverlayServer


class OverlayHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = OverlayServer(0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, path, *, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(method, quote(path, safe="/?=."), body=body, headers=headers or {})
        response = connection.getresponse()
        status, result_headers, content = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, result_headers, content

    def post(self, body, **headers):
        return self.request("/api/demo", method="POST", body=json.dumps(body), headers={
            "Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json", **headers,
        })

    def test_server_binds_only_ipv4_loopback(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_assets_and_compact_route_are_served_locally(self):
        for path in ("/", "/?compact=1", "/overlay.css", "/overlay.js"):
            with self.subTest(path=path):
                status, headers, content = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
                self.assertTrue(content)

    def test_only_named_fixtures_can_be_selected(self):
        status, _, content = self.post({"scenario": "valid"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["status"], "valid")
        for payload in ({"scenario": "live"}, {"scenario": "valid", "advice": {}}, {"scenario": []}, None):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload)[0], 400)

    def test_read_after_unknown_conflict_or_disconnect_contains_no_old_advice(self):
        for scenario in ("unknown", "conflict", "disconnected", "expired", "invalidated"):
            with self.subTest(scenario=scenario):
                self.post({"scenario": "valid"})
                self.post({"scenario": scenario})
                status, _, content = self.request("/api/view")
                view = json.loads(content)
                self.assertEqual(status, 200)
                self.assertEqual(view["status"], scenario)
                self.assertIsNone(view["advice"])
                self.assertNotIn("纸片小方", content.decode())

    def test_other_origins_missing_origin_and_dns_rebinding_are_denied(self):
        self.assertEqual(self.post({"scenario": "valid"}, Origin="https://example.invalid")[0], 403)
        self.assertEqual(self.post({"scenario": "valid"}, Host=f"example.invalid:{self.port}")[0], 403)
        self.assertEqual(self.post({"scenario": "valid"}, **{"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("/api/demo", method="POST", body='{"scenario":"valid"}', headers={"Content-Type": "application/json"})[0], 403)

    def test_no_generic_input_files_or_device_endpoint(self):
        for path in ("/api/publish", "/api/device", "/../../设备配置.env", "/model.py"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_bad_body_gives_chinese_message_without_exception_details(self):
        for body in ("{", "x" * 1_025):
            status, _, content = self.request("/api/demo", method="POST", body=body, headers={"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"})
            self.assertEqual(status, 400)
            self.assertIn("演示状态", json.loads(content)["message"])
            self.assertNotIn(b"Traceback", content)


if __name__ == "__main__":
    unittest.main()
