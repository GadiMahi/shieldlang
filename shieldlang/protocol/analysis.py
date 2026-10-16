"""Static analysis of protocol declarations. Produces ProtocolIR + diagnostics.

Scope labels (do not overstate):
  * reachability  = graph reachability from `initial` over declared steps.
  * dead-end      = non-terminal state with no path to any terminal state. Co-reachability is a *graph* property;
                    it is NOT a proof that a legitimate user can always finish (authorization/obstruction freedom).
  * binding check = nonempty intersection of the two field languages is NECESSARY for a bind to be satisfiable;
                    it does not prove that any complete path satisfies every constraint.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Dict, List, Optional

from ..regex.charset import Partition
from . import ast as P
from .ir import Event, ProtocolIR

SRC = "protocol"
_SEG = re.compile(r"^\{(\w+)\}$")


def _segs(path: str) -> List[str]:
    return [s for s in path.split("/") if s != ""]


def _overlap(a: str, b: str) -> bool:
    sa, sb = _segs(a), _segs(b)
    if len(sa) != len(sb):
        return False
    for x, y in zip(sa, sb):
        if _SEG.match(x) or _SEG.match(y):
            continue
        if x != y:
            return False
    return True


def binding_intersection(la, lb, budget: int = 20000):
    """Return a string accepted by both field languages, or None. Same saturated-length BFS idea as verification.compare_languages
    (separate implementation over the product of both DFA lists). Returns (witness|None, complete)."""
    dfas = la.dfas + lb.dfas
    if dfas:
        part = Partition(cs for d in dfas for cs in d.partition.classes)
        reps = sorted((cs.sample() for cs in part.classes), key=lambda ch: (not ch.isprintable(), ch))
    else:
        reps = ["a"]
    cap = max(la.minimum, lb.minimum, la.maximum or 0, lb.maximum or 0) + 1
    split = len(la.dfas)
    start = (tuple(d.start for d in dfas), 0)
    parents = {start: None}
    todo = deque([start])
    while todo:
        node = todo.popleft()
        states, n = node
        if la.accepts(states[:split], n) and lb.accepts(states[split:], n):
            chars, cur = [], node
            while parents[cur] is not None:
                prev, ch = parents[cur]
                chars.append(ch)
                cur = prev
            return "".join(reversed(chars)), True
        for ch in reps:
            nxt = (tuple(d.delta[s][d.partition.classify(ord(ch))] for d, s in zip(dfas, states)), min(n + 1, cap))
            if nxt not in parents:
                if len(parents) >= budget:
                    return None, False
                parents[nxt] = (node, ch)
                todo.append(nxt)
    return None, True


def analyze_protocols(program, model, diags, ir_module=None) -> List[ProtocolIR]:
    from ..verification import Unsupported, field_language
    out: List[ProtocolIR] = []
    protos = [i for i in program.items if isinstance(i, P.Protocol)]
    if not protos:
        return out
    rules = {r.name: r for r in model.rules} if model else {}
    claimed: Dict[str, str] = {}
    seen_names: Dict[str, P.Protocol] = {}

    def err(code, msg, node, hint=""):
        diags.error(code, msg, node.start, max(node.end, node.start + 1), SRC, hint)

    def warn(code, msg, node, hint=""):
        diags.warning(code, msg, node.start, max(node.end, node.start + 1), SRC, hint)

    def info(code, msg, node, hint=""):
        diags.info(code, msg, node.start, max(node.end, node.start + 1), SRC, hint)

    for pr in protos:
        errs0 = len(diags.errors)
        if pr.name in seen_names:
            diags.error("PE001", f"Duplicate protocol '{pr.name}'", pr.name_start, pr.name_end, SRC)
            continue
        seen_names[pr.name] = pr
        by = {}
        for s in pr.stmts:
            by.setdefault(type(s).__name__, []).append(s)

        def single(kind, required=True, what=None):
            lst = by.get(kind, [])
            what = what or kind.replace("Stmt", "").lower()
            if not lst:
                if required:
                    diags.error("PE002", f"Protocol '{pr.name}' needs a '{what}' statement", pr.name_start, pr.name_end, SRC)
                return None
            for extra in lst[1:]:
                err("PE003", f"Duplicate '{what}' statement", extra)
            return lst[0]

        res = single("ResourceStmt")
        states_stmts = by.get("StatesStmt", [])
        init = single("InitialStmt")
        key_stmt = single("KeyStmt")
        contract = single("ContractStmt", required=False)
        quiesce = single("QuiesceStmt", required=False)
        recon = single("ReconcileStmt", required=False, what="reconcile")

        # ---- states ----
        states: List[str] = []
        for sst in states_stmts:
            for n in sst.names:
                if n.name in states:
                    err("PE011", f"Duplicate state '{n.name}'", n)
                else:
                    states.append(n.name)
        if not states:
            diags.error("PE010", f"Protocol '{pr.name}' declares no states", pr.name_start, pr.name_end, SRC)
        initial = None
        if init:
            if init.name.name not in states:
                err("PE012", f"Undefined state '{init.name.name}' in 'initial'", init.name)
            else:
                initial = init.name.name
        terminals: List[str] = []
        for t in by.get("TerminalStmt", []):
            for n in t.names:
                if n.name not in states:
                    err("PE012", f"Undefined state '{n.name}' in 'terminal'", n)
                elif n.name in terminals:
                    warn("PE011", f"State '{n.name}' listed as terminal twice", n)
                else:
                    terminals.append(n.name)
        if states and not terminals:
            diags.error("PE014", f"Protocol '{pr.name}' declares no terminal state", pr.name_start, pr.name_end, SRC,
                        "Add `terminal <state>;` — completion must be explicit.")

        # ---- events ----
        events: Dict[str, Event] = {}
        creates = by.get("CreateStmt", [])
        steps = by.get("StepStmt", [])
        if len(creates) != 1:
            diags.error("PE024", f"Protocol '{pr.name}' needs exactly one `create` event (found {len(creates)})",
                        pr.name_start, pr.name_end, SRC, "A resource must be created through a governed event.")

        def lookup(n: P.Name, role: str):
            r = rules.get(n.name)
            if r is None:
                err("PE020", f"'{n.name}' is not a rule in this policy", n, "Events refer to existing ShieldLang rule names.")
            elif n.name in claimed and claimed[n.name] != pr.name:
                err("PE002", f"Rule '{n.name}' is already an event of protocol '{claimed[n.name]}'", n)
                return None
            return r

        key_name = key_stmt.name.name if key_stmt else None
        for c in creates[:1]:
            r = lookup(c.rule, "create")
            if r is not None and initial:
                events[r.name] = Event(r.name, "create", r.method, r.path, [(None, initial)], c.id_field,
                                       c.ref_field.name if c.ref_field else None, c.probe_path)
                if c.ref_field:
                    f = r.field_by_name(c.ref_field.name)
                    if f is None or f.source != "field" or f.optional or f.type.name != "string":
                        err("PE026", f"ref '{c.ref_field.name}' must be a required string body field of rule '{r.name}'", c.ref_field)
                    if c.probe_path is None or "{ref}" not in c.probe_path:
                        err("PE027", "probe path must contain {ref}", c)
                else:
                    warn("PE028", "create has no `ref`/`probe`: a lost create response can only be resolved by an operator", c,
                         "Declare `ref <field> probe get \"…{ref}\"` so reconciliation can find the resource.")
                if not c.id_field:
                    err("PE029", "create needs a non-empty id field name", c)
        transitions = {}
        for s in steps:
            r = lookup(s.rule, "step")
            ok = True
            for st in (s.frm, s.to):
                if st.name not in states:
                    err("PE012", f"Undefined state '{st.name}'", st)
                    ok = False
            if r is None or not ok:
                continue
            if r.method in ("GET", "HEAD", "OPTIONS"):
                warn("PE023", f"Rule '{r.name}' uses {r.method}: a safe method is unusual as a state transition", s.rule)
            k = (s.frm.name, r.name)
            if k in transitions:
                err("PE022", f"Nondeterministic: state '{s.frm.name}' already has a step on '{r.name}'", s)
                continue
            ev = events.get(r.name)
            if ev is not None and ev.kind == "create":
                err("PE021", f"Rule '{r.name}' is already the create event", s.rule)
                continue
            transitions[k] = s.to.name
            if ev is None:
                events[r.name] = Event(r.name, "step", r.method, r.path, [(s.frm.name, s.to.name)])
            else:
                ev.steps.append((s.frm.name, s.to.name))
            if s.frm.name in terminals:
                err("PE015", f"Terminal state '{s.frm.name}' has an outgoing step", s.frm)
            if key_name:
                f = r.field_by_name(key_name)
                if f is None or f.source != "param":
                    err("PE030", f"Rule '{r.name}' has no `param {key_name}` (the resource key)", s.rule,
                        "Every step event extracts the resource key from a path parameter.")
                elif f.type.name != "string":
                    err("PE031", f"Key param '{key_name}' of '{r.name}' must be a string", s.rule)
                elif "{" + key_name + "}" not in r.path:
                    err("PE032", f"Path '{r.path}' of '{r.name}' does not contain {{{key_name}}}", s.rule)
        for rn, ev in events.items():
            claimed[rn] = pr.name

        # ---- graph analyses (scope-labelled) ----
        if initial and states:
            adj: Dict[str, List[str]] = {s: [] for s in states}
            for (a, _), b in transitions.items():
                adj[a].append(b)
            reach = {initial}
            q = deque([initial])
            while q:
                u = q.popleft()
                for v in adj[u]:
                    if v not in reach:
                        reach.add(v)
                        q.append(v)
            radj: Dict[str, List[str]] = {s: [] for s in states}
            for (a, _), b in transitions.items():
                radj[b].append(a)
            co = set(terminals)
            q = deque(terminals)
            while q:
                u = q.popleft()
                for v in radj[u]:
                    if v not in co:
                        co.add(v)
                        q.append(v)
            for sst in states_stmts:
                for n in sst.names:
                    if n.name not in reach:
                        warn("PE016", f"State '{n.name}' is unreachable from initial state '{initial}' (graph reachability)", n)
                    elif n.name not in co and terminals:
                        warn("PE017", f"State '{n.name}' has no path to a terminal state (graph co-reachability; "
                             "this is not an obstruction-freedom proof)", n)
        else:
            reach = set()

        # ---- route ambiguity among events and against other rules ----
        order = [r.name for r in model.rules] if model else []
        for a in events.values():
            for b in events.values():
                if a.rule < b.rule and a.method == b.method and _overlap(a.path, b.path):
                    diags.error("PE040", f"Event routes '{a.rule}' ({a.path}) and '{b.rule}' ({b.path}) overlap; "
                                "first-match routing makes event identification ambiguous", pr.name_start, pr.name_end, SRC)
            for other in model.rules:
                if other.name == a.rule or other.name in events:
                    continue
                if other.method == a.method and _overlap(a.path, other.path):
                    r_a = order.index(a.rule)
                    r_o = order.index(other.name)
                    if r_o < r_a:
                        diags.warning("PE041", f"Rule '{other.name}' appears before event rule '{a.rule}' and overlaps its route: "
                                      f"requests may match '{other.name}' and bypass protocol governance", pr.name_start, pr.name_end, SRC)
        # complete-mediation hint: mutating non-event rules under a governed prefix
        for a in list(events.values())[:1]:
            prefix = "/" + (_segs(a.path)[0] if _segs(a.path) else "")
            for other in model.rules:
                if other.name not in events and other.method not in ("GET", "HEAD", "OPTIONS") and other.path.startswith(prefix):
                    diags.warning("PE042", f"Mutating rule '{other.name}' under '{prefix}' is not a protocol event: "
                                  "complete mediation of the resource is not established", pr.name_start, pr.name_end, SRC)

        # ---- outcomes ----
        succ, fail = [], []
        seen_cls = set()
        for o in by.get("OutcomeStmt", []):
            if o.cls in seen_cls:
                err("PE053", f"Duplicate 'outcome {o.cls}' statement", o)
            seen_cls.add(o.cls)
            for lo, hi in o.ranges:
                if lo > hi or lo < 100 or hi > 599:
                    err("PE050", f"Invalid status range {lo}..{hi}", o)
                else:
                    (succ if o.cls == "success" else fail).append((lo, hi))
        if not succ:
            diags.error("PE051", f"Protocol '{pr.name}' declares no `outcome success` classes", pr.name_start, pr.name_end, SRC)
        for sl, sh in succ:
            for fl, fh in fail:
                if max(sl, fl) <= min(sh, fh):
                    diags.error("PE052", f"Success {sl}..{sh} and failure {fl}..{fh} overlap", pr.name_start, pr.name_end, SRC)
        for fl, fh in fail:
            if fh >= 500:
                diags.error("PE054", f"Failure class {fl}..{fh} includes 5xx: a server error cannot establish 'no effect'",
                            pr.name_start, pr.name_end, SRC, "5xx statuses are treated as Unknown.")
        if any(lo < 200 or hi >= 300 for lo, hi in succ):
            diags.warning("PE055", "Success classes outside 2xx are unusual", pr.name_start, pr.name_end, SRC)
        if fail and not contract:
            diags.error("PE056", "A `failure` class asserts that those statuses mean 'no effect'; add `contract \"...\"` stating the "
                        "application guarantee it relies on", pr.name_start, pr.name_end, SRC)
        q = quiesce.seconds if quiesce else 0.0
        if q < 0:
            err("PE057", "quiesce must be nonnegative", quiesce)

        # ---- reconciliation ----
        rec = None
        if recon:
            if "{key}" not in recon.path:
                err("PE060", "reconcile path must contain {key}", recon)
            for obs, stn in recon.mapping:
                if stn.name not in states:
                    err("PE012", f"Undefined state '{stn.name}' in reconcile mapping", stn)
            mapped = {s.name for _, s in recon.mapping}
            for s in states:
                if s not in mapped:
                    warn("PE061", f"State '{s}' has no reconcile mapping; an observation of it can only be resolved by an operator", recon)
            rec = {"path": recon.path, "state_field": recon.state_field, "owner_field": recon.owner_field,
                   "mapping": {k: v.name for k, v in recon.mapping}}
        else:
            warn("PE062", "No `reconcile` declared: Unknown outcomes can only be resolved by explicit operator evidence",
                 pr.name if False else P.Name(pr.name_start, pr.name_end, pr.name))

        # ---- bindings ----
        bindings = []
        for b in by.get("BindStmt", []):
            ea, eb = events.get(b.a_event.name), events.get(b.b_event.name)
            if ea is None:
                err("PE070", f"'{b.a_event.name}' is not an event of this protocol", b.a_event); continue
            if eb is None:
                err("PE070", f"'{b.b_event.name}' is not an event of this protocol", b.b_event); continue
            fa = rules[ea.rule].field_by_name(b.a_field.name)
            fb = rules[eb.rule].field_by_name(b.b_field.name)
            bad = False
            for f, n in ((fa, b.a_field), (fb, b.b_field)):
                if f is None or f.source != "field":
                    err("PE071", f"'{n.name}' is not a body field of the rule", n); bad = True
                elif f.type.name != "string" or f.optional:
                    err("PE072", f"Bound field '{n.name}' must be a required string field (scalar strings only)", n); bad = True
            if bad:
                continue
            # B must be reachable after A
            starts = {to for _, to in ea.steps}
            after = set(starts)
            dq = deque(starts)
            while dq:
                u = dq.popleft()
                for (x, _), y in transitions.items():
                    if x == u and y not in after:
                        after.add(y); dq.append(y)
            if eb.kind != "step" or not any(frm in after for frm, _ in eb.steps):
                err("PE073", f"'{eb.rule}' can never follow '{ea.rule}' in this graph, so the binding can never be checked", b)
                continue
            bindings.append({"a_event": ea.rule, "a_field": b.a_field.name, "b_event": eb.rule, "b_field": b.b_field.name})
            if ir_module is not None:
                try:
                    la = field_language(_find_block(ir_module, rules[ea.rule], b.a_field.name), ir_module, model.consts)
                    lb = field_language(_find_block(ir_module, rules[eb.rule], b.b_field.name), ir_module, model.consts)
                    wit, complete = binding_intersection(la, lb)
                    if wit is None and complete:
                        err("PE074", f"Impossible binding: no string is accepted by both '{ea.rule}.{b.a_field.name}' and "
                            f"'{eb.rule}.{b.b_field.name}' (DFA language intersection is empty)", b)
                    elif wit is None:
                        warn("PE075", "Binding intersection search exhausted its budget; satisfiability unknown", b)
                    else:
                        info("PE076", f"Binding compatible: both fields accept {wit!r}. This is necessary, not sufficient — it does not "
                             "prove any complete path satisfies all constraints.", b)
                except Unsupported as exc:
                    warn("PE077", f"Binding language check unsupported for this field ({exc}); runtime equality still applies", b)
        if errs0 == len(diags.errors) and res and key_name and initial and states:
            summary = {"reachable": sorted(reach), "scope": "graph reachability only; not an obstruction-freedom proof; "
                       "binding checks are necessary conditions"}
            out.append(ProtocolIR(pr.name, res.label, states, initial, terminals, key_name, events, bindings, succ, fail,
                                  contract.text if contract else "", q, rec, summary))
    return out


def _find_block(module, rule, field_name):
    from ..verification import Unsupported
    for fn in module.functions:
        if fn.method == rule.method and fn.path == rule.path:
            for b in fn.blocks:
                if b.kind == "field" and b.attrs.get("source") == "field" and b.attrs.get("field") == field_name:
                    return b
    raise Unsupported("field block not found")
