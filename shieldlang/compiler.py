"""
ShieldLang compiler driver.

    source ─▶ lexer ─▶ parser ─▶ semantic analysis ─▶ IR lowering ─▶ optimiser ─▶ code generation
                                     │
                                     └─▶ RegexShield v2 for every regex literal
                                         (tokens → AST → ε-NFA → DFA → min-DFA → ReDoS → rewrite)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .codegen import generate_js, generate_python
from .diagnostics import DiagnosticBag, SourceMap
from .ir.ir import Module, lower
from .ir.optimize import PassReport, run_passes
from .lang.lexer import Lexer, Token
from .lang.parser import Parser
from .lang.sema import SemanticModel, analyze
from .vm import VM
from .verification import verify_optimizations

PHASES = ["lexer", "parser", "semantic", "regex", "ir", "optimizer", "codegen"]


@dataclass
class CompileResult:
    source: str
    name: str
    tokens: List[Token]
    program: Any
    model: Optional[SemanticModel]
    ir: Optional[Module]
    opt_ir: Optional[Module]
    passes: List[PassReport]
    python: str
    js: str
    diags: DiagnosticBag
    timings: Dict[str, float] = field(default_factory=dict)
    verification: Dict[str, Any] = field(default_factory=dict)
    protocols: List[Any] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.diags.has_errors()

    def vm(self, optimized: bool = True) -> VM:
        return VM(self.opt_ir if optimized else self.ir, self.model.consts)

    def report(self) -> Dict:
        """Security report: every regex with its verdict, every finding and every fix."""
        if not self.model:
            return {}
        rx = []
        for e in self.model.regexes.values():
            a = e.artifact
            if not a:
                continue
            rx.append({
                "rid": e.rid, "source": e.source, "flags": e.flags, "name": e.name, "used_by": e.used_by,
                "risk": a.redos.risk, "complexity": a.redos.complexity, "summary": a.redos.summary,
                "witness": a.redos.witness.to_json() if a.redos.witness else None,
                "rewrite": a.rewrite.to_json() if a.rewrite else None,
                "dfa_states": a.dfa.n, "nfa_states": a.nfa.size,
            })
        counts = {k: sum(1 for r in rx if r["risk"] == k) for k in ("HIGH", "MEDIUM", "LOW", "UNKNOWN")}
        fixed = sum(1 for r in rx if r["rewrite"] and r["rewrite"]["changed"] and r["rewrite"]["final_risk"] == "LOW")
        return {"regexes": rx, "counts": counts, "fixed": fixed,
                "errors": len(self.diags.errors),
                "warnings": sum(1 for d in self.diags.items if d.severity == "warning"),
                "rules": len(self.model.rules),
                "guarantee": "All generated validators match patterns with DFAs: worst-case O(n) per input string."}

    def to_json(self) -> Dict:
        sm = SourceMap(self.source)
        d: Dict[str, Any] = {
            "ok": self.ok, "name": self.name,
            "diagnostics": [x.to_json(sm) for x in self.diags.items],
            "timings_ms": {k: round(v * 1000, 3) for k, v in self.timings.items()},
            "tokens": [t.to_json() for t in self.tokens],
            "ast": self.program.to_json() if self.program else None,
        }
        if self.model:
            d["symbols"] = self.model.symbol_table(sm)
            d["rules"] = [r.to_json() for r in self.model.rules]
            d["regexes"] = {rid: dict(e.to_json(), artifact=e.artifact.to_json() if e.artifact else None)
                            for rid, e in self.model.regexes.items()}
            d["report"] = self.report()
        if self.ir:
            d["ir"] = self.ir.to_json()
        if self.opt_ir:
            d["opt_ir"] = self.opt_ir.to_json()
            d["passes"] = [p.to_json() for p in self.passes]
        d["python"] = self.python
        d["js"] = self.js
        d["verification"] = self.verification
        d["protocols"] = [p.to_json() for p in self.protocols]
        return d


def _protocol_phase(program, model, diags, ir):
    if not any(type(i).__name__ == "Protocol" for i in program.items):
        return []
    from .protocol.analysis import analyze_protocols
    return analyze_protocols(program, model, diags, ir)


def compile_source(source: str, name: str = "rules.shl") -> CompileResult:
    diags = DiagnosticBag()
    t: Dict[str, float] = {}
    t0 = time.perf_counter()
    tokens = Lexer(source, diags).tokenize()
    t["lexer"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    program = Parser(tokens, diags).parse()
    t["parser"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    model = analyze(program, diags)
    t["semantic"] = time.perf_counter() - t0
    regex_time = sum(sum(e.artifact.timings.values()) for e in model.regexes.values() if e.artifact)
    t["regex"] = regex_time
    t["semantic"] = max(0.0, t["semantic"] - regex_time)
    ir = opt = None
    passes: List[PassReport] = []
    py = js = ""
    verification = {}
    protocols: List[Any] = []
    if not diags.has_errors():
        t0 = time.perf_counter()
        ir = lower(model, source)
        t["ir"] = time.perf_counter() - t0
        t0 = time.perf_counter()
        opt, passes = run_passes(ir, model.consts)
        t["optimizer"] = time.perf_counter() - t0
        t0 = time.perf_counter()
        verification = verify_optimizations(ir, opt, model.consts)
        t["verification"] = time.perf_counter() - t0
        if verification["status"] == "failed":
            diags.error("V001", "Optimizer changed supported string-field acceptance; code generation stopped",
                        0, 0, "verification")
        t0 = time.perf_counter()
        protocols = _protocol_phase(program, model, diags, ir)
        t["protocol"] = time.perf_counter() - t0
        t0 = time.perf_counter()
        if not diags.has_errors():
            py = generate_python(opt, name)
            js = generate_js(opt, name)
        t["codegen"] = time.perf_counter() - t0
    else:
        protocols = _protocol_phase(program, model, diags, None)
    return CompileResult(source, name, tokens, program, model, ir, opt, passes, py, js, diags, t, verification, protocols)
