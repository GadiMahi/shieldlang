"""
Typed regex AST (RegexShield v2).

Extends the Review 1 node set (Literal, Concat, Alternation, Group, Star,
Plus, Question, AnchorStart, AnchorEnd) with character classes, the dot and
bounded repetition {m,n}. Every node keeps its exact [start, end) span in the
pattern source and receives a deterministic pre-order id (node_0, node_1, ...).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from .charset import CharSet


@dataclass(eq=False)
class RNode:
    start: int
    end: int
    node_id: str = field(default="", init=False)

    node_type = "Node"

    @property
    def children(self) -> List["RNode"]:
        return []

    def label(self) -> str:
        return self.node_type

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.node_id,
            "type": self.node_type,
            "label": self.label(),
            "span": {"start": self.start, "end": self.end},
            "children": [c.to_dict() for c in self.children],
        }

    def walk(self) -> Iterator["RNode"]:
        yield self
        for c in self.children:
            yield from c.walk()


@dataclass(eq=False)
class CharNode(RNode):
    """A single-character position: literal, escape, class or dot."""
    charset: CharSet = field(default_factory=CharSet.empty)
    text: str = ""
    kind: str = "Literal"  # Literal | CharClass | Dot | Escape

    @property
    def node_type(self) -> str:  # type: ignore[override]
        return self.kind

    def label(self) -> str:
        return f"{self.kind} {self.text}"


@dataclass(eq=False)
class Concat(RNode):
    items: List[RNode] = field(default_factory=list)
    node_type = "Concat"

    @property
    def children(self) -> List[RNode]:
        return self.items

    def label(self) -> str:
        return "Concat" if self.items else "Empty"


@dataclass(eq=False)
class Alternation(RNode):
    branches: List[RNode] = field(default_factory=list)
    node_type = "Alternation"

    @property
    def children(self) -> List[RNode]:
        return self.branches


@dataclass(eq=False)
class Group(RNode):
    child: Optional[RNode] = None
    capturing: bool = True
    index: int = 0
    node_type = "Group"

    @property
    def children(self) -> List[RNode]:
        return [self.child] if self.child else []

    def label(self) -> str:
        return f"Group #{self.index}" if self.capturing else "Group (?:)"


@dataclass(eq=False)
class Repeat(RNode):
    child: Optional[RNode] = None
    min: int = 0
    max: Optional[int] = None  # None = unbounded
    lazy: bool = False

    @property
    def node_type(self) -> str:  # type: ignore[override]
        if self.min == 0 and self.max is None:
            return "Star"
        if self.min == 1 and self.max is None:
            return "Plus"
        if self.min == 0 and self.max == 1:
            return "Question"
        return "Repeat"

    @property
    def unbounded(self) -> bool:
        return self.max is None

    @property
    def children(self) -> List[RNode]:
        return [self.child] if self.child else []

    def quant_text(self) -> str:
        t = {"Star": "*", "Plus": "+", "Question": "?"}.get(self.node_type)
        if t is None:
            if self.max is None:
                t = f"{{{self.min},}}"
            elif self.max == self.min:
                t = f"{{{self.min}}}"
            else:
                t = f"{{{self.min},{self.max}}}"
        return t + ("?" if self.lazy else "")

    def label(self) -> str:
        return f"{self.node_type} {self.quant_text()}"


@dataclass(eq=False)
class AnchorStart(RNode):
    node_type = "AnchorStart"

    def label(self) -> str:
        return "AnchorStart ^"


@dataclass(eq=False)
class AnchorEnd(RNode):
    node_type = "AnchorEnd"

    def label(self) -> str:
        return "AnchorEnd $"


def assign_ids(root: RNode, prefix: str = "node") -> RNode:
    for i, n in enumerate(root.walk()):
        n.node_id = f"{prefix}_{i}"
    return root


def parent_map(root: RNode) -> Dict[str, Optional[RNode]]:
    parents: Dict[str, Optional[RNode]] = {root.node_id: None}
    for n in root.walk():
        for c in n.children:
            parents[c.node_id] = n
    return parents


def to_source(node: RNode) -> str:
    """Pretty-prints an AST back to regex source (used by the rewriter)."""
    if isinstance(node, CharNode):
        return node.text
    if isinstance(node, Concat):
        return "".join(to_source(i) for i in node.items)
    if isinstance(node, Alternation):
        return "|".join(to_source(b) for b in node.branches)
    if isinstance(node, Group):
        inner = to_source(node.child) if node.child else ""
        return ("(" if node.capturing else "(?:") + inner + ")"
    if isinstance(node, Repeat):
        child = node.child
        inner = to_source(child) if child else ""
        if not isinstance(child, (CharNode, Group)):
            inner = "(?:" + inner + ")"
        return inner + node.quant_text()
    if isinstance(node, AnchorStart):
        return "^"
    if isinstance(node, AnchorEnd):
        return "$"
    raise TypeError(node)
