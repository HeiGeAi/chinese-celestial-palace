import base64
import io
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from scripts import generate_image as generator


def fixture(format):
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (123, 45, 67)).save(output, format=format)
    return output.getvalue()


def payload(data):
    return {"data": [{"b64_json": base64.b64encode(data).decode()}]}


class ImageValidationTests(unittest.TestCase):
    def test_genuine_supported_formats(self):
        for format in ("PNG", "JPEG", "WEBP"):
            with self.subTest(format=format):
                data = fixture(format)
                self.assertEqual(generator.image_bytes_from_payload(payload(data)), data)

    def test_signatures_and_truncated_images_rejected(self):
        cases = [b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF\x00\x00\x00\x00WEBP"]
        cases += [fixture(format)[:len(fixture(format)) // 2] for format in ("PNG", "JPEG", "WEBP")]
        for data in cases:
            with self.subTest(data=data[:12]), self.assertRaises(ValueError):
                generator.image_bytes_from_payload(payload(data))

    def test_invalid_pixels_and_oversized_dimensions_rejected(self):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        for width, height, compressed in ((2, 2, b"not-zlib"), (10000, 10000, zlib.compress(b"\x00"))):
            data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", compressed) + chunk(b"IEND", b""))
            with self.assertRaises(ValueError):
                generator.image_bytes_from_payload(payload(data))

    def test_corrupt_response_keeps_previous_output(self):
        class Response(io.BytesIO):
            pass
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "image.png"
            previous = fixture("PNG")
            output.write_bytes(previous)
            def opener(request, timeout):
                return Response(json.dumps(payload(b"\x89PNG\r\n\x1a\n")).encode())
            with patch.dict(os.environ, {"GPTX_API_KEY": "fake-test-key"}, clear=True), self.assertRaises(ValueError):
                generator.generate_image("prompt", output, opener=opener)
            self.assertEqual(output.read_bytes(), previous)
            self.assertEqual(list(Path(tmp).iterdir()), [output])

    def test_direct_atomic_write_rejects_corrupt_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "image.png"
            output.write_bytes(fixture("PNG"))
            with self.assertRaises(ValueError):
                generator._write_atomic(output, b"\xff\xd8\xff")
            self.assertEqual(output.read_bytes(), fixture("PNG"))

    def test_byte_limit_checked_before_decode(self):
        with patch.object(generator, "MAX_RESPONSE_BYTES", 8), self.assertRaises(ValueError):
            generator.image_bytes_from_payload(payload(fixture("PNG")))
