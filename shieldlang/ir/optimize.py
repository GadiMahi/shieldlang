"""
SIR optimisation passes.

Each pass is a function Module -> PassReport that rewrites the module in place
and explains every change it made:

  1. constant-folding          fold constant expressions / size units to literals
  2. dead-code-elimination     drop 'when' blocks whose condition is statically
                               false, unconditionalise statically-true ones,
                               remove unused temporaries
  3. redundant-check-elim      remove length checks implied by a pattern's DFA
                               length bounds, and trivially-true checks
  4. dfa-intersection          merge several MATCH ops on one value into one
                               product DFA (one pass over the string, not k)
  5. scan-fusion               fuse block-scans with the same scope into one
                               *tagged* product DFA that still reports which
                               rule fired
  6. check-reordering          order checks inside a field by the cost model,
                               cheapest first (fail fast)
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..lang import ast as A
from ..lang.lexer import UNITS
from ..regex.dfa import DFATooLarge, product
from .ir import COST, Block, DfaEntry, Instr, Module, expr_text


@dataclass
class PassReport:
    name: str
    title: str
    description: str
    changes: List[str] = field(default_factory=list)
    instrs_before: int = 0
    instrs_after: int = 0
    cost_before: int = 0
    cost_after: int = 0

    def to_json(self):
        return self.__dict__.copy()


def _fold(e, consts) -> Any:
    if isinstance(e, A.Num):
        return e.value * UNITS[e.unit] if e.unit else e.value
    if isinstance(e, A.Ref) and e.name in consts:
        return consts[e.name]
    if isinstance(e, A.Unary) and e.op == "-":
        v = _fold(e.operand, consts)
        return -v if isinstance(v, (int, float)) else e
    if isinstance(e, A.Binary) and e.op in "+-*/":
        a, b = _fold(e.left, consts), _fold(e.right, consts)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if e.op == "+":
                return a + b
            if e.op == "-":
                return a - b
            if e.op == "*":
                return a * b
            if b:
                return a // b if isinstance(a, int) and isinstance(b, int) else a / b
    return e


def constant_folding(mod: Module, consts: Dict[str, Any]) -> PassReport:
    rep = PassReport("constant-folding", "Constant folding",
                     "Evaluates constant expressions, named constants and size units at compile time.")
    for fn in mod.functions:
        for b in fn.blocks:
            for ins in b.instrs:
                new_args = []
                for a in ins.args:
                    if isinstance(a, A.Node):
                        v = _fold(a, consts)
                        if not isinstance(v, A.Node):
                            if not (isinstance(a, A.Num) and not a.unit):
                                rep.changes.append(f"{fn.rule}: {expr_text(a)} → {v}  ({ins.op})")
                            new_args.append(v)
                            continue
                    new_args.append(a)
                ins.args = new_args
    return rep


def dead_code_elimination(mod: Module) -> PassReport:
    rep = PassReport("dead-code-elimination", "Dead-code elimination",
                     "Removes 'when' blocks that can never fire (proved by interval/enum analysis in the semantic "
                     "phase) and simplifies conditions that are always true.")
    for fn in mod.functions:
        kept = []
        for b in fn.blocks:
            if b.kind == "when" and b.attrs.get("static") is False:
                rep.changes.append(f"{fn.rule}: removed dead block «{b.name}» ({len(b.instrs)} instrs)")
                continue
            if b.kind == "when" and b.attrs.get("static") is True:
                before = len(b.instrs)
                b.instrs = [i for i in b.instrs
                            if i.op not in ("CMP", "AND", "OR", "NOT", "BIN", "EXISTS", "JMP_IF_FALSE")]
                rep.changes.append(f"{fn.rule}: condition of «{b.name}» is always true; dropped {before - len(b.instrs)} "
                                   f"condition instrs")
            kept.append(b)
        fn.blocks = kept
    return rep


def redundant_check_elimination(mod: Module) -> PassReport:
    rep = PassReport("redundant-check-elim", "Redundant check elimination",
                     "Uses DFA length bounds to prove that some length checks can never fail, and removes checks "
                     "that are trivially true.")
    for fn in mod.functions:
        for b in fn.blocks:
            if b.kind != "field":
                continue
            bounds = []
            for i in b.instrs:
                if i.op == "MATCH":
                    bounds.append(mod.dfas[i.args[1]].dfa.length_bounds())
            new = []
            for i in b.instrs:
                if i.op == "CHECK_LEN":
                    lo, hi = i.args[1], i.args[2]
                    if (lo in (0, None)) and hi is None:
                        rep.changes.append(f"{fn.rule}.{b.attrs['field']}: CHECK_LEN 0..∞ is always true")
                        continue
                    proved = False
                    for rmin, rmax in bounds:
                        if rmin is None:
                            continue
                        if (lo in (0, None) or rmin >= lo) and (hi is None or (rmax is not None and rmax <= hi)):
                            rep.changes.append(
                                f"{fn.rule}.{b.attrs['field']}: CHECK_LEN {lo}..{hi if hi is not None else '∞'} removed — "
                                f"pattern only accepts lengths {rmin}..{rmax if rmax is not None else '∞'}")
                            proved = True
                            break
                    if proved:
                        continue
                if i.op == "CHECK_ITEMS" and (i.args[1] in (0, None)) and i.args[2] is None:
                    continue
                new.append(i)
            b.instrs = new
    return rep


def dfa_intersection(mod: Module) -> PassReport:
    rep = PassReport("dfa-intersection", "DFA intersection",
                     "Several patterns on the same value are merged into one product automaton, so the string is "
                     "scanned once instead of once per pattern.")
    for fn in mod.functions:
        for b in fn.blocks:
            matches = [i for i in b.instrs if i.op == "MATCH"]
            groups: Dict[tuple, List[Instr]] = {}
            for i in matches:
                groups.setdefault((i.args[0], i.each), []).append(i)
            for (t, each), ms in groups.items():
                if len(ms) < 2:
                    continue
                ents = [mod.dfas[i.args[1]] for i in ms]
                try:
                    merged = product([e.dfa for e in ents], "and")
                except DFATooLarge:
                    rep.changes.append(f"{fn.rule}: intersection too large, kept separate")
                    continue
                did = f"dfa{len(mod.dfas) + 1}"
                mod.dfas[did] = DfaEntry(did, merged, sum((e.rids for e in ents), []), "merged-match",
                                         sum((e.sources for e in ents), []), {0: ""},
                                         "intersection of " + ", ".join(e.did for e in ents))
                first = ms[0]
                tgt = mod.errors[first.err].target
                mod.errors[first.err].message = f"'{tgt}' must match all of: " + ", ".join(
                    "/" + s + "/" for e in ents for s in e.sources)
                first.args = [t, did]
                first.attrs["merged"] = [e.did for e in ents]
                b.instrs = [i for i in b.instrs if i is first or i not in ms]
                rep.changes.append(
                    f"{fn.rule}.{b.attrs.get('field')}: {' ∩ '.join(e.did for e in ents)} → {did} "
                    f"({' + '.join(str(e.dfa.n) for e in ents)} states → {merged.n} states, 1 pass)")
    return rep


def scan_fusion(mod: Module) -> PassReport:
    rep = PassReport("scan-fusion", "Scan fusion",
                     "Block rules that scan the same part of the request are fused into one tagged product DFA. "
                     "Accepting states carry the set of rules that matched, so error reporting is preserved.")
    for fn in mod.functions:
        scans = [b for b in fn.blocks if b.kind == "scan"]
        groups: Dict[tuple, List[Block]] = {}
        for b in scans:
            i = b.instrs[0]
            groups.setdefault((i.args[0], i.args[1]), []).append(b)
        for key, bs in groups.items():
            if len(bs) < 2:
                continue
            ents = [mod.dfas[b.instrs[0].args[2]] for b in bs]
            try:
                fused = product([e.dfa for e in ents], "tagged")
            except DFATooLarge:
                continue
            did = f"dfa{len(mod.dfas) + 1}"
            tag_errors = {j: b.instrs[0].err for j, b in enumerate(bs)}
            mod.dfas[did] = DfaEntry(did, fused, sum((e.rids for e in ents), []), "fused-scan",
                                     sum((e.sources for e in ents), []), tag_errors,
                                     "tagged product of " + ", ".join(e.did for e in ents))
            head = bs[0]
            head.instrs[0].args = [key[0], key[1], did]
            head.instrs[0].attrs["fused"] = [e.did for e in ents]
            head.name = f"{head.name} (fused ×{len(bs)})"
            fn.blocks = [b for b in fn.blocks if b is head or b not in bs]
            rep.changes.append(f"{fn.rule}: fused {len(bs)} scans on '{key[0]}' into {did} "
                               f"({' + '.join(str(e.dfa.n) for e in ents)} states → {fused.n} tagged states)")
    return rep


ORDER = {"LOAD": 0, "JMP_IF_MISSING": 1, "REQUIRE": 1, "CHECK_TYPE": 2, "CHECK_ITEMS": 3, "CHECK_LEN": 4,
         "CHECK_RANGE": 4, "CHECK_ENUM": 5, "MATCH": 9, "LABEL": 99}


def check_reordering(mod: Module) -> PassReport:
    rep = PassReport("check-reordering", "Cost-based check reordering",
                     "Inside each field, cheap structural checks run before expensive automaton runs, so invalid "
                     "requests are rejected as early as possible. Type checks stay first (later checks depend on them).")
    for fn in mod.functions:
        for b in fn.blocks:
            if b.kind != "field":
                continue
            before = [i.op for i in b.instrs]
            # stable sort; .each element checks after their list-level checks
            b.instrs.sort(key=lambda i: (ORDER.get(i.op, 50) + (0.5 if i.each and i.op == "CHECK_TYPE" else 0)))
            after = [i.op for i in b.instrs]
            if before != after:
                rep.changes.append(f"{fn.rule}.{b.attrs['field']}: " + " → ".join(
                    o for o in after if o not in ("LOAD", "LABEL")))
        # blocks: limits first, then fields by cost, whens, scans last
        kind_rank = {"limits": 0, "field": 1, "when": 2, "scan": 3}
        before_names = [b.name for b in fn.blocks]
        fn.blocks.sort(key=lambda b: (kind_rank[b.kind], b.cost() if b.kind == "field" else 0))
        if [b.name for b in fn.blocks] != before_names:
            rep.changes.append(f"{fn.rule}: field blocks ordered by cost: " +
                               ", ".join(f"{b.attrs.get('field')}({b.cost()})" for b in fn.blocks if b.kind == "field"))
    return rep


def run_passes(mod: Module, consts: Dict[str, Any]) -> (Module, List[PassReport]):
    out = copy.deepcopy(mod)
    reports = []
    passes = [lambda m: constant_folding(m, consts), dead_code_elimination, redundant_check_elimination,
              dfa_intersection, scan_fusion, check_reordering]
    for p in passes:
        ib, cb = out.instr_count(), out.cost()
        r = p(out)
        r.instrs_before, r.cost_before = ib, cb
        r.instrs_after, r.cost_after = out.instr_count(), out.cost()
        reports.append(r)
    return out, reports
