"""
ShieldLang intermediate representation (SIR).

A three-address, label-based IR. Every rule becomes a function made of
structured basic blocks (body-limits, one block per field, one per 'when',
one per 'block' scan) which are also linearised for display and for the VM.

    t3 = LOAD body "amount"
         JMP_IF_MISSING t3 -> L4
         CHECK_TYPE t3, number          !E7 -> L4
         CHECK_RANGE t3, 1, MAX_AMOUNT  !E8 -> L4
    L4:

On failure a check records its error code and jumps to the block's end label,
so one bad field never hides errors in another field.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..lang import ast as A
from ..lang.lexer import UNITS
from ..lang.sema import FieldInfo, RuleInfo, SemanticModel
from ..regex.dfa import DFA

# relative static cost of each op, used by the optimiser's cost model
COST = {"LOAD": 1, "JMP_IF_MISSING": 1, "REQUIRE": 1, "CHECK_TYPE": 1, "CHECK_ENUM": 2, "CHECK_LEN": 1,
        "CHECK_RANGE": 1, "CHECK_ITEMS": 1, "MATCH": 10, "SCAN": 25, "CMP": 1, "BIN": 1, "AND": 1, "OR": 1,
        "NOT": 1, "EXISTS": 1, "JMP_IF_FALSE": 1, "FAIL_IF_MISSING": 1, "FAIL_IF_PRESENT": 1,
        "CHECK_DEPTH": 8, "CHECK_SIZE": 8, "CHECK_KEYS": 8, "JMP": 1, "LABEL": 0, "CONST": 0}


@dataclass(frozen=True)
class Register:
    """A value operand, distinct from a string literal such as 't1'."""
    name: str


@dataclass
class Instr:
    op: str
    args: List[Any] = field(default_factory=list)
    dst: Optional[str] = None
    err: Optional[str] = None
    target: Optional[str] = None
    each: bool = False
    span: Optional[List[int]] = None
    attrs: Dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        if self.op == "LABEL":
            return f"{self.args[0]}:"
        op = self.op + (".each" if self.each else "")
        a = ", ".join(_fmt(x) for x in self.args)
        s = f"    {self.dst} = {op} {a}" if self.dst else f"         {op} {a}"
        if self.err:
            s += f"   !{self.err}"
        if self.target:
            s += f" -> {self.target}"
        return s.rstrip()

    def to_json(self):
        return {"op": self.op, "each": self.each, "dst": self.dst, "args": [_fmt(a) for a in self.args],
                "err": self.err, "target": self.target, "span": self.span, "text": self.text(),
                "attrs": {k: v for k, v in self.attrs.items() if isinstance(v, (str, int, float, bool, type(None)))}}


def _fmt(x) -> str:
    if isinstance(x, Register):
        return x.name
    if isinstance(x, A.Node):
        return expr_text(x)
    if isinstance(x, str) and (x.startswith("t") and x[1:].isdigit() or x.startswith("dfa") or x.startswith("L")):
        return x
    if isinstance(x, str):
        return '"' + x + '"'
    if isinstance(x, list):
        return "{" + ", ".join(_fmt(i) for i in x) + "}"
    if x is None:
        return "∞"
    return str(x)


def expr_text(e) -> str:
    if isinstance(e, A.Num):
        return f"{e.value} {e.unit}" if e.unit else str(e.value)
    if isinstance(e, A.Str):
        return '"' + e.value + '"'
    if isinstance(e, A.Bool):
        return "true" if e.value else "false"
    if isinstance(e, A.Ref):
        return e.name
    if isinstance(e, A.Exists):
        return f"exists {e.name}"
    if isinstance(e, A.Unary):
        return f"{e.op}{expr_text(e.operand)}"
    if isinstance(e, A.Binary):
        return f"({expr_text(e.left)} {e.op} {expr_text(e.right)})"
    return str(e)


@dataclass
class Block:
    kind: str                       # limits | field | when | scan
    name: str
    instrs: List[Instr]
    end_label: str
    attrs: Dict[str, Any] = field(default_factory=dict)

    def cost(self) -> int:
        return sum(COST.get(i.op, 1) for i in self.instrs)


@dataclass
class Function:
    rule: str
    method: str
    path: str
    segments: List[str]
    blocks: List[Block] = field(default_factory=list)

    def linear(self) -> List[Instr]:
        out: List[Instr] = []
        for b in self.blocks:
            out.extend(b.instrs)
        return out

    def text(self) -> str:
        lines = [f"func {self.rule}  ; {self.method} {self.path}"]
        for b in self.blocks:
            lines.append(f"  ; -- {b.kind}: {b.name}")
            lines += [i.text() for i in b.instrs]
        lines.append("    ALLOW_IF_NO_ERRORS")
        return "\n".join(lines)


@dataclass
class DfaEntry:
    did: str
    dfa: DFA
    rids: List[str]
    kind: str                         # match | scan | merged-match | fused-scan
    sources: List[str]
    tag_errors: Dict[int, str] = field(default_factory=dict)
    note: str = ""

    def to_json(self):
        lo, hi = self.dfa.length_bounds() if self.kind.endswith("match") else (None, None)
        return {"did": self.did, "kind": self.kind, "rids": self.rids, "sources": self.sources,
                "states": self.dfa.n, "classes": len(self.dfa.partition), "note": self.note,
                "tag_errors": {str(k): v for k, v in self.tag_errors.items()},
                "length_bounds": [lo, hi]}


@dataclass
class ErrorInfo:
    code: str
    kind: str
    target: str
    message: str

    def to_json(self):
        return {"code": self.code, "kind": self.kind, "target": self.target, "message": self.message}


@dataclass
class Module:
    functions: List[Function] = field(default_factory=list)
    dfas: Dict[str, DfaEntry] = field(default_factory=dict)
    errors: Dict[str, ErrorInfo] = field(default_factory=dict)

    def text(self) -> str:
        parts = ["; ShieldLang IR (SIR)", "; DFA table:"]
        for d in self.dfas.values():
            parts.append(f";   {d.did:6} {d.kind:12} {d.dfa.n:3} states  {' & '.join('/' + s + '/' for s in d.sources)[:90]}")
        parts.append("")
        for f in self.functions:
            parts.append(f.text())
            parts.append("")
        return "\n".join(parts)

    def instr_count(self) -> int:
        return sum(len([i for i in f.linear() if i.op != "LABEL"]) for f in self.functions)

    def cost(self) -> int:
        return sum(b.cost() for f in self.functions for b in f.blocks)

    def to_json(self):
        return {"text": self.text(), "instructions": self.instr_count(), "cost": self.cost(),
                "functions": [{"rule": f.rule, "method": f.method, "path": f.path,
                               "blocks": [{"kind": b.kind, "name": b.name, "cost": b.cost(),
                                           "instrs": [i.to_json() for i in b.instrs]} for b in f.blocks]}
                              for f in self.functions],
                "dfas": [d.to_json() for d in self.dfas.values()],
                "errors": [e.to_json() for e in self.errors.values()]}


# ---------------------------------------------------------------------------
class Lowerer:
    def __init__(self, model: SemanticModel, source_text: str):
        self.m = model
        self.src = source_text
        self.mod = Module()
        self.t = 0
        self.l = 0
        self.e = 0
        self.rid_to_did: Dict[str, str] = {}

    def temp(self) -> str:
        self.t += 1
        return f"t{self.t}"

    def label(self) -> str:
        self.l += 1
        return f"L{self.l}"

    def err(self, kind: str, target: str, message: str) -> str:
        self.e += 1
        code = f"E{self.e}"
        self.mod.errors[code] = ErrorInfo(code, kind, target, message)
        return code

    def dfa_for(self, rid: str, kind: str = "match") -> str:
        key = f"{rid}:{kind}"
        if key in self.rid_to_did:
            return self.rid_to_did[key]
        ent = self.m.regexes[rid]
        did = f"dfa{len(self.mod.dfas) + 1}"
        self.mod.dfas[did] = DfaEntry(did, ent.artifact.dfa, [rid], kind, [ent.source],
                                      {0: ""}, f"from {rid}")
        self.rid_to_did[key] = did
        return did

    def lower(self) -> Module:
        for r in self.m.rules:
            self.mod.functions.append(self.rule(r))
        return self.mod

    def _label_of(self, f: FieldInfo) -> str:
        return {"header": "header", "query": "query parameter", "field": "field", "param": "path parameter"}[f.source]

    def rule(self, r: RuleInfo) -> Function:
        fn = Function(r.name, r.method, r.path, r.segments)
        temps: Dict[str, str] = {}
        # body limits
        if r.limits:
            end = self.label()
            ins = []
            for kind, op, msg in (("max_depth", "CHECK_DEPTH", "Request body nesting depth exceeds {v}"),
                                  ("max_size", "CHECK_SIZE", "Request body exceeds {v} bytes"),
                                  ("max_keys", "CHECK_KEYS", "Request body has more than {v} keys")):
                if kind in r.limits:
                    code = self.err("limit", "body", msg.format(v=r.limits[kind]))
                    ins.append(Instr(op, [r.limit_exprs[kind]], err=code, target=end, attrs={"value": r.limits[kind]}))
            ins.append(Instr("LABEL", [end]))
            fn.blocks.append(Block("limits", "body limits", ins, end))
        # fields
        for f in r.fields:
            fn.blocks.append(self.field_block(f, temps))
        # when statements
        for w in r.whens:
            fn.blocks.append(self.when_block(r, w, temps))
        # block scans
        for b in r.blocks:
            end = self.label()
            did = self.dfa_for(b.rid, "scan")
            code = self.err("block", b.stmt.scope if not b.stmt.target else b.stmt.target, b.label)
            self.mod.dfas[did].tag_errors = {0: code}
            ins = [Instr("SCAN", [b.stmt.scope, b.stmt.target or "*", did], err=code, target=end,
                         span=[b.stmt.start, b.stmt.end]),
                   Instr("LABEL", [end])]
            fn.blocks.append(Block("scan", f"block if {b.stmt.scope}{' ' + b.stmt.target if b.stmt.target else ''}",
                                   ins, end, {"rid": b.rid}))
        return fn

    def field_block(self, f: FieldInfo, temps: Dict[str, str]) -> Block:
        end = self.label()
        t = self.temp()
        temps[f.name] = t
        temps[f.name.lower()] = t
        what = f"{self._label_of(f)} '{f.name}'"
        span = [f.decl.start, f.decl.end]
        ins = [Instr("LOAD", [f.source, f.name], dst=t, span=span)]
        if f.optional:
            ins.append(Instr("JMP_IF_MISSING", [t], target=end, span=span))
        else:
            ins.append(Instr("REQUIRE", [t], err=self.err("missing", f.name, f"Missing required {what}"),
                             target=end, span=span))
        ty = f.type
        if ty.name == "list":
            ins.append(Instr("CHECK_TYPE", [t, "list"], err=self.err("type", f.name, f"{what} must be a list"),
                             target=end, span=span))
            elem = ty.elem.name if ty.elem else "string"
            ins.append(Instr("CHECK_TYPE", [t, "string" if elem == "enum" else elem], each=True,
                             err=self.err("type", f.name, f"every item of {what} must be {_a(elem)}"), target=end, span=span,
                             attrs={"coerce": f.source != "field"}))
        else:
            base = "string" if ty.name == "enum" else ty.name
            ins.append(Instr("CHECK_TYPE", [t, base], err=self.err("type", f.name, f"{what} must be {_a(base)}"),
                             target=end, span=span, attrs={"coerce": f.source != "field"}))
        each = ty.name == "list"
        elem_ty = ty.elem if each else ty
        if elem_ty is not None and elem_ty.name == "enum":
            ins.append(Instr("CHECK_ENUM", [t, list(elem_ty.values)], each=each,
                             err=self.err("enum", f.name, f"{what} must be one of: {', '.join(elem_ty.values)}"),
                             target=end, span=span))
        # constraints in source order
        done_len = False
        for c in f.decl.constraints:
            cspan = [c.start, c.end]
            if c.kind in ("minlen", "maxlen") and not done_len and ("minlen" in f.exprs or "maxlen" in f.exprs):
                done_len = True
                lo, hi = f.exprs.get("minlen", A.Num(0, 0, 0)), f.exprs.get("maxlen")
                parts = []
                if f.minlen:
                    parts.append(f"at least {f.minlen}")
                if f.maxlen is not None:
                    parts.append(f"at most {f.maxlen}")
                ins.append(Instr("CHECK_LEN", [t, lo, hi], each=each,
                                 err=self.err("length", f.name, f"{what} must be {' and '.join(parts)} characters"),
                                 target=end, span=cspan))
            elif c.kind == "range" and "range" in f.exprs:
                lo, hi = f.exprs["range"]
                ins.append(Instr("CHECK_RANGE", [t, lo, hi], each=each,
                                 err=self.err("range", f.name, f"{what} must be between {f.lo} and {f.hi}"),
                                 target=end, span=cspan))
            elif c.kind in ("minitems", "maxitems") and c.kind in f.exprs:
                lo = f.exprs.get("minitems", A.Num(0, 0, 0)) if c.kind == "minitems" else A.Num(0, 0, 0)
                hi = f.exprs.get("maxitems") if c.kind == "maxitems" else None
                msg = f"{what} may contain at most {f.maxitems} items" if c.kind == "maxitems" \
                    else f"{what} must contain at least {f.minitems} items"
                ins.append(Instr("CHECK_ITEMS", [t, lo, hi], err=self.err("items", f.name, msg),
                                 target=end, span=cspan))
            elif c.kind == "matches":
                p = c.args[0]
                rid = p.rid
                if not rid or not self.m.regexes[rid].artifact:
                    continue
                did = self.dfa_for(rid, "match")
                src = self.m.regexes[rid].source
                ins.append(Instr("MATCH", [t, did], each=each,
                                 err=self.err("pattern", f.name, f"{what} does not match /{src}/"),
                                 target=end, span=cspan, attrs={"rid": rid}))
        ins.append(Instr("LABEL", [end]))
        return Block("field", f"{f.source} {f.name}", ins, end, {"field": f.name, "source": f.source})

    def cond(self, e, temps: Dict[str, str], ins: List[Instr]) -> Any:
        """Lowers an expression to three-address code; returns operand (temp or constant)."""
        if isinstance(e, A.Num):
            return e.value * UNITS[e.unit] if e.unit else e.value
        if isinstance(e, A.Str):
            return e.value
        if isinstance(e, A.Bool):
            return e.value
        if isinstance(e, A.Ref):
            if e.name in temps:
                return Register(temps[e.name])
            if e.name in self.m.consts:
                return self.m.consts[e.name]
            return None
        if isinstance(e, A.Exists):
            t = self.temp()
            ins.append(Instr("EXISTS", [temps.get(e.name, "?")], dst=t))
            return Register(t)
        if isinstance(e, A.Unary):
            v = self.cond(e.operand, temps, ins)
            t = self.temp()
            ins.append(Instr("NOT" if e.op == "!" else "BIN", [v] if e.op == "!" else [0, "-", v], dst=t))
            return Register(t)
        if isinstance(e, A.Binary):
            a = self.cond(e.left, temps, ins)
            b = self.cond(e.right, temps, ins)
            t = self.temp()
            if e.op in ("&&", "||"):
                ins.append(Instr("AND" if e.op == "&&" else "OR", [a, b], dst=t))
            elif e.op in ("+", "-", "*", "/"):
                ins.append(Instr("BIN", [a, e.op, b], dst=t))
            else:
                ins.append(Instr("CMP", [a, e.op, b], dst=t))
            return Register(t)
        return None

    def when_block(self, r: RuleInfo, w, temps: Dict[str, str]) -> Block:
        end = self.label()
        st = w.stmt
        ctext = self.src[st.cond.start:st.cond.end]
        ins: List[Instr] = []
        c = self.cond(st.cond, temps, ins)
        ins.append(Instr("JMP_IF_FALSE", [c], target=end, span=[st.start, st.end], attrs={"static": w.static}))
        for tg in st.targets:
            f = r.field_by_name(tg.name)
            if not f:
                continue
            op = "FAIL_IF_MISSING" if st.action == "require" else "FAIL_IF_PRESENT"
            msg = (f"'{tg.name}' is required when {ctext}" if st.action == "require"
                   else f"'{tg.name}' is not allowed when {ctext}")
            ins.append(Instr(op, [temps[f.name]], err=self.err(st.action, tg.name, msg), span=[tg.start, tg.end]))
        ins.append(Instr("LABEL", [end]))
        return Block("when", f"when {ctext} {st.action} {', '.join(t.name for t in st.targets)}", ins, end,
                     {"static": w.static, "cond": ctext})


def _a(word: str) -> str:
    return ("an " if word[0] in "aeiou" else "a ") + word


def lower(model: SemanticModel, source_text: str) -> Module:
    return Lowerer(model, source_text).lower()
