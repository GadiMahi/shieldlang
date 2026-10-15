"""
DFA construction and algorithms (RegexShield v2).

* Subset construction over a compressed alphabet (Partition classes)
* Moore partition-refinement minimisation (tag-aware)
* Product construction (intersection / union / difference / xor, and an
  N-way *tagged* product used by the optimiser to merge block rules)
* Language emptiness, equivalence with counter-example, length bounds
* A linear-time matcher: one table lookup per input character
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from .charset import CharSet, Partition
from .nfa import NFA

MAX_DFA_STATES = 4000


class DFATooLarge(Exception):
    pass


@dataclass
class DFA:
    partition: Partition
    delta: List[List[int]]                 # complete: delta[state][class] -> state
    start: int
    tags: List[FrozenSet[int]]             # non-empty => accepting
    dead: Optional[int] = None
    subsets: Optional[List[List[int]]] = None  # NFA states per DFA state (pre-minimisation)
    meta: Dict = field(default_factory=dict)

    # ---- basic properties ---------------------------------------------
    @property
    def n(self) -> int:
        return len(self.delta)

    def accepting(self, s: int) -> bool:
        return bool(self.tags[s])

    def accept_states(self) -> List[int]:
        return [s for s in range(self.n) if self.tags[s]]

    def step(self, s: int, ch: str) -> int:
        return self.delta[s][self.partition.classify(ord(ch))]

    def run(self, text: str) -> Tuple[int, int]:
        """Returns (final_state, steps). Stops early in the dead state."""
        s = self.start
        steps = 0
        delta, classify, dead = self.delta, self.partition.classify, self.dead
        for ch in text:
            s = delta[s][classify(ord(ch))]
            steps += 1
            if s == dead:
                break
        return s, steps

    def matches(self, text: str) -> bool:
        return self.accepting(self.run(text)[0])

    # ---- analysis -----------------------------------------------------
    def _live(self) -> List[bool]:
        """States from which an accepting state is reachable."""
        rev: List[List[int]] = [[] for _ in range(self.n)]
        for s, row in enumerate(self.delta):
            for t in set(row):
                rev[t].append(s)
        live = [False] * self.n
        dq = deque(s for s in range(self.n) if self.tags[s])
        for s in dq:
            live[s] = True
        while dq:
            s = dq.popleft()
            for p in rev[s]:
                if not live[p]:
                    live[p] = True
                    dq.append(p)
        return live

    def is_empty(self) -> bool:
        return self.shortest_accepted() is None

    def shortest_accepted(self, target_tag: Optional[int] = None) -> Optional[str]:
        prev: Dict[int, Tuple[int, int]] = {self.start: (-1, -1)}
        dq = deque([self.start])
        while dq:
            s = dq.popleft()
            if self.tags[s] and (target_tag is None or target_tag in self.tags[s]):
                out = []
                while prev[s][0] != -1:
                    p, c = prev[s]
                    out.append(self.partition.classes[c].sample())
                    s = p
                return "".join(reversed(out))
            for c, t in enumerate(self.delta[s]):
                if t not in prev:
                    prev[t] = (s, c)
                    dq.append(t)
        return None

    def length_bounds(self) -> Tuple[Optional[int], Optional[int]]:
        """(min_len, max_len) of accepted strings; max None = unbounded; (None, None) = empty."""
        mn = self.shortest_accepted()
        if mn is None:
            return None, None
        live = self._live()
        # reachable & live sub-graph
        reach = {self.start}
        dq = deque([self.start])
        while dq:
            s = dq.popleft()
            for t in set(self.delta[s]):
                if t not in reach and live[t]:
                    reach.add(t)
                    dq.append(t)
        # cycle detection + longest path (DAG) via DFS
        WHITE, GREY, BLACK = 0, 1, 2
        color = {s: WHITE for s in reach}
        longest: Dict[int, int] = {}

        import sys
        sys.setrecursionlimit(max(10000, sys.getrecursionlimit()))

        def dfs(s: int) -> Optional[int]:
            color[s] = GREY
            best = 0 if self.tags[s] else -1
            for t in set(self.delta[s]):
                if t not in reach:
                    continue
                if color[t] == GREY:
                    return None
                if color[t] == WHITE:
                    r = dfs(t)
                    if r is None:
                        return None
                sub = longest[t]
                if sub >= 0:
                    best = max(best, sub + 1)
            color[s] = BLACK
            longest[s] = best
            return best

        mx = dfs(self.start)
        return len(mn), mx

    # ---- presentation -------------------------------------------------
    def to_json(self, include_dead: bool = False) -> Dict:
        edges = []
        for s, row in enumerate(self.delta):
            if s == self.dead and not include_dead:
                continue
            by_target: Dict[int, CharSet] = {}
            for c, t in enumerate(row):
                if t == self.dead and not include_dead:
                    continue
                by_target[t] = by_target.get(t, CharSet.empty()).union(self.partition.classes[c])
            for t, cs in by_target.items():
                edges.append({"from": s, "to": t, "label": cs.to_display()})
        states = []
        for s in range(self.n):
            if s == self.dead and not include_dead:
                continue
            states.append({"id": s, "start": s == self.start, "accept": bool(self.tags[s]),
                           "tags": sorted(self.tags[s]),
                           "nfa_states": self.subsets[s] if self.subsets else None})
        return {"states": states, "edges": edges, "start": self.start,
                "accept": self.accept_states(), "dead": self.dead,
                "total_states": len(states), "alphabet_classes": len(self.partition),
                "meta": self.meta}

    def tables(self) -> Dict:
        """Compact tables for code generation."""
        return {"starts": self.partition.starts, "cls": self.partition.class_of_interval,
                "delta": self.delta, "start": self.start,
                "accept": [sorted(t) for t in self.tags], "dead": self.dead}


# ---------------------------------------------------------------------------
def determinize(nfa: NFA, partition: Optional[Partition] = None) -> DFA:
    part = partition or Partition(nfa.charsets())
    # class lists per NFA transition
    trans_cls: List[List[Tuple[List[int], int]]] = [
        [(part.classes_in(cs), d) for cs, d in ts] for ts in nfa.trans]
    start = nfa.closure([nfa.start])
    index: Dict[frozenset, int] = {start: 0}
    subsets = [start]
    delta: List[List[int]] = []
    k = len(part)
    i = 0
    while i < len(subsets):
        S = subsets[i]
        moves: List[set] = [set() for _ in range(k)]
        for q in S:
            for cls_list, d in trans_cls[q]:
                for c in cls_list:
                    moves[c].add(d)
        row = []
        for c in range(k):
            T = nfa.closure(moves[c]) if moves[c] else frozenset()
            if T not in index:
                if len(subsets) >= MAX_DFA_STATES:
                    raise DFATooLarge(f"DFA exceeds {MAX_DFA_STATES} states during subset construction")
                index[T] = len(subsets)
                subsets.append(T)
            row.append(index[T])
        delta.append(row)
        i += 1
    tags = [frozenset([0]) if nfa.accept in S else frozenset() for S in subsets]
    dead = index.get(frozenset())
    return DFA(part, delta, 0, tags, dead, [sorted(S) for S in subsets])


def minimize(d: DFA) -> DFA:
    """Moore's partition refinement. Initial blocks are split by accept tags."""
    n, k = d.n, len(d.partition)
    # unreachable removal
    reach = [False] * n
    reach[d.start] = True
    dq = deque([d.start])
    while dq:
        s = dq.popleft()
        for t in d.delta[s]:
            if not reach[t]:
                reach[t] = True
                dq.append(t)
    states = [s for s in range(n) if reach[s]]
    tag_ids: Dict[FrozenSet[int], int] = {}
    block = {}
    for s in states:
        block[s] = tag_ids.setdefault(d.tags[s], len(tag_ids))
    while True:
        sigs: Dict[Tuple, int] = {}
        new_block = {}
        for s in states:
            sig = (block[s],) + tuple(block[d.delta[s][c]] for c in range(k))
            new_block[s] = sigs.setdefault(sig, len(sigs))
        if len(sigs) == len(set(block.values())):
            block = new_block
            break
        block = new_block
    # renumber so the start state is 0, then BFS order
    order: Dict[int, int] = {}
    rep: Dict[int, int] = {}
    for s in states:
        rep.setdefault(block[s], s)
    dq = deque([block[d.start]])
    order[block[d.start]] = 0
    while dq:
        b = dq.popleft()
        for c in range(k):
            nb = block[d.delta[rep[b]][c]]
            if nb not in order:
                order[nb] = len(order)
                dq.append(nb)
    m = len(order)
    delta = [[0] * k for _ in range(m)]
    tags: List[FrozenSet[int]] = [frozenset()] * m
    subsets: List[List[int]] = [[] for _ in range(m)]
    for b, i in order.items():
        s = rep[b]
        tags[i] = d.tags[s]
        delta[i] = [order[block[d.delta[s][c]]] for c in range(k)]
    if d.subsets:
        for s in states:
            if block[s] in order:
                subsets[order[block[s]]] = sorted(set(subsets[order[block[s]]]) | set(d.subsets[s]))
    dead = None
    for i in range(m):
        if not tags[i] and all(t == i for t in delta[i]):
            dead = i
            break
    out = DFA(d.partition, delta, 0, tags, dead, subsets if d.subsets else None, dict(d.meta))
    return compress_alphabet(out)


