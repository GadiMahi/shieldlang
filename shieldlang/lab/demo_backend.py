"""Deliberately simple demonstration order/payment backend with an independent effect ledger.

This module intentionally imports nothing from the gateway or protocol packages: it is the ground truth that the
monitor's own logs are compared against. It is *not* secure on purpose — it does not enforce payment-before-confirm,
ownership, or single use — so effects reached through an unprotected path are visible in the ledger.

Fault injection (header `X-Demo-Fault`): none | decline | drop_after_commit | delay_commit:<ms> |
error500_after_commit | error500_no_effect.   Rendezvous (header `X-Demo-Barrier: name:count:timeout_ms`).
Loopback only. Demonstration semantics, not a model of any real payment product.
"""
from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional


class _Barriers:
    """Named rendezvous points: a request waits until `count` requests arrived (or the timeout passes)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, Any]] = {}

    def wait(self, name: str, count: int, timeout_ms: int) -> bool:
        with self._lock:
            st = self._state.setdefault(name, {"n": 0, "ev": threading.Event()})
            st["n"] += 1
            if st["n"] >= count:
                st["ev"].set()
            ev = st["ev"]
        ok = ev.wait(timeout_ms / 1000.0)
        return ok


class DemoBackend:
    def __init__(self, db_path: Optional[str] = None, host: str = "127.0.0.1", port: int = 0):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("demo backend binds loopback only")
        self.db_path = db_path or ":memory:"
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._barriers = _Barriers()
        self._seq = 0
        with self._lock:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, n INTEGER, owner TEXT, client_ref TEXT, state TEXT, item TEXT);
                CREATE TABLE IF NOT EXISTS ledger(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, op TEXT, order_id TEXT,
                                                  principal TEXT, request_id TEXT, detail TEXT);
            """)
        backend = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "DemoBackend/1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                backend._dispatch(self, "GET")

            def do_POST(self):
                backend._dispatch(self, "POST")

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.request_queue_size = 128
        self.port = self.httpd.server_address[1]
        self._thread: Optional[threading.Thread] = None

    # ---- lifecycle ----
    def start(self) -> "DemoBackend":
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="demo-backend")
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        with self._lock:
            self._db.close()

    # ---- ledger access (used by tests/pages; not through the gateway) ----
    def ledger(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT seq,ts,op,order_id,principal,request_id,detail FROM ledger ORDER BY seq").fetchall()
        return [{"seq": r[0], "ts": r[1], "op": r[2], "order_id": r[3], "principal": r[4], "request_id": r[5],
                 "detail": json.loads(r[6]) if r[6] else {}} for r in rows]

    def orders(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT id,owner,client_ref,state,item FROM orders ORDER BY n").fetchall()
        return [{"id": r[0], "owner": r[1], "client_ref": r[2], "state": r[3], "item": r[4]} for r in rows]

    def reset(self):
        with self._lock:
            self._db.execute("DELETE FROM orders")
            self._db.execute("DELETE FROM ledger")
            self._db.execute("DELETE FROM sqlite_sequence")
        self._barriers = _Barriers()

    def _effect(self, op, order_id, principal, request_id, detail=None):
        self._db.execute("INSERT INTO ledger(ts,op,order_id,principal,request_id,detail) VALUES (?,?,?,?,?,?)",
                         (time.time(), op, order_id, principal, request_id, json.dumps(detail or {})))

    # ---- HTTP handling ----
    def _send(self, h, code, obj):
        body = json.dumps(obj).encode()
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Connection", "close")
        h.end_headers()
        h.wfile.write(body)

    @staticmethod
    def _drop(h):
        h.close_connection = True
        try:
            h.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _dispatch(self, h, method):
        path = h.path.split("?")[0]
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n) if n else b""
        principal = h.headers.get("X-Gateway-Principal", "anonymous")
        rid = h.headers.get("X-Request-Id", "")
        fault = (h.headers.get("X-Demo-Fault") or "none").strip()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = None
        parts = [p for p in path.split("/") if p]
        if parts[:1] == ["__ledger"]:
            return self._send(h, 200, self.ledger())
        if parts[:1] == ["__orders"]:
            return self._send(h, 200, self.orders())
        if parts[:1] == ["__reset"] and method == "POST":
            self.reset()
            return self._send(h, 200, {"ok": True})
        barrier = h.headers.get("X-Demo-Barrier")
        if barrier and method == "POST":
            try:
                name, count, tmo = barrier.split(":")
                self._barriers.wait(name, int(count), int(tmo))
            except ValueError:
                pass
        if parts[:2] == ["api", "orders"]:
            if method == "POST" and len(parts) == 2:
                return self._create(h, body, principal, rid, fault)
            if method == "GET" and len(parts) == 4 and parts[2] == "by-ref":
                with self._lock:
                    r = self._db.execute("SELECT id,owner,state FROM orders WHERE client_ref=?", (parts[3],)).fetchone()
                return self._send(h, 200, {"id": r[0], "owner": r[1], "state": r[2]}) if r else self._send(h, 404, {"error": "not_found"})
            if method == "GET" and len(parts) == 3:
                with self._lock:
                    r = self._db.execute("SELECT id,owner,state FROM orders WHERE id=?", (parts[2],)).fetchone()
                return self._send(h, 200, {"id": r[0], "owner": r[1], "state": r[2]}) if r else self._send(h, 404, {"error": "not_found"})
            if method == "POST" and len(parts) == 4 and parts[3] in ("pay", "confirm"):
                return self._transition(h, parts[2], parts[3], body, principal, rid, fault)
        self._send(h, 404, {"error": "no_such_route"})

    def _create(self, h, body, principal, rid, fault):
        if not isinstance(body, dict) or not isinstance(body.get("client_ref"), str):
            return self._send(h, 400, {"error": "client_ref required"})
        ref = body["client_ref"]
        with self._lock:
            existing = self._db.execute("SELECT id,state FROM orders WHERE client_ref=? AND owner=?", (ref, principal)).fetchone()
            if existing:  # application-level correlation: same ref+owner is the same order
                return self._send(h, 200, {"id": existing[0], "state": existing[1], "owner": principal, "replayed": True})
            self._seq += 1
            n = self._db.execute("SELECT COALESCE(MAX(n),0)+1 FROM orders").fetchone()[0]
            oid = f"ord_{n}"
            self._db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)", (oid, n, principal, ref, "created", str(body.get("item", ""))))
            self._effect("create", oid, principal, rid, {"client_ref": ref})
        if fault == "drop_after_commit":
            return self._drop(h)
        if fault == "error500_after_commit":
            return self._send(h, 500, {"error": "internal"})
        self._send(h, 201, {"id": oid, "state": "created", "owner": principal})

    def _transition(self, h, oid, op, body, principal, rid, fault):
        if fault == "error500_no_effect":
            return self._send(h, 500, {"error": "internal"})
        with self._lock:
            exists = self._db.execute("SELECT 1 FROM orders WHERE id=?", (oid,)).fetchone()
        if not exists:
            return self._send(h, 404, {"error": "not_found"})
        if op == "pay":
            if not isinstance(body, dict) or not isinstance(body.get("card_token"), str):
                return self._send(h, 400, {"error": "card_token required"})
            if fault == "decline":
                return self._send(h, 402, {"error": "card_declined"})
        if fault.startswith("delay_commit:"):
            time.sleep(int(fault.split(":")[1]) / 1000.0)
        with self._lock:
            new_state = "paid" if op == "pay" else "confirmed"
            self._db.execute("UPDATE orders SET state=? WHERE id=?", (new_state, oid))
            self._effect(op, oid, principal, rid, {"fault": fault})
        if fault == "drop_after_commit":
            return self._drop(h)
        if fault == "error500_after_commit":
            return self._send(h, 500, {"error": "internal"})
        self._send(h, 200, {"id": oid, "state": new_state})


def ledger_violations(ledger: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Independent lifecycle checker over the backend ledger only (never consults the monitor).

    Lifecycle: create → pay → confirm, each at most once, by the order's creator.
    """
    owners: Dict[str, str] = {}
    seen: Dict[str, List[str]] = {}
    out = []
    for e in ledger:
        oid, op, who = e["order_id"], e["op"], e["principal"]
        if op == "create":
            owners[oid] = who
            seen[oid] = ["create"]
            continue
        if oid not in owners:
            out.append({"seq": e["seq"], "violation": "effect_on_unknown_order", "order": oid})
            continue
        hist = seen[oid]
        if owners[oid] != who:
            out.append({"seq": e["seq"], "violation": "cross_principal_effect", "order": oid, "by": who})
        if op in hist:
            out.append({"seq": e["seq"], "violation": f"repeated_{op}", "order": oid})
        if op == "confirm" and "pay" not in hist:
            out.append({"seq": e["seq"], "violation": "confirm_without_pay", "order": oid})
        if "confirm" in hist and op != "confirm":
            out.append({"seq": e["seq"], "violation": "effect_after_terminal", "order": oid})
        hist.append(op)
    return out
