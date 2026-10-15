"""
RegexShield v2 lexer and recursive-descent parser.

Grammar (extends Review 1):

    Regex       -> Alternation
    Alternation -> Concat ('|' Concat)*
    Concat      -> Repeat*
    Repeat      -> Atom Quantifier?
    Quantifier  -> ('*' | '+' | '?' | '{' n '}' | '{' n ',' '}' | '{' n ',' m '}') '?'?
    Atom        -> CHAR | CLASS | '.' | '(' Regex ')' | '(?:' Regex ')' | '^' | '$'

Supported escapes: \\d \\D \\w \\W \\s \\S \\n \\t \\r \\f \\v \\xHH \\uHHHH and
escaped metacharacters. Non-regular features (back-references, look-around,
word boundaries) are rejected with a precise diagnostic, because the analysis
and the generated linear-time matchers are only sound for regular languages.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .ast import (AnchorEnd, AnchorStart, Alternation, CharNode, Concat, Group,
                  Repeat, RNode, assign_ids)
from .charset import DIGIT, DOT, SPACE, WORD, CharSet

MAX_BOUNDED_REPEAT = 100


class RegexSyntaxError(Exception):
    def __init__(self, message: str, position: int, end: Optional[int] = None,
                 stage: str = "REGEX_PARSER", hint: str = ""):
        super().__init__(f"{message} (at position {position})")
        self.message = message
        self.position = position
        self.end = end if end is not None else position + 1
        self.stage = stage
        self.hint = hint

    def to_dict(self):
        return {"stage": self.stage, "message": self.message,
                "position": self.position, "end": self.end, "hint": self.hint}


@dataclass
class RToken:
    kind: str
    value: str
    start: int
    end: int
    charset: Optional[CharSet] = None
    rmin: int = 0
    rmax: Optional[int] = None
    char_kind: str = "Literal"

    def to_dict(self):
        d = {"type": self.kind, "value": self.value, "start": self.start, "end": self.end}
        if self.charset is not None:
            d["charset"] = self.charset.to_display()
        if self.kind == "REPEAT":
            d["min"], d["max"] = self.rmin, self.rmax
        return d


_SIMPLE_ESC = {"n": "\n", "t": "\t", "r": "\r", "f": "\f", "v": "\v", "0": "\0"}
_CLASS_ESC = {"d": DIGIT, "D": DIGIT.negate(), "w": WORD, "W": WORD.negate(),
              "s": SPACE, "S": SPACE.negate()}
_META = {"(": "LPAREN", ")": "RPAREN", "|": "OR", "*": "STAR", "+": "PLUS",
         "?": "QUESTION", "^": "CARET", "$": "DOLLAR"}


class RegexLexer:
    def __init__(self, source: str, ignore_case: bool = False):
        self.src = source
        self.pos = 0
        self.ic = ignore_case

    def _fold(self, cs: CharSet) -> CharSet:
        return cs.case_fold() if self.ic else cs

    def _escape(self, in_class: bool) -> Tuple[CharSet, str, bool]:
        """Reads an escape after the backslash. Returns (charset, text, is_class)."""
        start = self.pos - 1
        if self.pos >= len(self.src):
            raise RegexSyntaxError("Dangling backslash at end of pattern", start, stage="REGEX_LEXER")
        c = self.src[self.pos]
        self.pos += 1
        if c in _CLASS_ESC:
            return _CLASS_ESC[c], "\\" + c, True
        if c in _SIMPLE_ESC:
            return CharSet.of(_SIMPLE_ESC[c]), "\\" + c, False
        if c in "xu":
            n = 2 if c == "x" else 4
            hexs = self.src[self.pos:self.pos + n]
            if len(hexs) != n or any(h not in "0123456789abcdefABCDEF" for h in hexs):
                raise RegexSyntaxError(f"Invalid \\{c} escape: expected {n} hex digits", start,
                                       self.pos, stage="REGEX_LEXER")
            self.pos += n
            return CharSet.of(chr(int(hexs, 16))), "\\" + c + hexs, False
        if c.isdigit():
            raise RegexSyntaxError("Back-references are not regular and are not supported", start,
                                   self.pos, stage="REGEX_LEXER",
                                   hint="Back-references make matching NP-hard; restructure the rule instead.")
        if c in "bB":
            raise RegexSyntaxError("Word boundaries (\\b, \\B) are not supported", start, self.pos,
                                   stage="REGEX_LEXER", hint="Use explicit character classes instead.")
        if c.isalpha():
            raise RegexSyntaxError(f"Unknown escape sequence '\\{c}'", start, self.pos, stage="REGEX_LEXER")
        return CharSet.of(c), "\\" + c, False

    def _class(self) -> RToken:
        start = self.pos
        self.pos += 1  # '['
        negate = False
        if self.pos < len(self.src) and self.src[self.pos] == "^":
            negate = True
            self.pos += 1
        cs = CharSet.empty()
        first = True
        while True:
            if self.pos >= len(self.src):
                raise RegexSyntaxError("Unterminated character class", start, self.pos,
                                       stage="REGEX_LEXER", hint="Add a closing ']'.")
            c = self.src[self.pos]
            if c == "]" and not first:
                self.pos += 1
                break
            first = False
            # read one item
            if c == "\\":
                self.pos += 1
                item, _, is_cls = self._escape(True)
            else:
                self.pos += 1
                item, is_cls = CharSet.of(c), False
            # range?
            if (not is_cls and self.pos + 1 < len(self.src) and self.src[self.pos] == "-"
                    and self.src[self.pos + 1] != "]"):
                self.pos += 1
                d = self.src[self.pos]
                if d == "\\":
                    self.pos += 1
                    hi_cs, _, hi_cls = self._escape(True)
                    if hi_cls:
                        raise RegexSyntaxError("Invalid range end in character class", self.pos - 2,
                                               self.pos, stage="REGEX_LEXER")
                else:
                    self.pos += 1
                    hi_cs = CharSet.of(d)
                lo_cp, hi_cp = item.ranges[0][0], hi_cs.ranges[0][0]
                if lo_cp > hi_cp:
                    raise RegexSyntaxError(f"Invalid range {chr(lo_cp)}-{chr(hi_cp)}: start is after end",
                                           start, self.pos, stage="REGEX_LEXER")
                item = CharSet([(lo_cp, hi_cp)])
            cs = cs.union(item)
        cs = self._fold(cs)
        if negate:
            cs = cs.negate()
        if cs.is_empty():
            raise RegexSyntaxError("Character class matches nothing", start, self.pos, stage="REGEX_LEXER")
        return RToken("CHAR", self.src[start:self.pos], start, self.pos, cs, char_kind="CharClass")

    def _brace(self) -> Optional[RToken]:
        """Parses {n}, {n,}, {n,m}. Returns None if '{' is a literal."""
        start = self.pos
        j = self.pos + 1
        num1 = ""
        while j < len(self.src) and self.src[j].isdigit():
            num1 += self.src[j]
            j += 1
        if not num1:
            return None
        rmax: Optional[int]
        if j < len(self.src) and self.src[j] == "}":
            rmin = rmax = int(num1)
            j += 1
        elif j < len(self.src) and self.src[j] == ",":
            j += 1
            num2 = ""
            while j < len(self.src) and self.src[j].isdigit():
                num2 += self.src[j]
                j += 1
            if j >= len(self.src) or self.src[j] != "}":
                return None
            j += 1
            rmin = int(num1)
            rmax = int(num2) if num2 else None
        else:
            return None
        if rmax is not None and rmax < rmin:
            raise RegexSyntaxError(f"Invalid repetition {{{rmin},{rmax}}}: min is greater than max",
                                   start, j, stage="REGEX_LEXER")
        if rmin > MAX_BOUNDED_REPEAT or (rmax is not None and rmax > MAX_BOUNDED_REPEAT):
            raise RegexSyntaxError(f"Bounded repetition larger than {MAX_BOUNDED_REPEAT} is not supported",
                                   start, j, stage="REGEX_LEXER",
                                   hint="Use a field-level 'maxlen' constraint for large limits.")
        self.pos = j
        return RToken("REPEAT", self.src[start:j], start, j, rmin=rmin, rmax=rmax)

    def tokenize(self) -> List[RToken]:
        toks: List[RToken] = []
        while self.pos < len(self.src):
            c = self.src[self.pos]
            start = self.pos
            if c == "\\":
                self.pos += 1
                cs, text, is_cls = self._escape(False)
                toks.append(RToken("CHAR", text, start, self.pos, self._fold(cs),
                                   char_kind="CharClass" if is_cls else "Escape"))
            elif c == "[":
                toks.append(self._class())
            elif c == ".":
                self.pos += 1
                toks.append(RToken("CHAR", ".", start, self.pos, DOT, char_kind="Dot"))
            elif c == "(" and self.src.startswith("(?", self.pos):
                if self.src.startswith("(?:", self.pos):
                    self.pos += 3
                    toks.append(RToken("NCGROUP", "(?:", start, self.pos))
                else:
                    what = "Look-around assertions" if self.src[self.pos + 2:self.pos + 3] in ("=", "!", "<") \
                        else "Inline group modifiers"
                    raise RegexSyntaxError(f"{what} are not supported", start, start + 3,
                                           stage="REGEX_LEXER",
                                           hint="Look-around is not regular; express the condition as a separate ShieldLang constraint.")
            elif c == "{":
                tok = self._brace()
                if tok is None:
                    self.pos += 1
                    toks.append(RToken("CHAR", "{", start, self.pos, self._fold(CharSet.of("{"))))
                else:
                    toks.append(tok)
            elif c in _META:
                self.pos += 1
                toks.append(RToken(_META[c], c, start, self.pos))
            else:
                self.pos += 1
                toks.append(RToken("CHAR", c, start, self.pos, self._fold(CharSet.of(c))))
        toks.append(RToken("EOF", "", len(self.src), len(self.src)))
        return toks


class RegexParser:
    QUANTS = ("STAR", "PLUS", "QUESTION", "REPEAT")

    def __init__(self, tokens: List[RToken]):
        self.toks = tokens
        self.i = 0
        self.groups = 0

    @property
    def cur(self) -> RToken:
        return self.toks[self.i]

    def parse(self) -> RNode:
        node = self._alternation()
        if self.cur.kind != "EOF":
            t = self.cur
            if t.kind == "RPAREN":
                raise RegexSyntaxError("Unbalanced ')' with no matching '('", t.start, t.end)
            raise RegexSyntaxError(f"Unexpected '{t.value}'", t.start, t.end)
        assign_ids(node)
        return node

    def _alternation(self) -> RNode:
        start = self.cur.start
        branches = [self._concat()]
        while self.cur.kind == "OR":
            self.i += 1
            branches.append(self._concat())
        if len(branches) == 1:
            return branches[0]
        return Alternation(start, branches[-1].end, branches=branches)

    def _concat(self) -> RNode:
        start = self.cur.start
        items: List[RNode] = []
        while self.cur.kind not in ("OR", "RPAREN", "EOF"):
            items.append(self._repeat())
        if len(items) == 1:
            return items[0]
        return Concat(start, items[-1].end if items else start, items=items)

    def _repeat(self) -> RNode:
        t = self.cur
        if t.kind in self.QUANTS:
            raise RegexSyntaxError(f"Quantifier '{t.value}' has nothing to repeat", t.start, t.end)
        atom = self._atom()
        if self.cur.kind in self.QUANTS:
            q = self.cur
            self.i += 1
            if isinstance(atom, (AnchorStart, AnchorEnd)):
                raise RegexSyntaxError("Anchors cannot be repeated", q.start, q.end)
            rmin, rmax = {"STAR": (0, None), "PLUS": (1, None), "QUESTION": (0, 1)}.get(q.kind, (q.rmin, q.rmax))
            lazy = False
            end = q.end
            if self.cur.kind == "QUESTION":
                lazy = True
                end = self.cur.end
                self.i += 1
            if self.cur.kind in self.QUANTS:
                n = self.cur
                raise RegexSyntaxError(f"Multiple repeat: '{n.value}' follows another quantifier",
                                       n.start, n.end, hint="Wrap the inner expression in a group if this is intended.")
            return Repeat(atom.start, end, child=atom, min=rmin, max=rmax, lazy=lazy)
        return atom

    def _atom(self) -> RNode:
        t = self.cur
        if t.kind == "CHAR":
            self.i += 1
            return CharNode(t.start, t.end, charset=t.charset, text=t.value, kind=t.char_kind)
        if t.kind == "CARET":
            self.i += 1
            return AnchorStart(t.start, t.end)
        if t.kind == "DOLLAR":
            self.i += 1
            return AnchorEnd(t.start, t.end)
        if t.kind in ("LPAREN", "NCGROUP"):
            self.i += 1
            capturing = t.kind == "LPAREN"
            idx = 0
            if capturing:
                self.groups += 1
                idx = self.groups
            inner = self._alternation()
            if self.cur.kind != "RPAREN":
                raise RegexSyntaxError(f"Unclosed group: '(' at position {t.start} is never closed",
                                       self.cur.start, self.cur.end, hint="Add a closing ')'.")
            close = self.cur
            self.i += 1
            return Group(t.start, close.end, child=inner, capturing=capturing, index=idx)
        if t.kind == "RPAREN":
            raise RegexSyntaxError("Unbalanced ')' with no matching '('", t.start, t.end)
        raise RegexSyntaxError(f"Unexpected '{t.value or 'end of pattern'}'", t.start, t.end)


def parse_regex(source: str, ignore_case: bool = False) -> Tuple[List[RToken], RNode]:
    tokens = RegexLexer(source, ignore_case).tokenize()
    return tokens, RegexParser(tokens).parse()
