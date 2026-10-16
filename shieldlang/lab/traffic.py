"""Traffic Lab core: a local environment (demo backend + gateway), recorded-request replay, and labeled evaluation.

Recording format (JSON): {"name", "description", "entries": [{"id", "identity", "method", "path", "headers", "body",
"label": {"legitimate": bool}, "fault", "save_as", "concurrent_group"}]}. "{{var}}" in path/body is replaced by the `id`
of an earlier response saved with save_as. Effects are judged from the backend ledger, never from gateway logs.
"""
from __future__ import annotations

import http.client
import json
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..compiler import compile_source
from .artifacts import ArtifactStore, canonical as canonical_json, content_hash
from .demo_backend import DemoBackend, ledger_violations
from .gateway import Gateway, GatewayServer

TOKENS = {"alice": "demo-token-alice", "bob": "demo-token-bob", "carol": "demo-token-carol"}
MAX_ENTRIES = 200
_VAR = re.compile(r"\{\{(\w+)\}\}")


class RecordingError(ValueError):
    pass


ENTRY_KEYS = {"id", "identity", "method", "path", "headers", "body", "label", "fault", "save_as", "concurrent_group"}
TOP_KEYS = {"name", "description", "entries"}
METHODS = ("GET", "POST")
ALLOWED_HEADERS = {"content-type"}
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_VARNAME = re.compile(r"^[A-Za-z_]\w{0,31}$")
_FAULT = re.compile(r"^(decline|drop_after_commit|error500_after_commit|error500_no_effect|delay_commit:[0-9]{1,4})$")
MAX_RECORDING_BYTES = 256 * 1024
MAX_BODY_BYTES = 8 * 1024
MAX_BODY_DEPTH = 6


def _depth(v: Any, d: int = 0) -> int:
    if isinstance(v, dict):
        return max([_depth(x, d + 1) for x in v.values()] + [d + 1])
    if isinstance(v, list):
        return max([_depth(x, d + 1) for x in v] + [d + 1])
    return d


def _closest(word: str, options) -> str:
    import difflib
    m = difflib.get_close_matches(word, sorted(options), n=1)
    return f" (did you mean '{m[0]}'?)" if m else ""