def compress_alphabet(d: DFA) -> DFA:
    """Merges symbol classes whose columns are identical in the transition table."""
    k = len(d.partition)
    col_key: Dict[Tuple[int, ...], int] = {}
    mapping = []
    for c in range(k):
        key = tuple(d.delta[s][c] for s in range(d.n))
        mapping.append(col_key.setdefault(key, len(col_key)))
    if len(col_key) == k:
        return d
    new_sets: List[CharSet] = [CharSet.empty() for _ in col_key]
    for c, m in enumerate(mapping):
        new_sets[m] = new_sets[m].union(d.partition.classes[c])
    # The merged sets are disjoint and cover Σ, so Partition() yields exactly them.
    part = Partition(new_sets)
    old_of_new = [d.partition.classify(cls.ranges[0][0]) for cls in part.classes]
    delta = [[d.delta[s][c] for c in old_of_new] for s in range(d.n)]
    return DFA(part, delta, d.start, d.tags, d.dead, d.subsets, d.meta)


def compile_dfa(nfa: NFA) -> Tuple[DFA, DFA]:
    raw = determinize(nfa)
    return raw, minimize(raw)


# ---------------------------------------------------------------------------
def product(dfas: Sequence[DFA], mode: str = "and") -> DFA:
    """
    N-way product. mode: 'and' | 'or' | 'xor' (2 only) | 'diff' (2 only) | 'tagged'.
    In 'tagged' mode a product state carries the set of component indices that
    accept, which lets one DFA pass report *which* rule matched.
    """
    part = Partition([cs for d in dfas for cs in d.partition.classes])
    k = len(part)
    comp_cls = [[d.partition.classify(cls.ranges[0][0]) for cls in part.classes] for d in dfas]
    start = tuple(d.start for d in dfas)
    index = {start: 0}
    states = [start]
    delta: List[List[int]] = []
    i = 0
    while i < len(states):
        tup = states[i]
        row = []
        for c in range(k):
            nxt = tuple(d.delta[s][comp_cls[j][c]] for j, (d, s) in enumerate(zip(dfas, tup)))
            if nxt not in index:
                if len(states) >= MAX_DFA_STATES:
                    raise DFATooLarge("product DFA too large")
                index[nxt] = len(states)
                states.append(nxt)
            row.append(index[nxt])
        delta.append(row)
        i += 1
    tags = []
    for tup in states:
        acc = [bool(d.tags[s]) for d, s in zip(dfas, tup)]
        if mode == "and":
            t = frozenset([0]) if all(acc) else frozenset()
        elif mode == "or":
            t = frozenset([0]) if any(acc) else frozenset()
        elif mode == "xor":
            t = frozenset([0]) if acc[0] != acc[1] else frozenset()
        elif mode == "diff":
            t = frozenset([0]) if acc[0] and not acc[1] else frozenset()
        elif mode == "tagged":
            t = frozenset(j for j, a in enumerate(acc) if a)
        else:
            raise ValueError(mode)
        tags.append(t)
    return minimize(DFA(part, delta, 0, tags, None))


def equivalent(a: DFA, b: DFA) -> Tuple[bool, Optional[str]]:
    """Language equivalence; returns a distinguishing string when different."""
    x = product([a, b], "xor")
    w = x.shortest_accepted()
    return (w is None), w
