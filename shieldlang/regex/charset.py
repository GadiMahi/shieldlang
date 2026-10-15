"""
Character sets for RegexShield v2.

A CharSet is an immutable, normalised list of inclusive code-point ranges.
Character classes, escapes (\\d, \\w, \\s), the dot and literals are all
lowered to CharSets so that the automata layer works over *sets* of
characters instead of single symbols.

A Partition splits the whole Unicode range into the minimal set of disjoint
"symbol classes" (minterms) such that every CharSet used by an automaton is
a union of classes. DFAs are built over these class indices, which keeps
transition tables small even for patterns such as [A-Za-z0-9._-].
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Iterable, List, Sequence, Tuple

MAX_CP = 0x10FFFF
Range = Tuple[int, int]


def _normalise(ranges: Iterable[Range]) -> Tuple[Range, ...]:
    rs = sorted((max(0, lo), min(MAX_CP, hi)) for lo, hi in ranges if lo <= hi)
    out: List[Range] = []
    for lo, hi in rs:
        if out and lo <= out[-1][1] + 1:
            if hi > out[-1][1]:
                out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    return tuple(out)


class CharSet:
    __slots__ = ("ranges", "_hash")

    def __init__(self, ranges: Iterable[Range] = ()):
        self.ranges: Tuple[Range, ...] = _normalise(ranges)
        self._hash = hash(self.ranges)

    # ---- constructors -------------------------------------------------
    @staticmethod
    def of(ch: str) -> "CharSet":
        cp = ord(ch)
        return CharSet([(cp, cp)])

    @staticmethod
    def span(a: str, b: str) -> "CharSet":
        return CharSet([(ord(a), ord(b))])

    @staticmethod
    def any() -> "CharSet":
        return CharSet([(0, MAX_CP)])

    @staticmethod
    def empty() -> "CharSet":
        return CharSet()

    # ---- algebra ------------------------------------------------------
    def union(self, other: "CharSet") -> "CharSet":
        return CharSet(self.ranges + other.ranges)

    def negate(self) -> "CharSet":
        out: List[Range] = []
        prev = 0
        for lo, hi in self.ranges:
            if lo > prev:
                out.append((prev, lo - 1))
            prev = hi + 1
        if prev <= MAX_CP:
            out.append((prev, MAX_CP))
        return CharSet(out)

    def intersect(self, other: "CharSet") -> "CharSet":
        out: List[Range] = []
        i = j = 0
        a, b = self.ranges, other.ranges
        while i < len(a) and j < len(b):
            lo = max(a[i][0], b[j][0])
            hi = min(a[i][1], b[j][1])
            if lo <= hi:
                out.append((lo, hi))
            if a[i][1] < b[j][1]:
                i += 1
            else:
                j += 1
        return CharSet(out)

    def overlaps(self, other: "CharSet") -> bool:
        i = j = 0
        a, b = self.ranges, other.ranges
        while i < len(a) and j < len(b):
            if max(a[i][0], b[j][0]) <= min(a[i][1], b[j][1]):
                return True
            if a[i][1] < b[j][1]:
                i += 1
            else:
                j += 1
        return False

    def contains(self, cp: int) -> bool:
        rs = self.ranges
        lo, hi = 0, len(rs) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if cp < rs[mid][0]:
                hi = mid - 1
            elif cp > rs[mid][1]:
                lo = mid + 1
            else:
                return True
        return False

    def is_empty(self) -> bool:
        return not self.ranges

    def size(self) -> int:
        return sum(hi - lo + 1 for lo, hi in self.ranges)

    def case_fold(self) -> "CharSet":
        """Adds ASCII upper/lower counterparts (used for the /i flag)."""
        extra: List[Range] = []
        for lo, hi in self.ranges:
            for base, other in ((97, 65), (65, 97)):
                a, b = max(lo, base), min(hi, base + 25)
                if a <= b:
                    extra.append((a - base + other, b - base + other))
        return CharSet(self.ranges + tuple(extra))

    # ---- presentation -------------------------------------------------
    _PREFERRED = (
        list(range(ord("a"), ord("z") + 1))
        + list(range(ord("0"), ord("9") + 1))
        + list(range(ord("A"), ord("Z") + 1))
        + [ord(c) for c in "_-.@!#$%&*+/=?^~ "]
        + list(range(33, 127))
    )

    def sample(self) -> str:
        """A readable representative character of this set."""
        for cp in self._PREFERRED:
            if self.contains(cp):
                return chr(cp)
        if not self.ranges:
            raise ValueError("empty charset has no sample")
        return chr(self.ranges[0][0])

    def to_display(self) -> str:
        if not self.ranges:
            return "∅"
        if self.ranges == ((0, MAX_CP),):
            return "Σ"
        if self.ranges == DOT.ranges:
            return "."
        for name, cs in NAMED_DISPLAY:
            if self.ranges == cs.ranges:
                return name
        if len(self.ranges) == 1 and self.ranges[0][0] == self.ranges[0][1]:
            return _show_cp(self.ranges[0][0])
        neg = self.negate()
        if self.size() > neg.size() and len(neg.ranges) <= 4:
            return "[^" + _ranges_text(neg.ranges) + "]"
        if len(self.ranges) > 6:
            return "[" + _ranges_text(self.ranges[:6]) + "…]"
        return "[" + _ranges_text(self.ranges) + "]"

    def to_json(self) -> List[List[int]]:
        return [[lo, hi] for lo, hi in self.ranges]

    # ---- dunder -------------------------------------------------------
    def __eq__(self, other: object) -> bool:
        return isinstance(other, CharSet) and self.ranges == other.ranges

    def __hash__(self) -> int:
        return self._hash

    def __repr__(self) -> str:
        return f"CharSet({self.to_display()})"


def _show_cp(cp: int) -> str:
    specials = {9: "\\t", 10: "\\n", 13: "\\r", 32: "␠"}
    if cp in specials:
        return specials[cp]
    if 33 <= cp < 127:
        return chr(cp)
    return f"\\u{cp:04x}"


def _ranges_text(ranges: Sequence[Range]) -> str:
    parts = []
    for lo, hi in ranges:
        if lo == hi:
            parts.append(_show_cp(lo))
        elif hi == lo + 1:
            parts.append(_show_cp(lo) + _show_cp(hi))
        else:
            parts.append(_show_cp(lo) + "-" + _show_cp(hi))
    return "".join(parts)


DIGIT = CharSet([(48, 57)])
WORD = CharSet([(48, 57), (65, 90), (95, 95), (97, 122)])
SPACE = CharSet([(9, 13), (32, 32)])
DOT = CharSet.of("\n").negate()
NAMED_DISPLAY = [("\\d", DIGIT), ("\\w", WORD), ("\\s", SPACE),
                 ("\\D", DIGIT.negate()), ("\\W", WORD.negate()), ("\\S", SPACE.negate())]


class Partition:
    """
    Minimal partition of the code-point space induced by a family of CharSets
    (the classic 'minterm' / alphabet-compression step used by DFA compilers
    such as RE2 and lex).
    """

    def __init__(self, sets: Iterable[CharSet]):
        sets = list(dict.fromkeys(sets))  # unique, stable
        points = {0, MAX_CP + 1}
        for s in sets:
            for lo, hi in s.ranges:
                points.add(lo)
                points.add(hi + 1)
        pts = sorted(points)
        sig_to_class = {}
        class_ranges: List[List[Range]] = []
        self.starts: List[int] = []
        self.class_of_interval: List[int] = []
        for i in range(len(pts) - 1):
            lo, hi = pts[i], pts[i + 1] - 1
            sig = tuple(s.contains(lo) for s in sets)
            if sig not in sig_to_class:
                sig_to_class[sig] = len(class_ranges)
                class_ranges.append([])
            c = sig_to_class[sig]
            class_ranges[c].append((lo, hi))
            if self.class_of_interval and self.class_of_interval[-1] == c:
                continue  # merge adjacent interval with same class
            self.starts.append(lo)
            self.class_of_interval.append(c)
        self.classes: List[CharSet] = [CharSet(r) for r in class_ranges]

    def __len__(self) -> int:
        return len(self.classes)

    def classify(self, cp: int) -> int:
        return self.class_of_interval[bisect_right(self.starts, cp) - 1]

    def classes_in(self, cs: CharSet) -> List[int]:
        return [i for i, c in enumerate(self.classes) if c.ranges and cs.contains(c.ranges[0][0])]

    def to_json(self):
        return {
            "classes": [{"index": i, "display": c.to_display(), "ranges": c.to_json()}
                        for i, c in enumerate(self.classes)],
            "starts": self.starts,
            "class_of_interval": self.class_of_interval,
        }
