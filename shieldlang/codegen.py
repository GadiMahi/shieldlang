"""
Code generation: optimised SIR → standalone Python or JavaScript validators.

The emitted modules have no dependencies. Every pattern is emitted as a
table-driven DFA (alphabet-class boundaries + transition table), so matching
costs exactly one table lookup per input character: the generated firewall is
linear-time by construction, whatever regexes the rules contain.

Field blocks become if/elif chains (first failing check per field wins);
'when' conditions are rebuilt from their three-address code by expression
inlining; block scans run the (possibly fused, tagged) scan DFA once per value.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from .ir.ir import Block, Function, Instr, Module, Register


def _tables(dfa) -> Dict[str, Any]:
    t = dfa.tables()
    return t


# ---------------------------------------------------------------------------
class _Emitter:
    lang = ""

    def __init__(self, mod: Module, source_name: str):
        self.mod = mod
        self.src_name = source_name
        self.lines: List[str] = []

    def w(self, s: str = "", ind: int = 0):
        self.lines.append("    " * ind + s if self.lang == "py" else "  " * ind + s)

    # operand rendering shared by the condition inliner
    def operand(self, x, exprs: Dict[str, str]) -> str:
        if isinstance(x, Register):
            return exprs.get(x.name, x.name)
        return self.lit(x)

    def cond_exprs(self, b: Block) -> (Dict[str, str], str):
        exprs: Dict[str, str] = {}
        final = None
        for i in b.instrs:
            if i.op == "CMP":
                a, op, c = i.args
                exprs[i.dst] = self.cmp(self.operand(a, exprs), op, self.operand(c, exprs))
            elif i.op == "AND":
                exprs[i.dst] = self.and_(self.operand(i.args[0], exprs), self.operand(i.args[1], exprs))
            elif i.op == "OR":
                exprs[i.dst] = self.or_(self.operand(i.args[0], exprs), self.operand(i.args[1], exprs))
            elif i.op == "NOT":
                exprs[i.dst] = self.not_(self.operand(i.args[0], exprs))
            elif i.op == "EXISTS":
                exprs[i.dst] = self.exists(self.var(i.args[0]))
            elif i.op == "BIN":
                a, op, c = i.args
                exprs[i.dst] = self.bin(self.operand(a, exprs), op, self.operand(c, exprs))
            elif i.op == "JMP_IF_FALSE":
                final = self.operand(i.args[0], exprs)
        return exprs, final

    def var(self, t: str) -> str:
        return t


# ---------------------------------------------------------------------------
class PythonEmitter(_Emitter):
    lang = "py"

    def lit(self, x) -> str:
        return repr(x)

    def cmp(self, a, op, b):
        return f"_cmp({a}, {op!r}, {b})"

    def and_(self, a, b):
        return f"({a} and {b})"

    def or_(self, a, b):
        return f"({a} or {b})"

    def not_(self, a):
        return f"(not {a})"

    def exists(self, v):
        return f"({v} is not _MISSING)"

    def bin(self, a, op, b):
        return f"_bin({a}, {op!r}, {b})"

    def check_expr(self, i: Instr, v: str) -> str:
        op = i.op
        if op == "CHECK_TYPE":
            return f"_is_type({v}, {i.args[1]!r})"
        if op in ("CHECK_LEN", "CHECK_ITEMS"):
            lo, hi = i.args[1], i.args[2]
            parts = []
            if lo not in (0, None):
                parts.append(f"{lo} <= len({v})")
            if hi is not None:
                parts.append(f"len({v}) <= {hi}")
            return " and ".join(parts) or "True"
        if op == "CHECK_RANGE":
            return f"{i.args[1]!r} <= {v} <= {i.args[2]!r}"
        if op == "CHECK_ENUM":
            return f"{v} in {tuple(i.args[1])!r}"
        if op == "MATCH":
            return f"{i.args[1].upper()}.match({v})"
        raise ValueError(op)

    def field(self, b: Block):
        """A field block: 'break' plays the role of the IR's jump to the block's end label."""
        ins = [i for i in b.instrs if i.op != "LABEL"]
        load = ins[0]
        t = load.dst
        src, name = load.args
        getter = {"header": f"headers.get({name.lower()!r}, _MISSING)",
                  "query": f"query.get({name!r}, _MISSING)",
                  "param": f"params.get({name!r}, _MISSING)",
                  "field": f"_get(body, {name!r})"}[src]
        self.w(f"# {b.name}", 1)
        self.w(f"{t} = {getter}", 1)
        self.w("while True:", 1)
        for i in ins[1:]:
            if i.op == "JMP_IF_MISSING":
                self.w(f"if {t} is _MISSING:", 2)
                self.w("break", 3)
            elif i.op == "REQUIRE":
                self.w(f"if {t} is _MISSING:", 2)
                self.w(f"errors.append({i.err!r})", 3)
                self.w("break", 3)
            else:
                if i.op == "CHECK_TYPE" and i.attrs.get("coerce"):
                    fn = "_coerce_all" if i.each else "_coerce"
                    self.w(f"{t} = {fn}({t}, {i.args[1]!r})", 2)
                cond = (f"all({self.check_expr(i, '_x')} for _x in {t})" if i.each else self.check_expr(i, t))
                self.w(f"if not ({cond}):", 2)
                self.w(f"errors.append({i.err!r})", 3)
                self.w("break", 3)
        self.w("break", 2)
        self.w()

    def emit(self) -> str:
        m = self.mod
        w = self.w
        w("# " + "=" * 74)
        w(f"# Generated by the ShieldLang compiler from {self.src_name} — do not edit.")
        w(f"# {len(m.functions)} rule(s), {len(m.dfas)} DFA(s). Every pattern runs as a DFA: O(n), no backtracking.")
        w("# Usage:  from validator import validate;  validate({'method': 'POST', 'path': '/x', 'body': {...}})")
        w("#         echo '{...request json...}' | python validator.py")
        w("# " + "=" * 74)
        w("import json")
        w("import sys")
        w("import math")
        w("import re")
        w("from bisect import bisect_right")
        w()
        w("_MISSING = object()")
        w()
        w()
        w("class _DFA:")
        w('__slots__ = ("starts", "cls", "delta", "start", "accept", "dead")', 1)
        w()
        w("def __init__(self, starts, cls, delta, start, accept, dead):", 1)
        w("self.starts, self.cls, self.delta = starts, cls, delta", 2)
        w("self.start, self.accept, self.dead = start, accept, dead", 2)
        w()
        w("def run(self, text):", 1)
        w("s, d, starts, cls, dead = self.start, self.delta, self.starts, self.cls, self.dead", 2)
        w("for ch in text:", 2)
        w("s = d[s][cls[bisect_right(starts, ord(ch)) - 1]]", 3)
        w("if s == dead:", 3)
        w("break", 4)
        w("return s", 2)
        w()
        w("def match(self, text):", 1)
        w("return bool(self.accept[self.run(text)])", 2)
        w()
        w("def tags(self, text):", 1)
        w("return self.accept[self.run(text)]", 2)
        w()
        w()
        used = self._used_dfas()
        for did, ent in m.dfas.items():
            if did not in used:
                continue
            t = _tables(ent.dfa)
            w(f"# {did}: {ent.kind}, {ent.dfa.n} states, {len(ent.dfa.partition)} symbol classes — "
              + " & ".join("/" + s + "/" for s in ent.sources)[:120])
            w(f"{did.upper()} = _DFA({t['starts']!r},")
            w(f"    {t['cls']!r},")
            w(f"    {t['delta']!r},")
            w(f"    {t['start']}, {t['accept']!r}, {t['dead']!r})")
            w()
        w("ERRORS = {")
        for e in m.errors.values():
            w(f"{e.code!r}: ({e.kind!r}, {e.target!r}, {e.message!r}),", 1)
        w("}")
        w()
        w(_PY_RUNTIME)
        for fn in m.functions:
            self.function(fn)
        w("ROUTES = [")
        for fn in m.functions:
            w(f"({fn.method!r}, {fn.segments!r}, validate_{fn.rule}),", 1)
        w("]")
        w()
        w(_PY_DISPATCH)
        return "\n".join(self.lines) + "\n"

    def _used_dfas(self):
        used = set()
        for fn in self.mod.functions:
            for i in fn.linear():
                if i.op == "MATCH":
                    used.add(i.args[1])
                if i.op == "SCAN":
                    used.add(i.args[2])
        return used

    def function(self, fn: Function):
        w = self.w
        w(f"def validate_{fn.rule}(req, params):")
        w(f'"""{fn.method} {fn.path}"""', 1)
        w("errors = []", 1)
        w('headers = {str(k).lower(): v for k, v in (req.get("headers") or {}).items()}', 1)
        w('query = req.get("query") or {}', 1)
        w('body = req.get("body") if req.get("body") is not None else {}', 1)
        w()
        for b in fn.blocks:
            if b.kind == "limits":
                w("# body limits", 1)
                for i in b.instrs:
                    if i.op == "CHECK_DEPTH":
                        w(f"if _depth(body, {i.args[0]}) > {i.args[0]}:", 1)
                    elif i.op == "CHECK_SIZE":
                        w(f'if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) > {i.args[0]}:', 1)
                    elif i.op == "CHECK_KEYS":
                        w(f"if _keys(body, {i.args[0]}) > {i.args[0]}:", 1)
                    else:
                        continue
                    w(f"errors.append({i.err!r})", 2)
                    w("return errors", 2)
                w()
            elif b.kind == "field":
                self.field(b)
            elif b.kind == "when":
                exprs, final = self.cond_exprs(b)
                w(f"# {b.name}", 1)
                acts = [i for i in b.instrs if i.op in ("FAIL_IF_MISSING", "FAIL_IF_PRESENT")]
                ind = 1
                if final is not None:
                    w(f"if {final}:", 1)
                    ind = 2
                for a in acts:
                    test = "is" if a.op == "FAIL_IF_MISSING" else "is not"
                    w(f"if {a.args[0]} {test} _MISSING:", ind)
                    w(f"errors.append({a.err!r})", ind + 1)
                w()
            elif b.kind == "scan":
                i = b.instrs[0]
                scope, target, did = i.args
                ent = self.mod.dfas[did]
                w(f"# {b.name}", 1)
                src = {"any": "_strings([body, query, list(params.values())])",
                       "field": f"_strings([_get(body, {target!r}) if _get(body, {target!r}) is not _MISSING else query.get({target!r})])",
                       "header": f"[str(headers[{str(target).lower()!r}])] if {str(target).lower()!r} in headers else []",
                       "path": '[req.get("path", "")]',
                       "body": "[json.dumps(body)]"}[scope]
                w("hits = set()", 1)
                w(f"for s in {src}:", 1)
                w(f"hits.update({did.upper()}.tags(s))", 2)
                w(f"errors.extend({ {k: v for k, v in ent.tag_errors.items()}!r}[h] for h in sorted(hits))", 1)
                w()
        w("return errors", 1)
        w()
        w()


_PY_RUNTIME = r'''
def _get(body, path):
    v = body
    for part in path.split("."):
        if not isinstance(v, dict) or part not in v:
            return _MISSING
        v = v[part]
    return v


def _is_type(v, ty):
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


def _coerce(v, ty):
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


def _coerce_all(vs, ty):
    return [_coerce(v, ty) for v in vs] if isinstance(vs, list) else vs


def _cmp(a, op, b):
    if a is _MISSING or b is _MISSING or a is None or b is None:
        return False
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if isinstance(a, bool) or isinstance(b, bool) or not all(isinstance(x, (int, float)) for x in (a, b)):
        return False
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def _bin(a, op, b):
    try:
        return {"+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b, "/": lambda: a / b}[op]()
    except Exception:
        return _MISSING


def _depth(v, limit=None):
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


def _keys(v, limit=None):
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


def _strings(vals):
    out, stack = [], list(vals)
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return out

'''

_PY_DISPATCH = '''def route(method, path):
    segs = [s for s in path.split("?")[0].split("/") if s]
    for m, pattern, fn in ROUTES:
        if m not in (method.upper(), "ANY") or len(pattern) != len(segs):
            continue
        params = {}
        for p, s in zip(pattern, segs):
            if p.startswith("{") and p.endswith("}"):
                params[p[1:-1]] = s
            elif p != s:
                break
        else:
            return fn, params
    return None, {}


def validate(request):
    method, path = str(request.get("method", "GET")), str(request.get("path", "/"))
    fn, params = route(method, path)
    if fn is None:
        return {"allowed": False, "rule": None,
                "errors": [{"code": "E0", "kind": "route", "target": path,
                            "message": "No rule handles %s %s (default deny)" % (method.upper(), path)}]}
    codes = fn(request, params)
    errs = [{"code": c, "kind": ERRORS[c][0], "target": ERRORS[c][1], "message": ERRORS[c][2]} for c in codes]
    return {"allowed": not errs, "rule": fn.__name__[len("validate_"):], "params": params, "errors": errs}


if __name__ == "__main__":
    print(json.dumps(validate(json.load(sys.stdin)), indent=2))
'''


# ---------------------------------------------------------------------------
class JSEmitter(_Emitter):
    lang = "js"

    def lit(self, x) -> str:
        if isinstance(x, bool):
            return "true" if x else "false"
        return json.dumps(x)

    def cmp(self, a, op, b):
        return f"_cmp({a}, {json.dumps(op)}, {b})"

    def and_(self, a, b):
        return f"({a} && {b})"

    def or_(self, a, b):
        return f"({a} || {b})"

    def not_(self, a):
        return f"(!{a})"

    def exists(self, v):
        return f"({v} !== MISSING)"

    def bin(self, a, op, b):
        return f"_bin({a}, {json.dumps(op)}, {b})"

    def check_expr(self, i: Instr, v: str) -> str:
        op = i.op
        if op == "CHECK_TYPE":
            return f"_isType({v}, {json.dumps(i.args[1])})"
        if op in ("CHECK_LEN", "CHECK_ITEMS"):
            lo, hi = i.args[1], i.args[2]
            ln = f"_len({v})" if op == "CHECK_LEN" else f"{v}.length"
            parts = []
            if lo not in (0, None):
                parts.append(f"{lo} <= {ln}")
            if hi is not None:
                parts.append(f"{ln} <= {hi}")
            return " && ".join(parts) or "true"
        if op == "CHECK_RANGE":
            return f"{json.dumps(i.args[1])} <= {v} && {v} <= {json.dumps(i.args[2])}"
        if op == "CHECK_ENUM":
            return f"{json.dumps(i.args[1])}.includes({v})"
        if op == "MATCH":
            return f"{i.args[1].upper()}.match({v})"
        raise ValueError(op)

    def field(self, b: Block):
        ins = [i for i in b.instrs if i.op != "LABEL"]
        load = ins[0]
        t = load.dst
        src, name = load.args
        getter = {"header": f"_own(headers, {json.dumps(name.lower())})",
                  "query": f"_own(query, {json.dumps(name)})",
                  "param": f"_own(params, {json.dumps(name)})",
                  "field": f"_get(body, {json.dumps(name)})"}[src]
        self.w(f"// {b.name}", 1)
        self.w(f"let {t} = {getter};", 1)
        self.w("do {", 1)
        for i in ins[1:]:
            if i.op == "JMP_IF_MISSING":
                self.w(f"if ({t} === MISSING) break;", 2)
            elif i.op == "REQUIRE":
                self.w(f"if ({t} === MISSING) {{ errors.push({json.dumps(i.err)}); break; }}", 2)
            else:
                if i.op == "CHECK_TYPE" and i.attrs.get("coerce"):
                    fn = "_coerceAll" if i.each else "_coerce"
                    self.w(f"{t} = {fn}({t}, {json.dumps(i.args[1])});", 2)
                cond = (f"{t}.every((x) => {self.check_expr(i, 'x')})" if i.each else self.check_expr(i, t))
                self.w(f"if (!({cond})) {{ errors.push({json.dumps(i.err)}); break; }}", 2)
        self.w("} while (false);", 1)
        self.w()

    def emit(self) -> str:
        m = self.mod
        w = self.w
        w("// " + "=" * 74)
        w(f"// Generated by the ShieldLang compiler from {self.src_name} — do not edit.")
        w(f"// {len(m.functions)} rule(s), {len(m.dfas)} DFA(s). Every pattern runs as a DFA: O(n), no backtracking.")
        w("// Usage (Node / browser):  const { validate } = require('./validator.js'); validate(request)")
        w("// " + "=" * 74)
        w('"use strict";')
        w()
        w("const MISSING = Symbol('missing');")
        w()
        w("class DFA {")
        w("constructor(starts, cls, delta, start, accept, dead) {", 1)
        w("Object.assign(this, { starts, cls, delta, start, accept, dead });", 2)
        w("}", 1)
        w("classOf(cp) {", 1)
        w("let lo = 0, hi = this.starts.length - 1;", 2)
        w("while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (this.starts[mid] <= cp) lo = mid; else hi = mid - 1; }", 2)
        w("return this.cls[lo];", 2)
        w("}", 1)
        w("run(text) {", 1)
        w("let s = this.start;", 2)
        w("for (const ch of text) {", 2)
        w("s = this.delta[s][this.classOf(ch.codePointAt(0))];", 3)
        w("if (s === this.dead) break;", 3)
        w("}", 2)
        w("return s;", 2)
        w("}", 1)
        w("match(text) { return this.accept[this.run(text)].length > 0; }", 1)
        w("tags(text) { return this.accept[this.run(text)]; }", 1)
        w("}")
        w()
        used = PythonEmitter._used_dfas(self)
        for did, ent in m.dfas.items():
            if did not in used:
                continue
            t = _tables(ent.dfa)
            w(f"// {did}: {ent.kind}, {ent.dfa.n} states — " + " & ".join("/" + s + "/" for s in ent.sources)[:120])
            w(f"const {did.upper()} = new DFA({json.dumps(t['starts'])},")
            w(f"  {json.dumps(t['cls'])},")
            w(f"  {json.dumps(t['delta'])},")
            w(f"  {t['start']}, {json.dumps(t['accept'])}, {json.dumps(t['dead'])});")
            w()
        w("const ERRORS = {")
        for e in m.errors.values():
            w(f"{json.dumps(e.code)}: [{json.dumps(e.kind)}, {json.dumps(e.target)}, {json.dumps(e.message)}],", 1)
        w("};")
        w()
        w(_JS_RUNTIME)
        for fn in m.functions:
            self.function(fn)
        w("const ROUTES = [")
        for fn in m.functions:
            w(f"[{json.dumps(fn.method)}, {json.dumps(fn.segments)}, validate_{fn.rule}],", 1)
        w("];")
        w()
        w(_JS_DISPATCH)
        return "\n".join(self.lines) + "\n"

    def function(self, fn: Function):
        w = self.w
        w(f"/** {fn.method} {fn.path} */")
        w(f"function validate_{fn.rule}(req, params) {{")
        w("const errors = [];", 1)
        w("const headers = {};", 1)
        w("for (const [k, v] of Object.entries(req.headers || {})) headers[String(k).toLowerCase()] = v;", 1)
        w("const query = req.query || {};", 1)
        w("const body = req.body != null ? req.body : {};", 1)
        w()
        for b in fn.blocks:
            if b.kind == "limits":
                w("// body limits", 1)
                for i in b.instrs:
                    if i.op == "CHECK_DEPTH":
                        w(f"if (_depth(body, {i.args[0]}) > {i.args[0]}) {{ errors.push({json.dumps(i.err)}); return errors; }}", 1)
                    elif i.op == "CHECK_SIZE":
                        w(f"if (new TextEncoder().encode(JSON.stringify(body)).length > {i.args[0]}) "
                          f"{{ errors.push({json.dumps(i.err)}); return errors; }}", 1)
                    elif i.op == "CHECK_KEYS":
                        w(f"if (_keys(body, {i.args[0]}) > {i.args[0]}) {{ errors.push({json.dumps(i.err)}); return errors; }}", 1)
                w()
            elif b.kind == "field":
                self.field(b)
            elif b.kind == "when":
                exprs, final = self.cond_exprs(b)
                w(f"// {b.name}", 1)
                acts = [i for i in b.instrs if i.op in ("FAIL_IF_MISSING", "FAIL_IF_PRESENT")]
                w(f"if ({final if final is not None else 'true'}) {{", 1)
                for a in acts:
                    test = "===" if a.op == "FAIL_IF_MISSING" else "!=="
                    w(f"if ({a.args[0]} {test} MISSING) errors.push({json.dumps(a.err)});", 2)
                w("}", 1)
                w()
            elif b.kind == "scan":
                i = b.instrs[0]
                scope, target, did = i.args
                ent = self.mod.dfas[did]
                tl = json.dumps(str(target).lower())
                src = {"any": "_strings([body, query, Object.values(params)])",
                       "field": f"_strings([_get(body, {json.dumps(target)}) !== MISSING ? _get(body, {json.dumps(target)}) : query[{json.dumps(target)}]])",
                       "header": f"({tl} in headers ? [String(headers[{tl}])] : [])",
                       "path": "[String(req.path || '')]",
                       "body": "[JSON.stringify(body)]"}[scope]
                w(f"// {b.name}", 1)
                w("{", 1)
                w("const hits = new Set();", 2)
                w(f"for (const s of {src}) for (const t of {did.upper()}.tags(s)) hits.add(t);", 2)
                w(f"const tagErr = {json.dumps({str(k): v for k, v in ent.tag_errors.items()})};", 2)
                w("[...hits].sort().forEach((h) => errors.push(tagErr[h]));", 2)
                w("}", 1)
                w()
        w("return errors;", 1)
        w("}")
        w()


_JS_RUNTIME = '''function _own(o, k) { return Object.prototype.hasOwnProperty.call(o, k) ? o[k] : MISSING; }
function _get(body, path) {
  let v = body;
  for (const part of path.split(".")) {
    if (v === null || typeof v !== "object" || Array.isArray(v) || !Object.prototype.hasOwnProperty.call(v, part)) return MISSING;
    v = v[part];
  }
  return v;
}
function _len(s) { return [...s].length; }
function _isType(v, ty) {
  if (ty === "string") return typeof v === "string";
  if (ty === "bool") return typeof v === "boolean";
  if (ty === "int") return Number.isInteger(v);
  if (ty === "number") return typeof v === "number" && Number.isFinite(v);
  if (ty === "list") return Array.isArray(v);
  return false;
}
function _coerce(v, ty) {
  if (typeof v !== "string") return v;
  if (ty === "int" && /^-?[0-9]+$/.test(v) && !v.endsWith("\\n") && Number.isSafeInteger(Number(v))) return Number(v);
  if (ty === "number" && /^-?[0-9]+(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/.test(v) && !v.endsWith("\\n") && Number.isFinite(Number(v))) return Number(v);
  if (ty === "bool" && (v === "true" || v === "false")) return v === "true";
  return v;
}
function _coerceAll(vs, ty) { return Array.isArray(vs) ? vs.map((v) => _coerce(v, ty)) : vs; }
function _cmp(a, op, b) {
  if (a === MISSING || b === MISSING || a == null || b == null) return false;
  if (op === "==") return a === b;
  if (op === "!=") return a !== b;
  if (typeof a !== "number" || typeof b !== "number") return false;
  return { "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b }[op];
}
function _bin(a, op, b) {
  if (typeof a !== "number" || typeof b !== "number") return MISSING;
  return { "+": a + b, "-": a - b, "*": a * b, "/": a / b }[op];
}
function _depth(v, limit = Infinity) {
  const stack = [[v, 0]];
  let deepest = 0;
  while (stack.length) {
    const [item, level] = stack.pop();
    if (item && typeof item === "object") {
      deepest = Math.max(deepest, level + 1);
      if (deepest > limit) return deepest;
      for (const child of Object.values(item)) stack.push([child, level + 1]);
    }
  }
  return deepest;
}
function _keys(v, limit = Infinity) {
  const stack = [v];
  let count = 0;
  while (stack.length) {
    const item = stack.pop();
    if (item && typeof item === "object") {
      if (!Array.isArray(item)) count += Object.keys(item).length;
      if (count > limit) return count;
      for (const child of Object.values(item)) stack.push(child);
    }
  }
  return count;
}
function _strings(vals) {
  const out = [], stack = [...vals];
  while (stack.length) {
    const v = stack.pop();
    if (typeof v === "string") out.push(v);
    else if (Array.isArray(v)) stack.push(...v);
    else if (v && typeof v === "object") stack.push(...Object.values(v));
  }
  return out;
}
'''

_JS_DISPATCH = '''function route(method, path) {
  const segs = String(path).split("?")[0].split("/").filter(Boolean);
  for (const [m, pattern, fn] of ROUTES) {
    if ((m !== String(method).toUpperCase() && m !== "ANY") || pattern.length !== segs.length) continue;
    const params = {};
    let ok = true;
    for (let i = 0; i < pattern.length; i++) {
      const p = pattern[i];
      if (p.startsWith("{") && p.endsWith("}")) params[p.slice(1, -1)] = segs[i];
      else if (p !== segs[i]) { ok = false; break; }
    }
    if (ok) return [fn, params];
  }
  return [null, {}];
}

function validate(request) {
  const method = String(request.method || "GET"), path = String(request.path || "/");
  const [fn, params] = route(method, path);
  if (!fn) return { allowed: false, rule: null, errors: [{ code: "E0", kind: "route", target: path,
    message: `No rule handles ${method.toUpperCase()} ${path} (default deny)` }] };
  const errs = fn(request, params).map((c) => ({ code: c, kind: ERRORS[c][0], target: ERRORS[c][1], message: ERRORS[c][2] }));
  return { allowed: errs.length === 0, rule: fn.name.slice("validate_".length), params, errors: errs };
}

if (typeof module !== "undefined") module.exports = { validate, route, ERRORS };
'''


def generate_python(mod: Module, source_name: str = "rules.shl") -> str:
    return PythonEmitter(mod, source_name).emit()


def generate_js(mod: Module, source_name: str = "rules.shl") -> str:
    return JSEmitter(mod, source_name).emit()
