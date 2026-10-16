"""
ReDoS analysis (RegexShield v2).

Two complementary layers:

1. Structural indicators on the AST (carried over from Review 1, extended):
   nested unbounded quantifiers, overlapping alternatives under repetition,
   adjacent overlapping loops.

2. Automata-based ambiguity analysis on the ε-free Thompson NFA, the
   standard theory behind backtracking blow-up:
   * EDA (exponential degree of ambiguity): some state q has two distinct
     loops on the same word. Detected as a strongly-connected component of
     the NFA×NFA pair graph that contains a diagonal pair (q,q) and either an
     off-diagonal pair or a "split" step (two distinct paths that land on the
     same state). Backtracking time: O(2^n).
   * IDA (infinite / polynomial degree): states p≠q with p→p, p→q, q→q all on
     the same word w. Detected by search in the NFA×NFA×NFA triple graph.
     Chains of such pairs give degree k and time O(n^k).

For every finding we build a concrete attack string prefix·pump^n·suffix,
where the suffix is chosen with the DFA so the whole input is *rejected*
(rejection is what forces a backtracking engine to explore every path), and
we map the offending automaton states back to the AST node responsible.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .ast import Alternation, CharNode, Concat, Group, Repeat, RNode, parent_map
from .charset import CharSet
from .dfa import DFA
from .nfa import NFA, EFEdge, EpsFreeNFA, eps_free

PAIR_BUDGET = 300_000
TRIPLE_BUDGET = 400_000


@dataclass
class Finding:
    kind: str                 # NESTED_QUANTIFIER | OVERLAPPING_ALTERNATIVES | ADJACENT_LOOPS | EDA | IDA
    layer: str                # structural | automata
    severity: str             # HIGH | MEDIUM | LOW | INFO
    message: str
    spans: List[Dict[str, int]] = field(default_factory=list)
    node_ids: List[str] = field(default_factory=list)
    detail: Dict = field(default_factory=dict)

    def to_json(self):
        return {"kind": self.kind, "layer": self.layer, "severity": self.severity,
                "message": self.message, "spans": self.spans, "node_ids": self.node_ids,
                "detail": self.detail}


@dataclass
class Witness:
    prefix: str
    pump: str
    suffix: str
    kind: str

    def build(self, n: int) -> str:
        return self.prefix + self.pump * n + self.suffix

    def to_json(self):
        return {"prefix": self.prefix, "pump": self.pump, "suffix": self.suffix, "kind": self.kind,
                "example": self.build(12 if self.kind == "EDA" else 40)}


@dataclass
class ReDoSReport:
    risk: str                       # HIGH | MEDIUM | LOW | UNKNOWN
    complexity: str                 # "O(2^n)", "O(n^2)", "O(n)"
    summary: str
    findings: List[Finding]
    witness: Optional[Witness]
    degree: Optional[int]
    notes: List[str]
    stats: Dict

    def to_json(self):
        return {"risk": self.risk, "complexity": self.complexity, "summary": self.summary,
                "findings": [f.to_json() for f in self.findings],
                "witness": self.witness.to_json() if self.witness else None,
                "degree": self.degree, "notes": self.notes, "stats": self.stats}


# ---------------------------------------------------------------------------
# Structural layer
# ---------------------------------------------------------------------------
def _strip_groups(n: Optional[RNode]) -> Optional[RNode]:
    while isinstance(n, Group):
        n = n.child
    return n


def nullable(n: Optional[RNode]) -> bool:
    if n is None:
        return True
    if isinstance(n, CharNode):
        return False
    if isinstance(n, Concat):
        return all(nullable(i) for i in n.items)
    if isinstance(n, Alternation):
        return any(nullable(b) for b in n.branches)
    if isinstance(n, Group):
        return nullable(n.child)
    if isinstance(n, Repeat):
        return n.min == 0 or nullable(n.child)
    return True  # anchors


def first_set(n: Optional[RNode]) -> CharSet:
    if n is None:
        return CharSet.empty()
    if isinstance(n, CharNode):
        return n.charset
    if isinstance(n, Concat):
        out = CharSet.empty()
        for i in n.items:
            out = out.union(first_set(i))
            if not nullable(i):
                break
        return out
    if isinstance(n, Alternation):
        out = CharSet.empty()
        for b in n.branches:
            out = out.union(first_set(b))
        return out
    if isinstance(n, (Group, Repeat)):
        return first_set(n.child)
    return CharSet.empty()


def _span(n: RNode) -> Dict[str, int]:
    return {"start": n.start, "end": n.end}


def structural_findings(root: RNode, source: str) -> List[Finding]:
    out: List[Finding] = []
    for n in root.walk():
        if isinstance(n, Repeat) and n.unbounded:
            for inner in n.child.walk() if n.child else []:
                if isinstance(inner, Repeat) and inner is not n and (inner.unbounded or nullable(inner)) \
                        and not nullable(inner.child) and inner.max != 1:
                    out.append(Finding(
                        "NESTED_QUANTIFIER", "structural", "HIGH",
                        f"Unbounded quantifier '{source[inner.start:inner.end]}' is nested inside "
                        f"'{source[n.start:n.end]}': the same input can be split between the loops in exponentially many ways.",
                        [_span(n), _span(inner)], [n.node_id, inner.node_id]))
                    break
                if isinstance(inner, Repeat) and inner is not n and inner.max == 1 and not nullable(inner.child) \
                        and _strip_groups(n.child) is inner:
                    out.append(Finding(
                        "NESTED_QUANTIFIER", "structural", "HIGH",
                        f"Optional '{source[inner.start:inner.end]}' directly under '{source[n.start:n.end]}' creates empty loop iterations.",
                        [_span(n), _span(inner)], [n.node_id, inner.node_id]))
                    break
            body = _strip_groups(n.child)
            if isinstance(body, Alternation):
                firsts = [first_set(b) for b in body.branches]
                for i in range(len(firsts)):
                    for j in range(i + 1, len(firsts)):
                        if firsts[i].overlaps(firsts[j]):
                            bi, bj = body.branches[i], body.branches[j]
                            out.append(Finding(
                                "OVERLAPPING_ALTERNATIVES", "structural", "MEDIUM",
                                f"Alternatives '{source[bi.start:bi.end]}' and '{source[bj.start:bj.end]}' can start with the same "
                                f"character ({firsts[i].intersect(firsts[j]).to_display()}) inside a repetition.",
                                [_span(bi), _span(bj)], [bi.node_id, bj.node_id]))
                            break
                    else:
                        continue
                    break
        if isinstance(n, Concat):
            loops = n.items
            for a, b in zip(loops, loops[1:]):
                if isinstance(a, Repeat) and isinstance(b, Repeat) and a.unbounded and b.unbounded \
                        and first_set(a.child).overlaps(first_set(b.child)):
                    out.append(Finding(
                        "ADJACENT_LOOPS", "structural", "MEDIUM",
                        f"Adjacent loops '{source[a.start:a.end]}' and '{source[b.start:b.end]}' accept overlapping characters, "
                        f"so the split point between them is ambiguous.",
                        [_span(a), _span(b)], [a.node_id, b.node_id]))
    return out


# ---------------------------------------------------------------------------
# Automata layer
# ---------------------------------------------------------------------------
@dataclass
class PairEdge:
    src: Tuple[int, int]
    dst: Tuple[int, int]
    cs: CharSet
    split: bool
    e1: EFEdge
    e2: EFEdge


class TooBig(Exception):
    pass


def _pair_graph(ef: EpsFreeNFA) -> Tuple[Dict[Tuple[int, int], List[PairEdge]], int]:
    start = (ef.start, ef.start)
    graph: Dict[Tuple[int, int], List[PairEdge]] = {}
    dq = deque([start])
    graph[start] = []
    count = 0
    while dq:
        node = dq.popleft()
        p1, p2 = node
        out = []
        for e1 in ef.edges[p1]:
            for e2 in ef.edges[p2]:
                inter = e1.cs.intersect(e2.cs)
                if inter.is_empty():
                    continue
                split = p1 == p2 and (e1 is not e2 or e1.mult >= 2)
                dst = (e1.dst, e2.dst)
                out.append(PairEdge(node, dst, inter, split, e1, e2))
                count += 1
                if count > PAIR_BUDGET:
                    raise TooBig()
                if dst not in graph:
                    graph[dst] = []
                    dq.append(dst)
        graph[node] = out
    return graph, count


def _tarjan(graph: Dict) -> Dict:
    index: Dict = {}
    low: Dict = {}
    on: Set = set()
    stack: List = []
    comp: Dict = {}
    counter = [0]
    cid = [0]
    for root in graph:
        if root in index:
            continue
        work = [(root, iter(graph[root]))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for e in it:
                w = e.dst if hasattr(e, "dst") else e
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(graph.get(w, []))))
                    advanced = True
                    break
                elif w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                u = work[-1][0]
                low[u] = min(low[u], low[v])
            if low[v] == index[v]:
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp[w] = cid[0]
                    if w == v:
                        break
                cid[0] += 1
    return comp


def _bfs_path(graph, src, dst_pred, allowed=None):
    """BFS over pair/triple graph edges; returns list of edges from src to a node satisfying dst_pred."""
    prev = {src: None}
    dq = deque([src])
    while dq:
        u = dq.popleft()
        for e in graph.get(u, []):
            v = e.dst
            if allowed is not None and v not in allowed:
                continue
            if v not in prev:
                prev[v] = e
                if dst_pred(v):
                    path = []
                    while prev[v] is not None:
                        path.append(prev[v])
                        v = prev[v].src
                    return list(reversed(path))
                dq.append(v)
    return None


def _nfa_prefix(ef: EpsFreeNFA, target: int) -> Optional[str]:
    prev: Dict[int, Optional[Tuple[int, str]]] = {ef.start: None}
    dq = deque([ef.start])
    while dq:
        u = dq.popleft()
        if u == target:
            out = []
            while prev[u] is not None:
                p, c = prev[u]
                out.append(c)
                u = p
            return "".join(reversed(out))
        for e in ef.edges[u]:
            if e.dst not in prev:
                prev[e.dst] = (u, e.cs.sample())
                dq.append(e.dst)
    return None


def _rejecting_suffix(dfa: DFA, prefix: str, pump: str) -> Optional[str]:
    cands = [""] + [c.sample() for c in dfa.partition.classes] + ["!", "\x00", "\n"]
    good = []
    for suf in dict.fromkeys(cands):
        ok = True
        dead_hit = True
        for k in (3, 4, 5, 8):
            s, _ = dfa.run(prefix + pump * k + suf)
            if dfa.accepting(s):
                ok = False
                break
            if s != dfa.dead:
                dead_hit = False
        if ok:
            if dead_hit:
                return suf
            good.append(suf)
    return good[0] if good else None


class _Provenance:
    def __init__(self, nfa: NFA, root: RNode):
        self.nfa = nfa
        self.root = root
        self.parents = parent_map(root)
        self.by_id = {n.node_id: n for n in root.walk()}

    def ancestors(self, nid: str) -> List[RNode]:
        chain = []
        n = self.by_id.get(nid)
        while n is not None:
            chain.append(n)
            n = self.parents.get(n.node_id)
        return chain  # innermost first

    def culprit(self, states: Set[int]) -> Optional[RNode]:
        owners = {self.nfa.owner[s] for s in states if self.nfa.owner[s]}
        if not owners:
            return None
        chains = [list(reversed(self.ancestors(o))) for o in owners]  # root first
        dca = None
        for level in zip(*chains):
            if all(x is level[0] for x in level):
                dca = level[0]
            else:
                break
        n = dca
        while n is not None and not (isinstance(n, Repeat) and n.unbounded):
            n = self.parents.get(n.node_id)
        return n or dca

    def loop_of(self, state: int) -> Optional[RNode]:
        o = self.nfa.owner[state]
        if not o:
            return None
        for a in self.ancestors(o):
            if isinstance(a, Repeat) and a.unbounded:
                return a
        return None


def automata_analysis(nfa: NFA, root: RNode, dfa: DFA, source: str) -> Tuple[List[Finding], Optional[Witness], Optional[int], List[str], Dict]:
    notes: List[str] = []
    ef = eps_free(nfa)
    if not ef.complete:
        return [], None, None, ["Epsilon-path budget exceeded; ambiguity analysis inconclusive."], {}
    prov = _Provenance(nfa, root)
    stats = {"eps_free_states": len(ef.states),
             "eps_free_edges": sum(len(v) for v in ef.edges.values())}
    findings: List[Finding] = []
    witness: Optional[Witness] = None
    degree: Optional[int] = None

    # ---- EDA -------------------------------------------------------------
    try:
        graph, n_edges = _pair_graph(ef)
    except TooBig:
        notes.append("Pair-graph budget exceeded; exponential ambiguity check inconclusive.")
        return findings, None, None, notes, stats
    stats["pair_nodes"] = len(graph)
    stats["pair_edges"] = n_edges
    comp = _tarjan(graph)
    members: Dict[int, List] = {}
    for v, c in comp.items():
        members.setdefault(c, []).append(v)
    eda_found = False
    for c, nodes in members.items():
        diag = [v for v in nodes if v[0] == v[1]]
        if not diag:
            continue
        nodeset = set(nodes)
        split_edge = None
        for v in nodes:
            for e in graph[v]:
                if e.dst in nodeset and (e.split or e.dst[0] != e.dst[1]):
                    split_edge = e
                    break
            if split_edge:
                break
        if not split_edge:
            continue
        # cycle (q,q) -> split_edge.src -> split_edge.dst -> (q,q)
        q = diag[0]
        p1 = [] if split_edge.src == q else _bfs_path(graph, q, lambda v, t=split_edge.src: v == t, nodeset)
        p2 = [] if split_edge.dst == q else _bfs_path(graph, split_edge.dst, lambda v, t=q: v == t, nodeset)
        if p1 is None or p2 is None:
            continue
        cycle = p1 + [split_edge] + p2
        pump = "".join(e.cs.sample() for e in cycle)
        prefix = _nfa_prefix(ef, q[0]) or ""
        involved: Set[int] = set()
        for e in cycle:
            involved |= set(e.e1.via) | set(e.e2.via) | {e.dst[0], e.dst[1], e.e1.src_state, e.e2.src_state}
        node = prov.culprit(involved)
        suffix = _rejecting_suffix(dfa, prefix, pump)
        span = [_span(node)] if node else []
        where = f"'{source[node.start:node.end]}'" if node else "the implicit search prefix"
        if suffix is None:
            findings.append(Finding(
                "EDA", "automata", "UNKNOWN",
                f"State q{q[0]} has two distinct loops on '{pump}' ({where}). No rejecting witness was found; "
                f"accepted inputs may still backtrack before a later alternative succeeds. Safety is unverified.",
                span, [node.node_id] if node else [], {"state": q[0], "pump": pump}))
            continue
        eda_found = True
        witness = Witness(prefix, pump, suffix, "EDA")
        findings.append(Finding(
            "EDA", "automata", "HIGH",
            f"Exponential ambiguity: NFA state q{q[0]} can loop on '{pump}' along two different paths through {where}. "
            f"Each extra '{pump}' doubles the work of a backtracking matcher.",
            span, [node.node_id] if node else [],
            {"state": q[0], "pump": pump, "prefix": prefix, "suffix": suffix,
             "cycle_length": len(cycle)}))
        break

    if eda_found:
        return findings, witness, None, notes, stats

    # ---- IDA -------------------------------------------------------------
    # loop states of the ε-free NFA
    simple_graph = {s: [e.dst for e in ef.edges[s]] for s in ef.states}
    scc = _tarjan(simple_graph)
    scc_size: Dict[int, int] = {}
    for s, c in scc.items():
        scc_size[c] = scc_size.get(c, 0) + 1
    loop_states = [s for s in ef.states
                   if scc_size[scc[s]] > 1 or any(e.dst == s for e in ef.edges[s])]
    reach_from: Dict[int, Set[int]] = {}
    for s in loop_states:
        seen = {s}
        dq = deque([s])
        while dq:
            u = dq.popleft()
            for v in simple_graph[u]:
                if v not in seen:
                    seen.add(v)
                    dq.append(v)
        reach_from[s] = seen

    @dataclass
    class TEdge:
        src: Tuple
        dst: Tuple
        cs: CharSet

    budget = [0]

    def triple_path(p: int, q: int) -> Optional[List[TEdge]]:
        start = (p, p, q)
        target = (p, q, q)
        prev: Dict[Tuple, Optional[TEdge]] = {}
        dq = deque()
        # first step taken explicitly so the word is non-empty
        def expand(node):
            a, b, c = node
            for e1 in ef.edges[a]:
                for e2 in ef.edges[b]:
                    i12 = e1.cs.intersect(e2.cs)
                    if i12.is_empty():
                        continue
                    for e3 in ef.edges[c]:
                        inter = i12.intersect(e3.cs)
                        if inter.is_empty():
                            continue
                        budget[0] += 1
                        if budget[0] > TRIPLE_BUDGET:
                            raise TooBig()
                        yield TEdge(node, (e1.dst, e2.dst, e3.dst), inter)
        for e in expand(start):
            if e.dst not in prev:
                prev[e.dst] = e
                dq.append(e.dst)
        while dq:
            u = dq.popleft()
            if u == target:
                path = []
                v = u
                while True:
                    e = prev[v]
                    path.append(e)
                    if e.src == start:
                        break
                    v = e.src
                return list(reversed(path))
            for e in expand(u):
                if e.dst not in prev:
                    prev[e.dst] = e
                    dq.append(e.dst)
        return None

    pairs: Dict[Tuple[int, int], List[TEdge]] = {}
    try:
        for p in loop_states:
            for q in loop_states:
                if p == q or scc[p] == scc[q] or q not in reach_from[p]:
                    continue
                path = triple_path(p, q)
                if path:
                    pairs[(p, q)] = path
    except TooBig:
        notes.append("Triple-product budget exceeded; polynomial ambiguity check is partial.")
    stats["ida_pairs"] = len(pairs)
    if not pairs:
        return findings, None, None, notes, stats

    # keep only exploitable pairs (a rejecting suffix exists), then
    # degree = longest chain p1 ⊳ p2 ⊳ ... + 1
    exploitable = {}
    for (p, q), path in pairs.items():
        pump = "".join(e.cs.sample() for e in path)
        prefix = _nfa_prefix(ef, p) or ""
        if _rejecting_suffix(dfa, prefix, pump) is not None:
            exploitable[(p, q)] = path
    if not exploitable:
        findings.append(Finding("IDA", "automata", "UNKNOWN",
                                "Overlapping loops exist; witness search did not establish their backtracking cost.",
                                [], [], {}))
        return findings, None, None, notes, stats
    pairs = exploitable
    succ: Dict[int, List[int]] = {}
    for (p, q) in pairs:
        succ.setdefault(p, []).append(q)
    memo: Dict[int, int] = {}

    def chain(u: int, depth=0) -> int:
        if u in memo:
            return memo[u]
        if depth > 50:
            return 0
        memo[u] = 0
        best = 0
        for v in succ.get(u, []):
            best = max(best, 1 + chain(v, depth + 1))
        memo[u] = best
        return best

    # pick an exploitable pair, prefer the one starting the longest chain
    ordered = sorted(pairs.items(), key=lambda kv: -chain(kv[0][0]))
    for (p, q), path in ordered:
        pump = "".join(e.cs.sample() for e in path)
        prefix = _nfa_prefix(ef, p) or ""
        suffix = _rejecting_suffix(dfa, prefix, pump)
        lp, lq = prov.loop_of(p), prov.loop_of(q)
        def desc(n):
            return f"'{source[n.start:n.end]}'" if n else "the implicit Σ* search prefix"
        spans = [_span(n) for n in (lp, lq) if n]
        ids = [n.node_id for n in (lp, lq) if n]
        if suffix is None:
            continue
        degree = chain(p) + 1
        witness = Witness(prefix, pump, suffix, "IDA")
        findings.append(Finding(
            "IDA", "automata", "MEDIUM",
            f"Polynomial ambiguity (degree {degree}): {desc(lp)} and {desc(lq)} can both consume '{pump}', "
            f"so a backtracking matcher tries O(n^{degree}) ways to split a failing input.",
            spans, ids, {"p": p, "q": q, "pump": pump, "prefix": prefix, "suffix": suffix,
                         "degree": degree, "implicit_prefix": lp is None}))
        if lp is None:
            notes.append("The pattern is not anchored with '^': a backtracking engine retries from every start position, "
                         "which acts like an implicit Σ* loop.")
        break
    else:
        findings.append(Finding("IDA", "automata", "UNKNOWN",
                                "Overlapping loops exist; witness search did not establish their backtracking cost.",
                                [], [], {}))
    return findings, witness, degree, notes, stats


def analyze(nfa: NFA, root: RNode, dfa: DFA, source: str) -> ReDoSReport:
    structural = structural_findings(root, source)
    auto, witness, degree, notes, stats = automata_analysis(nfa, root, dfa, source)
    findings = auto + structural
    kinds = {f.kind for f in auto if f.severity in ("HIGH", "MEDIUM")}
    inconclusive = any("inconclusive" in n or "partial" in n for n in notes) or any(
        f.severity == "UNKNOWN" for f in auto)
    if "EDA" in kinds:
        risk, cx = "HIGH", "O(2^n)"
        summary = "Exponential backtracking: a short crafted input can freeze a backtracking regex engine."
    elif "IDA" in kinds:
        risk, cx = "MEDIUM", f"O(n^{degree})"
        summary = f"Polynomial backtracking (degree {degree}): long crafted inputs cause super-linear slow-down."
    elif inconclusive:
        risk, cx = "UNKNOWN", "?"
        summary = "Backtracking safety is unverified: analysis is incomplete or no exploit witness was found."
    else:
        risk, cx = "LOW", "O(n)"
        summary = "No exploitable ambiguity detected within the supported analysis. Generated DFA checks do not backtrack."
        if any(f.severity in ("HIGH", "MEDIUM") for f in structural):
            notes.append("Structural indicators were found; no exploitable ambiguity was detected by this analysis.")
    return ReDoSReport(risk, cx, summary, findings, witness, degree, notes, stats)
