"""A tiny, deliberately simple loopback application used only to confirm Bypass Lab fixtures by local replay.

Binds 127.0.0.1 only. It is a fixture, not a product: its virtual file tree and duplicate-key handling are declared
by the fixture under test (`app_stages`), using the same named transforms as transforms.py — never invented ad hoc.
"""
from __future__ import annotations

import json
import posixpath
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

from . import rawjson, transforms

VIRTUAL_FILES = {
    "public/report.txt": "quarterly report (ordinary, in-bounds file)",
    "secret.txt": "TOP SECRET: this file is outside the public/ directory",
}


def _apply_str_stages(value: str, stages: List[str]) -> str:
    named = [s for s in stages if not s.startswith("dup:")]
    out, _ = transforms.apply_stages(value, named)
    return out


class LabApp:
    def __init__(self, app_stages: List[str], host: str = "127.0.0.1", port: int = 0):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("lab app binds loopback only")
        self.app_stages = app_stages
        self.last: Optional[dict] = None
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "LabApp/1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if "?" not in self.path:
                    return self._send(400, {"error": "missing query"})
                route, qs = self.path.split("?", 1)
                params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
                if route == "/file":
                    raw = params.get("name", "")
                    decoded = _apply_str_stages(raw, app.app_stages)
                    norm = posixpath.normpath("public/" + decoded)
                    content = VIRTUAL_FILES.get(norm)
                    app.last = {"endpoint": "file", "raw": raw, "decoded": decoded, "resolved_path": norm,
                               "served": content is not None}
                    if content is None:
                        return self._send(404, {"error": "not_found", "resolved_path": norm})
                    return self._send(200, {"path": norm, "content": content})
                self._send(404, {"error": "no_such_route"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                text = raw.decode("utf-8", "replace")
                if self.path == "/echo_role":
                    policy = "last" if "dup:last" in app.app_stages else "first"
                    try:
                        value, count = rawjson.lookup(text, "role", duplicates=policy)
                    except rawjson.RawJsonError as exc:
                        return self._send(400, {"error": str(exc)})
                    app.last = {"endpoint": "echo_role", "raw": text, "effective": value, "duplicate_count": count,
                               "duplicate_policy": policy}
                    return self._send(200, {"effective": value, "duplicate_count": count})
                if self.path == "/reflect":
                    try:
                        value, _ = rawjson.lookup(text, "comment", duplicates="last")
                    except rawjson.RawJsonError as exc:
                        return self._send(400, {"error": str(exc)})
                    stored = _apply_str_stages(value, app.app_stages) if isinstance(value, str) else value
                    app.last = {"endpoint": "reflect", "raw": text, "stored": stored}
                    return self._send(200, {"stored": stored})
                self._send(404, {"error": "no_such_route"})

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = self.httpd.server_address[1]
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "LabApp":
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="labapp")
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
