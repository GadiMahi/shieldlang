"""Policy-side view of a raw request (standard profile). The bypass pipelines extend this in lab/bypass.py."""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict

from .httpsubset import RawRequest


class ViewError(Exception):
    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code, self.status, self.message = code, status, message


def _no_constant(name):
    raise ValueError(f"non-standard JSON constant {name}")


def parse_json_bytes(raw: bytes) -> Any:
    """Strict-ish JSON: UTF-8 only, no NaN/Infinity. Duplicate keys: last wins (Python json.loads; JS JSON.parse agrees)."""
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ViewError("invalid_json", 400, f"body is not valid JSON: {exc}")


def standard_view(raw: RawRequest) -> Dict[str, Any]:
    """Request dictionary evaluated by the policy VM. Path is routed raw (no decoding); query values are percent-decoded."""
    query = dict(urllib.parse.parse_qsl(raw.query, keep_blank_values=True))
    headers: Dict[str, str] = {}
    for k, v in raw.headers:
        headers[k] = f"{headers[k]}, {v}" if k in headers else v
    body: Any = {}
    if raw.body:
        ctype = (raw.header("content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ViewError("unsupported_media_type", 415, "only application/json bodies are supported")
        body = parse_json_bytes(raw.body)
    return {"method": raw.method, "path": raw.path, "headers": headers, "query": query, "body": body}
