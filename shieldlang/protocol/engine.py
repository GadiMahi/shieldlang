"""Protocol runtime: durable reservations, outcome classification, recovery, reconciliation (SQLite).

Semantics are specified in docs/protocol_semantics.md. Summary of the guarantees this module *tries* to provide, under the
stated assumptions (complete mediation, single host, one SQLite file):
  * at most one unresolved reservation per resource (compare-and-set inside a write transaction);
  * send intent is persisted before the upstream call, so a crash can produce a false Unknown but never a false release;
  * only an authoritative success commits; only a declared no-effect failure (or a connect failure) releases;
  * everything else is Unknown, which blocks the resource until reconciliation evidence arrives.
It makes no exactly-once claim and does not make the application idempotent.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .faults import crash_point
from .ir import ProtocolIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources(
  ns TEXT, protocol TEXT, key TEXT, owner TEXT, state TEXT, version INTEGER, data TEXT DEFAULT '{}',
  reserved_tx TEXT, unknown_tx TEXT, diverged INTEGER DEFAULT 0, created_at REAL,
  PRIMARY KEY(ns, protocol, key));
CREATE TABLE IF NOT EXISTS reservations(
  tx TEXT PRIMARY KEY, ns TEXT, protocol TEXT, key TEXT, kind TEXT, rule TEXT, principal TEXT, frm TEXT, to_state TEXT,
  ref TEXT, fields TEXT, status TEXT, request_id TEXT, created_at REAL, sent_at REAL, resolved_at REAL, evidence TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ns TEXT, protocol TEXT, tx TEXT, key TEXT, type TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_res_key ON reservations(ns, protocol, key, status);
"""


@dataclass
class Admission:
    allowed: bool
    reason: Optional[str] = None
    tx: Optional[str] = None
    key: Optional[str] = None
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)


