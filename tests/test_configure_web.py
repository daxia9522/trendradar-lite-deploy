"""Drive legacy HTTP handlers in memory: no socket, network, mail or AI."""
import contextlib
import importlib.util
import io
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


class WebRegressionTests(unittest.TestCase):
    def run_server(self, path, requests, deployment="docker", application=None):
        responses = []
        requests = iter(requests)
        class Server:
            server_port = 12345
            def __init__(self, _address, handler):
                self.handler = handler
            def handle_request(self):
                try:
                    method, fields = next(requests)
                except StopIteration:
                    raise EOFError("test requests exhausted")
                handler = self.handler.__new__(self.handler)
                body = urllib.parse.urlencode(fields).encode()
                handler.headers = {"Content-Length": str(len(body))}
                handler.rfile = io.BytesIO(body)
                handler.wfile = io.BytesIO()
                status = []
                handler.send_response = status.append
                handler.send_header = lambda *_: None
                handler.end_headers = lambda: None
                getattr(handler, f"do_{method}")()
                responses.append((status[0], handler.wfile.getvalue().decode()))
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
            responses = self.run_server(path, [("POST", dict(values, AI_ANALYSIS_ENABLED="yes")), ("POST", values)])
            self.assertEqual([status for status, _ in responses], [400, 200])
            self.assertFalse(read_env(path, "docker").get("AI_ANALYSIS_ENABLED"))


if __name__ == "__main__":
    unittest.main()
