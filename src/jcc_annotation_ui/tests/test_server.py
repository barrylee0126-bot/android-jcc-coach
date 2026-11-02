from contextlib import redirect_stderr, redirect_stdout
import http.client
import io
import json
import socket
import threading
import time

from src.jcc_annotation_ui.__main__ import main
from src.jcc_annotation_ui.server import AnnotationServer, MAX_BODY
from src.jcc_annotation_ui.tests.helpers import PrivateFixture


class ServerTests(PrivateFixture):
    def setUp(self):
        super().setUp()
        self.server = AnnotationServer(self.ui)
        self.thread = threading.Thread(target=self.server.run, name="jcc-ui-test-server")
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.stopping.set()
        self.thread.join(5)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive())

    def request(self, path, *, method="GET", payload=None, auth=True, headers=None, raw=None):
        h = {"Host": self.server.origin.removeprefix("http://")}
        if auth: h["X-JCC-Session"] = self.server.session_token
        if method == "POST": h.update(Origin=self.server.origin, **{"Content-Type":"application/json"})
        if headers:
            for key, value in headers.items():
                if value is None: h.pop(key, None)
                else: h[key] = value
        body = raw if raw is not None else None if payload is None else json.dumps(payload).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request(method, path, body=body, headers=h)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_list_requires_capability_and_discloses_no_private_directory(self):
        before = self.snapshot()
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, raw = self.request("/api/samples", auth=False)
        self.assertEqual(status,403)
        self.assertNotIn(self.sample_id.encode(),raw)
        status, headers, raw = self.request("/api/samples")
        self.assertEqual(status,200)
        self.assertNotIn(str(self.root).encode(),raw)
        self.assertIsNone(json.loads(raw)["accuracy"])
        self.assertEqual(self.snapshot(), before)

    def test_static_and_private_responses_have_no_cache_cors_or_framing(self):
        for path in ("/", "/app.js", "/style.css", "/api/samples", "/api/sample/"+self.sample_id):
            status, headers, _ = self.request(path)
            self.assertEqual(status,200)
            self.assertIn("no-store",headers["Cache-Control"])
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertIn("frame-ancestors 'none'",headers["Content-Security-Policy"])
            self.assertNotIn("unsafe-inline",headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin",headers)
            self.assertNotIn("Set-Cookie",headers)

    def test_host_origin_fetch_site_and_capability_checks(self):
        checks = ({"Host":"localhost:"+str(self.server.server_address[1])}, {"Host":"attacker.example"},
                  {"Origin":"https://attacker.example"}, {"Origin":"null"}, {"Sec-Fetch-Site":"cross-site"},
                  {"Sec-Fetch-Site":"same-site"}, {"X-JCC-Session":"x"*43})
        before = self.snapshot()
        for headers in checks:
            with self.subTest(headers=list(headers)):
                self.assertEqual(self.request("/api/samples", headers=headers)[0],403)
                self.assertEqual(self.request("/api/annotate", method="POST", payload=self.edit(),headers=headers)[0],403)
        self.assertEqual(before,self.snapshot())

    def test_missing_origin_form_encoding_and_preflight_are_denied(self):
        before = self.snapshot()
        self.assertEqual(self.request("/api/annotate",method="POST",payload=self.edit(),headers={"Origin":None})[0],403)
        for content_type in ("application/x-www-form-urlencoded","text/plain","multipart/form-data"):
            self.assertEqual(self.request("/api/annotate",method="POST",payload=self.edit(),headers={"Content-Type":content_type})[0],400)
        self.assertEqual(self.request("/api/annotate",method="OPTIONS")[0],405)
        self.assertEqual(self.snapshot(),before)

    def test_duplicate_security_headers_rejected(self):
        for duplicate in ("Host", "Origin", "X-JCC-Session", "Content-Length"):
            connection = http.client.HTTPConnection("127.0.0.1",self.server.server_address[1],timeout=5)
            connection.putrequest("POST","/api/heartbeat",skip_host=True)
            values = {"Host":self.server.origin.removeprefix("http://"),"Origin":self.server.origin,
                      "X-JCC-Session":self.server.session_token,"Content-Type":"application/json","Content-Length":"2"}
            for key,value in values.items():
                connection.putheader(key,value)
                if key == duplicate: connection.putheader(key,value)
            connection.endheaders(b"{}")
            response = connection.getresponse()
            self.assertIn(response.status,(400,403)); response.read(); connection.close()

    def test_arbitrary_file_query_traversal_and_unsupported_methods_denied(self):
        for path in ("/workspace.json","/etc/passwd","/../workspace.json","/%2e%2e/workspace.json",
                     "/api/samples?path=workspace.json","/api/image/../../workspace.json","//api/samples"):
            self.assertEqual(self.request(path)[0],404)
        for method in ("PUT","PATCH","DELETE","HEAD"):
            self.assertEqual(self.request("/api/annotate",method=method)[0],405)

    def test_image_exact_bytes_authenticated_and_revision_bound(self):
        view = self.view()
        headers = {"X-JCC-View":view["view_token"], "X-JCC-Revision":str(view["revision"]),"X-JCC-Sample-Kind":"synthetic"}
        route = "/api/image/"+self.sample_id
        self.assertEqual(self.request(route,headers=headers,auth=False)[0],403)
        status, response_headers, raw = self.request(route,headers=headers)
        self.assertEqual(status,200)
        self.assertEqual(raw,self.ui.image(self.anchor(view))[0])
        self.assertIn("no-store",response_headers["Cache-Control"])
        self.ui.annotate(self.edit(view))
        self.assertEqual(self.request(route,headers=headers)[0],409)

    def test_post_annotate_review_then_stale_review_are_atomic(self):
        edit = self.known(self.edit())
        self.assertEqual(self.request("/api/annotate",method="POST",payload=edit)[0],200)
        review = self.review()
        self.assertEqual(self.request("/api/review",method="POST",payload=review)[0],200)
        self.assertEqual(self.request("/api/review",method="POST",payload=review)[0],409)
        self.assertEqual(self.view()["record"]["labels"]["gold"]["value"],32)
        self.assertTrue(self.view()["review_binding"]["current"])
        self.assertEqual(list((self.root / "edits").iterdir()),[])

    def test_json_duplicates_nonfinite_oversize_and_chunking_fail_without_writes(self):
        before = self.snapshot()
        for raw in (b'{"a":1,"a":2}',b'{"a":NaN}',b'{"a":1e999}',b'\xff'):
            self.assertEqual(self.request("/api/annotate",method="POST",raw=raw)[0],422)
        self.assertEqual(self.request("/api/heartbeat",method="POST",raw=b"{}",headers={"Content-Length":str(MAX_BODY+1)})[0],400)
        self.assertEqual(self.request("/api/heartbeat",method="POST",raw=b"{}",headers={"Transfer-Encoding":"chunked"})[0],400)
        self.assertEqual(self.request("/api/heartbeat",method="POST",raw=b"{}",headers={"Content-Encoding":"gzip"})[0],400)
        self.assertEqual(self.snapshot(),before)

    def test_bad_sample_errors_and_logs_do_not_echo_private_contents(self):
        edit = self.edit(); edit["annotator_id"] = "PRIVATE_UNTRUSTED_LABEL_11F"
        out,err = io.StringIO(),io.StringIO()
        with redirect_stdout(out),redirect_stderr(err):
            status,_,raw = self.request("/api/annotate",method="POST",payload=edit)
        self.assertEqual(status,422)
        self.assertNotIn(edit["annotator_id"].encode(),raw)
        self.assertEqual(out.getvalue()+err.getvalue(),"")

    def test_untrusted_text_is_json_and_never_in_html_shell(self):
        attack = '</textarea><img src=x onerror="window.jccInjected=1">'
        edit = self.edit(); edit["conditions"] = [attack]
        self.assertEqual(self.request("/api/annotate",method="POST",payload=edit)[0],200)
        self.assertIn(attack,self.view()["conditions"])
        _,headers,raw = self.request("/")
        self.assertNotIn(attack.encode(),raw)
        self.assertIn("text/html",headers["Content-Type"])
        _,headers,_ = self.request("/api/sample/"+self.sample_id)
        self.assertIn("application/json",headers["Content-Type"])

    def test_corrupt_or_insecure_workspace_never_returns_image(self):
        headers = {"X-JCC-View":self.view()["view_token"],"X-JCC-Revision":"1","X-JCC-Sample-Kind":"synthetic"}
        path = self.root / self.data()["assets"][self.sample_id]["path"]
        path.chmod(0o644)
        for route in ("/api/samples","/api/sample/"+self.sample_id,"/api/image/"+self.sample_id):
            status,response_headers,raw = self.request(route,headers=headers)
            self.assertEqual(status,422)
            self.assertIn("application/json",response_headers["Content-Type"])
            self.assertNotIn(str(path).encode(),raw)
        path.chmod(0o600)

    def test_shutdown_ends_server_and_port_is_closed(self):
        port = self.server.server_address[1]
        self.assertEqual(self.request("/api/shutdown",method="POST",payload={})[0],200)
        self.thread.join(4)
        self.assertFalse(self.thread.is_alive())
        with socket.socket() as client:
            client.settimeout(.5)
            self.assertNotEqual(client.connect_ex(("127.0.0.1",port)),0)

    def test_idle_service_stops_without_a_browser(self):
        server = AnnotationServer(self.ui,lease_seconds=.05)
        thread = threading.Thread(target=server.run)
        thread.start(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(server.stopping.is_set())

    def test_full_preflight_is_authenticated_read_only_and_schema_bound(self):
        before=self.snapshot()
        self.assertEqual(self.request('/api/verify',method='POST',payload={},auth=False)[0],403)
        self.assertEqual(self.request('/api/verify',method='POST',payload={},headers={'Origin':None})[0],403)
        self.assertEqual(self.request('/api/verify',method='POST',payload={'path':'workspace.json'})[0],422)
        status,headers,raw=self.request('/api/verify',method='POST',payload={})
        self.assertEqual(status,200)
        result=json.loads(raw)
        self.assertEqual(result['status'],'content_verified')
        self.assertEqual(result['verification']['scope'],'full_content')
        self.assertEqual(result['verification']['decoded_assets'],1)
        self.assertIn('no-store',headers['Cache-Control'])
        self.assertEqual(self.snapshot(),before)

    def test_cli_rejects_private_values_without_echo(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            main(["serve","--private-dir","PRIVATE_PATH_DO_NOT_ECHO","--sample-kind","bad-value"])
        self.assertEqual(caught.exception.code,2)
        self.assertNotIn("PRIVATE_PATH",err.getvalue())
        self.assertNotIn("bad-value",err.getvalue())
