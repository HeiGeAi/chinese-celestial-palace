import base64
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import HTTPSHandler, build_opener
from urllib.response import addinfourl

from scripts.generate_image import (
    _HttpsOnlyRedirectHandler,
    generate_image,
    image_bytes_from_payload,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ImageApiHandler(BaseHTTPRequestHandler):
    request_headers = None
    request_json = None
    redirect_location = None
    post_count = 0
    get_count = 0

    def do_POST(self):
        type(self).post_count += 1
        length = int(self.headers.get("Content-Length", "0"))
        type(self).request_headers = self.headers
        type(self).request_json = json.loads(self.rfile.read(length))
        if "/redirect/" in self.path or self.path.endswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", type(self).redirect_location)
            self.end_headers()
            return
        if "/error-string/" in self.path or self.path.endswith("/error-string"):
            body = json.dumps({"error": "invalid token secret-key"}).encode()
            self.send_response(401)
        elif "/error-null/" in self.path or self.path.endswith("/error-null"):
            body = json.dumps({"error": None, "message": "rate limited"}).encode()
            self.send_response(429)
        elif "/error/" in self.path or self.path.endswith("/error"):
            body = json.dumps({"error": {"message": "insufficient balance for secret-key"}}).encode()
            self.send_response(402)
        else:
            body = json.dumps({"data": [{"b64_json": base64.b64encode(PNG_1X1).decode()}]}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).get_count += 1
        body = json.dumps({"data": [{"b64_json": base64.b64encode(PNG_1X1).decode()}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class GenerateImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ImageApiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/v1"
        ImageApiHandler.redirect_location = f"http://127.0.0.1:{cls.server.server_port}/downgraded"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_sends_gptx_request_and_saves_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.png"
            with patch.dict(os.environ, {"GPTX_API_KEY": "test-key"}):
                result = generate_image(
                    prompt="云海上的中式天宫",
                    output=output,
                    size="1536x2048",
                    base_url=self.base_url,
                    model="gpt-image-2",
                )

            self.assertEqual(result, output.resolve())
            self.assertEqual(output.read_bytes(), PNG_1X1)
            self.assertEqual(ImageApiHandler.request_headers["Authorization"], "Bearer test-key")
            self.assertEqual(
                ImageApiHandler.request_json,
                {
                    "model": "gpt-image-2",
                    "prompt": "云海上的中式天宫",
                    "n": 1,
                    "size": "1536x2048",
                },
            )

    def test_requires_environment_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "GPTX_API_KEY"):
                generate_image("prompt", Path("unused.png"), base_url=self.base_url)

    def test_function_does_not_accept_api_key_argument(self):
        self.assertNotIn("api_key", inspect.signature(generate_image).parameters)

    def test_surfaces_sanitized_http_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"GPTX_API_KEY": "secret-key"}):
                with self.assertRaisesRegex(RuntimeError, "HTTP 402.*insufficient balance") as caught:
                    generate_image(
                        "prompt",
                        Path(tmp) / "unused.png",
                        base_url=f"{self.base_url}/error",
                    )
            self.assertNotIn("secret-key", str(caught.exception))

    def test_does_not_follow_api_redirects(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"GPTX_API_KEY": "test-key"}):
                with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                    generate_image(
                        "prompt",
                        Path(tmp) / "unused.png",
                        base_url=f"{self.base_url}/redirect",
                    )

    def test_rejects_non_https_result_url(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            image_bytes_from_payload({"data": [{"url": "http://example.com/image.png"}]})

    def test_rejects_non_string_url_and_invalid_b64_types(self):
        for bad_item in (
            {"url": 123},
            {"url": ["https://example.com/image.png"]},
            {"url": ""},
            {"b64_json": False},
            {"b64_json": 123},
        ):
            with self.subTest(bad_item=bad_item), self.assertRaises(ValueError):
                image_bytes_from_payload({"data": [bad_item]})

    def test_https_redirect_handler_blocks_http_downgrade_before_request(self):
        target_http_url = f"http://127.0.0.1:{self.server.server_port}/downgraded"
        ImageApiHandler.get_count = 0

        class RedirectingHttpsHandler(HTTPSHandler):
            def https_open(self, req):
                headers = Message()
                headers["Location"] = target_http_url
                resp = addinfourl(io.BytesIO(b""), headers, req.full_url, code=302)
                resp.msg = "Found"
                return resp

        opener = build_opener(RedirectingHttpsHandler, _HttpsOnlyRedirectHandler).open
        with self.assertRaisesRegex(ValueError, "非 HTTPS"):
            image_bytes_from_payload(
                {"data": [{"url": "https://cdn.example.com/result.png"}]},
                opener=opener,
            )
        self.assertEqual(ImageApiHandler.get_count, 0)

    def test_http_error_handles_string_and_null_error_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"GPTX_API_KEY": "secret-key"}):
                with self.assertRaisesRegex(RuntimeError, "HTTP 401.*invalid token") as caught:
                    generate_image(
                        "prompt",
                        Path(tmp) / "unused.png",
                        base_url=f"{self.base_url}/error-string",
                    )
                self.assertNotIn("secret-key", str(caught.exception))

                with self.assertRaisesRegex(RuntimeError, "HTTP 429.*rate limited"):
                    generate_image(
                        "prompt",
                        Path(tmp) / "unused.png",
                        base_url=f"{self.base_url}/error-null",
                    )

    def test_preflight_rejects_symlink_directory_and_invalid_timeout_before_api_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "keep.png"
            target.write_bytes(b"original")
            symlink_out = root / "link.png"
            symlink_out.symlink_to(target)
            dir_out = root / "subdir"
            dir_out.mkdir()

            before_posts = ImageApiHandler.post_count
            with patch.dict(os.environ, {"GPTX_API_KEY": "test-key"}):
                with self.assertRaisesRegex(ValueError, "符号链接"):
                    generate_image("prompt", symlink_out, base_url=self.base_url)
                with self.assertRaisesRegex(ValueError, "目录"):
                    generate_image("prompt", dir_out, base_url=self.base_url)
                for bad_timeout in (0, -5, True, float("nan")):
                    with self.subTest(timeout=bad_timeout), self.assertRaisesRegex(ValueError, "timeout"):
                        generate_image("prompt", root / "ok.png", base_url=self.base_url, timeout=bad_timeout)

            self.assertEqual(ImageApiHandler.post_count, before_posts)
            self.assertEqual(target.read_bytes(), b"original")

    def test_cli_end_to_end_saves_image_against_local_server(self):
        script = Path(__file__).resolve().parent.parent / "scripts" / "generate_image.py"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cli-out.png"
            env = {**os.environ, "GPTX_API_KEY": "cli-test-key"}
            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--prompt",
                    "云海天宫实测",
                    "--size",
                    "1536x2048",
                    "--base-url",
                    self.base_url,
                    "--output",
                    str(out),
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertEqual(out.read_bytes(), PNG_1X1)


if __name__ == "__main__":
    unittest.main()
