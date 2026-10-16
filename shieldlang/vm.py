"""
SIR virtual machine.

Executes the (optimised or unoptimised) IR directly against an HTTP request,
counting every instruction and every DFA step, and producing an execution
trace. Used by the web console and to cross-check the generated code
(differential testing).

Request format:
    {"method": "POST", "path": "/api/pay",
     "headers": {...}, "query": {...}, "body": {...}}
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from .ir.ir import Function, Instr, Module, Register
from .lang import ast as A
from .lang.lexer import UNITS


class _Missing:
    def __repr__(self):
        return "MISSING"


MISSING = _Missing()


def route(mod: Module, method: str, path: str) -> Tuple[Optional[Function], Dict[str, str]]:
    segs = [s for s in path.split("?")[0].split("/") if s]
    for fn in mod.functions:
        if fn.method not in (method.upper(), "ANY") or len(fn.segments) != len(segs):
            continue
        params = {}
        ok = True
        for pat, seg in zip(fn.segments, segs):
            if pat.startswith("{") and pat.endswith("}"):
                params[pat[1:-1]] = seg
            elif pat != seg:
                ok = False
                break
        if ok:
            return fn, params
    return None, {}


def _depth(v, limit=None) -> int:
    stack, deepest = [(v, 0)], 0
    while stack:
        item, level = stack.pop()
        children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else None
        if children is not None:
            deepest = max(deepest, level + 1)
            if limit is not None and deepest > limit:
                return deepest
            stack.extend((child, level + 1) for child in children)
    return deepest


def _keys(v, limit=None) -> int:
    stack, count = [v], 0
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            count += len(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        if limit is not None and count > limit:
            return count
    return count


def _strings(v, out: List[str]):
    if isinstance(v, str):
        out.append(v)
    elif isinstance(v, dict):
        for x in v.values():
            _strings(x, out)
    elif isinstance(v, list):
        for x in v:
            _strings(x, out)


def _type_ok(v, ty: str, params: bool) -> bool:
    if ty == "string":
        return isinstance(v, str)
    if ty == "bool":
        return isinstance(v, bool)
    if ty == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if ty == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    if ty == "list":
        return isinstance(v, list)
    return False


def _coerce(v, ty: str):
    """Path/query/header values arrive as strings; coerce them to the declared scalar type."""
    if not isinstance(v, str):
        return v
    try:
        if ty == "int" and re.fullmatch(r"-?[0-9]+", v):
            n = int(v)
            return n if abs(n) <= 9007199254740991 else v
        if ty == "number" and re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", v):
            n = float(v)
            return n if math.isfinite(n) else v
        if ty == "bool" and v in ("true", "false"):
            return v == "true"
    except ValueError:
        pass
    return v


class VM:
    def __init__(self, mod: Module, consts: Dict[str, Any]):
        self.mod = mod
        self.consts = consts

    def val(self, x, regs):
        if isinstance(x, A.Num):
            return x.value * UNITS[x.unit] if x.unit else x.value
        if isinstance(x, A.Node):
            from .ir.optimize import _fold
            return _fold(x, self.consts)
        if isinstance(x, Register):
            return regs.get(x.name, MISSING)
        return x

    def run(self, request: Dict[str, Any], trace: bool = True) -> Dict[str, Any]:
        method = str(request.get("method", "GET"))
        path = str(request.get("path", "/"))
        fn, params = route(self.mod, method, path)
        if fn is None:
            return {"allowed": False, "rule": None, "errors": [{"code": "E0", "kind": "route",
                    "target": path, "message": f"No rule handles {method.upper()} {path} (default deny)"}],
                    "instructions": 0, "dfa_steps": 0, "trace": []}
        headers = {str(k).lower(): v for k, v in (request.get("headers") or {}).items()}
        query = request.get("query") or {}
        body = request.get("body") if request.get("body") is not None else {}
        regs: Dict[str, Any] = {}
        errors: List[Dict] = []
        tr: List[Dict] = []
        ninstr = 0
        dfa_steps = 0
        failed_fields = set()

        def fail(ins: Instr):
            e = self.mod.errors[ins.err]
            errors.append(e.to_json())
            failed_fields.add(e.target)

        code = fn.linear()
        labels = {i.args[0]: k for k, i in enumerate(code) if i.op == "LABEL"}
        pc = 0
        while pc < len(code):
            ins = code[pc]
            pc += 1
            if ins.op == "LABEL":
                continue
            ninstr += 1
            note = ""
            jump = None
            op = ins.op
            if op == "LOAD":
                src, name = ins.args
                ty = None
                if src == "header":
                    v = headers.get(name.lower(), MISSING)
                elif src == "query":
                    v = query.get(name, MISSING)
                elif src == "param":
                    v = params.get(name, MISSING)
                else:
                    v = body
                    for part in name.split("."):
                        v = v.get(part, MISSING) if isinstance(v, dict) else MISSING
                        if v is MISSING:
                            break
                regs[ins.dst] = v
                note = "missing" if v is MISSING else _short(v)
            elif op == "JMP_IF_MISSING":
                if regs[ins.args[0]] is MISSING:
                    jump = ins.target
                    note = "missing → skip"
            elif op == "REQUIRE":
                if regs[ins.args[0]] is MISSING:
                    fail(ins)
                    jump = ins.target
                    note = "FAIL"
            elif op == "CHECK_TYPE":
                t, ty = ins.args
                v = regs[t]
                if ins.each:
                    if ins.attrs.get("coerce", False):
                        v = [_coerce(x, ty) for x in v]
                        regs[t] = v
                    ok = all(_type_ok(x, ty, False) for x in v)
                else:
                    if ins.attrs.get("coerce", False):
                        v = _coerce(v, ty)
                        regs[t] = v
                    ok = _type_ok(v, ty, False)
                if not ok:
                    fail(ins)
                    jump = ins.target
                note = "ok" if ok else "FAIL"
            elif op in ("CHECK_LEN", "CHECK_RANGE", "CHECK_ITEMS"):
                t = ins.args[0]
                lo, hi = self.val(ins.args[1], regs), self.val(ins.args[2], regs)
                vals = regs[t] if (ins.each and op != "CHECK_ITEMS") else [regs[t]]
                ok = True
                for v in vals:
                    m = len(v) if op != "CHECK_RANGE" else v
                    if (lo is not None and m < lo) or (hi is not None and m > hi):
                        ok = False
                        break
                if not ok:
                    fail(ins)
                    jump = ins.target
                note = "ok" if ok else "FAIL"
            elif op == "CHECK_ENUM":
                t, members = ins.args
                vals = regs[t] if ins.each else [regs[t]]
                ok = all(v in members for v in vals)
                if not ok:
                    fail(ins)
                    jump = ins.target
                note = "ok" if ok else "FAIL"
            elif op == "MATCH":
                t, did = ins.args
                dfa = self.mod.dfas[did].dfa
                vals = regs[t] if ins.each else [regs[t]]
                ok = True
                steps = 0
                for v in vals:
                    s, k = dfa.run(v)
                    steps += k
                    if not dfa.accepting(s):
                        ok = False
                        break
                dfa_steps += steps
                if not ok:
                    fail(ins)
                    jump = ins.target
                note = f"{'ok' if ok else 'FAIL'} ({steps} DFA steps)"
            elif op in ("CHECK_DEPTH", "CHECK_SIZE", "CHECK_KEYS"):
                lim = self.val(ins.args[0], regs)
                if op == "CHECK_DEPTH":
                    m = _depth(body, lim)
                elif op == "CHECK_SIZE":
                    m = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
                else:
                    m = _keys(body, lim)
                ok = m <= lim
                if not ok:
                    fail(ins)
                    break  # structural limits stop validation before deeper work
                note = f"{m} ≤ {lim}" if ok else f"FAIL ({m} > {lim})"
            elif op == "EXISTS":
                regs[ins.dst] = regs.get(ins.args[0], MISSING) is not MISSING
                note = str(regs[ins.dst])
            elif op == "CMP":
                a, o, b = self.val(ins.args[0], regs), ins.args[1], self.val(ins.args[2], regs)
                regs[ins.dst] = _compare(a, o, b)
                note = str(regs[ins.dst])
            elif op == "BIN":
                a, o, b = self.val(ins.args[0], regs), ins.args[1], self.val(ins.args[2], regs)
                try:
                    regs[ins.dst] = {"+": lambda: a + b, "-": lambda: a - b,
                                     "*": lambda: a * b, "/": lambda: a / b}[o]()
                except Exception:
                    regs[ins.dst] = MISSING
            elif op in ("AND", "OR"):
                a, b = self.val(ins.args[0], regs), self.val(ins.args[1], regs)
                regs[ins.dst] = (a is True and b is True) if op == "AND" else (a is True or b is True)
                note = str(regs[ins.dst])
            elif op == "NOT":
                regs[ins.dst] = not (self.val(ins.args[0], regs) is True)
            elif op == "JMP_IF_FALSE":
                if self.val(ins.args[0], regs) is not True:
                    jump = ins.target
                    note = "false → skip"
                else:
                    note = "true"
            elif op == "FAIL_IF_MISSING":
                if regs[ins.args[0]] is MISSING:
                    fail(ins)
                    note = "FAIL"
            elif op == "FAIL_IF_PRESENT":
                if regs[ins.args[0]] is not MISSING:
                    fail(ins)
                    note = "FAIL"
            elif op == "SCAN":
                scope, target, did = ins.args
                ent = self.mod.dfas[did]
                texts: List[str] = []
                if scope == "any":
                    _strings(body, texts)
                    _strings(query, texts)
                    _strings(list(params.values()), texts)
                elif scope == "field":
                    v = regs.get(_field_regs(code).get(target, ""), MISSING)
                    if v is MISSING:
                        v = body.get(target, query.get(target, MISSING)) if isinstance(body, dict) else MISSING
                    if v is not MISSING:
                        _strings(v, texts)
                elif scope == "header":
                    v = headers.get(str(target).lower())
                    if v is not None:
                        texts.append(str(v))
                elif scope == "path":
                    texts.append(path)
                elif scope == "body":
                    texts.append(json.dumps(body))
                hit = set()
                steps = 0
                for s_ in texts:
                    st, k = ent.dfa.run(s_)
                    steps += k
                    hit |= set(ent.dfa.tags[st])
                dfa_steps += steps
                for tag in sorted(hit):
                    e = self.mod.errors[ent.tag_errors[tag]]
                    errors.append(e.to_json())
                note = f"{len(texts)} value(s), {steps} DFA steps, " + ("BLOCKED" if hit else "clean")
            if trace:
                tr.append({"text": ins.text().strip(), "note": note, "jump": jump})
            if jump:
                pc = labels[jump]
        return {"allowed": not errors, "rule": fn.rule, "params": params, "errors": errors,
                "instructions": ninstr, "dfa_steps": dfa_steps, "trace": tr if trace else []}


def _field_regs(code):
    out = {}
    for i in code:
        if i.op == "LOAD":
            out[i.args[1]] = i.dst
    return out


def _compare(a, op, b) -> bool:
    if a is MISSING or b is MISSING or a is None or b is None:
        return False
    try:
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if isinstance(a, bool) or isinstance(b, bool) or not all(isinstance(x, (int, float)) for x in (a, b)):
            return False
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
    except Exception:
        return False


def _short(v) -> str:
    s = json.dumps(v) if not isinstance(v, str) else repr(v)
    return s if len(s) <= 40 else s[:37] + "…"
