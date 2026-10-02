"""Drive legacy HTTP handlers in memory: no socket, network, mail or AI."""
import contextlib
import importlib.util
import io
import re
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("web_configure", ROOT / "deploy/configure.py")
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)
from envfile import read_env, write_env
from native_config import NativeApplication

CSRF = re.compile(r'name="_csrf" value="([^"]+)"')
BROWSER = {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"}
VALUES = {"EMAIL_FROM": "sender@example.com", "EMAIL_TO": "reader@example.com",
          "EMAIL_PASSWORD": "secret", "TZ": "UTC"}


class WebRegressionTests(unittest.TestCase):
    def test_new_invalid_boolean_draft_survives_get_blank_and_omitted_retries(self):
        for deployment in ("linux", "docker"):
            with self.subTest(deployment=deployment), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "env"
                write_env(path, dict(VALUES, AI_ANALYSIS_ENABLED="false"), deployment)
                responses = self.run_server(path, [
                    ("POST", dict(VALUES, AI_ANALYSIS_ENABLED="synthetic-private-boolean")),
                    ("GET", {}),
                    ("POST", dict(VALUES, AI_ANALYSIS_ENABLED="")),
                    ("POST", dict(VALUES)),
                    ("POST", dict(VALUES, AI_ANALYSIS_ENABLED=":clear")),
                ], deployment)
                self.assertEqual([status for status, _ in responses], [400, 200, 400, 400, 200])
                self.assertEqual(read_env(path, deployment)["AI_ANALYSIS_ENABLED"], "")
                for _, page in responses:
                    self.assertNotIn("synthetic-private-boolean", page)

    def test_invalid_boolean_is_redacted_and_blank_retry_does_not_bypass_rejection(self):
        for deployment in ("linux", "docker"):
            with self.subTest(deployment=deployment), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "env"
                values = dict(VALUES, AI_ANALYSIS_ENABLED="synthetic-private-boolean")
                write_env(path, values, deployment)
                responses = self.run_server(path, [
                    ("GET", {}),
                    ("POST", dict(VALUES, AI_ANALYSIS_ENABLED="")),
                    ("POST", dict(VALUES, AI_ANALYSIS_ENABLED=":clear")),
                ], deployment)
                self.assertEqual([status for status, _ in responses], [200, 400, 200])
                self.assertEqual(read_env(path, deployment)["AI_ANALYSIS_ENABLED"], "")
                for _, page in responses:
                    self.assertNotIn("synthetic-private-boolean", page)

    def run_server(self, path, requests, deployment="docker", application=None):
        """Each request is (method, fields[, headers]); a None header value omits it."""
        responses = []
        requests = iter(requests)
        class Server:
            server_port = 12345
            def __init__(self, _address, handler):
                self.handler = handler
            def call(self, method, fields, headers=None):
                handler = self.handler.__new__(self.handler)
                body = urllib.parse.urlencode(fields).encode()
                merged = {**BROWSER, "Content-Length": str(len(body)), **(headers or {})}
                handler.headers = {key: value for key, value in merged.items() if value is not None}
                handler.rfile = io.BytesIO(body)
                handler.wfile = io.BytesIO()
                status = []
                handler.send_response = status.append
                handler.send_header = lambda *_: None
                handler.end_headers = lambda: None
                getattr(handler, f"do_{method}")()
                return status[0], handler.wfile.getvalue().decode()
            def handle_request(self):
                try:
                    method, fields, *headers = next(requests)
                except StopIteration:
                    raise EOFError("test requests exhausted")
                if method == "POST" and "_csrf" not in fields:
                    # Submit like a browser: from the page on screen, else a fresh GET.
                    page = responses[-1][1] if responses and CSRF.search(responses[-1][1]) else self.call("GET", {})[1]
                    fields = dict(fields, _csrf=CSRF.search(page)[1])
                responses.append(self.call(method, fields, *headers))
            def server_close(self):
                pass
        with mock.patch.object(configure, "HTTPServer", Server), contextlib.redirect_stdout(io.StringIO()):
            configure.serve(path, "127.0.0.1", 0, 0, "user", "server", 22, deployment, application=application)
        return responses

    def test_docker_web_preserves_unknowns_and_blank_existing_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            values = {"EMAIL_FROM": "sender@example.com", "EMAIL_TO": "reader@example.com",
                      "EMAIL_PASSWORD": "secret $literal", "AI_API_KEY": "api $literal", "TZ": "UTC",
                      "TREND_RADAR_IMAGE": "registry.invalid/app:fixed", "S3_EXTRA": "keep me"}
            write_env(path, values, "docker")
            posted = dict(values, EMAIL_PASSWORD="", AI_API_KEY="", EMAIL_TO="new@example.com")
            posted.pop("TREND_RADAR_IMAGE")
            posted.pop("S3_EXTRA")
            responses = self.run_server(path, [("GET", {}), ("POST", posted)])
            loaded = read_env(path, "docker")
            self.assertEqual(loaded["EMAIL_PASSWORD"], values["EMAIL_PASSWORD"])
            self.assertEqual(loaded["AI_API_KEY"], values["AI_API_KEY"])
            self.assertEqual(loaded["S3_EXTRA"], "keep me")
            self.assertEqual(loaded["TREND_RADAR_IMAGE"], values["TREND_RADAR_IMAGE"])
            self.assertEqual(loaded["EMAIL_TO"], "new@example.com")
            for status, html in responses:
                self.assertEqual(status, 200)
                self.assertNotIn(values["EMAIL_PASSWORD"], html)
                self.assertNotIn(values["AI_API_KEY"], html)

    def test_linux_legacy_web_mail_change_does_not_write_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            values = {"EMAIL_FROM": "sender@example.com", "EMAIL_TO": "reader@example.com", "EMAIL_PASSWORD": "secret"}
            write_env(path, values)
            runner = mock.Mock(side_effect=AssertionError("systemctl forbidden"))
            app = NativeApplication(path, unit_dir=Path(tmp) / "units", runner=runner)
            responses = self.run_server(path, [("POST", dict(values, EMAIL_TO="new@example.com"))], "linux", app)
            self.assertEqual(responses[0][0], 200)
            self.assertEqual(read_env(path)["EMAIL_TO"], "new@example.com")
            self.assertFalse(read_env(path).get("TZ"))
            runner.assert_not_called()

    def test_web_validation_does_not_mutate_before_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            values = {"EMAIL_FROM": "sender@example.com", "EMAIL_TO": "reader@example.com",
                      "EMAIL_PASSWORD": "secret", "TZ": "UTC"}
            write_env(path, values, "docker")
            # Redacted invalid drafts now require an explicit correction/clear;
            # omitting the field must not silently turn it into YAML fallback.
            responses = self.run_server(path, [("POST", dict(values, AI_ANALYSIS_ENABLED="yes")),
                                                ("POST", dict(values, AI_ANALYSIS_ENABLED=":clear"))])
            self.assertEqual([status for status, _ in responses], [400, 200])
            self.assertFalse(read_env(path, "docker").get("AI_ANALYSIS_ENABLED"))

    def test_credential_url_never_renders_and_blank_submit_retains_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            credential_url = "https://user:***@router.example/v1"
            values = {"EMAIL_FROM": "sender@example.com", "EMAIL_TO": "reader@example.com",
                      "EMAIL_PASSWORD": "secret", "TZ": "UTC", "AI_API_BASE": credential_url}
            write_env(path, values, "docker")
            responses = self.run_server(path, [("GET", {}),
                                               ("POST", dict(values, AI_API_BASE=""))])
            self.assertEqual([status for status, _ in responses], [200, 200])
            for _status, page in responses:
                self.assertNotIn("user:***", page)
                self.assertNotIn("abc123", page)
            # Blank submit of the redacted field keeps the stored credential.
            self.assertEqual(read_env(path, "docker")["AI_API_BASE"], credential_url)

    def test_credential_url_clear_sentinel_empties_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            credential_url = "https://user:***@router.example/v1"
            values = {"EMAIL_FROM": "sender@example.com", "EMAIL_TO": "reader@example.com",
                      "EMAIL_PASSWORD": "secret", "TZ": "UTC", "AI_API_BASE": credential_url}
            write_env(path, values, "docker")
            responses = self.run_server(path, [("POST", dict(values, AI_API_BASE=":clear"))])
            self.assertEqual(responses[0][0], 200)
            self.assertNotIn("user:***", responses[0][1])
            self.assertEqual(read_env(path, "docker")["AI_API_BASE"], "")

    def test_cross_site_and_rebinding_requests_are_rejected_without_page_or_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            write_env(path, VALUES, "docker")
            attack = dict(VALUES, EMAIL_PASSWORD="", EMAIL_SMTP_SERVER="smtp.attacker.example", EMAIL_SMTP_PORT="465")
            rebind = {"Host": "rebind.attacker.example:8765", "Origin": "http://rebind.attacker.example:8765"}
            responses = self.run_server(path, [
                ("GET", {}, dict(rebind, Origin=None)),
                ("POST", attack, rebind),
                ("POST", attack, {"Origin": "https://attacker.example"}),
                ("POST", attack, {"Origin": "null"}),
                ("POST", dict(attack, _csrf=""), {"Origin": None}),
                ("POST", dict(attack, _csrf="guessed"), {"Origin": None}),
                ("POST", VALUES),
            ])
            self.assertEqual([status for status, _ in responses], [403, 403, 403, 403, 403, 403, 200])
            for _status, page in responses[:-1]:
                self.assertNotIn("reader@example.com", page)
                self.assertNotIn("_csrf", page)
            self.assertNotIn("attacker", path.read_text())

    def test_linux_oversized_or_malformed_form_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            write_env(path, VALUES)
            app = NativeApplication(path, unit_dir=Path(tmp) / "units",
                                    runner=mock.Mock(side_effect=AssertionError("systemctl forbidden")))
            changed = dict(VALUES, EMAIL_TO="changed@example.com")
            responses = self.run_server(path, [
                ("POST", changed, {"Content-Length": str(configure.MAX_FORM_BYTES + 1)}),
                ("POST", changed, {"Content-Length": "-1"}),
                ("POST", changed, {"Content-Length": "not-a-number"}),
                ("POST", VALUES),
            ], "linux", app)
            self.assertEqual([status for status, _ in responses], [400, 400, 400, 200])
            self.assertIn("请求格式无效", responses[0][1])
            self.assertEqual(read_env(path)["EMAIL_TO"], "reader@example.com")

    def test_request_guard_host_origin_and_token_rules(self):
        guard = configure.RequestGuard("0.0.0.0")
        for host in ("127.0.0.1:8765", "localhost:9000", "[::1]:8765", "LOCALHOST"):
            self.assertTrue(guard.trusted({"Host": host}), host)
        for host in (None, "", "attacker.example:8765", "127.0.0.1.attacker.example", "192.0.2.7:8765", "[::1"):
            self.assertFalse(guard.trusted({"Host": host}), host)
        self.assertTrue(guard.trusted({"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"}))
        for origin in ("null", "https://attacker.example", "http://127.0.0.1:9999", "http://localhost:8765"):
            self.assertFalse(guard.trusted({"Host": "127.0.0.1:8765", "Origin": origin}), origin)
        # An explicit, non-wildcard bind address is what the operator opens.
        self.assertTrue(configure.RequestGuard("192.0.2.7").trusted({"Host": "192.0.2.7:8765"}))
        self.assertTrue(guard.token_matches({"_csrf": [guard.token]}))
        for form in ({}, {"_csrf": [""]}, {"_csrf": ["令牌"]}, {"_csrf": [configure.RequestGuard("").token]}):
            self.assertFalse(guard.token_matches(form), form)
        page = guard.stamp(configure.render(VALUES))
        self.assertEqual(CSRF.findall(page), [guard.token])


if __name__ == "__main__":
    unittest.main()
