"""Protocol IR: a transition table plus an event map, with a canonical JSON form used for content hashing.

This is deliberately separate from the request-validation SIR (which has only forward jumps and no state).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Event:
    rule: str
    kind: str                      # create | step
    method: str
    path: str
    steps: List[Tuple[Optional[str], str]]   # (from, to); create has [(None, initial)]
    id_field: Optional[str] = None
    ref_field: Optional[str] = None
    probe_path: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["steps"] = [list(x) for x in self.steps]
        return d


@dataclass
class ProtocolIR:
    name: str
    resource: str
    states: List[str]
    initial: str
    terminals: List[str]
    key: str
    events: Dict[str, Event]
    bindings: List[Dict[str, str]] = field(default_factory=list)
    success: List[Tuple[int, int]] = field(default_factory=list)
    failure: List[Tuple[int, int]] = field(default_factory=list)
    contract: str = ""
    quiesce: float = 0.0
    reconcile: Optional[Dict[str, Any]] = None
    diagnostics_summary: Dict[str, Any] = field(default_factory=dict)

    def transitions(self) -> Dict[Tuple[str, str], str]:
        return {(frm, e.rule): to for e in self.events.values() if e.kind == "step" for frm, to in e.steps}

    def canonical(self) -> Dict[str, Any]:
        return {
            "name": self.name, "resource": self.resource, "states": list(self.states), "initial": self.initial,
            "terminals": sorted(self.terminals), "key": self.key,
            "events": [self.events[k].to_json() for k in sorted(self.events)],
            "bindings": sorted(self.bindings, key=lambda b: json.dumps(b, sort_keys=True)),
            "success": [list(r) for r in self.success], "failure": [list(r) for r in self.failure],
            "contract": self.contract, "quiesce": self.quiesce, "reconcile": self.reconcile,
        }

    @property
    def hash(self) -> str:
        data = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(data.encode()).hexdigest()[:16]

    def classify(self, status: int) -> str:
        if any(lo <= status <= hi for lo, hi in self.success):
            return "success"
        if any(lo <= status <= hi for lo, hi in self.failure):
            return "failure"
        return "unknown"

    def graph(self) -> Dict[str, Any]:
        return {"nodes": [{"id": s, "initial": s == self.initial, "terminal": s in self.terminals} for s in self.states],
                "edges": [{"from": frm or "(new)", "to": to, "rule": e.rule, "kind": e.kind}
                          for e in self.events.values() for frm, to in e.steps]}

    def to_json(self) -> Dict[str, Any]:
        d = self.canonical()
        d["hash"] = self.hash
        d["graph"] = self.graph()
        d["diagnostics_summary"] = self.diagnostics_summary
        return d
