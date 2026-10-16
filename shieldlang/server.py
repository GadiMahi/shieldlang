"""
Zero-dependency HTTP server for the ShieldLang Studio web interface.

    GET  /                    → web/demo.html (guided presentation)
    GET  /studio              → web/index.html (original workbench)
    GET  /protocols           → web/protocols.html (Protocol Studio)
    GET  /lab/bypasses        → web/bypasses.html (Bypass Lab)
    GET  /traffic             → web/traffic.html (Traffic Lab)
    GET  /static/<file>       → web assets
    GET  /api/examples        → bundled example programs (+ sample requests)
    POST /api/compile         {source, name}               → full compiler output
    POST /api/run             {source, request, name}      → VM execution (optimised and unoptimised)
    POST /api/regex           {pattern, flags}             → RegexShield v2 artifact
    POST /api/bench           {pattern, flags}             → backtracking vs DFA timings

Protocol Studio / Traffic Lab / Bypass Lab routes are implemented in shieldlang/lab/api.py and dispatched below;
see that module's docstrings for the full route list.
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
from .lab import api as lab_api

MAX_BODY = 4 * 1024 * 1024   # bytes; larger POST bodies get 413 without being read

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
        pages = {"/": "demo.html", "/index.html": "demo.html", "/demo": "demo.html",
                 "/demo/": "demo.html", "/studio": "index.html", "/studio/": "index.html",
                 "/protocols": "protocols.html", "/protocols/": "protocols.html",
                 "/traffic": "traffic.html", "/traffic/": "traffic.html",
                 "/lab/bypasses": "bypasses.html", "/lab/bypasses/": "bypasses.html"}
        if path in pages:
            path = "/static/" + pages[path]
        if path == "/api/examples":
            return self._json(examples())
        lab_get = _lab_get_route(path)
        if lab_get is not None:
            if not self._loopback_client():
                return self._json({"error": "forbidden", "message": "the lab API only answers loopback clients"}, 403)
            fn, args = lab_get
            try:
                code, obj = fn(*args, None)
            except Exception as exc:
                traceback.print_exc()
                code, obj = 500, {"error": "internal_error", "message": f"{type(exc).__name__}: {exc}"}
            return self._json(obj, code)
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
            if n > MAX_BODY:
                self.close_connection = True
                return self._json({"error": "payload_too_large", "message": f"request body over {MAX_BODY} bytes"}, 413)
            data = json.loads(self.rfile.read(n) or b"{}")
            path = self.path.split("?")[0]
            if path == "/api/compile":
                res = _compiled(data.get("source", ""), data.get("name", "rules.shl"))
                return self._json(res.to_json())
            if path == "/api/compare":
                from .verification import compare_policies
                return self._json(compare_policies(data.get("before", ""), data.get("after", "")))
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
            lab_post = _lab_post_route(path)
            if lab_post is not None:
                if not self._loopback_client():
                    return self._json({"error": "forbidden", "message": "the lab API only answers loopback clients"}, 403)
                fn, args = lab_post
                code, obj = fn(*args, data)
                return self._json(obj, code)
            self._json({"error": "unknown endpoint"}, 404)
        except Exception as exc:  # report, never crash the server
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _loopback_client(self) -> bool:
        """The lab API creates sessions and stores data; refuse any peer that is not on this machine."""
        host = self.client_address[0]
        return host in ("127.0.0.1", "::1") or host.startswith("::ffff:127.")

    def do_DELETE(self):
        path = self.path.split("?")[0]
        try:
            parts = [p for p in path.split("/") if p]
            if not self._loopback_client():
                code, obj = 403, {"error": "forbidden", "message": "the lab API only answers loopback clients"}
            elif parts[:2] == ["api", "traffic"] and len(parts) == 4 and parts[2] == "runs":
                code, obj = lab_api.traffic_run_delete(parts[3], None)
            elif parts[:2] == ["api", "traffic"] and len(parts) == 4 and parts[2] == "recordings":
                code, obj = lab_api.traffic_recording_delete(parts[3], None)
            elif parts[:2] == ["api", "protocol"] and len(parts) == 4 and parts[2] == "sessions":
                code, obj = lab_api.protocol_session_close(parts[3], None)
            else:
                code, obj = 404, {"error": "unknown endpoint"}
        except Exception as exc:
            traceback.print_exc()
            code, obj = 500, {"error": f"{type(exc).__name__}: {exc}"}
        self._json(obj, code)


def _lab_get_route(path: str):
    """GET routes implemented by shieldlang/lab/api.py. Returns (handler, positional_args) or None."""
    parts = [p for p in path.split("/") if p]
    if parts[:2] == ["api", "protocol"]:
        if parts[2:] == ["examples"]:
            return lab_api.protocol_examples, ()
        if len(parts) == 4 and parts[2] == "sessions":
            return lab_api.protocol_session_get, (parts[3],)
        if parts[2:] == ["runs"]:
            return lab_api.protocol_runs_list, ()
        if len(parts) == 4 and parts[2] == "runs":
            return lab_api.protocol_run_get, (parts[3],)
        if len(parts) == 4 and parts[2] == "source":
            return lab_api.protocol_source_get, (parts[3],)
    if parts[:2] == ["api", "traffic"]:
        if parts[2:] == ["recordings"]:
            return lab_api.traffic_recordings, ()
        if parts[2:] == ["policies"]:
            return lab_api.traffic_policies, ()
        if len(parts) == 4 and parts[2] == "bypass":
            return lab_api.traffic_bypass_handoff_get, (parts[3],)
        if len(parts) == 4 and parts[2] == "recordings":
            return lab_api.traffic_recording_get, (parts[3],)
        if parts[2:] == ["runs"]:
            return lab_api.traffic_runs_list, ()
        if len(parts) == 4 and parts[2] == "runs":
            return lab_api.traffic_run_get, (parts[3],)
    if parts[:2] == ["api", "bypass"]:
        if parts[2:] == ["fixtures"]:
            return lab_api.bypass_fixtures, ()
        if parts[2:] == ["runs"]:
            return lab_api.bypass_runs_list, ()
        if len(parts) == 4 and parts[2] == "runs":
            return lab_api.bypass_run_get, (parts[3],)
    return None


def _lab_post_route(path: str):
    """POST routes implemented by shieldlang/lab/api.py. Returns (handler, positional_args) or None."""
    parts = [p for p in path.split("/") if p]
    if parts[:2] == ["api", "protocol"]:
        if parts[2:] == ["compile"]:
            return lab_api.protocol_compile, ()
        if parts[2:] == ["sessions"]:
            return lab_api.protocol_session_create, ()
        if len(parts) == 5 and parts[2] == "sessions":
            action = {"request": lab_api.protocol_session_request, "reconcile": lab_api.protocol_session_reconcile,
                     "operator": lab_api.protocol_session_operator, "restart": lab_api.protocol_session_restart,
                     "evidence": lab_api.protocol_save_evidence}.get(parts[4])
            if action:
                return action, (parts[3],)
    if parts[:2] == ["api", "traffic"]:
        if parts[2:] == ["run"]:
            return lab_api.traffic_run, ()
        if parts[2:] == ["recordings"]:
            return lab_api.traffic_recording_import, ()
        if parts[2:] == ["recordings", "check"]:
            return lab_api.traffic_recording_check, ()
        if parts[2:] == ["export"]:
            return lab_api.traffic_export, ()
    if parts[:2] == ["api", "bypass"]:
        if parts[2:] == ["run"]:
            return lab_api.bypass_run, ()
        if parts[2:] == ["search"]:
            return lab_api.bypass_search, ()
        if parts[2:] == ["minimize"]:
            return lab_api.bypass_minimize, ()
    return None


def serve(host: str = "127.0.0.1", port: int = 8080, open_browser: bool = True):
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"ShieldLang demo: {url}")
    print(f"ShieldLang Studio: {url}studio  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
