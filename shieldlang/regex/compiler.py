"""
RegexShield v2 pipeline: source → tokens → AST → ε-NFA → DFA → min-DFA → ReDoS
analysis → verified rewrite. Used standalone and as ShieldLang's regex sub-compiler.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional

from .ast import RNode
from .dfa import DFA, compile_dfa
from .nfa import NFA, build_nfa
from .parser import RToken, parse_regex
from .redos import ReDoSReport, analyze
from .rewrite import RewriteResult, rewrite


@dataclass
class RegexArtifact:
    source: str
    flags: str
    tokens: List[RToken]
    ast: RNode
    nfa: NFA
    raw_dfa: DFA
    dfa: DFA
    redos: ReDoSReport
    timings: Dict[str, float] = field(default_factory=dict)
    rewrite: Optional[RewriteResult] = None

    @property
    def length_bounds(self):
        return self.dfa.length_bounds()

    def python_source(self) -> str:
        return self.source

    def to_json(self, full: bool = True) -> Dict:
        lo, hi = self.length_bounds
        d = {
            "source": self.source, "flags": self.flags,
            "risk": self.redos.risk, "complexity": self.redos.complexity,
            "redos": self.redos.to_json(),
            "length_bounds": {"min": lo, "max": hi},
            "dfa_states": self.dfa.n, "raw_dfa_states": self.raw_dfa.n, "nfa_states": self.nfa.size,
            "alphabet_classes": len(self.dfa.partition),
            "timings_ms": {k: round(v * 1000, 3) for k, v in self.timings.items()},
            "rewrite": self.rewrite.to_json() if self.rewrite else None,
        }
        if full:
            d["tokens"] = [t.to_dict() for t in self.tokens]
            d["ast"] = self.ast.to_dict()
            d["nfa"] = self.nfa.to_json() if self.nfa.size <= 400 else None
            d["dfa"] = self.dfa.to_json() if self.dfa.n <= 400 else None
            d["raw_dfa"] = self.raw_dfa.to_json() if self.raw_dfa.n <= 400 else None
            d["partition"] = [c.to_display() for c in self.dfa.partition.classes]
        return d


def _analyse(source: str, flags: str) -> RegexArtifact:
    t = {}
    t0 = time.perf_counter()
    tokens, ast = parse_regex(source, ignore_case="i" in flags)
    t1 = time.perf_counter()
    nfa = build_nfa(ast)
    t2 = time.perf_counter()
    raw, dfa = compile_dfa(nfa)
    t3 = time.perf_counter()
    rep = analyze(nfa, ast, dfa, source)
    t4 = time.perf_counter()
    t.update(parse=t1 - t0, thompson=t2 - t1, dfa=t3 - t2, redos=t4 - t3)
    return RegexArtifact(source, flags, tokens, ast, nfa, raw, dfa, rep, t)


@lru_cache(maxsize=512)
def _cached(source: str, flags: str) -> RegexArtifact:
    return _analyse(source, flags)


def compile_pattern(source: str, flags: str = "", with_rewrite: bool = True) -> RegexArtifact:
    art = _cached(source, flags)
    if with_rewrite and art.rewrite is None:
        t0 = time.perf_counter()
        if art.redos.risk != "LOW" or any(f.severity in ("HIGH", "MEDIUM") for f in art.redos.findings):
            art.rewrite = rewrite(source, flags, _cached)
        else:
            art.rewrite = None
        art.timings["rewrite"] = time.perf_counter() - t0
    return art
