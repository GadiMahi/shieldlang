"""
Thompson ε-NFA construction with source provenance (RegexShield v2).

Every NFA state remembers the AST node that created it ("owner"), and every
AST node records the fragment it was lowered to, so that analysis results on
the automaton can be mapped back to the exact sub-expression in the source.

Anchors are only meaningful at pattern boundaries. A pattern without '^' gets
an implicit Σ* prefix and one without '$' an implicit Σ* suffix, which models
the search semantics of re.search / RegExp.test exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .ast import (AnchorEnd, AnchorStart, Alternation, CharNode, Concat, Group,
                  Repeat, RNode)
from .charset import CharSet
from .parser import RegexSyntaxError


@dataclass
class NFA:
    eps: List[List[int]] = field(default_factory=list)
    trans: List[List[Tuple[CharSet, int]]] = field(default_factory=list)
    owner: List[Optional[str]] = field(default_factory=list)
    role: List[str] = field(default_factory=list)
    start: int = 0
    accept: int = 0
    provenance: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.eps)

    def new_state(self, owner: Optional[str], role: str = "") -> int:
        self.eps.append([])
        self.trans.append([])
        self.owner.append(owner)
        self.role.append(role)
        return len(self.eps) - 1

    def add_eps(self, a: int, b: int):
        self.eps[a].append(b)

    def add_sym(self, a: int, cs: CharSet, b: int):
        self.trans[a].append((cs, b))

    def charsets(self) -> List[CharSet]:
        return [cs for ts in self.trans for cs, _ in ts]

    def closure(self, states) -> frozenset:
        stack = list(states)
        seen = set(stack)
        while stack:
            s = stack.pop()
            for t in self.eps[s]:
                if t not in seen:
                    seen.add(t)
                    stack.append(t)
        return frozenset(seen)

    def transition_count(self) -> int:
        return sum(len(e) for e in self.eps) + sum(len(t) for t in self.trans)

    def to_json(self) -> Dict[str, Any]:
        edges = []
        for s in range(self.size):
            for t in self.eps[s]:
                edges.append({"from": s, "to": t, "label": "ε", "epsilon": True})
            for cs, t in self.trans[s]:
                edges.append({"from": s, "to": t, "label": cs.to_display(), "epsilon": False})
        return {
            "states": [{"id": s, "owner": self.owner[s], "role": self.role[s],
                        "start": s == self.start, "accept": s == self.accept}
                       for s in range(self.size)],
            "edges": edges,
            "start": self.start,
            "accept": [self.accept],
            "total_states": self.size,
            "total_transitions": len(edges),
            "provenance": self.provenance,
        }


class Thompson:
    def __init__(self):
        self.nfa = NFA()

    def _record(self, node: RNode, s: int, f: int, rule: str, first_state: int):
        self.nfa.provenance.append({
            "ast_node_id": node.node_id,
            "node_type": node.node_type,
            "span": {"start": node.start, "end": node.end},
            "start_state": s,
            "accept_state": f,
            "states": list(range(first_state, self.nfa.size)),
            "lowering_rule": rule,
        })

    def frag(self, node: RNode) -> Tuple[int, int]:
        n = self.nfa
        first = n.size
        oid = node.node_id
        if isinstance(node, CharNode):
            s, f = n.new_state(oid), n.new_state(oid)
            n.add_sym(s, node.charset, f)
            self._record(node, s, f, f"Base: q{s} --{node.charset.to_display()}--> q{f}", first)
            return s, f
        if isinstance(node, Concat):
            s, f = self.seq(node.items, node)
            self._record(node, s, f, f"Concat: chain {len(node.items)} fragment(s) with ε-links", first)
            return s, f
        if isinstance(node, Alternation):
            s, f = n.new_state(oid, "alt-split"), n.new_state(oid, "alt-join")
            for b in node.branches:
                bs, bf = self.frag(b)
                n.add_eps(s, bs)
                n.add_eps(bf, f)
            self._record(node, s, f, f"Alternation: ε-split q{s} into {len(node.branches)} branches, join at q{f}", first)
            return s, f
        if isinstance(node, Group):
            s, f = self.frag(node.child) if node.child else self.seq([], node)
            self._record(node, s, f, "Group: transparent (reuses inner fragment)", first)
            return s, f
        if isinstance(node, Repeat):
            s, f = self.repeat(node)
            self._record(node, s, f, f"{node.node_type} {node.quant_text()}: loop/optional ε-structure", first)
            return s, f
        if isinstance(node, (AnchorStart, AnchorEnd)):
            raise RegexSyntaxError("Anchors are only supported at the start or end of the pattern (or of a top-level branch)",
                                   node.start, node.end, stage="NFA_LOWERING")
        raise TypeError(node)

    def seq(self, items: List[RNode], owner: RNode) -> Tuple[int, int]:
        n = self.nfa
        if not items:
            s, f = n.new_state(owner.node_id), n.new_state(owner.node_id)
            n.add_eps(s, f)
            return s, f
        frags = [self.frag(i) for i in items]
        for (_, a_f), (b_s, _) in zip(frags, frags[1:]):
            n.add_eps(a_f, b_s)
        return frags[0][0], frags[-1][1]

    def repeat(self, node: Repeat) -> Tuple[int, int]:
        n = self.nfa
        oid = node.node_id
        child = node.child
        lo, hi = node.min, node.max
        if lo == 0 and hi is None:  # star
            s, f = n.new_state(oid, "loop-entry"), n.new_state(oid, "loop-exit")
            cs, cf = self.frag(child)
            n.add_eps(s, cs)
            n.add_eps(s, f)
            n.add_eps(cf, cs)
            n.add_eps(cf, f)
            return s, f
        if lo == 1 and hi is None:  # plus
            s, f = n.new_state(oid, "loop-entry"), n.new_state(oid, "loop-exit")
            cs, cf = self.frag(child)
            n.add_eps(s, cs)
            n.add_eps(cf, cs)
            n.add_eps(cf, f)
            return s, f
        # general {lo,hi}: lo mandatory copies, then a star or (hi-lo) nested optionals
        s = n.new_state(oid, "repeat-entry")
        cur = s
        for _ in range(lo):
            cs, cf = self.frag(child)
            n.add_eps(cur, cs)
            cur = cf
        f = n.new_state(oid, "repeat-exit")
        if hi is None:
            loop = n.new_state(oid, "loop-entry")
            n.add_eps(cur, loop)
            cs, cf = self.frag(child)
            n.add_eps(loop, cs)
            n.add_eps(loop, f)
            n.add_eps(cf, cs)
            n.add_eps(cf, f)
        else:
            for _ in range(hi - lo):
                cs, cf = self.frag(child)
                n.add_eps(cur, cs)
                n.add_eps(cur, f)
                cur = cf
            n.add_eps(cur, f)
        return s, f


@dataclass
class Branch:
    anchored_start: bool
    anchored_end: bool
    items: List[RNode]
    node: RNode


def split_anchors(root: RNode) -> List[Branch]:
    branches = root.branches if isinstance(root, Alternation) else [root]
    out = []
    for b in branches:
        items = list(b.items) if isinstance(b, Concat) else [b]
        a_s = bool(items) and isinstance(items[0], AnchorStart)
        if a_s:
            items = items[1:]
        a_e = bool(items) and isinstance(items[-1], AnchorEnd)
        if a_e:
            items = items[:-1]
        for it in items:
            for sub in it.walk():
                if isinstance(sub, (AnchorStart, AnchorEnd)):
                    raise RegexSyntaxError(
                        "Anchors are only supported at the start or end of the pattern (or of a top-level branch)",
                        sub.start, sub.end, stage="NFA_LOWERING",
                        hint="Move '^' to the very start and '$' to the very end.")
        out.append(Branch(a_s, a_e, items, b))
    return out


def build_nfa(root: RNode) -> NFA:
    """Lowers a regex AST to a single-accept ε-NFA with search semantics."""
    th = Thompson()
    n = th.nfa
    start = n.new_state(None, "start")
    accept = n.new_state(None, "accept")
    for br in split_anchors(root):
        bs, bf = th.seq(br.items, br.node) if len(br.items) != 1 else th.frag(br.items[0])
        if br.anchored_start:
            n.add_eps(start, bs)
        else:
            pre = n.new_state(None, "search-prefix Σ*")
            n.add_sym(pre, CharSet.any(), pre)
            n.add_eps(start, pre)
            n.add_eps(pre, bs)
        if br.anchored_end:
            n.add_eps(bf, accept)
        else:
            post = n.new_state(None, "search-suffix Σ*")
            n.add_sym(post, CharSet.any(), post)
            n.add_eps(bf, post)
            n.add_eps(post, accept)
    n.start, n.accept = start, accept
    return n


# ---------------------------------------------------------------------------
# ε-elimination that *preserves path multiplicity* (needed for ambiguity)
# ---------------------------------------------------------------------------
@dataclass
class EFEdge:
    cs: CharSet
    dst: int
    mult: int              # 1 or 2 (= "two or more" distinct ε-paths)
    via: frozenset         # Thompson states traversed on the ε-paths
    src_state: int         # Thompson state owning the symbol transition


@dataclass
class EpsFreeNFA:
    states: List[int]
    start: int
    accepting: Set[int]
    edges: Dict[int, List[EFEdge]]


def eps_free(nfa: NFA) -> EpsFreeNFA:
    kept = {nfa.start}
    for ts in nfa.trans:
        for _, d in ts:
            kept.add(d)
    edges: Dict[int, List[EFEdge]] = {}
    accepting: Set[int] = set()
    for p in kept:
        counts: Dict[int, int] = {}
        via: Dict[int, Set[int]] = {}
        stack = [(p, (p,))]
        steps = 0
        while stack and steps < 20000:
            steps += 1
            u, path = stack.pop()
            counts[u] = counts.get(u, 0) + 1
            via.setdefault(u, set()).update(path)
            if counts[u] > 2:
                continue
            for v in nfa.eps[u]:
                if v not in path:
                    stack.append((v, path + (v,)))
        if nfa.accept in counts:
            accepting.add(p)
        out = []
        for q, c in counts.items():
            for cs, r in nfa.trans[q]:
                out.append(EFEdge(cs, r, min(c, 2), frozenset(via[q]), q))
        edges[p] = out
    # keep only states reachable from start
    reach = {nfa.start}
    stack = [nfa.start]
    while stack:
        s = stack.pop()
        for e in edges.get(s, []):
            if e.dst not in reach:
                reach.add(e.dst)
                stack.append(e.dst)
    return EpsFreeNFA(sorted(reach), nfa.start, accepting & reach,
                      {s: edges.get(s, []) for s in reach})
