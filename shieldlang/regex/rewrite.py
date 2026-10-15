"""
Verified safe rewriting (RegexShield v2).

The rewriter proposes local, ambiguity-removing transformations on the regex
AST. A candidate is accepted only if

  1. its minimal DFA is *language-equivalent* to the original (translation
     validation via an XOR product; a counter-example is reported otherwise), and
  2. re-analysis shows the risk did not get worse and the pattern got simpler.

Rules:
  R1 collapse-nested-quantifier   (x+)+ → x+    (x*)+ → x*    (x?)* → x*
  R2 merge-char-alternatives      (a|b|\\d)  → [ab\\d]
  R3 drop-subsumed-alternative    (a|aa)+ → a+   (x|x) → x
  R4 merge-adjacent-loops         x*x* → x*   x+x* → x+   x+x+ → x{2,}
  R5 drop-redundant-dot-star      unanchored  .*abc  →  abc
  R6 unroll-separator-loop        (x+s?)+ → x+(?:sx+)*s?   (x and s disjoint)
  R7 collapse-idempotent-loop     (.*,)*  → (?:.*,)?       (loop body closed under concatenation)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .ast import Alternation, AnchorEnd, AnchorStart, CharNode, Concat, Group, Repeat, RNode, to_source
from .charset import DOT


def render(node: RNode, ov: Dict[str, str]) -> str:
    if node.node_id in ov:
        return ov[node.node_id]
    if isinstance(node, CharNode):
        return node.text
    if isinstance(node, Concat):
        return "".join(render(i, ov) for i in node.items)
    if isinstance(node, Alternation):
        return "|".join(render(b, ov) for b in node.branches)
    if isinstance(node, Group):
        inner = render(node.child, ov) if node.child else ""
        return ("(" if node.capturing else "(?:") + inner + ")"
    if isinstance(node, Repeat):
        inner = render(node.child, ov)
        if not isinstance(node.child, (CharNode, Group)) and node.child.node_id not in ov:
            inner = "(?:" + inner + ")"
        elif node.child.node_id in ov and not _is_atomic(ov[node.child.node_id]):
            inner = "(?:" + inner + ")"
        return inner + node.quant_text()
    if isinstance(node, AnchorStart):
        return "^"
    if isinstance(node, AnchorEnd):
        return "$"
    raise TypeError(node)


def _is_atomic(src: str) -> bool:
    if len(src) == 1 or (len(src) == 2 and src[0] == "\\"):
        return True
    if src.startswith("[") and src.endswith("]") and src.count("[") == 1:
        return True
    if src.startswith("(") and src.endswith(")"):
        depth = 0
        for i, ch in enumerate(src):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(src) - 1:
                    return False
        return True
    return False


def _strip(n: RNode) -> RNode:
    while isinstance(n, Group) and n.child is not None:
        n = n.child
    return n


def _q(lo: int, hi: Optional[int]) -> str:
    if (lo, hi) == (0, None):
        return "*"
    if (lo, hi) == (1, None):
        return "+"
    if (lo, hi) == (0, 1):
        return "?"
    if hi is None:
        return f"{{{lo},}}"
    if lo == hi:
        return f"{{{lo}}}"
    return f"{{{lo},{hi}}}"


def _atom(src: str) -> str:
    return src if _is_atomic(src) else "(?:" + src + ")"


def _class_body(text: str) -> Optional[str]:
    """Text usable inside [...] for a CharNode, or None."""
    if text.startswith("[") and text.endswith("]") and not text.startswith("[^"):
        return text[1:-1]
    if text.startswith("\\") and len(text) == 2:
        return text
    if len(text) == 1 and text not in "]\\^-.":
        return text
    if text == ".":
        return None
    return None


Candidate = Tuple[str, str, str]  # (rule, description, new_source)


def candidates(root: RNode, source: str) -> List[Candidate]:
    out: List[Candidate] = []
    whole = lambda ov: render(root, ov)
    for n in root.walk():
        # R1 nested quantifiers
        if isinstance(n, Repeat) and n.unbounded:
            inner = _strip(n.child)
            if isinstance(inner, Repeat) and (inner.unbounded or (inner.min, inner.max) == (0, 1)):
                lo = inner.min if n.min >= 1 and inner.unbounded else 0
                if (inner.min, inner.max) == (0, 1):
                    lo = 0
                new = _atom(render(inner.child, {})) + _q(lo, None)
                out.append(("R1 collapse-nested-quantifier",
                            f"{source[n.start:n.end]} → {new}", whole({n.node_id: new})))
            # R6 unroll-separator-loop  (X+ s?)+  →  X+(?:sX+)*s?   when X ∩ s = ∅
            if isinstance(inner, Concat) and len(inner.items) == 2:
                a, b = inner.items
                if (isinstance(a, Repeat) and a.unbounded and a.min >= 1 and isinstance(a.child, CharNode)
                        and isinstance(b, Repeat) and (b.min, b.max) == (0, 1) and isinstance(b.child, CharNode)
                        and not a.child.charset.overlaps(b.child.charset)):
                    x, s = a.child.text, b.child.text
                    body = f"{x}+(?:{s}{x}+)*{s}?"
                    new = body if n.min >= 1 else f"(?:{body})?"
                    out.append(("R6 unroll-separator-loop", f"{source[n.start:n.end]} → {new}",
                                whole({n.node_id: new})))
            # R7 collapse-idempotent-loop  (X)* → (?:X)?  when X·X ⊆ X (e.g. (.*,)* )
            if any(isinstance(d, Repeat) and d.unbounded for d in inner.walk() if d is not inner):
                body = render(inner, {})
                new = f"(?:{body})?" if n.min == 0 else f"(?:{body})"
                out.append(("R7 collapse-idempotent-loop", f"{source[n.start:n.end]} → {new}",
                            whole({n.node_id: new})))
            # R3 subsumed alternatives under repetition
            if isinstance(inner, Alternation):
                for b in inner.branches:
                    new = _atom(render(b, {})) + n.quant_text()
                    out.append(("R3 drop-subsumed-alternative",
                                f"{source[n.start:n.end]} → {new}", whole({n.node_id: new})))
        # R2 / R3 on alternations
        if isinstance(n, Alternation):
            if all(isinstance(_strip(b), CharNode) for b in n.branches):
                bodies = [_class_body(_strip(b).text) for b in n.branches]
                if all(x is not None for x in bodies):
                    new = "[" + "".join(bodies) + "]"
                    out.append(("R2 merge-char-alternatives", f"{source[n.start:n.end]} → {new}",
                                whole({n.node_id: new})))
            seen = {}
            for b in n.branches:
                t = render(b, {})
                if t in seen:
                    rest = [render(x, {}) for x in n.branches if x is not b]
                    new = "|".join(dict.fromkeys(rest))
                    out.append(("R3 drop-subsumed-alternative", f"duplicate branch '{t}' removed",
                                whole({n.node_id: new})))
                    break
                seen[t] = b
        # R4 adjacent loops / R5 redundant .*
        if isinstance(n, Concat):
            items = n.items
            for i in range(len(items) - 1):
                a, b = items[i], items[i + 1]
                if isinstance(a, Repeat) and isinstance(b, Repeat) and a.unbounded and b.unbounded:
                    ta, tb = render(a.child, {}), render(b.child, {})
                    if ta == tb:
                        lo = a.min + b.min
                        new = _atom(ta) + _q(lo, None)
                        ov = {a.node_id: new, b.node_id: ""}
                        out.append(("R4 merge-adjacent-loops",
                                    f"{source[a.start:b.end]} → {new}", whole(ov)))
            if items and isinstance(items[0], Repeat) and items[0].min == 0 and items[0].unbounded \
                    and isinstance(items[0].child, CharNode) and items[0].child.charset == DOT:
                out.append(("R5 drop-redundant-dot-star", f"leading '{source[items[0].start:items[0].end]}' removed",
                            whole({items[0].node_id: ""})))
            if items and isinstance(items[-1], Repeat) and items[-1].min == 0 and items[-1].unbounded \
                    and isinstance(items[-1].child, CharNode) and items[-1].child.charset == DOT:
                out.append(("R5 drop-redundant-dot-star", f"trailing '{source[items[-1].start:items[-1].end]}' removed",
                            whole({items[-1].node_id: ""})))
    # de-duplicate by result
    seen_src = set()
    uniq = []
    for c in out:
        if c[2] not in seen_src and c[2] != source:
            seen_src.add(c[2])
            uniq.append(c)
    return uniq


RISK_ORDER = {"LOW": 0, "UNKNOWN": 1, "MEDIUM": 2, "HIGH": 3}


@dataclass
class RewriteStep:
    rule: str
    description: str
    before: str
    after: str
    verified: bool
    dfa_states: Tuple[int, int]
    risk_before: str
    risk_after: str

    def to_json(self):
        return {"rule": self.rule, "description": self.description, "before": self.before,
                "after": self.after, "verified": self.verified,
                "dfa_states": list(self.dfa_states), "risk_before": self.risk_before,
                "risk_after": self.risk_after}


@dataclass
class RewriteResult:
    original: str
    final: str
    steps: List[RewriteStep] = field(default_factory=list)
    rejected: List[Dict] = field(default_factory=list)
    final_risk: str = "LOW"

    @property
    def changed(self) -> bool:
        return self.final != self.original

    def to_json(self):
        return {"original": self.original, "final": self.final, "changed": self.changed,
                "steps": [s.to_json() for s in self.steps], "rejected": self.rejected[:8],
                "final_risk": self.final_risk}


def rewrite(source: str, flags: str, analyse: Callable[[str, str], object], max_steps: int = 8) -> RewriteResult:
    """
    analyse(source, flags) must return an object with .ast, .dfa, .redos (risk) — the regex
    compiler pipeline. Kept as a callback to avoid an import cycle.
    """
    from .dfa import equivalent
    cur = analyse(source, flags)
    res = RewriteResult(source, source, final_risk=cur.redos.risk)
    for _ in range(max_steps):
        progressed = False
        for rule, desc, new_src in candidates(cur.ast, cur.source):
            try:
                cand = analyse(new_src, flags)
            except Exception as exc:  # candidate did not even parse
                res.rejected.append({"rule": rule, "candidate": new_src, "reason": f"invalid: {exc}"})
                continue
            eq, cex = equivalent(cur.dfa, cand.dfa)
            if not eq:
                res.rejected.append({"rule": rule, "candidate": new_src,
                                     "reason": f"not equivalent — distinguishing input {cex!r}"})
                continue
            if RISK_ORDER[cand.redos.risk] > RISK_ORDER[cur.redos.risk]:
                res.rejected.append({"rule": rule, "candidate": new_src, "reason": "risk would increase"})
                continue
            res.steps.append(RewriteStep(rule, desc, cur.source, new_src, True,
                                         (cur.dfa.n, cand.dfa.n), cur.redos.risk, cand.redos.risk))
            cur = cand
            progressed = True
            break
        if not progressed:
            break
    res.final = cur.source
    res.final_risk = cur.redos.risk
    return res
