"""Evaluate candidates against declared fixture pipelines and confirm with local replay.

The bounded search and reducer in bypass_search.py reuse this evaluator. Neither
the evaluator nor search accepts an external target.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from . import rawjson, transforms
from .bypass_fixtures import BY_ID
from .labapp import LabApp


def _dup_value(raw: str, field: str, stages) -> Any:
    policy = "last" if "dup:last" in stages else "first" if "dup:first" in stages else None
    if policy is None:
        return None
    try:
        v, n = rawjson.lookup(raw, field, duplicates=policy)
        return v
    except rawjson.RawJsonError:
        return None


def interpret(raw: str, scope: Dict[str, Any], stages) -> Any:
    """What one side (policy or app) sees for this candidate, under its declared stages."""
    if any(s.startswith("dup:") for s in stages):
        return _dup_value(raw, scope["name"], stages)
    # Not a duplicate-key fixture: the candidate IS the field's literal value (not a whole JSON document).
    v, _ = transforms.apply_stages(raw, [s for s in stages if not s.startswith("dup:")])
    return v


def run_fixture(fixture_id: str, raw: str, *, replay: bool = True,
                app: Optional[LabApp] = None, replay_deadline: Optional[float] = None) -> Dict[str, Any]:
    fx = BY_ID.get(fixture_id)
    if fx is None:
        raise KeyError(f"no such fixture {fixture_id!r}")
    scope = fx["scope"]
    policy_value = interpret(raw, scope, fx["policy_stages"])
    app_value = interpret(raw, scope, fx["app_stages"])
    oracle = dict(fx["oracle"])
    from . import oracle as O
    violated_policy_view = O.violated(oracle, policy_value) if isinstance(policy_value, str) else (policy_value in oracle.get("values", []) if oracle["kind"] == "equals_any" else False)
    violated_app_view = O.violated(oracle, app_value) if isinstance(app_value, str) else (app_value in oracle.get("values", []) if oracle["kind"] == "equals_any" else False)
    policy_would_accept = not violated_policy_view     # the policy only ever sees its OWN interpretation
    result: Dict[str, Any] = {
        "fixture_id": fixture_id, "title": fx["title"], "label": fx["label"], "raw": raw, "scope": scope,
        "policy_stages": fx["policy_stages"], "app_stages": fx["app_stages"], "oracle": oracle,
        "policy_interpretation": policy_value, "app_interpretation": app_value,
        "policy_would_accept": policy_would_accept, "oracle_violation_on_app_view": violated_app_view,
    }
    if not replay:
        result["replay"] = {"skipped": True}
        result["confirmed_bypass"] = False
        return result
    rep = fx["replay"]
    app_stages_for_server = fx["app_stages"]
    server = app if app is not None else LabApp(app_stages_for_server).start()
    c = None
    try:
        import http.client
        import urllib.parse
        timeout = 5.0
        if replay_deadline is not None:
            timeout = min(timeout, replay_deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("shared replay deadline expired before HTTP replay")
        c = http.client.HTTPConnection("127.0.0.1", server.port, timeout=timeout)
        if rep["endpoint"] == "file":
            encoded = transforms.transport_encode(raw, "path_param")
            c.request("GET", f"/file?name={encoded}")
            resp = c.getresponse()
            body = json.loads(resp.read())
            replay_effect = {"status": resp.status, **body}
            confirmed = resp.status == 200 and body.get("path") == "secret.txt"
        elif rep["endpoint"] == "echo_role":
            body_text = raw
            c.request("POST", "/echo_role", body=body_text.encode(), headers={"Content-Type": "application/json"})
            resp = c.getresponse()
            body = json.loads(resp.read())
            replay_effect = {"status": resp.status, **body}
            confirmed = resp.status == 200 and body.get("effective") in oracle.get("values", [])
        elif rep["endpoint"] == "reflect":
            payload = json.dumps({"comment": raw})
            c.request("POST", "/reflect", body=payload.encode(), headers={"Content-Type": "application/json"})
            resp = c.getresponse()
            body = json.loads(resp.read())
            replay_effect = {"status": resp.status, **body}
            stored = body.get("stored")
            confirmed = isinstance(stored, str) and O.violated(oracle, stored)
        else:
            replay_effect, confirmed = {"error": f"unknown replay endpoint {rep['endpoint']}"}, False
    finally:
        if c is not None:
            c.close()
        if app is None:
            server.stop()
    result["replay"] = replay_effect
    result["confirmed_bypass"] = bool(policy_would_accept and violated_app_view and confirmed)
    result["note"] = ("Confirmed: the declared policy pipeline would accept this exact candidate, the independent "
                      "oracle finds the application-side interpretation violates the property, and local replay "
                      "against the fixture app reproduced the effect." if result["confirmed_bypass"] else
                      "Not confirmed for this candidate: " +
                      ("the policy pipeline's own interpretation already violates the property (nothing to bypass)." if not policy_would_accept else
                       "the oracle found no violation in the application-side interpretation." if not violated_app_view else
                       "the oracle flagged a violation but local replay did not reproduce the same effect."))
    return result
