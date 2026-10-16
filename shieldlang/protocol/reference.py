"""Independent reference interpreter for protocol semantics.

Deliberately shares NOTHING with the runtime: it imports only the protocol AST node classes, builds its tables straight from
the parsed statements, keeps everything in memory, and has no store, threads, HTTP or SQL. It implements docs/protocol_semantics.md
sections 3-5 for *abstract traces*:

  {"op":"create","who":p,"ref":r,"out":"S|F|U|P"}
  {"op":"event","who":p,"rule":R,"key":k,"fields":{...},"out":"S|F|U|P"}     (P = reserved, outcome pending)
  {"op":"settle","key":k,"out":"S|F|U"}                                      (resolve a pending reservation)
  {"op":"reconcile","key":k,"observed":state,"quiesced":bool}               (evidence for an unknown reservation)
  {"op":"reconcile_ref","who":p,"ref":r,"found":bool}                        (evidence for an unknown create)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import ast as P


class Reference:
    def __init__(self, proto: P.Protocol):
        self.initial = None
        self.steps: Dict[tuple, str] = {}
        self.create_rule = None
        self.binds: List[tuple] = []
        self.recon: Dict[str, str] = {}
        for s in proto.stmts:
            if isinstance(s, P.InitialStmt):
                self.initial = s.name.name
            elif isinstance(s, P.StepStmt):
                self.steps[(s.frm.name, s.rule.name)] = s.to.name
            elif isinstance(s, P.CreateStmt):
                self.create_rule = s.rule.name
            elif isinstance(s, P.BindStmt):
                self.binds.append((s.a_event.name, s.a_field.name, s.b_event.name, s.b_field.name))
            elif isinstance(s, P.ReconcileStmt):
                self.recon = {k: v.name for k, v in s.mapping}
        self.res: Dict[str, Dict[str, Any]] = {}
        self.pending_creates: List[Dict[str, Any]] = []
        self._n = 0

    # ---- helpers ----
    def _new_key(self):
        self._n += 1
        return f"k{self._n}"

    def _commit_resource(self, r, res):
        r["state"] = res["to"]
        for k, v in res["fields"].items():
            r["stored"][(res["rule"], k)] = v
        r["res"] = None

    def applicable(self, op) -> bool:
        """Harness precondition only (settle/reconcile need something to act on)."""
        k = op["op"]
        if k == "settle":
            r = self.res.get(op["key"])
            return bool(r and r["res"] and r["res"]["status"] == "reserved")
        if k == "reconcile":
            r = self.res.get(op["key"])
            return bool(r and r["res"] and r["res"]["status"] == "unknown")
        if k == "reconcile_ref":
            return any(pc["who"] == op["who"] and pc["ref"] == op["ref"] and pc["status"] == "unknown" for pc in self.pending_creates)
        return True

    def run(self, trace: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for op in trace:
            out.append(getattr(self, "_" + op["op"])(op))
        return out

    # ---- operations ----
    def _create(self, op):
        who, ref = op["who"], op.get("ref")
        for pc in self.pending_creates:
            if pc["who"] == who and pc["ref"] == ref and pc["status"] == "unknown":
                return {"admitted": False, "reason": "blocked_unknown"}
        pc = {"who": who, "ref": ref, "status": "reserved"}
        o = op["out"]
        if o == "S":
            key = self._new_key()
            self.res[key] = {"owner": who, "state": self.initial, "version": 1, "stored": {}, "res": None}
            return {"admitted": True, "reason": None, "key": key}
        if o == "F":
            return {"admitted": True, "reason": None}
        pc["status"] = "unknown" if o == "U" else "pending"
        self.pending_creates.append(pc)
        return {"admitted": True, "reason": None}

    def _event(self, op):
        who, rule, key = op["who"], op["rule"], op["key"]
        r = self.res.get(key)
        if r is None or r["owner"] != who:
            return {"admitted": False, "reason": "no_such_resource_or_not_owner"}
        cur = r["res"]
        if cur is not None and cur["status"] == "unknown":
            return {"admitted": False, "reason": "blocked_unknown"}
        if cur is not None:
            return {"admitted": False, "reason": "busy"}
        to = self.steps.get((r["state"], rule))
        if to is None:
            return {"admitted": False, "reason": "illegal_transition"}
        fields = op.get("fields", {})
        for a_ev, a_f, b_ev, b_f in self.binds:
            if b_ev == rule:
                if (a_ev, a_f) not in r["stored"] or b_f not in fields or fields[b_f] != r["stored"][(a_ev, a_f)]:
                    return {"admitted": False, "reason": "binding_mismatch"}
        r["version"] += 1
        r["res"] = {"rule": rule, "frm": r["state"], "to": to, "fields": dict(fields), "status": "reserved"}
        self._resolve(r, op["out"])
        return {"admitted": True, "reason": None}

    def _resolve(self, r, out):
        res = r["res"]
        if out == "S":
            self._commit_resource(r, res)
        elif out == "F":
            r["res"] = None
        elif out == "U":
            res["status"] = "unknown"
        # "P": stays reserved

    def _settle(self, op):
        r = self.res[op["key"]]
        assert r["res"] is not None and r["res"]["status"] == "reserved"
        self._resolve(r, op["out"])
        return {"admitted": True, "reason": None}

    def _reconcile(self, op):
        r = self.res[op["key"]]
        res = r["res"]
        if res is None or res["status"] != "unknown":
            return {"result": "nothing_to_reconcile"}
        obs = op["observed"]
        if obs == res["to"]:
            self._commit_resource(r, res)
            return {"result": "committed"}
        if obs == res["frm"]:
            if op.get("quiesced"):
                r["res"] = None
                return {"result": "released"}
            return {"result": "inconclusive"}
        return {"result": "conflict"}

    def _reconcile_ref(self, op):
        for pc in self.pending_creates:
            if pc["who"] == op["who"] and pc["ref"] == op["ref"] and pc["status"] == "unknown":
                if op["found"]:
                    key = self._new_key()
                    self.res[key] = {"owner": op["who"], "state": self.initial, "version": 1, "stored": {}, "res": None}
                    pc["status"] = "committed"
                    return {"result": "committed", "key": key}
                pc["status"] = "released"
                return {"result": "released"}
        return {"result": "nothing_to_reconcile"}

    def snapshot(self) -> Dict[str, Any]:
        return {k: {"owner": r["owner"], "state": r["state"], "version": r["version"],
                    "unresolved": (r["res"]["status"] if r["res"] else None)} for k, r in sorted(self.res.items())}
