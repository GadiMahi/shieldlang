"""ShieldLang abstract syntax tree."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union


@dataclass
class Node:
    start: int
    end: int

    def to_json(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"node": type(self).__name__, "span": [self.start, self.end]}
        for k, v in self.__dict__.items():
            if k in ("start", "end"):
                continue
            d[k] = _j(v)
        return d


def _j(v):
    if isinstance(v, Node):
        return v.to_json()
    if isinstance(v, list):
        return [_j(x) for x in v]
    if isinstance(v, tuple):
        return [_j(x) for x in v]
    return v


# ---- expressions ----------------------------------------------------------
@dataclass
class Num(Node):
    value: Union[int, float]
    unit: str = ""


@dataclass
class Str(Node):
    value: str


@dataclass
class Bool(Node):
    value: bool


@dataclass
class Ref(Node):
    name: str


@dataclass
class Exists(Node):
    name: str


@dataclass
class Unary(Node):
    op: str
    operand: "Expr"


@dataclass
class Binary(Node):
    op: str
    left: "Expr"
    right: "Expr"


Expr = Union[Num, Str, Bool, Ref, Exists, Unary, Binary]


# ---- regex ----------------------------------------------------------------
@dataclass
class RegexLit(Node):
    pattern: str
    flags: str
    rid: str = ""          # assigned by semantic analysis

    @property
    def inner_start(self) -> int:
        return self.start + 1


@dataclass
class PatternRef(Node):
    name: str
    rid: str = ""


PatternExpr = Union[RegexLit, PatternRef]


# ---- declarations ---------------------------------------------------------
@dataclass
class TypeNode(Node):
    name: str                      # string | int | number | bool | list | enum
    elem: Optional["TypeNode"] = None
    values: List[str] = field(default_factory=list)

    def display(self) -> str:
        if self.name == "list":
            return f"list<{self.elem.display() if self.elem else '?'}>"
        if self.name == "enum":
            return "enum(" + ", ".join(f'"{v}"' for v in self.values) + ")"
        return self.name


@dataclass
class Constraint(Node):
    kind: str                      # matches | minlen | maxlen | range | minitems | maxitems
    args: List[Any] = field(default_factory=list)


@dataclass
class FieldDecl(Node):
    source: str                    # header | query | field | param
    name: str
    name_start: int
    name_end: int
    optional: bool
    type: TypeNode
    constraints: List[Constraint] = field(default_factory=list)


@dataclass
class WhenStmt(Node):
    cond: Expr
    action: str                    # require | forbid
    targets: List[Ref] = field(default_factory=list)


@dataclass
class BlockStmt(Node):
    scope: str                     # any | field | header | path | body
    target: Optional[str]
    target_start: int
    target_end: int
    pattern: PatternExpr
    label: str = ""


@dataclass
class BodyLimit(Node):
    kind: str                      # max_depth | max_size | max_keys
    value: Expr


@dataclass
class BodyStmt(Node):
    limits: List[BodyLimit] = field(default_factory=list)


Stmt = Union[FieldDecl, WhenStmt, BlockStmt, BodyStmt]


@dataclass
class Rule(Node):
    name: str
    name_start: int
    name_end: int
    method: str
    path: str
    path_start: int
    path_end: int
    body: List[Stmt] = field(default_factory=list)


@dataclass
class ConstDecl(Node):
    name: str
    name_start: int
    name_end: int
    value: Expr


@dataclass
class PatternDecl(Node):
    name: str
    name_start: int
    name_end: int
    regex: RegexLit


@dataclass
class Program(Node):
    items: List[Union[Rule, ConstDecl, PatternDecl]] = field(default_factory=list)

    @property
    def rules(self) -> List[Rule]:
        return [i for i in self.items if isinstance(i, Rule)]
