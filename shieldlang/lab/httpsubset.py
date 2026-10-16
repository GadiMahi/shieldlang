"""The HTTP subset the lab gateway supports, enforced explicitly (this is not a production HTTP parser).

Supported: HTTP/1.0 and HTTP/1.1 origin-form requests, methods GET HEAD POST PUT PATCH DELETE OPTIONS, a single
Content-Length body (raw bytes, bounded *before* any decoding), one request per connection (Connection: close).
Rejected explicitly: Transfer-Encoding (chunked), Content-Encoding other than identity, Expect, Upgrade, absolute-form or
authority-form targets, targets with control/non-ASCII characters or fragments, duplicate or non-numeric Content-Length,
bodies on GET/HEAD, bodies above the limit, non-JSON bodies at the policy stage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

ALLOWED_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
BODY_METHODS = {"POST", "PUT", "PATCH"}


class HttpReject(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Limits:
    max_body: int = 64 * 1024
    max_target: int = 2048
    read_timeout: float = 5.0


@dataclass
class RawRequest:
    method: str
    target: str
    path: str
    query: str
    headers: List[Tuple[str, str]]
    body: bytes = b""
    version: str = "HTTP/1.1"

    def header(self, name: str) -> Optional[str]:
        low = name.lower()
        for k, v in self.headers:
            if k.lower() == low:
                return v
        return None


def split_target(target: str) -> Tuple[str, str]:
    if "?" in target:
        p, q = target.split("?", 1)
        return p, q
    return target, ""


def check_target(target: str, limits: Limits):
    if len(target) > limits.max_target:
        raise HttpReject(414, "target_too_long", "request target too long")
    if not target.startswith("/") or target.startswith("//"):
        raise HttpReject(400, "unsupported_target_form", "only origin-form targets starting with a single '/' are supported")
    if "#" in target:
        raise HttpReject(400, "fragment_in_target", "fragments are not valid in a request target")
    if any(ord(c) < 0x21 or ord(c) > 0x7e for c in target):
        raise HttpReject(400, "invalid_target_characters", "control, space or non-ASCII characters in target (percent-encode them)")


def read_request(handler, limits: Limits) -> RawRequest:
    """Build a RawRequest from a BaseHTTPRequestHandler after its own request-line/header parse."""
    if handler.request_version not in ("HTTP/1.0", "HTTP/1.1"):
        raise HttpReject(505, "unsupported_version", "only HTTP/1.0 and HTTP/1.1 are supported")
    method = handler.command
    if method not in ALLOWED_METHODS:
        raise HttpReject(501, "unsupported_method", f"method {method} is not supported")
    target = handler.path
    check_target(target, limits)
    hdrs = handler.headers
    names = {k.lower() for k in hdrs.keys()}
    if "transfer-encoding" in names:
        raise HttpReject(501, "unsupported_transfer_encoding", "Transfer-Encoding (chunked) is not supported; send Content-Length")
    ce = hdrs.get_all("content-encoding") or []
    if any(v.strip().lower() not in ("", "identity") for v in ce):
        raise HttpReject(415, "unsupported_content_encoding", "Content-Encoding other than identity is not supported")
    if "expect" in names:
        raise HttpReject(417, "unsupported_expect", "Expect is not supported")
    if "upgrade" in names:
        raise HttpReject(400, "unsupported_upgrade", "Upgrade is not supported")
    cls = hdrs.get_all("content-length") or []
    if len(cls) > 1:
        raise HttpReject(400, "duplicate_content_length", "multiple Content-Length headers")
    length = 0
    if cls:
        raw_len = cls[0].strip()
        if not raw_len.isascii() or not raw_len.isdigit():
            raise HttpReject(400, "invalid_content_length", "Content-Length must be a non-negative decimal integer")
        length = int(raw_len)
    elif method in BODY_METHODS:
        raise HttpReject(411, "length_required", "Content-Length is required for this method")
    if length > limits.max_body:
        raise HttpReject(413, "body_too_large", f"body exceeds {limits.max_body} bytes")
    if length and method in ("GET", "HEAD"):
        raise HttpReject(400, "body_not_allowed", f"{method} with a body is not supported")
    body = b""
    if length:
        try:
            handler.connection.settimeout(limits.read_timeout)
            body = handler.rfile.read(length)
        except OSError:
            raise HttpReject(408, "read_timeout", "timed out reading the request body")
        if len(body) != length:
            raise HttpReject(400, "incomplete_body", "connection closed before the declared body was received")
    path, query = split_target(target)
    return RawRequest(method, target, path, query, [(k, v) for k, v in hdrs.items()], body, handler.request_version)
