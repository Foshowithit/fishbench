"""Extra LLM request headers — the opencode-go lane needs
x-opencode-session on every chat call, and the key must stay off CLIs.

Covers config plumbing (env JSON + dataclass merge) and one real HTTP
round trip against a header-capturing stub server.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench.bench import main  # noqa: E402
from fishbench.llm import HTTPBackend, LLMConfig, LLMError  # noqa: E402


class HeaderConfigTests(unittest.TestCase):
    def test_from_env_parses_header_json(self):
        cfg = LLMConfig.from_env({
            "FISHBENCH_LLM_BASE_URL": "https://opencode.ai/zen/go/v1",
            "FISHBENCH_LLM_HEADERS":
                json.dumps({"x-opencode-session": "fishbench-1"}),
        })
        self.assertEqual(cfg.backend, "http")
        self.assertEqual(cfg.extra_headers,
                         {"x-opencode-session": "fishbench-1"})

    def test_from_env_ignores_garbage_header_json(self):
        cfg = LLMConfig.from_env({"FISHBENCH_LLM_HEADERS": "{not json"})
        self.assertEqual(cfg.extra_headers, {})

    def test_default_has_no_extra_headers(self):
        self.assertEqual(LLMConfig().extra_headers, {})


class _Capture(BaseHTTPRequestHandler):
    headers: dict = {}

    def do_POST(self):  # noqa: N802
        # drain the request body first — closing with unread bytes makes the
        # kernel RST the socket and the client dies with ECONNRESET
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        type(self).headers = {k.lower(): v for k, v in self.headers.items()}
        body = json.dumps({"choices": [{"message": {"content": "pong"}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):  # quiet
        pass


class HeaderRoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Capture)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def _complete(self, cfg: LLMConfig):
        return HTTPBackend(cfg).complete("sys", [{"role": "user",
                                                  "content": "ping"}])

    def test_session_header_reaches_the_endpoint(self):
        cfg = LLMConfig(backend="http", base_url=self.base, api_key="sk-test",
                        extra_headers={"x-opencode-session": "fishbench-1"})
        reply = self._complete(cfg)
        self.assertEqual(reply.text, "pong")
        self.assertEqual(_Capture.headers.get("x-opencode-session"),
                         "fishbench-1")
        self.assertEqual(_Capture.headers.get("authorization"), "Bearer sk-test")

    def test_no_headers_no_extras(self):
        cfg = LLMConfig(backend="http", base_url=self.base)
        self._complete(cfg)
        self.assertNotIn("authorization", _Capture.headers)
        self.assertNotIn("x-opencode-session", _Capture.headers)


class CliHeaderFlagTests(unittest.TestCase):
    """--header parsing without spending a real run: a malformed header
    must exit 2 before anything touches the network."""

    def test_bad_header_format_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "card.json")
            code = main(["--header", "no-equals-sign", "--out", out])
        self.assertEqual(code, 2)

    def test_good_header_parses(self):
        # offline backend: headers accepted, run completes, card seals
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "card.json")
            code = main(["--header", "x-opencode-session=fishbench-t",
                         "--stages", "1", "--out", out])
            self.assertEqual(code, 0)
            with open(out) as f:
                card = json.load(f)
            shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(card["spec"], "fishbench-1")


if __name__ == "__main__":
    unittest.main()
