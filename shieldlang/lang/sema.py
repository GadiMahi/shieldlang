"""
ShieldLang semantic analysis.

* Symbol table with a global scope (constants, patterns, rules) and one scope
  per rule (headers, query parameters, body fields, path parameters).
* Constant evaluation (compile-time arithmetic with size units).
* Type checking of constraints and of 'when' conditions.
* Regex sub-compilation through RegexShield v2, with errors mapped back to
  the exact character inside the regex literal.
* Contradiction detection by interval reasoning and automata:
  minlen > maxlen, empty ranges, regexes whose length bounds can never fit
  the field's length limits, mutually exclusive patterns (empty DFA
  intersection), conditions that can never be true, forbidden-but-required
  fields, conflicting routes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..diagnostics import DiagnosticBag
from ..regex.compiler import RegexArtifact, compile_pattern
from ..regex.dfa import DFATooLarge, product
from ..regex.parser import RegexSyntaxError
from . import ast as A
from .lexer import UNITS

PHASE = "semantic"
PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class RegexEntry:
    rid: str
    source: str
    flags: str
    start: int
    end: int
    artifact: Optional[RegexArtifact]
    used_by: List[str] = field(default_factory=list)
    name: str = ""

    def to_json(self):
        a = self.artifact
        return {"rid": self.rid, "source": self.source, "flags": self.flags, "name": self.name,
                "span": [self.start, self.end], "used_by": self.used_by,
                "risk": a.redos.risk if a else "ERROR",
                "complexity": a.redos.complexity if a else None}


@dataclass
class FieldInfo:
    rule: str
    source: str
    name: str
    optional: bool
    type: A.TypeNode
    decl: A.FieldDecl
    minlen: Optional[int] = None
    maxlen: Optional[int] = None
    lo: Optional[float] = None
    hi: Optional[float] = None
    minitems: Optional[int] = None
    maxitems: Optional[int] = None
    patterns: List[str] = field(default_factory=list)
    exprs: Dict[str, Any] = field(default_factory=dict)   # unevaluated constraint expressions (for IR)
    conditionally_required: bool = False

    @property
    def key(self) -> str:
        return f"{self.source}:{self.name}"

    @property
    def elem_type(self) -> str:
        return self.type.elem.name if self.type.name == "list" and self.type.elem else self.type.name

    def to_json(self):
        return {"source": self.source, "name": self.name, "optional": self.optional,
                "type": self.type.display(), "minlen": self.minlen, "maxlen": self.maxlen,
                "range": None if self.lo is None and self.hi is None else [self.lo, self.hi],
                "minitems": self.minitems, "maxitems": self.maxitems, "patterns": self.patterns,
                "conditionally_required": self.conditionally_required,
                "line_span": [self.decl.start, self.decl.end]}


@dataclass
class WhenInfo:
    stmt: A.WhenStmt
    static: Optional[bool]       # None = decided at run time


@dataclass
class BlockInfo:
    stmt: A.BlockStmt
    rid: str
    label: str


@dataclass
class RuleInfo:
    name: str
    method: str
    path: str
    decl: A.Rule
    segments: List[str]
    fields: List[FieldInfo] = field(default_factory=list)
    whens: List[WhenInfo] = field(default_factory=list)
    blocks: List[BlockInfo] = field(default_factory=list)
    limits: Dict[str, int] = field(default_factory=dict)
    limit_exprs: Dict[str, Any] = field(default_factory=dict)

    def field_by_name(self, name: str) -> Optional[FieldInfo]:
        for f in self.fields:
            if f.name == name or (f.source == "header" and f.name.lower() == name.lower()):
                return f
        return None

    def to_json(self):
        return {"name": self.name, "method": self.method, "path": self.path,
                "fields": [f.to_json() for f in self.fields],
                "whens": [{"action": w.stmt.action, "targets": [t.name for t in w.stmt.targets],
                           "static": w.static} for w in self.whens],
                "blocks": [{"scope": b.stmt.scope, "target": b.stmt.target, "rid": b.rid, "label": b.label}
                           for b in self.blocks],
                "limits": self.limits}


@dataclass
class SemanticModel:
    program: A.Program
    consts: Dict[str, Any] = field(default_factory=dict)
    const_decls: Dict[str, A.ConstDecl] = field(default_factory=dict)
    patterns: Dict[str, str] = field(default_factory=dict)          # name -> rid
    regexes: Dict[str, RegexEntry] = field(default_factory=dict)
    rules: List[RuleInfo] = field(default_factory=list)

    def symbol_table(self, sm) -> Dict:
        def line(off):
            return sm.line_col(off)[0]
        glob = []
        for n, v in self.consts.items():
            d = self.const_decls[n]
            glob.append({"name": n, "kind": "const", "type": "int" if isinstance(v, int) else type(v).__name__,
                         "value": v, "line": line(d.start)})
        for n, rid in self.patterns.items():
            e = self.regexes[rid]
            glob.append({"name": n, "kind": "pattern", "type": "regex", "value": f"/{e.source}/{e.flags}",
                         "line": line(e.start), "risk": e.artifact.redos.risk if e.artifact else "ERROR"})
        for r in self.rules:
            glob.append({"name": r.name, "kind": "rule", "type": "route", "value": f"{r.method} {r.path}",
                         "line": line(r.decl.start)})
        scopes = []
        for r in self.rules:
            syms = []
            for f in r.fields:
                cons = []
                if f.minlen is not None:
                    cons.append(f"minlen {f.minlen}")
                if f.maxlen is not None:
                    cons.append(f"maxlen {f.maxlen}")
                if f.lo is not None:
                    cons.append(f"in {f.lo}..{f.hi}")
                if f.minitems is not None:
                    cons.append(f"minitems {f.minitems}")
                if f.maxitems is not None:
                    cons.append(f"maxitems {f.maxitems}")
                cons += [f"matches {rid}" for rid in f.patterns]
                syms.append({"name": f.name, "kind": f.source, "type": f.type.display(),
                             "optional": f.optional, "constraints": cons, "line": line(f.decl.start)})
            scopes.append({"rule": r.name, "route": f"{r.method} {r.path}", "symbols": syms,
                           "limits": r.limits})
        return {"global": glob, "rules": scopes}


class Analyzer:
    def __init__(self, program: A.Program, diags: DiagnosticBag):
        self.p = program
        self.d = diags
        self.m = SemanticModel(program)
        self._rid = 0
        self._used_consts = set()
        self._used_patterns = set()

    # ---- entry ----------------------------------------------------------
    def run(self) -> SemanticModel:
        for item in self.p.items:
            if isinstance(item, A.ConstDecl):
                self.const(item)
            elif isinstance(item, A.PatternDecl):
                self.pattern(item)
        names = {}
        routes = {}
        for item in self.p.items:
            if isinstance(item, A.Rule):
                if item.name in names:
                    self.d.error("S001", f"Duplicate rule name '{item.name}'", item.name_start, item.name_end, PHASE,
                                 f"First defined at offset {names[item.name]}")
                names[item.name] = item.start
                norm = (item.method, PARAM_RE.sub("{}", item.path))
                if norm in routes:
                    self.d.error("S002", f"Route conflict: '{item.method} {item.path}' is already handled by rule "
                                         f"'{routes[norm]}'", item.path_start, item.path_end, PHASE)
                else:
                    routes[norm] = item.name
                self.rule(item)
        for n, decl in self.m.const_decls.items():
            if n not in self._used_consts:
                self.d.warning("S017", f"Constant '{n}' is never used", decl.name_start, decl.name_end, PHASE)
        for item in self.p.items:
            if isinstance(item, A.PatternDecl) and item.name not in self._used_patterns:
                self.d.warning("S017", f"Pattern '{item.name}' is never used", item.name_start, item.name_end, PHASE)
        return self.m

    # ---- constants -----------------------------------------------------
    def eval_const(self, e) -> Any:
        if isinstance(e, A.Num):
            return e.value * UNITS[e.unit] if e.unit else e.value
        if isinstance(e, A.Str):
            return e.value
        if isinstance(e, A.Bool):
            return e.value
        if isinstance(e, A.Ref):
            if e.name in self.m.consts:
                self._used_consts.add(e.name)
                return self.m.consts[e.name]
            self.d.error("S004", f"Undefined constant '{e.name}'", e.start, e.end, PHASE)
            return None
        if isinstance(e, A.Unary) and e.op == "-":
            v = self.eval_const(e.operand)
            return -v if isinstance(v, (int, float)) else None
        if isinstance(e, A.Binary) and e.op in "+-*/":
            a, b = self.eval_const(e.left), self.eval_const(e.right)
            if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
                if a is not None and b is not None:
                    self.d.error("S013", f"Arithmetic '{e.op}' needs numbers", e.start, e.end, PHASE)
                return None
            if e.op == "+":
                return a + b
            if e.op == "-":
                return a - b
            if e.op == "*":
                return a * b
            if b == 0:
                self.d.error("S020", "Division by zero in constant expression", e.start, e.end, PHASE)
                return None
            return a // b if isinstance(a, int) and isinstance(b, int) else a / b
        self.d.error("S013", "Expression is not a compile-time constant", e.start, e.end, PHASE)
        return None

    def const(self, c: A.ConstDecl):
        if c.name in self.m.consts:
            self.d.error("S003", f"Duplicate constant '{c.name}'", c.name_start, c.name_end, PHASE)
        v = self.eval_const(c.value)
        self.m.consts[c.name] = v
        self.m.const_decls[c.name] = c

    # ---- regexes -------------------------------------------------------
    def regex(self, lit: A.RegexLit, owner: str, name: str = "") -> str:
        self._rid += 1
        rid = f"re{self._rid}"
        lit.rid = rid
        art = None
        try:
            art = compile_pattern(lit.pattern, lit.flags)
        except RegexSyntaxError as exc:
            s = lit.inner_start + exc.position
            self.d.error("R001", f"Regex error: {exc.message}", s, max(s + 1, lit.inner_start + exc.end), "regex",
                         exc.hint)
        except DFATooLarge as exc:
            self.d.error("R002", f"Regex too complex: {exc}", lit.start, lit.end, "regex",
                         "Simplify the pattern or split it into several constraints.")
        entry = RegexEntry(rid, lit.pattern, lit.flags, lit.start, lit.end, art, [owner], name)
        self.m.regexes[rid] = entry
        if art:
            rep = art.redos
            if art.dfa.is_empty():
                self.d.error("S010", f"Regex /{lit.pattern}/ matches no string at all", lit.start, lit.end, PHASE)
            rw = art.rewrite.final if art.rewrite and art.rewrite.changed else None
            fix = f" Suggested verified rewrite: /{rw}/." if rw else ""
            if rep.risk == "HIGH":
                self.d.warning("R101", f"ReDoS: /{lit.pattern}/ has exponential backtracking {rep.complexity} in "
                                       f"Python re / JavaScript.{fix}", lit.start, lit.end, "regex",
                               "ShieldLang compiles it to a DFA, so the generated validator stays linear.")
            elif rep.risk == "MEDIUM":
                self.d.warning("R102", f"ReDoS: /{lit.pattern}/ has polynomial backtracking {rep.complexity}.{fix}",
                               lit.start, lit.end, "regex",
                               "ShieldLang compiles it to a DFA, so the generated validator stays linear.")
        return rid

    def pattern(self, p: A.PatternDecl):
        if p.name in self.m.patterns:
            self.d.error("S003", f"Duplicate pattern '{p.name}'", p.name_start, p.name_end, PHASE)
        self.m.patterns[p.name] = self.regex(p.regex, f"pattern {p.name}", p.name)

    def resolve_pat(self, pe, owner: str) -> Optional[str]:
        if isinstance(pe, A.RegexLit):
            return self.regex(pe, owner)
        if pe.name in self.m.patterns:
            self._used_patterns.add(pe.name)
            rid = self.m.patterns[pe.name]
            pe.rid = rid
            self.m.regexes[rid].used_by.append(owner)
            return rid
        self.d.error("S003", f"Undefined pattern '{pe.name}'", pe.start, pe.end, PHASE,
                     "Declare it with: pattern NAME = /.../;")
        return None

    # ---- rules ---------------------------------------------------------
    def rule(self, r: A.Rule):
        segs = [s for s in r.path.split("/") if s]
        info = RuleInfo(r.name, r.method, r.path, r, segs)
        self.m.rules.append(info)
        path_params = PARAM_RE.findall(r.path)
        if not r.path.startswith("/"):
            self.d.error("S016", "Route path must start with '/'", r.path_start, r.path_end, PHASE)
        seen: Dict[str, A.FieldDecl] = {}
        for st in r.body:
            if isinstance(st, A.FieldDecl):
                key = f"{st.source}:{st.name.lower() if st.source == 'header' else st.name}"
                if key in seen:
                    self.d.error("S005", f"Duplicate {st.source} '{st.name}' in rule '{r.name}'",
                                 st.name_start, st.name_end, PHASE)
                    continue
                seen[key] = st
                info.fields.append(self.field_decl(info, st))
        declared_params = {f.name for f in info.fields if f.source == "param"}
        for p in path_params:
            if p not in declared_params:
                self.d.warning("S016", f"Path parameter '{{{p}}}' has no 'param' declaration; it is treated as an "
                                       f"unconstrained string", r.path_start, r.path_end, PHASE,
                               f"Add: param {p} : string ...;")
        for f in info.fields:
            if f.source == "param" and f.name not in path_params:
                self.d.error("S016", f"'param {f.name}' does not appear in route path '{r.path}'",
                             f.decl.name_start, f.decl.name_end, PHASE)
            if f.source == "param" and f.optional:
                self.d.error("S016", "Path parameters cannot be optional", f.decl.name_start, f.decl.name_end, PHASE)
        for st in r.body:
            if isinstance(st, A.WhenStmt):
                self.when(info, st)
            elif isinstance(st, A.BlockStmt):
                self.block(info, st)
            elif isinstance(st, A.BodyStmt):
                for lim in st.limits:
                    v = self.eval_const(lim.value)
                    if v is None:
                        continue
                    if not isinstance(v, int) or v <= 0:
                        self.d.error("S022", f"'{lim.kind}' must be a positive integer, got {v}", lim.start, lim.end, PHASE)
                        continue
                    if lim.kind in info.limits:
                        self.d.warning("S018", f"'{lim.kind}' set twice; the last value wins", lim.start, lim.end, PHASE)
                    info.limits[lim.kind] = v
                    info.limit_exprs[lim.kind] = lim.value
        if r.method in ("GET", "DELETE", "HEAD") and any(f.source == "field" for f in info.fields):
            self.d.warning("S025", f"{r.method} requests normally have no body, but rule '{r.name}' declares body fields",
                           r.name_start, r.name_end, PHASE)

    def field_decl(self, rule: RuleInfo, st: A.FieldDecl) -> FieldInfo:
        f = FieldInfo(rule.name, st.source, st.name, st.optional, st.type, st)
        t = st.type
        if t.name == "list" and t.elem and t.elem.name == "list":
            self.d.error("S006", "Nested lists are not supported", t.start, t.end, PHASE)
        if st.source in ("header", "param", "query") and t.name == "list" and st.source != "query":
            self.d.error("S006", f"A {st.source} cannot be a list", t.start, t.end, PHASE)
        if t.name == "enum" and len(set(t.values)) != len(t.values):
            self.d.warning("S018", "Duplicate enum members", t.start, t.end, PHASE)
        elem = f.elem_type
        is_list = t.name == "list"
        for c in st.constraints:
            if c.kind == "matches":
                if elem not in ("string",):
                    self.d.error("S006", f"'matches' needs a string field, but '{st.name}' is {t.display()}",
                                 c.start, c.end, PHASE)
                    continue
                rid = self.resolve_pat(c.args[0], f"{rule.name}.{st.name}")
                if rid:
                    f.patterns.append(rid)
            elif c.kind in ("minlen", "maxlen"):
                if elem != "string":
                    hint = "Use 'minitems'/'maxitems' for lists." if is_list else ""
                    self.d.error("S006", f"'{c.kind}' applies to strings, but '{st.name}' is {t.display()}",
                                 c.start, c.end, PHASE, hint)
                    continue
                v = self.eval_const(c.args[0])
                if v is not None and (not isinstance(v, int) or v < 0):
                    self.d.error("S020", f"'{c.kind}' must be a non-negative integer, got {v}", c.start, c.end, PHASE)
                    continue
                setattr(f, c.kind, v)
                f.exprs[c.kind] = c.args[0]
            elif c.kind in ("minitems", "maxitems"):
                if not is_list:
                    self.d.error("S006", f"'{c.kind}' applies to lists, but '{st.name}' is {t.display()}",
                                 c.start, c.end, PHASE, "Use 'minlen'/'maxlen' for strings.")
                    continue
                v = self.eval_const(c.args[0])
                if v is not None and (not isinstance(v, int) or v < 0):
                    self.d.error("S020", f"'{c.kind}' must be a non-negative integer, got {v}", c.start, c.end, PHASE)
                    continue
                setattr(f, c.kind, v)
                f.exprs[c.kind] = c.args[0]
            elif c.kind == "range":
                if elem not in ("int", "number"):
                    self.d.error("S006", f"Range 'in a..b' applies to int/number, but '{st.name}' is {t.display()}",
                                 c.start, c.end, PHASE)
                    continue
                lo, hi = self.eval_const(c.args[0]), self.eval_const(c.args[1])
                if lo is None or hi is None:
                    continue
                if elem == "int" and (isinstance(lo, float) or isinstance(hi, float)):
                    self.d.error("S006", f"Range bounds of int field '{st.name}' must be integers", c.start, c.end, PHASE)
                if lo > hi:
                    self.d.error("S008", f"Empty range {lo}..{hi}: lower bound is greater than upper bound",
                                 c.start, c.end, PHASE, "No value can ever satisfy this constraint.")
                f.lo, f.hi = lo, hi
                f.exprs["range"] = (c.args[0], c.args[1])
        if t.name == "enum" and f.patterns:
            self.d.warning("S018", f"'matches' on an enum field is redundant", st.start, st.end, PHASE)
        # ---- contradictions ------------------------------------------------
        if f.minlen is not None and f.maxlen is not None and f.minlen > f.maxlen:
            self.d.error("S007", f"Contradiction on '{st.name}': minlen {f.minlen} > maxlen {f.maxlen}",
                         st.start, st.end, PHASE, "No string can satisfy both limits.")
        arts = [self.m.regexes[r].artifact for r in f.patterns if self.m.regexes[r].artifact]
        for rid in f.patterns:
            a = self.m.regexes[rid].artifact
            if not a:
                continue
            rmin, rmax = a.length_bounds
            if rmin is None:
                continue
            if f.maxlen is not None and rmin > f.maxlen:
                self.d.error("S009", f"'{st.name}' can never be valid: /{a.source}/ needs at least {rmin} characters "
                                     f"but maxlen is {f.maxlen}", st.start, st.end, PHASE)
            if f.minlen is not None and rmax is not None and rmax < f.minlen:
                self.d.error("S009", f"'{st.name}' can never be valid: /{a.source}/ accepts at most {rmax} characters "
                                     f"but minlen is {f.minlen}", st.start, st.end, PHASE)
        if len(arts) >= 2:
            try:
                inter = product([a.dfa for a in arts], "and")
                if inter.is_empty():
                    self.d.error("S023", f"Patterns on '{st.name}' are mutually exclusive: no string matches all of them",
                                 st.start, st.end, PHASE, "The DFA intersection of the patterns is empty.")
            except DFATooLarge:
                pass
        return f

    # ---- conditions ----------------------------------------------------
    def cond_type(self, rule: RuleInfo, e) -> str:
        if isinstance(e, A.Num):
            return "number"
        if isinstance(e, A.Str):
            return "string"
        if isinstance(e, A.Bool):
            return "bool"
        if isinstance(e, A.Exists):
            if not rule.field_by_name(e.name):
                self.d.error("S011", f"'exists {e.name}': no such field in rule '{rule.name}'", e.start, e.end, PHASE)
            return "bool"
        if isinstance(e, A.Ref):
            f = rule.field_by_name(e.name)
            if f:
                if f.type.name == "list":
                    return "list"
                return {"int": "number", "number": "number", "enum": "string"}.get(f.type.name, f.type.name)
            if e.name in self.m.consts:
                self._used_consts.add(e.name)
                v = self.m.consts[e.name]
                return "number" if isinstance(v, (int, float)) else "string" if isinstance(v, str) else "bool"
            self.d.error("S011", f"Unknown name '{e.name}' in condition (not a field of rule '{rule.name}' or a constant)",
                         e.start, e.end, PHASE)
            return "error"
        if isinstance(e, A.Unary):
            t = self.cond_type(rule, e.operand)
            want = "bool" if e.op == "!" else "number"
            if t not in (want, "error"):
                self.d.error("S013", f"Operator '{e.op}' expects {want}, got {t}", e.start, e.end, PHASE)
            return want
        if isinstance(e, A.Binary):
            lt, rt = self.cond_type(rule, e.left), self.cond_type(rule, e.right)
            if e.op in ("&&", "||"):
                for side, t in ((e.left, lt), (e.right, rt)):
                    if t not in ("bool", "error"):
                        self.d.error("S013", f"'{e.op}' expects boolean operands, got {t}", side.start, side.end, PHASE)
                return "bool"
            if e.op in ("+", "-", "*", "/"):
                if "error" not in (lt, rt) and (lt != "number" or rt != "number"):
                    self.d.error("S013", f"Arithmetic '{e.op}' needs numbers, got {lt} and {rt}", e.start, e.end, PHASE)
                return "number"
            if "error" in (lt, rt):
                return "bool"
            if e.op in ("<", "<=", ">", ">=") and (lt != "number" or rt != "number"):
                self.d.error("S013", f"Comparison '{e.op}' needs numbers, got {lt} and {rt}", e.start, e.end, PHASE)
            elif lt != rt:
                self.d.error("S013", f"Cannot compare {lt} with {rt}", e.start, e.end, PHASE)
            # enum membership
            for a, b in ((e.left, e.right), (e.right, e.left)):
                if isinstance(a, A.Ref) and isinstance(b, A.Str):
                    f = rule.field_by_name(a.name)
                    if f and f.type.name == "enum" and b.value not in f.type.values:
                        self.d.error("S012", f"\"{b.value}\" is not a member of {f.name}'s "
                                             f"{f.type.display()}", b.start, b.end, PHASE,
                                     "This comparison can never be true.")
            return "bool"
        return "error"

    def static_eval(self, rule: RuleInfo, e) -> Optional[bool]:
        """Interval / enum reasoning: True, False or None (unknown at compile time)."""
        if isinstance(e, A.Bool):
            return e.value
        if isinstance(e, A.Unary) and e.op == "!":
            v = self.static_eval(rule, e.operand)
            return None if v is None else not v
        if isinstance(e, A.Binary) and e.op in ("&&", "||"):
            a, b = self.static_eval(rule, e.left), self.static_eval(rule, e.right)
            if e.op == "&&":
                if a is False or b is False:
                    return False
                return True if a and b else None
            if a is True or b is True:
                return True
            return False if a is False and b is False else None
        if isinstance(e, A.Binary) and e.op in ("==", "!=", "<", "<=", ">", ">="):
            def as_lit(x):
                if isinstance(x, (A.Num, A.Str)):
                    return x
                if isinstance(x, A.Ref) and not rule.field_by_name(x.name) and x.name in self.m.consts:
                    v = self.m.consts[x.name]
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        return A.Num(x.start, x.end, v)
                    if isinstance(v, str):
                        return A.Str(x.start, x.end, v)
                return None
            ref, lit, flip = None, None, False
            if isinstance(e.left, A.Ref) and rule.field_by_name(e.left.name) and as_lit(e.right):
                ref, lit = e.left, as_lit(e.right)
            elif isinstance(e.right, A.Ref) and rule.field_by_name(e.right.name) and as_lit(e.left):
                ref, lit, flip = e.right, as_lit(e.left), True
            if not ref:
                return None
            f = rule.field_by_name(ref.name)
            if not f:
                return None
            op = e.op
            if flip:
                op = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(op, op)
            if isinstance(lit, A.Str) and f.type.name == "enum":
                if op == "==" and lit.value not in f.type.values:
                    return False
                if op == "!=" and lit.value not in f.type.values:
                    return True
                return None
            if isinstance(lit, A.Num) and f.lo is not None and f.hi is not None and f.lo <= f.hi:
                v = lit.value * UNITS[lit.unit] if lit.unit else lit.value
                lo, hi = f.lo, f.hi
                table = {
                    ">": (hi <= v, lo > v), ">=": (hi < v, lo >= v),
                    "<": (lo >= v, hi < v), "<=": (lo > v, hi <= v),
                    "==": (v < lo or v > hi, lo == hi == v), "!=": (lo == hi == v, v < lo or v > hi),
                }
                never, always = table[op]
                if never:
                    return False
                if always:
                    return True
        return None

    def when(self, rule: RuleInfo, st: A.WhenStmt):
        t = self.cond_type(rule, st.cond)
        if t not in ("bool", "error"):
            self.d.error("S013", f"Condition must be boolean, got {t}", st.cond.start, st.cond.end, PHASE)
        static = self.static_eval(rule, st.cond)
        if static is False:
            self.d.warning("S014", "Condition can never be true given the declared constraints; this statement is dead code",
                           st.cond.start, st.cond.end, PHASE, "The optimiser removes it.")
        elif static is True:
            self.d.info("S014", "Condition is always true given the declared constraints",
                        st.cond.start, st.cond.end, PHASE)
        for tg in st.targets:
            f = rule.field_by_name(tg.name)
            if not f:
                self.d.error("S011", f"'{st.action} {tg.name}': no such field in rule '{rule.name}'",
                             tg.start, tg.end, PHASE, "Declare it as optional, e.g. field name? : string;")
                continue
            if st.action == "require" and not f.optional:
                self.d.warning("S015", f"'{tg.name}' is already always required; 'require' is redundant",
                               tg.start, tg.end, PHASE, f"Declare it optional: {f.source} {f.name}? : ...")
            if st.action == "require":
                f.conditionally_required = True
            if st.action == "forbid" and not f.optional:
                self.d.error("S024", f"Contradiction: '{tg.name}' is required but forbidden when the condition holds",
                             tg.start, tg.end, PHASE)
        rule.whens.append(WhenInfo(st, static))

    def block(self, rule: RuleInfo, st: A.BlockStmt):
        if st.scope == "field":
            f = rule.field_by_name(st.target)
            if not f:
                self.d.error("S011", f"'block if {st.target}': no such field in rule '{rule.name}'",
                             st.target_start, st.target_end, PHASE)
            elif f.elem_type not in ("string",):
                self.d.error("S006", f"Cannot scan non-string field '{st.target}' with a pattern",
                             st.target_start, st.target_end, PHASE)
        rid = self.resolve_pat(st.pattern, f"{rule.name}.block")
        if rid:
            rule.blocks.append(BlockInfo(st, rid, st.label or f"blocked by /{self.m.regexes[rid].source}/"))


def analyze(program: A.Program, diags: DiagnosticBag) -> SemanticModel:
    return Analyzer(program, diags).run()
