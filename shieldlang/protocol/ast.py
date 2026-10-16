"""Protocol DSL abstract syntax. Reuses the ShieldLang Node base (spans + JSON)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..lang.ast import Node


@dataclass
class Name(Node):
    name: str


@dataclass
class ResourceStmt(Node):
    label: str


@dataclass
class StatesStmt(Node):
    names: List[Name]


@dataclass
class InitialStmt(Node):
    name: Name


@dataclass
class TerminalStmt(Node):
    names: List[Name]


@dataclass
class KeyStmt(Node):
    name: Name


@dataclass
class CreateStmt(Node):
    rule: Name
    id_field: str
    ref_field: Optional[Name]
    probe_path: Optional[str]


@dataclass
class StepStmt(Node):
    rule: Name
    frm: Name
    to: Name


@dataclass
class BindStmt(Node):
    a_event: Name
    a_field: Name
    b_event: Name
    b_field: Name


@dataclass
class OutcomeStmt(Node):
    cls: str                       # success | failure
    ranges: List[Tuple[int, int]]


@dataclass
class ContractStmt(Node):
    text: str


@dataclass
class QuiesceStmt(Node):
    seconds: float


@dataclass
class ReconcileStmt(Node):
    path: str
    state_field: str
    owner_field: str
    mapping: List[Tuple[str, Name]]


@dataclass
class Protocol(Node):
    name: str
    name_start: int
    name_end: int
    stmts: list = field(default_factory=list)