def check_recording(doc: Any) -> Dict[str, Any]:
    """Validate a recording completely and report *every* problem with a JSON-path-like location.

    Returns {"errors": [{"path", "message"}], "warnings": [...]}; a recording is acceptable iff there are no errors.
    Bounds exist so an imported file cannot make the lab do unbounded work, and paths are restricted to the demo
    backend's /api/ routes so a recording cannot reach its internal endpoints (e.g. /__reset) via the gateway.
    """
    errs: List[Dict[str, str]] = []
    warns: List[Dict[str, str]] = []

    def err(path, msg):
        if len(errs) < 40:
            errs.append({"path": path, "message": msg})

    if not isinstance(doc, dict):
        return {"errors": [{"path": "$", "message": "a recording must be a JSON object"}], "warnings": []}
    try:
        size = len(canonical_json(doc).encode("utf-8"))
    except (TypeError, ValueError):
        return {"errors": [{"path": "$", "message": "recording is not plain JSON data"}], "warnings": []}
    if size > MAX_RECORDING_BYTES:
        err("$", f"recording is {size} bytes; the limit is {MAX_RECORDING_BYTES}")
    for k in doc:
        if k not in TOP_KEYS:
            warns.append({"path": f"$.{k}", "message": "unknown top-level field; it is ignored and not stored"})
    if "name" in doc and not (isinstance(doc["name"], str) and 0 < len(doc["name"]) <= 80 and doc["name"].isprintable()):
        err("$.name", "name must be a printable string of 1-80 characters")
    if "description" in doc and not (isinstance(doc["description"], str) and len(doc["description"]) <= 400):
        err("$.description", "description must be a string of at most 400 characters")
    entries = doc.get("entries")
    if not isinstance(entries, list):
        err("$.entries", "recording must have an 'entries' list")
        return {"errors": errs, "warnings": warns}
    if not entries:
        err("$.entries", "recording has no entries")
    if len(entries) > MAX_ENTRIES:
        err("$.entries", f"too many entries ({len(entries)}); the limit is {MAX_ENTRIES}")
    seen_ids, saved_vars, labelled = set(), set(), 0
    groups: List[Any] = []
    for i, e in enumerate(entries[:MAX_ENTRIES]):
        at = f"$.entries[{i}]"
        if not isinstance(e, dict):
            err(at, "entry must be an object")
            continue
        for k in e:
            if k not in ENTRY_KEYS:
                err(f"{at}.{k}", f"unknown field '{k}'{_closest(k, ENTRY_KEYS)}")
        eid = e.get("id")
        if not (isinstance(eid, str) and _ID.match(eid)):
            err(f"{at}.id", "id must be 1-64 characters from letters, digits, '_', '.', '-'")
        elif eid in seen_ids:
            err(f"{at}.id", f"duplicate entry id '{eid}'")
        else:
            seen_ids.add(eid)
        if e.get("method") not in METHODS:
            err(f"{at}.method", f"method must be one of {', '.join(METHODS)} (what the demo backend serves)")
        path = e.get("path")
        if not isinstance(path, str) or not path:
            err(f"{at}.path", "path must be a non-empty string")
        else:
            if not path.startswith("/api/"):
                err(f"{at}.path", "path must start with /api/ (the demo backend's internal endpoints are not replayable)")
            if len(path) > 300 or any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in path) or "://" in path:
                err(f"{at}.path", "path must be a plain path: at most 300 characters, no whitespace, control characters, backslashes or URLs")
            for m in _VAR.finditer(path):
                if m.group(1) not in saved_vars:
                    err(f"{at}.path", f"'{{{{{m.group(1)}}}}}' is used before any earlier entry saves it with save_as")
        body = e.get("body")
        if body is not None:
            try:
                bsz = len(canonical_json(body).encode("utf-8"))
            except (TypeError, ValueError):
                bsz = -1
                err(f"{at}.body", "body must be plain JSON data")
            if bsz > MAX_BODY_BYTES:
                err(f"{at}.body", f"body is {bsz} bytes; the limit is {MAX_BODY_BYTES}")
            elif bsz >= 0 and _depth(body) > MAX_BODY_DEPTH:
                err(f"{at}.body", f"body nests deeper than {MAX_BODY_DEPTH} levels")
            if bsz >= 0:
                for m in _VAR.finditer(canonical_json(body)):
                    if m.group(1) not in saved_vars:
                        err(f"{at}.body", f"'{{{{{m.group(1)}}}}}' is used before any earlier entry saves it with save_as")
        if e.get("identity") not in (None, *TOKENS):
            err(f"{at}.identity", f"unknown demo identity {e.get('identity')!r}; use one of {', '.join(TOKENS)} or omit it")
        if "label" in e:
            lab = e["label"]
            if not (isinstance(lab, dict) and set(lab) == {"legitimate"} and isinstance(lab["legitimate"], bool)):
                err(f"{at}.label", 'label must be exactly {"legitimate": true|false}')
            else:
                labelled += 1
        if e.get("fault") is not None and not (isinstance(e["fault"], str) and _FAULT.match(e["fault"])):
            err(f"{at}.fault", "fault must be one of decline, drop_after_commit, error500_after_commit, error500_no_effect, delay_commit:<ms 0-9999>")
        if "headers" in e and e["headers"] is not None:
            h = e["headers"]
            if not (isinstance(h, dict) and all(isinstance(k, str) and isinstance(v, str) and len(v) <= 200 for k, v in h.items())):
                err(f"{at}.headers", "headers must be an object of short strings")
            else:
                for k in h:
                    if k.lower() not in ALLOWED_HEADERS:
                        err(f"{at}.headers.{k}", f"header '{k}' is not allowed (allowed: {', '.join(sorted(ALLOWED_HEADERS))}; use 'fault' for fault injection)")
        sv = e.get("save_as")
        if sv is not None:
            if not (isinstance(sv, str) and _VARNAME.match(sv)):
                err(f"{at}.save_as", "save_as must be a short identifier (letters, digits, '_')")
            elif sv in saved_vars:
                err(f"{at}.save_as", f"'{sv}' is already saved by an earlier entry")
            else:
                saved_vars.add(sv)
        g = e.get("concurrent_group")
        if g is not None and not (isinstance(g, str) and _ID.match(g)):
            err(f"{at}.concurrent_group", "concurrent_group must be a short identifier")
        groups.append(g)
    for i, g in enumerate(groups):  # replay fires *adjacent* entries with the same group together
        if g is not None and g in groups[:i] and groups[i - 1] != g:
            err(f"$.entries[{i}].concurrent_group", f"entries of group '{g}' must be adjacent")
    if entries and not labelled:
        warns.append({"path": "$.entries", "message": "no entry has a label, so the evaluation matrix will be empty (the run still works)"})
    return {"errors": errs, "warnings": warns}