class ProtocolStore:
    def __init__(self, path: str, synchronous: str = "FULL"):
        assert synchronous in ("FULL", "NORMAL", "OFF")   # only tests that do not exercise durability may relax this
        self.path = str(path)
        self.synchronous = synchronous
        with self._write() as c:
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    c.execute(stmt)

    def _connect(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute(f"PRAGMA synchronous={self.synchronous}")
        return c

    @contextlib.contextmanager
    def _write(self):
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    @contextlib.contextmanager
    def _read(self):
        c = self._connect()
        try:
            yield c
        finally:
            c.close()

    def clear(self, ns: str):
        with self._write() as c:
            for t in ("resources", "reservations", "events"):
                c.execute(f"DELETE FROM {t} WHERE ns=?", (ns,))


def _now() -> float:
    return time.time()


class ProtocolEngine:
    MAX_BOUND = 256

    def __init__(self, ir: ProtocolIR, store: ProtocolStore, *, shadow: bool = False, ns: Optional[str] = None,
                 clock: Callable[[], float] = _now, recover: bool = True):
        self.ir = ir
        self.store = store
        self.shadow = shadow
        self.ns = ns or ("shadow" if shadow else "enforced")
        self.clock = clock
        self.version = ir.hash
        self._trans = ir.transitions()
        if recover and not shadow:
            self.recover()

    # ---- identification ----
    def event_for(self, rule: str) -> Optional[Dict[str, str]]:
        ev = self.ir.events.get(rule)
        return {"protocol": self.ir.name, "kind": ev.kind} if ev else None

    def close(self):
        pass

    def _log(self, c, type_, tx=None, key=None, **detail):
        c.execute("INSERT INTO events(ts,ns,protocol,tx,key,type,detail) VALUES(?,?,?,?,?,?,?)",
                  (self.clock(), self.ns, self.ir.name, tx, key, type_, json.dumps(detail, sort_keys=True)))

    # ---- admission ----
    def _bound_values(self, rule: str, body: Dict[str, Any]) -> Dict[str, str]:
        """Values this event must remember for later bindings (a-side) – scalar strings only."""
        out = {}
        for b in self.ir.bindings:
            if b["a_event"] == rule:
                v = body.get(b["a_field"])
                if isinstance(v, str) and len(v.encode()) <= self.MAX_BOUND:
                    out[f'{rule}.{b["a_field"]}'] = v
        return out

    def _check_step(self, row, principal, rule, body) -> Tuple[Optional[str], Optional[str]]:
        if row is None or row["owner"] != principal:
            return "no_such_resource_or_not_owner", None
        if row["unknown_tx"] is not None:
            return "blocked_unknown", None
        if row["reserved_tx"] is not None:
            return "busy", None
        to = self._trans.get((row["state"], rule))
        if to is None:
            return "illegal_transition", None
        data = json.loads(row["data"] or "{}")
        for b in self.ir.bindings:
            if b["b_event"] == rule:
                want = data.get(f'{b["a_event"]}.{b["a_field"]}')
                have = body.get(b["b_field"])
                if want is None or not isinstance(have, str) or have != want:
                    return "binding_mismatch", None
        return None, to

    def admit(self, *, principal, rule, params, body, request_id="") -> Admission:
        ev = self.ir.events[rule]
        body = body if isinstance(body, dict) else {}
        if ev.kind == "create":
            return self._admit_create(principal, ev, body, request_id)
        key = (params or {}).get(self.ir.key)
        if not isinstance(key, str):
            return Admission(False, "no_such_resource_or_not_owner", detail={"why": "key parameter missing"})
        return self._admit_step(principal, rule, key, body, request_id)

    def _admit_step(self, principal, rule, key, body, request_id) -> Admission:
        with self.store._write() as c:
            row = c.execute("SELECT * FROM resources WHERE ns=? AND protocol=? AND key=?", (self.ns, self.ir.name, key)).fetchone()
            if self.shadow and row is not None and row["diverged"] and row["owner"] == principal:
                return Admission(True, "shadow_diverged", key=key, from_state=row["state"],
                                 detail={"diverged": True, "note": "shadow state no longer tracks this resource"})
            reason, to = self._check_step(row, principal, rule, body)
            if reason:
                self._log(c, "denied", None, key, rule=rule, principal=principal, reason=reason, shadow=self.shadow)
                return Admission(False, reason, key=key, from_state=row["state"] if row else None)
            if self.shadow:
                return Admission(True, key=key, from_state=row["state"], to_state=to,
                                 detail={"diverged": bool(row["diverged"])})
            tx = "tx-" + uuid.uuid4().hex[:12]
            ok = self._reserve_step(c, tx, row, principal, rule, to, key, body, request_id)
            if not ok:
                return Admission(False, "busy", key=key, from_state=row["state"])
        crash_point("after_reserve")
        return Admission(True, tx=tx, key=key, from_state=row["state"], to_state=to)

    def _reserve_step(self, c, tx, row, principal, rule, to, key, body, request_id) -> bool:
        """Atomic compare-and-set on the resource row; fails if anything changed since it was read."""
        n = c.execute(
            "UPDATE resources SET reserved_tx=?, version=version+1 WHERE ns=? AND protocol=? AND key=? AND version=? "
            "AND reserved_tx IS NULL AND unknown_tx IS NULL AND state=? AND owner=?",
            (tx, self.ns, self.ir.name, key, row["version"], row["state"], principal)).rowcount
        if n != 1:
            return False
        c.execute("INSERT INTO reservations(tx,ns,protocol,key,kind,rule,principal,frm,to_state,ref,fields,status,request_id,created_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (tx, self.ns, self.ir.name, key, "step", rule, principal, row["state"], to, None,
                   json.dumps(self._bound_values(rule, body)), "reserved", request_id, self.clock()))
        self._log(c, "reserved", tx, key, rule=rule, principal=principal, frm=row["state"], to=to)
        return True

    def _admit_create(self, principal, ev, body, request_id) -> Admission:
        ref = body.get(ev.ref_field) if ev.ref_field else None
        if ev.ref_field and not isinstance(ref, str):
            return Admission(False, "binding_mismatch", detail={"why": "create ref field missing"})
        with self.store._write() as c:
            if ref is not None:
                clash = c.execute("SELECT status FROM reservations WHERE ns=? AND protocol=? AND kind='create' AND principal=? AND ref=? "
                                  "AND status IN ('reserved','sent','unknown')", (self.ns, self.ir.name, principal, ref)).fetchone()
                if clash:
                    reason = "blocked_unknown" if clash["status"] == "unknown" else "busy"
                    self._log(c, "denied", None, None, rule=ev.rule, principal=principal, reason=reason, ref=ref, shadow=self.shadow)
                    return Admission(False, reason, detail={"ref": ref})
            if self.shadow:
                return Admission(True, to_state=ev.steps[0][1], detail={"ref": ref, "principal": principal})
            tx = "tx-" + uuid.uuid4().hex[:12]
            c.execute("INSERT INTO reservations(tx,ns,protocol,key,kind,rule,principal,frm,to_state,ref,fields,status,request_id,created_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (tx, self.ns, self.ir.name, None, "create", ev.rule, principal, None, ev.steps[0][1], ref,
                       json.dumps(self._bound_values(ev.rule, body)), "reserved", request_id, self.clock()))
            self._log(c, "reserved", tx, None, rule=ev.rule, principal=principal, ref=ref)
        crash_point("after_reserve")
        return Admission(True, tx=tx, to_state=ev.steps[0][1], detail={"ref": ref})

    # ---- send intent / settlement ----
    def mark_sent(self, tx: str):
        with self.store._write() as c:
            c.execute("UPDATE reservations SET status='sent', sent_at=? WHERE tx=? AND status='reserved'", (self.clock(), tx))
            self._log(c, "send_intent", tx, None)

    def classify(self, adm: Admission, kind: str, status: Optional[int], body: bytes) -> Tuple[str, Dict[str, Any], Optional[str]]:
        """-> (outcome: committed|released|unknown, evidence, new_key)"""
        ev = {"upstream": kind, "status": status}
        if kind == "not_delivered":
            return "released", dict(ev, reason="connect failed before any byte was sent"), None
        if kind != "response":
            return "unknown", dict(ev, reason="delivery/response ambiguous after send"), None
        cls = self.ir.classify(status)
        ev["class"] = cls
        if cls == "failure":
            return "released", dict(ev, reason="declared failure class (no effect per protocol contract)"), None
        if cls != "success":
            return "unknown", dict(ev, reason="status is in no declared class"), None
        new_key = None
        if adm.to_state is not None and adm.key is None:   # create
            id_field = self.ir.events[self._rule_of(adm.tx)].id_field
            try:
                doc = json.loads(body.decode("utf-8"))
                new_key = doc[id_field]
            except Exception:
                new_key = None
            if not isinstance(new_key, str) or not new_key or len(new_key) > 128:
                return "unknown", dict(ev, reason=f"success response without usable '{id_field}'"), None
        return "committed", dict(ev, reason="authoritative success"), new_key

    def _rule_of(self, tx):
        with self.store._read() as c:
            return c.execute("SELECT rule FROM reservations WHERE tx=?", (tx,)).fetchone()["rule"]

    def settle(self, adm: Admission, kind: str, status: Optional[int], body: bytes) -> Dict[str, Any]:
        outcome, evidence, new_key = self.classify(adm, kind, status, body)
        return self._apply(adm.tx, outcome, evidence, new_key)

    def _apply(self, tx: str, outcome: str, evidence: Dict[str, Any], new_key: Optional[str] = None,
               source: str = "upstream") -> Dict[str, Any]:
        with self.store._write() as c:
            r = c.execute("SELECT * FROM reservations WHERE tx=?", (tx,)).fetchone()
            if r is None or r["status"] not in ("reserved", "sent", "unknown"):
                return {"kind": "noop", "evidence": evidence}
            now = self.clock()
            final = outcome
            if outcome == "committed":
                if r["kind"] == "step":
                    c.execute("UPDATE resources SET state=?, reserved_tx=NULL, unknown_tx=NULL, data=? WHERE ns=? AND protocol=? AND key=?",
                              (r["to_state"], self._merged(c, r), self.ns, self.ir.name, r["key"]))
                else:
                    ex = c.execute("SELECT owner FROM resources WHERE ns=? AND protocol=? AND key=?", (self.ns, self.ir.name, new_key)).fetchone()
                    if ex is not None and ex["owner"] != r["principal"]:
                        final = "unknown"
                        evidence = dict(evidence, reason="created id already belongs to another principal", conflict=True)
                    elif ex is None:
                        c.execute("INSERT INTO resources(ns,protocol,key,owner,state,version,data,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                  (self.ns, self.ir.name, new_key, r["principal"], r["to_state"], 1, r["fields"], now))
                    else:
                        evidence = dict(evidence, adopted_existing=True)
                    c.execute("UPDATE reservations SET key=? WHERE tx=?", (new_key, tx))
            elif outcome == "released":
                if r["kind"] == "step":
                    c.execute("UPDATE resources SET reserved_tx=NULL, unknown_tx=NULL WHERE ns=? AND protocol=? AND key=?",
                              (self.ns, self.ir.name, r["key"]))
            if final == "unknown":
                if r["kind"] == "step":
                    c.execute("UPDATE resources SET reserved_tx=NULL, unknown_tx=? WHERE ns=? AND protocol=? AND key=?",
                              (tx, self.ns, self.ir.name, r["key"]))
                c.execute("UPDATE reservations SET status='unknown', evidence=? WHERE tx=?", (json.dumps(evidence), tx))
            else:
                c.execute("UPDATE reservations SET status=?, resolved_at=?, evidence=? WHERE tx=?", (final, now, json.dumps(evidence), tx))
            self._log(c, final, tx, r["key"] or new_key, source=source, **{k: v for k, v in evidence.items() if k != "source"})
        return {"kind": final, "evidence": evidence, "key": new_key or r["key"]}

    def _merged(self, c, r):
        row = c.execute("SELECT data FROM resources WHERE ns=? AND protocol=? AND key=?", (self.ns, self.ir.name, r["key"])).fetchone()
        d = json.loads(row["data"] or "{}")
        d.update(json.loads(r["fields"] or "{}"))
        return json.dumps(d, sort_keys=True)

    # ---- recovery ----
    def recover(self) -> Dict[str, int]:
        n_rel = n_unk = 0
        with self.store._write() as c:
            for r in c.execute("SELECT * FROM reservations WHERE ns=? AND protocol=? AND status IN ('reserved','sent')",
                               (self.ns, self.ir.name)).fetchall():
                if r["status"] == "reserved":      # no send intent was ever persisted -> nothing left the gateway
                    if r["kind"] == "step":
                        c.execute("UPDATE resources SET reserved_tx=NULL WHERE ns=? AND protocol=? AND key=? AND reserved_tx=?",
                                  (self.ns, self.ir.name, r["key"], r["tx"]))
                    c.execute("UPDATE reservations SET status='released', resolved_at=?, evidence=? WHERE tx=?",
                              (self.clock(), json.dumps({"source": "recovery", "reason": "crash before send intent"}), r["tx"]))
                    self._log(c, "released", r["tx"], r["key"], source="recovery")
                    n_rel += 1
                else:                              # send intent persisted, no outcome: may or may not have taken effect
                    if r["kind"] == "step":
                        c.execute("UPDATE resources SET reserved_tx=NULL, unknown_tx=? WHERE ns=? AND protocol=? AND key=?",
                                  (r["tx"], self.ns, self.ir.name, r["key"]))
                    c.execute("UPDATE reservations SET status='unknown', evidence=? WHERE tx=?",
                              (json.dumps({"source": "recovery", "reason": "crash after send intent, outcome not persisted"}), r["tx"]))
                    self._log(c, "unknown", r["tx"], r["key"], source="recovery")
                    n_unk += 1
        return {"released": n_rel, "unknown": n_unk}

    # ---- reconciliation ----
    def _unknown_for_key(self, key: str):
        with self.store._read() as c:
            return c.execute("SELECT * FROM reservations WHERE ns=? AND protocol=? AND key=? AND status='unknown' AND kind='step'",
                             (self.ns, self.ir.name, key)).fetchone()

    def reconcile_key(self, key: str, prober: Callable[[str], Tuple[Optional[int], Any]]) -> Dict[str, Any]:
        r = self._unknown_for_key(key)
        rc = self.ir.reconcile
        if r is None:
            return {"result": "nothing_to_reconcile"}
        if not rc:
            return {"result": "no_reconcile_declared", "tx": r["tx"]}
        path = rc["path"].replace("{key}", key)
        status, doc = prober(path)
        ev = {"source": "probe", "path": path, "status": status, "observed": doc if isinstance(doc, dict) else None}
        if status != 200 or not isinstance(doc, dict):
            return {"result": "inconclusive", "tx": r["tx"], "evidence": dict(ev, reason="probe did not return a readable resource")}
        if doc.get(rc["owner_field"]) != r["principal"]:
            return {"result": "conflict", "tx": r["tx"], "evidence": dict(ev, reason="owner differs from reservation principal")}
        observed = rc["mapping"].get(doc.get(rc["state_field"]))
        if observed is None:
            return {"result": "conflict", "tx": r["tx"], "evidence": dict(ev, reason="observed state has no mapping")}
        if observed == r["to_state"]:
            out = self._apply(r["tx"], "committed", dict(ev, reason="probe observed target state"), source="probe")
            return {"result": "committed", "tx": r["tx"], "evidence": out["evidence"]}
        if observed == r["frm"]:
            waited = self.clock() - (r["sent_at"] or r["created_at"])
            if waited < self.ir.quiesce:
                return {"result": "inconclusive", "tx": r["tx"],
                        "evidence": dict(ev, reason=f"observed source state, but only {waited:.2f}s < quiesce {self.ir.quiesce}s since send; "
                                         "the attempt may still be in flight")}
            out = self._apply(r["tx"], "released", dict(ev, reason="probe observed source state after quiesce"), source="probe")
            return {"result": "released", "tx": r["tx"], "evidence": out["evidence"]}
        return {"result": "conflict", "tx": r["tx"], "evidence": dict(ev, reason=f"observed '{observed}' is neither source nor target")}

    def reconcile_ref(self, principal: str, ref: str, prober) -> Dict[str, Any]:
        with self.store._read() as c:
            r = c.execute("SELECT * FROM reservations WHERE ns=? AND protocol=? AND kind='create' AND principal=? AND ref=? AND status='unknown'",
                          (self.ns, self.ir.name, principal, ref)).fetchone()
        if r is None:
            return {"result": "nothing_to_reconcile"}
        ev = self.ir.events[r["rule"]]
        rc = self.ir.reconcile
        if not ev.probe_path or not rc:
            return {"result": "no_reconcile_declared", "tx": r["tx"]}
        path = ev.probe_path.replace("{ref}", ref)
        status, doc = prober(path)
        evid = {"source": "probe", "path": path, "status": status, "observed": doc if isinstance(doc, dict) else None}
        if status == 404:
            waited = self.clock() - (r["sent_at"] or r["created_at"])
            if waited < self.ir.quiesce:
                return {"result": "inconclusive", "tx": r["tx"], "evidence": dict(evid, reason="not found, but within quiesce window")}
            out = self._apply(r["tx"], "released", dict(evid, reason="not found after quiesce"), source="probe")
            return {"result": "released", "tx": r["tx"], "evidence": out["evidence"]}
        if status != 200 or not isinstance(doc, dict) or not isinstance(doc.get("id"), str):
            return {"result": "inconclusive", "tx": r["tx"], "evidence": dict(evid, reason="unreadable probe response")}
        if doc.get(rc["owner_field"]) != principal:
            return {"result": "conflict", "tx": r["tx"], "evidence": dict(evid, reason="owner differs from reservation principal")}
        state = rc["mapping"].get(doc.get(rc["state_field"]))
        if state is None:
            return {"result": "conflict", "tx": r["tx"], "evidence": dict(evid, reason="observed state has no mapping")}
        evid["adopt_state"] = state
        out = self._apply_adopt(r, doc["id"], state, evid)
        return {"result": "committed", "tx": r["tx"], "key": doc["id"], "evidence": out}

    def _apply_adopt(self, r, key, state, evid):
        with self.store._write() as c:
            ex = c.execute("SELECT 1 FROM resources WHERE ns=? AND protocol=? AND key=?", (self.ns, self.ir.name, key)).fetchone()
            if ex is None:
                c.execute("INSERT INTO resources(ns,protocol,key,owner,state,version,data,created_at) VALUES(?,?,?,?,?,?,?,?)",
                          (self.ns, self.ir.name, key, r["principal"], state, 1, r["fields"], self.clock()))
            c.execute("UPDATE reservations SET status='committed', key=?, resolved_at=?, evidence=? WHERE tx=?",
                      (key, self.clock(), json.dumps(evid), r["tx"]))
            self._log(c, "committed", r["tx"], key, source="probe", adopted=True)
        return evid

    def operator_resolve(self, tx: str, decision: str, evidence_text: str, operator: str = "operator") -> Dict[str, Any]:
        if decision not in ("committed", "released"):
            raise ValueError("decision must be committed or released")
        if not isinstance(evidence_text, str) or len(evidence_text.strip()) < 8:
            raise ValueError("operator resolution requires evidence text (at least 8 characters)")
        with self.store._read() as c:
            r = c.execute("SELECT * FROM reservations WHERE tx=? AND ns=? AND protocol=?", (tx, self.ns, self.ir.name)).fetchone()
        if r is None or r["status"] != "unknown":
            raise ValueError("no unknown reservation with that id")
        if r["kind"] == "create":
            if decision == "committed":
                raise ValueError("a create cannot be committed by an operator without a resource key; use reconcile_ref or adopt")
            return self._apply(tx, "released", {"source": "operator", "operator": operator, "evidence_text": evidence_text}, source="operator")
        return self._apply(tx, decision, {"source": "operator", "operator": operator, "evidence_text": evidence_text}, source="operator")

    # ---- shadow ----
    def shadow_apply(self, adm: Admission, event, up_kind, up_status, up_body) -> Dict[str, Any]:
        """Advance shadow state only on actual upstream success; never on predictions."""
        cls = self.ir.classify(up_status) if up_kind == "response" else "unknown"
        info = {"upstream_class": cls, "would_admit": adm.allowed}
        if cls != "success":
            return info
        with self.store._write() as c:
            if adm.allowed:
                if adm.key is None:   # create
                    rule = next((r for r, e in self.ir.events.items() if e.kind == "create"), None)
                    try:
                        key = json.loads(up_body.decode())[self.ir.events[rule].id_field]
                    except Exception:
                        key = None
                    if isinstance(key, str):
                        c.execute("INSERT OR IGNORE INTO resources(ns,protocol,key,owner,state,version,data,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                  (self.ns, self.ir.name, key, adm.detail.get("principal", "unknown"), adm.to_state, 1, "{}", self.clock()))
                elif adm.to_state is not None:
                    c.execute("UPDATE resources SET state=?, version=version+1 WHERE ns=? AND protocol=? AND key=?",
                              (adm.to_state, self.ns, self.ir.name, adm.key))
            elif adm.key is not None and adm.reason in ("illegal_transition", "binding_mismatch", "busy", "blocked_unknown"):
                c.execute("UPDATE resources SET diverged=1 WHERE ns=? AND protocol=? AND key=?", (self.ns, self.ir.name, adm.key))
                info["diverged"] = True
            self._log(c, "shadow_applied", None, adm.key, **info)
        return info

    # ---- inspection ----
    def resources(self) -> List[Dict[str, Any]]:
        with self.store._read() as c:
            return [dict(r) for r in c.execute("SELECT * FROM resources WHERE ns=? AND protocol=? ORDER BY key", (self.ns, self.ir.name))]

    def reservations(self) -> List[Dict[str, Any]]:
        with self.store._read() as c:
            return [dict(r) for r in c.execute("SELECT * FROM reservations WHERE ns=? AND protocol=? ORDER BY created_at", (self.ns, self.ir.name))]

    def timeline(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self.store._read() as c:
            rows = c.execute("SELECT * FROM events WHERE ns=? AND protocol=? ORDER BY id DESC LIMIT ?", (self.ns, self.ir.name, limit)).fetchall()
        return [dict(r, detail=json.loads(r["detail"])) for r in reversed(rows)]
