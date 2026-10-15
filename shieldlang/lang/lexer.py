"""
ShieldLang lexical analyser.

Hand-written maximal-munch scanner producing typed tokens with exact source
offsets. Regex literals /.../flags are recognised with the classic
JavaScript rule: '/' starts a regex unless the previous significant token can
end an operand (number, identifier, ')' or a size unit), in which case it is
the division operator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from ..diagnostics import DiagnosticBag

KEYWORDS = {
    "rule", "on", "const", "pattern", "header", "query", "field", "param", "body",
    "when", "require", "forbid", "block", "if", "any", "matches", "minlen", "maxlen",
    "in", "maxitems", "minitems", "max_depth", "max_size", "max_keys", "as",
    "string", "int", "number", "bool", "list", "enum", "true", "false", "exists",
    "and", "or", "not", "path",
}
UNITS = {"B": 1, "KB": 1024, "MB": 1024 * 1024}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY"}

PUNCT = [
    ("..", "DOTDOT"), ("==", "EQ"), ("!=", "NE"), ("<=", "LE"), (">=", "GE"),
    ("&&", "AND"), ("||", "OR"),
    ("{", "LBRACE"), ("}", "RBRACE"), ("(", "LPAREN"), (")", "RPAREN"),
    ("<", "LT"), (">", "GT"), (";", "SEMI"), (":", "COLON"), (",", "COMMA"),
    ("?", "QMARK"), ("!", "NOT"), ("=", "ASSIGN"), ("+", "PLUS"), ("-", "MINUS"),
    ("*", "STAR"), ("/", "SLASH"), (".", "DOT"),
]


@dataclass
class Token:
    kind: str     # KEYWORD kinds are upper-case keyword text, e.g. RULE
    value: object
    start: int
    end: int
    text: str = ""

    def to_json(self):
        v = self.value
        if isinstance(v, tuple):
            v = {"pattern": v[0], "flags": v[1]}
        return {"kind": self.kind, "value": v, "start": self.start, "end": self.end, "text": self.text}


class Lexer:
    def __init__(self, src: str, diags: DiagnosticBag):
        self.src = src
        self.pos = 0
        self.diags = diags
        self.tokens: List[Token] = []

    def _prev_ends_operand(self) -> bool:
        if not self.tokens:
            return False
        t = self.tokens[-1]
        return t.kind in ("NUMBER", "IDENT", "RPAREN", "STRING") or (t.kind == "IDENT" and t.value in UNITS)

    def _add(self, kind, value, start):
        self.tokens.append(Token(kind, value, start, self.pos, self.src[start:self.pos]))

    def tokenize(self) -> List[Token]:
        src = self.src
        n = len(src)
        while self.pos < n:
            c = src[self.pos]
            start = self.pos
            if c in " \t\r\n":
                self.pos += 1
                continue
            if src.startswith("//", self.pos) or c == "#":
                while self.pos < n and src[self.pos] != "\n":
                    self.pos += 1
                continue
            if src.startswith("/*", self.pos):
                end = src.find("*/", self.pos + 2)
                if end < 0:
                    self.diags.error("L005", "Unterminated block comment", start, n, "lexer", "Close it with */")
                    self.pos = n
                else:
                    self.pos = end + 2
                continue
            if c.isalpha() or c == "_":
                while self.pos < n and (src[self.pos].isalnum() or src[self.pos] == "_"):
                    self.pos += 1
                word = src[start:self.pos]
                if word in KEYWORDS:
                    self._add("KW_" + word.upper(), word, start)
                else:
                    self._add("IDENT", word, start)
                continue
            if c.isdigit():
                while self.pos < n and (src[self.pos].isdigit() or src[self.pos] == "_"):
                    self.pos += 1
                is_float = False
                if self.pos + 1 < n and src[self.pos] == "." and src[self.pos + 1].isdigit():
                    is_float = True
                    self.pos += 1
                    while self.pos < n and src[self.pos].isdigit():
                        self.pos += 1
                text = src[start:self.pos].replace("_", "")
                self._add("NUMBER", float(text) if is_float else int(text), start)
                continue
            if c == '"':
                self.pos += 1
                buf = []
                closed = False
                while self.pos < n:
                    ch = src[self.pos]
                    if ch == "\\" and self.pos + 1 < n:
                        esc = src[self.pos + 1]
                        buf.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(esc, esc))
                        self.pos += 2
                        continue
                    if ch == '"':
                        self.pos += 1
                        closed = True
                        break
                    if ch == "\n":
                        break
                    buf.append(ch)
                    self.pos += 1
                if not closed:
                    self.diags.error("L002", "Unterminated string literal", start, self.pos, "lexer",
                                     'Add a closing double quote (").')
                self._add("STRING", "".join(buf), start)
                continue
            if c == "/" and not self._prev_ends_operand():
                self.pos += 1
                in_class = False
                closed = False
                while self.pos < n:
                    ch = src[self.pos]
                    if ch == "\\" and self.pos + 1 < n:
                        self.pos += 2
                        continue
                    if ch == "\n":
                        break
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        closed = True
                        break
                    self.pos += 1
                body = src[start + 1:self.pos]
                if not closed:
                    self.diags.error("L003", "Unterminated regex literal", start, self.pos, "lexer",
                                     "Close the pattern with '/'.")
                else:
                    self.pos += 1
                fstart = self.pos
                while self.pos < n and src[self.pos].isalpha():
                    self.pos += 1
                flags = src[fstart:self.pos]
                for f in flags:
                    if f != "i":
                        self.diags.error("L004", f"Unsupported regex flag '{f}'", fstart, self.pos, "lexer",
                                         "Only the 'i' (case-insensitive) flag is supported.")
                self._add("REGEX", (body, "".join(ch for ch in flags if ch == "i")), start)
                continue
            for lit, kind in PUNCT:
                if src.startswith(lit, self.pos):
                    self.pos += len(lit)
                    self._add(kind, lit, start)
                    break
            else:
                self.pos += 1
                self.diags.error("L001", f"Unexpected character '{c}'", start, self.pos, "lexer")
        self.tokens.append(Token("EOF", None, n, n, ""))
        return self.tokens
