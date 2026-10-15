"""Source-aware diagnostics shared by every compiler phase."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Dict, List


class SourceMap:
    def __init__(self, text: str):
        self.text = text
        self.line_starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self.line_starts.append(i + 1)

    def line_col(self, offset: int):
        line = bisect_right(self.line_starts, offset) - 1
        return line + 1, offset - self.line_starts[line] + 1

    def line_text(self, line: int) -> str:
        start = self.line_starts[line - 1]
        end = self.line_starts[line] - 1 if line < len(self.line_starts) else len(self.text)
        return self.text[start:end]


@dataclass
class Diagnostic:
    code: str
    severity: str      # error | warning | info
    message: str
    start: int
    end: int
    phase: str
    hint: str = ""
    related: List[Dict] = field(default_factory=list)

    def to_json(self, sm: SourceMap) -> Dict:
        l1, c1 = sm.line_col(self.start)
        l2, c2 = sm.line_col(max(self.start, self.end))
        return {"code": self.code, "severity": self.severity, "message": self.message,
                "phase": self.phase, "hint": self.hint,
                "span": {"start": self.start, "end": self.end, "line": l1, "col": c1,
                         "end_line": l2, "end_col": c2},
                "related": self.related}

    def render(self, sm: SourceMap) -> str:
        l, c = sm.line_col(self.start)
        text = sm.line_text(l)
        width = max(1, min(self.end, sm.line_starts[l] - 1 if l < len(sm.line_starts) else len(sm.text)) - self.start)
        out = f"{self.severity}[{self.code}] line {l}:{c}: {self.message}\n    {text}\n    {' ' * (c - 1)}{'^' * width}"
        if self.hint:
            out += f"\n    hint: {self.hint}"
        return out


class DiagnosticBag:
    def __init__(self):
        self.items: List[Diagnostic] = []

    def add(self, code, severity, message, start, end, phase, hint="", related=None):
        self.items.append(Diagnostic(code, severity, message, start, end, phase, hint, related or []))

    def error(self, code, message, start, end, phase, hint=""):
        self.add(code, "error", message, start, end, phase, hint)

    def warning(self, code, message, start, end, phase, hint=""):
        self.add(code, "warning", message, start, end, phase, hint)

    def info(self, code, message, start, end, phase, hint=""):
        self.add(code, "info", message, start, end, phase, hint)

    @property
    def errors(self) -> List[Diagnostic]:
        return [d for d in self.items if d.severity == "error"]

    def has_errors(self) -> bool:
        return bool(self.errors)
