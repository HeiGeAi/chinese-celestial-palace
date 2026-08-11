import base64
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scripts.generate_image import generate_image, image_bytes_from_payload


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ImageApiHandler(BaseHTTPRequestHandler):
    request_headers = None
    request_json = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).request_headers = self.headers
        type(self).request_json = json.loads(self.rfile.read(length))
        if "/error/" in self.path or self.path.endswith("/error"):
            body = json.dumps({"error": {"message": "insufficient balance for secret-key"}}).encode()
            self.send_response(402)
        else:
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

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_sends_gptx_request_and_saves_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.png"
            result = generate_image(
                prompt="云海上的中式天宫",
                output=output,
                size="1536x2048",
                api_key="test-key",
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
        with self.assertRaisesRegex(ValueError, "GPTX_API_KEY"):
            generate_image("prompt", Path("unused.png"), api_key="", base_url=self.base_url)

    def test_surfaces_sanitized_http_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "HTTP 402.*insufficient balance") as caught:
                generate_image(
                    "prompt",
                    Path(tmp) / "unused.png",
                    api_key="secret-key",
                    base_url=f"{self.base_url}/error",
                )
            self.assertNotIn("secret-key", str(caught.exception))

    def test_rejects_non_https_result_url(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            image_bytes_from_payload({"data": [{"url": "http://example.com/image.png"}]})


if __name__ == "__main__":
    unittest.main()