def validate_recording(doc: Any) -> Dict[str, Any]:
    """Raise RecordingError (message = first problem, with its location) unless the recording is acceptable."""
    res = check_recording(doc)
    if res["errors"]:
        first = res["errors"][0]
        more = f" (+{len(res['errors']) - 1} more)" if len(res["errors"]) > 1 else ""
        raise RecordingError(f"{first['path']}: {first['message']}{more}")
    return doc


def normalized_recording(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Only the known fields, so unknown top-level data never reaches storage."""
    out = {k: doc[k] for k in ("name", "description") if k in doc}
    out["entries"] = doc["entries"]
    return out


# ---- which policies can the Traffic Lab meaningfully exercise? ----
DEMO_ROUTES = [("POST", ("api", "orders")), ("GET", ("api", "orders", "*")), ("GET", ("api", "orders", "by-ref", "*")),
               ("POST", ("api", "orders", "*", "pay")), ("POST", ("api", "orders", "*", "confirm"))]


def _route_shape(path: str):
    return tuple("*" if (p.startswith("{") and p.endswith("}")) else p for p in path.split("/") if p)


def rule_targets_demo_backend(method: str, path: str) -> bool:
    shape = _route_shape(path)
    return any(m == method and len(shape) == len(rs) and all(a == b or b == "*" or a == "*" for a, b in zip(shape, rs))
               for m, rs in DEMO_ROUTES)


def policy_support(compiled) -> Dict[str, Any]:
    """Whether a compiled policy can be exercised here: it must compile and at least one rule must target the demo backend."""
    if not compiled.ok:
        return {"supported": False, "reason": "the policy has compile errors"}
    rules = list(getattr(compiled.model, "rules", []) or [])
    hits = [r.name for r in rules if rule_targets_demo_backend(r.method, r.path)]
    if not hits:
        return {"supported": False, "relevant_rules": [],
                "reason": (f"none of its {len(rules)} rule(s) match the demo backend's order routes "
                           "(POST /api/orders, GET /api/orders/{id}, POST /api/orders/{id}/pay|confirm), so replaying it would only forward everything unchecked")}
    return {"supported": True, "relevant_rules": hits, "reason": ""}


def _subst(v: Any, env: Dict[str, str]) -> Any:
    if isinstance(v, str):
        return _VAR.sub(lambda m: env.get(m.group(1), m.group(0)), v)
    if isinstance(v, dict):
        return {k: _subst(x, env) for k, x in v.items()}
    if isinstance(v, list):
        return [_subst(x, env) for x in v]
    return v


def backend_prober(port: int):
    """GET against the fixed loopback backend port. Used for reconciliation evidence only."""
    def probe(path: str):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request("GET", path)
            r = conn.getresponse()
            data = r.read()
            try:
                return r.status, json.loads(data)
            except ValueError:
                return r.status, None
        except Exception:
            return None, None
        finally:
            conn.close()
    return probe


class LabEnv:
    """One backend + one gateway on ephemeral loopback ports; each env owns its own temporary state."""

    def __init__(self, policy_source: str, *, mode: str = "enforce", engine=None, name: str = "policy.shl",
                 protocol: Optional[str] = None, store_path: Optional[str] = None, engine_cls=None,
                 workdir: Optional[str] = None, **gw_kwargs):
        """protocol: name of a protocol in the policy to enforce (or shadow, in observe mode)."""
        self.compiled = compile_source(policy_source, name)
        if not self.compiled.ok:
            raise ValueError("policy does not compile")
        self.tmp = tempfile.TemporaryDirectory(prefix="shieldlang-env-") if workdir is None else None
        base = Path(workdir) if workdir else Path(self.tmp.name)
        base.mkdir(parents=True, exist_ok=True)
        self.store_path = store_path or str(base / "protocol.db")
        if protocol is not None and engine is None:
            from ..protocol.engine import ProtocolEngine, ProtocolStore
            ir = next(p for p in self.compiled.protocols if p.name == protocol)
            engine = (engine_cls or ProtocolEngine)(ir, ProtocolStore(self.store_path), shadow=(mode == "observe"))
        self.backend = DemoBackend(str(base / "backend.db")).start()
        self.gateway = Gateway(self.compiled, self.backend.port, mode=mode, engine=engine, **gw_kwargs)
        self.server = GatewayServer(self.gateway).start()
        self.engine = engine

    def prober(self):
        return backend_prober(self.backend.port)

    def close(self):
        for fn in (self.server.stop, self.backend.stop):
            try:
                fn()
            except Exception:
                pass
        try:
            if self.engine is not None and hasattr(self.engine, "close"):
                self.engine.close()
        except Exception:
            pass
        if self.tmp is not None:
            self.tmp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def send(self, method: str, path: str, *, identity: Optional[str] = None, body: Any = None,
             headers: Optional[Dict[str, str]] = None, raw_body: Optional[bytes] = None, timeout: float = 10.0):
        hdrs = dict(headers or {})
        if identity:
            hdrs["Authorization"] = "Bearer " + TOKENS[identity]
        data = raw_body
        if data is None and body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=timeout)
        try:
            conn.request(method, path, body=data, headers=hdrs)
            r = conn.getresponse()
            payload = r.read()
            return r.status, {k: v for k, v in r.getheaders()}, payload
        finally:
            conn.close()


def replay(env: LabEnv, recording: Dict[str, Any], *, selected: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Replay entries in order against env; entries sharing a concurrent_group are fired simultaneously."""
    entries = [e for e in recording["entries"] if selected is None or e["id"] in selected]
    saved: Dict[str, str] = {}
    results: List[Dict[str, Any]] = []
    i = 0

    def fire(e):
        ent = _subst(e, saved)
        t0 = time.perf_counter()
        hdrs = dict(ent.get("headers") or {})
        if ent.get("fault"):
            hdrs["X-Demo-Fault"] = ent["fault"]
        try:
            status, rh, payload = env.send(ent["method"], ent["path"], identity=ent.get("identity"), body=ent.get("body"),
                                           headers=hdrs)
            err = None
        except Exception as exc:  # client-side transport error
            status, rh, payload, err = None, {}, b"", f"{type(exc).__name__}: {exc}"
        rec = env.gateway.records.get(rh.get("X-ShieldLang-Run", ""), {})
        try:
            parsed = json.loads(payload) if payload else None
        except ValueError:
            parsed = None
        return {"id": e["id"], "identity": ent.get("identity"), "method": ent["method"], "path": ent["path"],
                "label": e.get("label"), "status": status, "decision": rh.get("X-ShieldLang-Decision"),
                "would_decision": rec.get("would_decision"), "would_reasons": rec.get("would_reasons"),
                "run": rh.get("X-ShieldLang-Run"), "latency_ms": round((time.perf_counter() - t0) * 1e3, 2),
                "transport_error": err, "response": parsed, "gateway": rec, "_save": e.get("save_as"),
                "unpredicted": any(st.get("reason") == "shadow_diverged" for st in rec.get("stages", []))}

    while i < len(entries):
        g = entries[i].get("concurrent_group")
        if g is None:
            batch = [entries[i]]
        else:
            batch = [e for e in entries[i:] if e.get("concurrent_group") == g]
        out: List[Optional[Dict[str, Any]]] = [None] * len(batch)
        if len(batch) == 1:
            out[0] = fire(batch[0])
        else:
            def run(k):
                out[k] = fire(batch[k])
            ts = [threading.Thread(target=run, args=(k,)) for k in range(len(batch))]
            [t.start() for t in ts]
            [t.join() for t in ts]
        for r in out:
            if r["_save"] and isinstance(r["response"], dict) and "id" in r["response"]:
                saved[r["_save"]] = str(r["response"]["id"])
            r.pop("_save")
            results.append(r)
        i += len(batch)
    return results


def evaluate(results: List[Dict[str, Any]], mode: str) -> Dict[str, Any]:
    """Confusion matrix over *labeled* entries only. Positive class = illegitimate request; 'blocked' means the gateway
    denied it (enforce) or would have denied it (observe). Unknown outcomes count as blocked-for-this-request only if
    the gateway refused to forward; an Unknown after forwarding is reported separately."""
    tp = fp = tn = fn = unlabeled = unpredicted = 0
    rows = []
    for r in results:
        if not r["label"]:
            unlabeled += 1
            continue
        if r.get("unpredicted"):      # shadow state diverged from the real backend; its "allow" is not a prediction
            unpredicted += 1
            rows.append({"id": r["id"], "legitimate": r["label"]["legitimate"], "blocked": None, "unpredicted": True})
            continue
        legit = r["label"]["legitimate"]
        blocked = (r["would_decision"] == "deny") if mode == "observe" else (r["decision"] == "deny")
        if not legit and blocked: tp += 1
        elif not legit: fn += 1
        elif blocked: fp += 1
        else: tn += 1
        rows.append({"id": r["id"], "legitimate": legit, "blocked": blocked})
    return {"labeled": tp + fp + tn + fn, "unlabeled": unlabeled, "unpredicted": unpredicted, "true_positive": tp, "false_negative": fn,
            "false_positive": fp, "true_negative": tn, "rows": rows,
            "note": "Labels are supplied by the recording author, not inferred; counts cover labeled entries only. "
                    "'unpredicted' = labeled entries after the observe-mode shadow diverged from the backend; they are excluded from the matrix."}


def run_traffic(policy_source: str, recording: Dict[str, Any], *, mode: str = "enforce", protocol: Optional[str] = None,
                store: Optional[ArtifactStore] = None, selected: Optional[List[str]] = None) -> Dict[str, Any]:
    validate_recording(recording)
    with LabEnv(policy_source, mode=mode, protocol=protocol) as env:
        t0 = time.perf_counter()
        results = replay(env, recording, selected=selected)
        wall = round((time.perf_counter() - t0) * 1e3, 1)
        ledger = env.backend.ledger()
        viol = ledger_violations(ledger)
        lat = sorted(r["latency_ms"] for r in results)
        eng = env.engine
        ir = next((p for p in env.compiled.protocols if p.name == protocol), None) if protocol else None
        doc = {
            "kind": "traffic", "mode": mode,
            "versions": {"policy": content_hash(policy_source), "recording": content_hash(recording),
                         "protocol": ir.hash if ir else None},
            "protocol": protocol,
            "entries": [{k: v for k, v in r.items() if k != "gateway"} | {"stages": r["gateway"].get("stages"),
                         "timings_ms": r["gateway"].get("timings_ms"), "upstream": r["gateway"].get("upstream"),
                         "outcome_class": r["gateway"].get("outcome_class")} for r in results],
            "evaluation": evaluate(results, mode),
            "ledger": ledger, "ledger_violations": viol,
            "protocol_state": ({"resources": eng.resources(), "reservations": eng.reservations(), "timeline": eng.timeline()}
                               if eng is not None else None),
            "latency_ms": {"median": lat[len(lat) // 2] if lat else None, "max": lat[-1] if lat else None, "wall": wall},
            "evidence_note": "Effects are read from the backend ledger. Gateway records and monitor state describe decisions only."
                             + (" Observe mode: protocol state is a shadow that advances only on actual upstream success." if mode == "observe" else ""),
        }
    if store is not None:
        doc["run_id"] = store.put_run("traffic", doc)
    return doc
