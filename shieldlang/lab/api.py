"""HTTP route handlers for Protocol Studio, Traffic Lab and Bypass Lab, kept out of server.py for size.

Each function takes the parsed JSON body (or query dict) and returns (status, obj). server.py just dispatches path ->
function and JSON-encodes the result. No handler ever talks to anything but loopback services this process itself started.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

from ..compiler import compile_source
from .artifacts import content_hash, default_store
from .bypass import run_fixture
from .bypass_search import search_fixture, minimize_fixture, fixture_version
from .bypass_fixtures import BY_ID, FIXTURES
from .demo_backend import ledger_violations
from .sessions import Registry
from .traffic import (RecordingError, check_recording, normalized_recording, policy_support, run_traffic,
                      validate_recording)

ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLES = ROOT / "examples"
REGISTRY = Registry()


def _err(status, code, message) -> Tuple[int, Dict[str, Any]]:
    return status, {"error": code, "message": message}


def _session(sid):
    s = REGISTRY.get(sid)
    if s is None:
        raise KeyError(sid)
    return s


def _session_view(s) -> Dict[str, Any]:
    eng = s.env.engine
    return {"session_id": s.id, "protocol": s.protocol_name, "mode": s.mode, "policy_hash": s.policy_hash,
            "protocol_hash": s.protocol_hash, "created": s.created,
            "resources": eng.resources() if eng else [], "reservations": eng.reservations() if eng else [],
            "timeline": eng.timeline() if eng else [], "ledger": s.env.backend.ledger(),
            "ledger_violations": ledger_violations(s.env.backend.ledger())}


# ---------------------------------------------------------------- Protocol Studio
def protocol_examples(_):
    out = []
    for f in sorted(EXAMPLES.glob("*.shl")):
        r = compile_source(f.read_text(encoding="utf-8"), f.name)
        if r.protocols:
            out.append({"name": f.name, "source": f.read_text(encoding="utf-8"),
                        "protocols": [p.name for p in r.protocols]})
    return 200, out


def protocol_compile(data):
    source = data.get("source", "")
    r = compile_source(source, data.get("name", "protocol.shl"))
    d = r.to_json()
    d["policy_hash"] = content_hash(source)
    return 200, d


def protocol_session_create(data):
    source = data.get("source", "")
    protocol = data.get("protocol")
    mode = data.get("mode", "enforce")
    if mode not in ("enforce", "observe"):
        return _err(400, "bad_mode", "mode must be 'enforce' or 'observe'")
    r = compile_source(source, data.get("name", "protocol.shl"))
    if not r.ok:
        return _err(400, "compile_error", "source has compile errors")
    names = [p.name for p in r.protocols]
    if protocol not in names:
        return _err(400, "no_such_protocol", f"no protocol named {protocol!r}; available: {names}")
    default_store().put_source(source, "policy", data.get("name", "protocol.shl"))
    s = REGISTRY.create(source, protocol, mode)
    v = _session_view(s)
    v["handoff"] = {"traffic_lab": f"/traffic?policy_hash={s.policy_hash}&protocol={protocol}"}
    return 200, v


def protocol_session_get(sid, _):
    try:
        s = _session(sid)
    except KeyError:
        return _err(404, "no_such_session", "session not found or expired")
    return 200, _session_view(s)


def protocol_session_request(sid, data):
    try:
        s = _session(sid)
    except KeyError:
        return _err(404, "no_such_session", "session not found or expired")
    headers = dict(data.get("headers") or {})
    if data.get("fault"):
        headers["X-Demo-Fault"] = data["fault"]
    status, rh, body = s.env.send(data.get("method", "GET"), data.get("path", "/"), identity=data.get("identity"),
                                  body=data.get("body"), headers=headers)
    import json
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        parsed = None
    rec = s.env.gateway.records.get(rh.get("X-ShieldLang-Run", ""), {})
    return 200, {"status": status, "decision": rh.get("X-ShieldLang-Decision"), "response": parsed,
                "run": rh.get("X-ShieldLang-Run"), "record": rec, "session": _session_view(s)}


def protocol_session_reconcile(sid, data):
    try:
        s = _session(sid)
    except KeyError:
        return _err(404, "no_such_session", "session not found or expired")
    prober = s.env.prober()
    if data.get("key"):
        out = s.env.engine.reconcile_key(data["key"], prober)
    elif data.get("ref") and data.get("principal"):
        out = s.env.engine.reconcile_ref(data["principal"], data["ref"], prober)
    else:
        return _err(400, "bad_request", "need either 'key' or ('ref' and 'principal')")
    return 200, {"result": out, "session": _session_view(s)}


def protocol_session_operator(sid, data):
    try:
        s = _session(sid)
    except KeyError:
        return _err(404, "no_such_session", "session not found or expired")
    try:
        out = s.env.engine.operator_resolve(data.get("tx", ""), data.get("decision", ""),
                                            data.get("evidence_text", ""), data.get("operator", "operator"))
    except ValueError as exc:
        return _err(400, "bad_request", str(exc))
    return 200, {"result": out, "session": _session_view(s)}


def protocol_session_restart(sid, _data):
    out = REGISTRY.restart(sid)
    if out is None:
        return _err(404, "no_such_session", "session not found or expired")
    s, recovered = out
    view = _session_view(s)
    view["recovery"] = recovered
    view["recovery_note"] = ("This reopened the on-disk store and ran the same recovery pass as a real crash "
                             "(docs/protocol_semantics.md §5): reserved-without-send-intent -> released, "
                             "sent-without-outcome -> unknown. It did not kill a process; "
                             "tests/test_protocol_runtime.py exercises that with a real subprocess crash.")
    return 200, view


def protocol_session_close(sid, _data):
    return (200, {"closed": True}) if REGISTRY.close(sid) else _err(404, "no_such_session", "already closed")


def protocol_save_evidence(sid, data):
    try:
        s = _session(sid)
    except KeyError:
        return _err(404, "no_such_session", "session not found or expired")
    doc = dict(_session_view(s), kind="protocol")
    rid = default_store().put_run("protocol", doc)
    return 200, {"run_id": rid}


def protocol_runs_list(_):
    return 200, default_store().list_runs("protocol", 50)


def protocol_run_get(rid, _):
    doc = default_store().get_run(rid)
    return (200, doc) if doc else _err(404, "no_such_run", "run not found (it may have been pruned)")


def protocol_source_get(h, _):
    text = default_store().get_source(h)
    return (200, {"hash": h, "source": text}) if text is not None else _err(404, "no_such_source", "source not found (it may have been pruned, or never saved)")


# ---------------------------------------------------------------- Traffic Lab
def traffic_recordings(_):
    out = []
    for f in sorted((EXAMPLES / "recordings").glob("*.json")):
        import json
        doc = json.loads(f.read_text())
        out.append({"name": f.stem, "value": f.stem, "hash": content_hash(doc), "entries": len(doc.get("entries", [])),
                    "description": doc.get("description", ""), "source": "bundled"})
    for r in default_store().list_recordings():
        out.append({**r, "value": r["hash"], "source": "imported"})
    return 200, out


def traffic_recording_import(data):
    doc = data.get("recording")
    check = check_recording(doc)
    if check["errors"]:
        return 400, {"error": "bad_recording", "message": f"recording has {len(check['errors'])} problem(s)",
                     "errors": check["errors"], "warnings": check["warnings"]}
    norm = normalized_recording(doc)
    h = default_store().put_recording(norm)
    return 200, {"hash": h, "entries": len(norm["entries"]), "warnings": check["warnings"]}


def traffic_recording_check(data):
    """Validate without storing — lets the page show every problem before the person commits to an import."""
    return 200, check_recording(data.get("recording"))


def traffic_recording_delete(h, _):
    return (200, {"deleted": True}) if default_store().delete_recording(h) else _err(404, "no_such_recording", "not an imported recording (bundled recordings cannot be deleted)")


def traffic_policies(_):
    """Which policies the Traffic Lab can meaningfully exercise against the demo backend, and why."""
    out = []
    for f in sorted(EXAMPLES.glob("*.shl")):
        text = f.read_text(encoding="utf-8")
        r = compile_source(text, f.name)
        sup = policy_support(r)
        out.append({"name": f.stem, "value": f.stem, "source": "bundled", "hash": content_hash(text),
                    "protocols": [p.name for p in r.protocols], **sup})
    for src in default_store().list_sources("policy", 30):
        text = default_store().get_source(src["hash"])
        r = compile_source(text, src["name"] or "policy.shl")
        sup = policy_support(r)
        out.append({"name": src["name"] or src["hash"][:14], "value": src["hash"], "source": "imported", "hash": src["hash"],
                    "protocols": [p.name for p in r.protocols], **sup})
    return 200, out


def traffic_recording_get(name_or_hash, _):
    import json
    f = EXAMPLES / "recordings" / f"{name_or_hash}.json"
    if f.is_file():
        return 200, json.loads(f.read_text())
    doc = default_store().get_recording(name_or_hash)
    return (200, doc) if doc else _err(404, "no_such_recording", "not found")


def traffic_run(data):
    import json
    source = data.get("source")
    policy_name = data.get("policy")
    if source is None and policy_name:
        f = EXAMPLES / f"{policy_name}.shl"
        source = f.read_text(encoding="utf-8") if f.is_file() else default_store().get_source(policy_name)
    if source is None:
        return _err(400, "no_such_policy", f"no bundled example or imported policy named/hashed {policy_name!r}")
    default_store().put_source(source, "policy", policy_name or "policy.shl")
    recording = data.get("recording")
    if isinstance(recording, str):
        f = EXAMPLES / "recordings" / f"{recording}.json"
        recording = json.loads(f.read_text()) if f.is_file() else default_store().get_recording(recording)
    if not isinstance(recording, dict):
        return _err(400, "no_such_recording", "need 'recording' as an object, a bundled name, or an imported recording's hash")
    try:
        validate_recording(recording)
    except RecordingError as exc:
        return _err(400, "bad_recording", str(exc))
    mode = data.get("mode", "enforce")
    if mode not in ("enforce", "observe"):
        return _err(400, "bad_mode", "mode must be 'enforce' or 'observe'")
    try:
        doc = run_traffic(source, recording, mode=mode, protocol=data.get("protocol"), store=default_store(),
                          selected=data.get("selected"))
    except ValueError as exc:
        return _err(400, "bad_request", str(exc))
    if data.get("protocol"):
        doc["handoff"] = {"protocol_studio": f"/protocols?policy_hash={content_hash(source)}&protocol={data['protocol']}"}
    return 200, doc


def traffic_runs_list(_):
    return 200, default_store().list_runs("traffic", 50)


def traffic_run_get(rid, _):
    doc = default_store().get_run(rid)
    return (200, doc) if doc else _err(404, "no_such_run", "run not found (it may have been pruned)")


def traffic_run_delete(rid, _):
    return (200, {"deleted": True}) if default_store().delete_run(rid) else _err(404, "no_such_run", "already gone")


def traffic_export(data):
    return 200, default_store().export(data.get("kind", "traffic"))


# ---------------------------------------------------------------- Bypass Lab (bounded local experiments)
def bypass_fixtures(_):
    return 200, [{k: v for k, v in fx.items()} for fx in FIXTURES]


def bypass_run(data):
    fid = data.get("fixture_id")
    raw = data.get("raw")
    if fid not in BY_ID:
        return _err(400, "no_such_fixture", f"unknown fixture {fid!r}")
    if not isinstance(raw, str) or not raw:
        return _err(400, "bad_request", "need a non-empty 'raw' candidate string")
    out = run_fixture(fid, raw, replay=bool(data.get("replay", True)))
    out["fixture_hash"] = fixture_version(fid)
    out["run_id"] = default_store().put_run("bypass", out)
    return 200, out


def bypass_search(data):
    allowed = {"seeds", "budget", "max_depth", "seed", "max_findings", "minimize",
               "reduction_budget", "time_budget_ms"}
    try:
        if not isinstance(data.get("fixture_id"), str):
            raise ValueError("fixture_id must be text")
        out = search_fixture(data["fixture_id"], **{k: v for k, v in data.items() if k in allowed})
    except ValueError as exc:
        return _err(400, "bad_search", str(exc))
    out["run_id"] = default_store().put_run("bypass", out)
    return 200, out


def bypass_minimize(data):
    try:
        if not isinstance(data.get("fixture_id"), str):
            raise ValueError("fixture_id must be text")
        out = minimize_fixture(data["fixture_id"], data.get("raw"),
                               budget=data.get("budget", 100), time_budget_ms=data.get("time_budget_ms", 5000))
    except ValueError as exc:
        return _err(400, "bad_reduction", str(exc))
    # Preserve the single-candidate evidence contract used by the Traffic Lab
    # handoff, while retaining the complete reduction record and nested result.
    for key, value in out["result"].items():
        out.setdefault(key, value)
    out["run_id"] = default_store().put_run("bypass", out)
    return 200, out


def bypass_runs_list(_):
    return 200, default_store().list_runs("bypass", 50)


def bypass_run_get(rid, _):
    doc = default_store().get_run(rid)
    return (200, doc) if doc else _err(404, "no_such_run", "run not found (it may have been pruned)")


# ---------------------------------------------------------------- Bypass Lab -> Traffic Lab handoff
# Deliberately NOT a "replay through the gateway": the Bypass Lab fixtures run their declared transform pipelines
# (nfkc/percent_decode/dup:first.../strip_nul, see bypass_fixtures.py) against a tiny standalone LabApp
# (file/echo_role/reflect routes, see labapp.py); the Traffic Lab gateway runs compiled ShieldLang RULES against the
# unrelated DemoBackend order API (see demo_backend.py). Neither the routes nor the interpretation pipelines
# correspond, so "sending" a candidate through the real gateway would silently exercise different code under the
# same two interpretations shown on the Bypass Lab page, and could visually look like a working end-to-end replay
# when it is not measuring the thing the person just saw. Traffic Lab therefore shows the ORIGINAL fixture
# evaluation's own preserved interpretation/oracle/replay verbatim, labeled as such, with an explicit statement of
# this limitation, rather than attempting a second, misleading replay against the gateway.
def traffic_bypass_handoff_get(rid, _):
    doc = default_store().get_run(rid)
    if not doc or doc.get("kind") != "bypass":
        return _err(404, "no_such_run", "no bypass run with that id (it may have been pruned, or the id is not a bypass run)")
    return 200, {
        "run_id": rid, "fixture_id": doc.get("fixture_id"), "title": doc.get("title"), "label": doc.get("label"),
        "scope": doc.get("scope"), "policy_stages": doc.get("policy_stages"), "app_stages": doc.get("app_stages"),
        "oracle": doc.get("oracle"), "raw": doc.get("raw"),
        "policy_interpretation": doc.get("policy_interpretation"), "app_interpretation": doc.get("app_interpretation"),
        "policy_would_accept": doc.get("policy_would_accept"), "oracle_violation_on_app_view": doc.get("oracle_violation_on_app_view"),
        "replay": doc.get("replay"), "confirmed_bypass": doc.get("confirmed_bypass"), "note": doc.get("note"),
        "gateway_limitation": ("This is the Bypass Lab's OWN recorded evaluation, shown read-only. It was not re-run through this "
                               "page's gateway or demo backend: the fixture's pipelines (e.g. nfkc, percent_decode, duplicate-key "
                               "handling) and its routes (a virtual file tree, an echo-role endpoint, a reflect endpoint) have no "
                               "equivalent in the order-API rules and demo backend that Traffic Lab exercises, so a second replay here "
                               "would not test the same thing and would misrepresent what was shown on the Bypass Lab page."),
    }
