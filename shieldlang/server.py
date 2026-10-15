"""
Zero-dependency HTTP server for the ShieldLang Studio web interface.

    GET  /                    → web/index.html
    GET  /static/<file>       → web assets
    GET  /api/examples        → bundled example programs (+ sample requests)
    POST /api/compile         {source, name}               → full compiler output
    POST /api/run             {source, request, name}      → VM execution (optimised and unoptimised)
    POST /api/regex           {pattern, flags}             → RegexShield v2 artifact
    POST /api/bench           {pattern, flags}             → backtracking vs DFA timings
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .bench import run_benchmark
from .compiler import compile_source
from .regex.compiler import compile_pattern
from .regex.parser import RegexSyntaxError

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
EXAMPLES = ROOT / "examples"

_cache: dict = {}
_lock = threading.Lock()


def _compiled(source: str, name: str):
    key = hashlib.sha256((name + "\0" + source).encode()).hexdigest()
    with _lock:
        if key in _cache:
            return _cache[key]
    res = compile_source(source, name)
    with _lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = res
    return res


def examples():
    out = []
    order = ["payments", "regex_lab", "diagnostics_demo"]
    files = sorted(EXAMPLES.glob("*.shl"), key=lambda p: (order.index(p.stem) if p.stem in order else 99, p.stem))
    for f in files:
        req = EXAMPLES / f"{f.stem}.requests.json"
        out.append({"name": f.name, "title": f.stem.replace("_", " ").title(),
                    "source": f.read_text(encoding="utf-8"),
                    "requests": json.loads(req.read_text(encoding="utf-8")) if req.exists() else []})
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "ShieldLang/1.0"

    def log_message(self, fmt, *args):  # quieter console
        if os.environ.get("SHIELDLANG_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            path = "/static/index.html"
        if path == "/api/examples":
            return self._json(examples())
        if path.startswith("/static/"):
            f = (WEB / path[len("/static/"):]).resolve()
            if WEB in f.parents and f.is_file():
                ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype.endswith("javascript"):
                    ctype += "; charset=utf-8"
                return self._send(200, f.read_bytes(), ctype)
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) or b"{}")
            path = self.path.split("?")[0]
            if path == "/api/compile":
                res = _compiled(data.get("source", ""), data.get("name", "rules.shl"))
                return self._json(res.to_json())
            if path == "/api/run":
                res = _compiled(data.get("source", ""), data.get("name", "rules.shl"))
                if not res.ok:
                    return self._json({"error": "Program has compile errors; fix them first."}, 400)
                req = data.get("request") or {}
                if isinstance(req, str):
                    req = json.loads(req)
                opt = res.vm(True).run(req)
                unopt = res.vm(False).run(req, trace=False)
                return self._json({"optimized": opt, "unoptimized": unopt})
            if path == "/api/regex":
                try:
                    art = compile_pattern(data.get("pattern", ""), data.get("flags", ""))
                except RegexSyntaxError as exc:
                    return self._json({"error": exc.to_dict()}, 400)
                return self._json(art.to_json())
            if path == "/api/bench":
                return self._json(run_benchmark(data.get("pattern", ""), data.get("flags", "")))
            self._json({"error": "unknown endpoint"}, 404)
        except Exception as exc:  # report, never crash the server
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def serve(host: str = "127.0.0.1", port: int = 8080, open_browser: bool = True):
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"ShieldLang Studio running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
