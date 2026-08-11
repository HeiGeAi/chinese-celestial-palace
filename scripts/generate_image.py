#!/usr/bin/env python3
import argparse
import base64
import binascii
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://api.gptx.cc/v1"
DEFAULT_MODEL = "gpt-image-2"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
IMAGE_SIGNATURES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF")


def _is_loopback(hostname):
    return hostname in {"127.0.0.1", "localhost", "::1"}


def image_endpoint(base_url):
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("GPTX_BASE_URL 必须是有效的 HTTP(S) URL")
    if parsed.scheme != "https" and not _is_loopback(parsed.hostname):
        raise ValueError("GPTX_BASE_URL 必须使用 HTTPS")
    clean = base_url.rstrip("/")
    return clean if clean.endswith("/images/generations") else f"{clean}/images/generations"


def _read_limited(response, limit=MAX_RESPONSE_BYTES):
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("生图接口响应超过 64 MiB 安全上限")
    return data


def _http_error(error, secret=None):
    try:
        payload = json.loads(error.read(4097)[:4096])
        detail = payload.get("error", {}).get("message") or payload.get("message")
    except (AttributeError, json.JSONDecodeError, UnicodeDecodeError):
        detail = None
    if detail:
        detail = re.sub(r"(?i)Bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", str(detail))
        if secret:
            detail = detail.replace(secret, "[REDACTED]")
    suffix = f"：{detail[:300]}" if detail else ""
    return RuntimeError(f"生图接口 HTTP {error.code}{suffix}")


def _valid_image(data):
    if data.startswith(IMAGE_SIGNATURES[:2]):
        return True
    return data.startswith(b"RIFF") and data[8:12] == b"WEBP"


def image_bytes_from_payload(payload, opener=urlopen, timeout=300):
    data = payload.get("data") if isinstance(payload, dict) else None
    candidate = data[0] if isinstance(data, list) and data else None
    if not isinstance(candidate, dict):
        raise ValueError("生图响应缺少 data[0]")

    encoded = candidate.get("b64_json")
    if encoded:
        if encoded.startswith("data:"):
            encoded = encoded.partition(",")[2]
        try:
            image = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("生图响应包含无效 base64") from error
    elif candidate.get("url"):
        image_url = candidate["url"]
        parsed = urlsplit(image_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("生图结果 URL 必须使用 HTTPS")
        request = Request(image_url, headers={"Accept": "image/*", "User-Agent": "Chinese-Celestial-Palace/1.0"})
        try:
            with opener(request, timeout=timeout) as response:
                content_type = response.headers.get_content_type()
                if not content_type.startswith("image/"):
                    raise ValueError("生图结果 URL 未返回图片")
                image = _read_limited(response)
        except HTTPError as error:
            raise _http_error(error) from error
        except URLError as error:
            raise RuntimeError(f"下载生图结果失败：{error.reason}") from error
    else:
        raise ValueError("生图响应缺少 b64_json 或 url")

    if not image or not _valid_image(image):
        raise ValueError("生图响应不是受支持的 PNG、JPEG 或 WebP 图片")
    return image


def _write_atomic(output, data):
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.", delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp_path, output)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return output


def generate_image(
    prompt,
    output,
    size="1536x2048",
    api_key=None,
    base_url=None,
    model=None,
    timeout=300,
    opener=urlopen,
):
    key = os.environ.get("GPTX_API_KEY") if api_key is None else api_key
    if not key:
        raise ValueError("缺少 GPTX_API_KEY 环境变量")
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    if not re.fullmatch(r"(?:\d{3,5}x\d{3,5}|[124]K)", size):
        raise ValueError("size 必须是像素尺寸或 1K、2K、4K")
    model = model or os.environ.get("GPTX_IMAGE_MODEL", DEFAULT_MODEL)
    if not model.strip():
        raise ValueError("GPTX_IMAGE_MODEL 不能为空")
    endpoint = image_endpoint(base_url or os.environ.get("GPTX_BASE_URL", DEFAULT_BASE_URL))
    body = json.dumps(
        {"model": model, "prompt": prompt, "n": 1, "size": size},
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Chinese-Celestial-Palace/1.0",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            raw = _read_limited(response)
    except HTTPError as error:
        raise _http_error(error, key) from error
    except URLError as error:
        raise RuntimeError(f"生图接口连接失败：{error.reason}") from error
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("生图接口返回了无效 JSON") from error
    return _write_atomic(output, image_bytes_from_payload(payload, opener=opener, timeout=timeout))


def _prompt_from_args(args):
    if args.prompt is not None:
        return args.prompt
    path = Path(args.prompt_file).expanduser()
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("提示词文件超过 1 MiB")
    return path.read_text(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="使用 GPTX 兼容 Images API 生成一张图片")
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file")
    parser.add_argument("--output", required=True)
    parser.add_argument("--size", default="1536x2048")
    parser.add_argument("--base-url", default=os.environ.get("GPTX_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("GPTX_IMAGE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    try:
        result = generate_image(
            _prompt_from_args(args),
            args.output,
            size=args.size,
            base_url=args.base_url,
            model=args.model,
            timeout=args.timeout,
        )
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"错误：{error}\n")
    print(result)


if __name__ == "__main__":
    main()
