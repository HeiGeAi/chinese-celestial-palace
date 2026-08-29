#!/usr/bin/env python3
import argparse
import base64
import binascii
import json
import os
import re
import stat
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_BASE_URL = "https://api.gptx.cc/v1"
DEFAULT_MODEL = "gpt-image-2"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
IMAGE_SIGNATURES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


_NO_REDIRECT_OPEN = build_opener(_NoRedirect).open


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


def image_bytes_from_payload(payload, opener=None, timeout=300):
    opener = opener or _NO_REDIRECT_OPEN
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


def _open_output_parent(output):
    output = Path(os.path.abspath(os.path.expanduser(os.fspath(output))))
    if output.parts[:2] == ("/", "var") and Path("/var").resolve() == Path("/private/var"):
        output = Path("/private/var", *output.parts[2:])
    elif output.parts[:2] == ("/", "tmp") and Path("/tmp").resolve() == Path("/private/tmp"):
        output = Path("/private/tmp", *output.parts[2:])
    parts = output.parent.parts
    directory_fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[1:]:
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                os.mkdir(component, mode=0o755, dir_fd=directory_fd)
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except (NotADirectoryError, OSError) as error:
                raise ValueError("输出目录不能包含符号链接") from error
            os.close(directory_fd)
            directory_fd = next_fd
        try:
            existing = os.stat(output.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise ValueError("输出文件不能是符号链接")
        return output, directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _write_atomic(output, data):
    output, directory_fd = _open_output_parent(output)
    temp_name = f".{output.name}.{uuid.uuid4().hex}.tmp"
    temp_fd = None
    try:
        temp_fd = os.open(
            temp_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(temp_fd, "wb") as handle:
            temp_fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temp_name,
            output.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except Exception:
        if temp_fd is not None:
            os.close(temp_fd)
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(directory_fd)
    return output


def generate_image(
    prompt,
    output,
    size="1536x2048",
    base_url=None,
    model=None,
    timeout=300,
    opener=None,
):
    key = os.environ.get("GPTX_API_KEY")
    if not key:
        raise ValueError("缺少 GPTX_API_KEY 环境变量")
    opener = opener or _NO_REDIRECT_OPEN
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
