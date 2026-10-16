"""Local gateway/adapter shared by Protocol Studio, Bypass Lab and Traffic Lab.

enforce: policy and protocol checks run before the protected operation; denied requests never reach the upstream.
observe: every request is forwarded; the gateway records what enforcement *would* have done (shadow protocol state is isolated).
Upstream is a fixed loopback port chosen at construction; request data can never select a destination.
Principal comes from the adapter's identity table (fixed demo tokens), never from a client-claimed header value.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

from ..protocol.faults import crash_point
from .httpsubset import HttpReject, Limits, RawRequest, read_request
from .pipeline import ViewError, standard_view

DEMO_IDENTITIES = {"demo-token-alice": "alice", "demo-token-bob": "bob", "demo-token-carol": "carol"}
PASS_HEADERS = ("content-type",)
LAB_HEADERS = ("x-demo-fault", "x-demo-barrier")   # demonstration hooks understood only by the demo backend
MAX_RESPONSE = 1 << 20


@dataclass
class UpstreamResult:
    kind: str                    # response | not_delivered | timeout | ambiguous
    status: Optional[int] = None
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    error: str = ""
    elapsed_ms: float = 0.0


@dataclass
class GatewayResult:
    status: int
    headers: Dict[str, str]
    body: bytes
    record: Dict[str, Any]


def _excerpt(b: bytes, n: int = 400) -> str:
    return b[:n].decode("utf-8", "replace") + ("…" if len(b) > n else "")


class Gateway:
    def __init__(self, compiled, upstream_port: int, *, mode: str = "enforce", engine=None,
                 identities: Optional[Dict[str, str]] = None, limits: Optional[Limits] = None,
                 view_builder: Optional[Callable[[RawRequest], Dict[str, Any]]] = None,
                 upstream_timeout: float = 2.0, artifacts: Optional[Dict[str, Any]] = None, gid: str = "gw",
                 allow_lab_headers: bool = True, verbose_denials: bool = True):
        if mode not in ("enforce", "observe"):
            raise ValueError("mode must be enforce or observe")
        if not compiled.ok:
            raise ValueError("policy has compile errors")
        self.compiled = compiled
        self.vm = compiled.vm(True)
        self.upstream_port = int(upstream_port)
        self.mode = mode
        self.engine = engine
        self.identities = dict(DEMO_IDENTITIES if identities is None else identities)
        self.limits = limits or Limits()
        self.view_builder = view_builder or standard_view
        self.upstream_timeout = upstream_timeout
        self.artifacts = artifacts or {}
        self.gid = gid
        self.allow_lab_headers = allow_lab_headers
        self.verbose_denials = verbose_denials
        self._n = 0
        self._lock = threading.Lock()
        self.records: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    # ---- upstream ----
    def forward(self, raw: RawRequest, principal: Optional[str], request_id: str) -> UpstreamResult:
        t0 = time.perf_counter()
        hdrs = {"X-Request-Id": request_id}
        for k, v in raw.headers:
            low = k.lower()
            if low in PASS_HEADERS or (self.allow_lab_headers and low in LAB_HEADERS):
                hdrs[k] = v
        if principal:
            hdrs["X-Gateway-Principal"] = principal
        conn = http.client.HTTPConnection("127.0.0.1", self.upstream_port, timeout=self.upstream_timeout)
        try:
            try:
                conn.connect()
            except OSError as exc:
                return UpstreamResult("not_delivered", error=f"connect failed: {exc}", elapsed_ms=(time.perf_counter() - t0) * 1e3)
            try:
                conn.request(raw.method, raw.target, body=raw.body or None, headers=hdrs)
                resp = conn.getresponse()
                body = resp.read(MAX_RESPONSE + 1)
                return UpstreamResult("response", resp.status, {k: v for k, v in resp.getheaders()}, body[:MAX_RESPONSE],
                                      elapsed_ms=(time.perf_counter() - t0) * 1e3)
            except (socket.timeout, TimeoutError) as exc:
                return UpstreamResult("timeout", error=f"timeout after send: {exc}", elapsed_ms=(time.perf_counter() - t0) * 1e3)
            except (http.client.HTTPException, OSError) as exc:
                return UpstreamResult("ambiguous", error=f"{type(exc).__name__}: {exc}", elapsed_ms=(time.perf_counter() - t0) * 1e3)
        finally:
            conn.close()

    # ---- main entry ----
    def handle(self, raw: RawRequest, token: Optional[str] = None, hooks: Optional[Dict[str, Callable]] = None) -> GatewayResult:
        hooks = hooks or {}
        t_start = time.perf_counter()
        with self._lock:
            self._n += 1
            rid = f"{self.gid}-{self._n}"
        principal = self.identities.get(token) if token else None
        enforce = self.mode == "enforce"
        rec: Dict[str, Any] = {
            "id": rid, "mode": self.mode, "principal": principal,
            "request": {"method": raw.method, "target": raw.target, "body_bytes": len(raw.body),
                        "body_text": raw.body[:2048].decode("utf-8", "replace"),
                        "headers": {k: ("***" if k.lower() == "authorization" else v) for k, v in raw.headers}},
            "stages": [], "enforced": enforce, "timings_ms": {},
        }
        timings = rec["timings_ms"]

        def finish(status, body_obj, decision, extra_headers=None, body_bytes=None):
            timings["total"] = round((time.perf_counter() - t_start) * 1e3, 3)
            rec["decision"] = decision
            rec["client_status"] = status
            hdrs = {"Content-Type": "application/json", "X-ShieldLang-Decision": decision, "X-ShieldLang-Run": rid}
            hdrs.update(extra_headers or {})
            body = body_bytes if body_bytes is not None else json.dumps(body_obj).encode()
            with self._lock:
                self.records[rid] = rec
                while len(self.records) > 2000:
                    self.records.popitem(last=False)
            return GatewayResult(status, hdrs, body, rec)

        # --- stateless policy ---
        t0 = time.perf_counter()
        would_reasons: List[str] = []
        try:
            req = self.view_builder(raw)
        except ViewError as exc:
            rec["stages"].append({"stage": "policy", "allowed": False, "errors": [{"code": exc.code, "message": exc.message}]})
            would_reasons.append(exc.code)
            rec["would_decision"] = "deny"
            rec["would_reasons"] = would_reasons
            if enforce:
                return finish(exc.status, {"decision": "deny", "stage": "http", "error": exc.code, "message": exc.message}, "deny")
            req = None
        verdict = None
        if req is not None:
            verdict = self.vm.run(req, trace=False)
            timings["policy"] = round((time.perf_counter() - t0) * 1e3, 3)
            rec["stages"].append({"stage": "policy", "allowed": verdict["allowed"], "rule": verdict.get("rule"),
                                  "errors": verdict["errors"], "instructions": verdict["instructions"],
                                  "dfa_steps": verdict["dfa_steps"]})
            if not verdict["allowed"]:
                would_reasons.append("policy")
                rec["would_decision"] = "deny"
                rec["would_reasons"] = would_reasons
                if enforce:
                    return finish(403, {"decision": "deny", "stage": "policy", "rule": verdict.get("rule"),
                                        "errors": verdict["errors"]}, "deny")
        rule = verdict.get("rule") if verdict else None
        engine = self.engine
        event = engine.event_for(rule) if (engine and rule) else None
        adm = None
        if event:
            rec["protocol"] = {"name": event["protocol"], "event": rule, "kind": event["kind"]}
        if event and verdict and verdict["allowed"]:
            if principal is None:
                would_reasons.append("unauthenticated")
                if enforce:
                    rec["would_decision"] = "deny"
                    rec["would_reasons"] = would_reasons
                    return finish(401, {"decision": "deny", "stage": "identity", "error": "unauthenticated"}, "deny")
            if hooks.get("before_admit"):
                hooks["before_admit"]()
            t1 = time.perf_counter()
            adm = engine.admit(principal=principal, rule=rule, params=verdict.get("params") or {},
                               body=req.get("body") if isinstance(req.get("body"), dict) else {}, request_id=rid)
            timings["admit"] = round((time.perf_counter() - t1) * 1e3, 3)
            rec["stages"].append({"stage": "protocol", "allowed": adm.allowed, "reason": adm.reason, "tx": adm.tx,
                                  "key": adm.key, "from": adm.from_state, "to": adm.to_state, "shadow": engine.shadow,
                                  "detail": adm.detail})
            if not adm.allowed:
                would_reasons.append(adm.reason or "protocol")
            if not adm.allowed and enforce:
                rec["would_decision"] = "deny"
                rec["would_reasons"] = would_reasons
                code = {"no_such_resource_or_not_owner": 403, "binding_mismatch": 422}.get(adm.reason, 409)
                body = {"decision": "deny", "stage": "protocol", "protocol": event["protocol"]}
                body["reason"] = adm.reason if self.verbose_denials else "denied"
                return finish(code, body, "deny")
        rec["would_decision"] = "deny" if would_reasons else "allow"
        rec["would_reasons"] = would_reasons
        # --- forward ---
        reserved = adm is not None and adm.tx is not None and not engine.shadow
        if reserved:
            engine.mark_sent(adm.tx)
            crash_point("after_send_intent")
        up = self.forward(raw, principal, rid)
        crash_point("after_upstream_response")
        timings["upstream"] = round(up.elapsed_ms, 3)
        rec["upstream"] = {"kind": up.kind, "status": up.status, "error": up.error, "body_excerpt": _excerpt(up.body)}
        settlement = None
        if adm is not None and adm.tx is not None and not engine.shadow:
            t2 = time.perf_counter()
            settlement = engine.settle(adm, up.kind, up.status, up.body)
            timings["settle"] = round((time.perf_counter() - t2) * 1e3, 3)
            rec["outcome_class"] = settlement["kind"]
            rec["settlement"] = settlement
            crash_point("after_outcome_persisted")
        elif adm is not None and engine.shadow:
            rec["shadow"] = engine.shadow_apply(adm, event, up.kind, up.status, up.body)
        if settlement is not None and enforce:
            if settlement["kind"] == "unknown":
                return finish(502, {"decision": "unknown", "tx": adm.tx, "evidence": settlement["evidence"],
                                    "message": "outcome not definitive; this resource is blocked pending reconciliation"},
                              "unknown")
            if up.kind != "response":
                return finish(502, {"decision": "not_delivered", "tx": adm.tx, "evidence": settlement["evidence"]},
                              "not_delivered")
        if up.kind != "response":
            return finish(502, {"decision": "upstream_error", "kind": up.kind, "error": up.error}, "upstream_error")
        decision = "allow" if enforce else "forwarded"
        return finish(up.status, None, decision, body_bytes=up.body,
                      extra_headers={"Content-Type": up.headers.get("Content-Type", "application/json")})


class GatewayServer:
    def __init__(self, gateway: Gateway, host: str = "127.0.0.1", port: int = 0):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("gateway binds loopback only")
        gw = gateway
        self.gateway = gw

        class Handler(BaseHTTPRequestHandler):
            server_version = "ShieldLangGateway/1"

            def log_message(self, *a):
                pass

            def handle_expect_100(self):
                self._reject(HttpReject(417, "unsupported_expect", "Expect is not supported"))
                return False

            def _reject(self, exc: HttpReject):
                body = json.dumps({"decision": "reject", "stage": "http", "error": exc.code, "message": exc.message}).encode()
                try:
                    self.send_response(exc.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("X-ShieldLang-Decision", "reject")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def _serve(self):
                try:
                    raw = read_request(self, gw.limits)
                except HttpReject as exc:
                    return self._reject(exc)
                auth = raw.header("authorization") or ""
                token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
                result = gw.handle(raw, token)   # completes (and persists the outcome) even if the client has gone
                try:
                    self.send_response(result.status)
                    for k, v in result.headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(result.body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if raw.method != "HEAD":
                        self.wfile.write(result.body)
                except OSError:
                    result.record["client_disconnected"] = True

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _serve

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.request_queue_size = 128
        self.port = self.httpd.server_address[1]
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "GatewayServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="gateway")
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
