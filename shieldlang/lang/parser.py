"""
ShieldLang recursive-descent parser with panic-mode error recovery.

    program    → item*
    item       → const | pattern | rule
    const      → 'const' IDENT '=' expr ';'
    pattern    → 'pattern' IDENT '=' REGEX ';'
    rule       → 'rule' IDENT 'on' METHOD STRING '{' stmt* '}'
    stmt       → fieldStmt | whenStmt | blockStmt | bodyStmt
    fieldStmt  → ('header'|'query'|'field'|'param') name '?'? ':' type constraint* ';'
    type       → 'string' | 'int' | 'number' | 'bool' | 'list' '<' type '>'
               | 'enum' '(' STRING (',' STRING)* ')'
    constraint → 'matches' pat | 'minlen' expr | 'maxlen' expr | 'in' expr '..' expr
               | 'minitems' expr | 'maxitems' expr
    whenStmt   → 'when' expr ('require'|'forbid') name (',' name)* ';'
    blockStmt  → 'block' 'if' scope 'matches' pat ('as' STRING)? ';'
    scope      → 'any' 'field' | 'header' (STRING|IDENT) | 'path' | 'body' | name
    bodyStmt   → 'body' (('max_depth'|'max_size'|'max_keys') expr)+ ';'
    pat        → REGEX | IDENT
    expr       → or ;  or → and (('||'|'or') and)* ;  and → not (('&&'|'and') not)*
    not        → ('!'|'not') not | cmp ;  cmp → add (('=='|'!='|'<'|'<='|'>'|'>=') add)?
    add        → mul (('+'|'-') mul)* ;  mul → unary (('*'|'/') unary)*
    unary      → '-' unary | primary
    primary    → NUMBER UNIT? | STRING | 'true' | 'false' | 'exists' name | name | '(' expr ')'
    name       → IDENT ('.' IDENT)*

On a syntax error the parser records a diagnostic and synchronises on ';'
or '}' so that several independent errors are reported in one run.
"""
from __future__ import annotations

from typing import List, Optional

from ..diagnostics import DiagnosticBag
from . import ast as A
from .lexer import METHODS, UNITS, Token


class ParseError(Exception):
    pass


FIELD_SOURCES = {"KW_HEADER": "header", "KW_QUERY": "query", "KW_FIELD": "field", "KW_PARAM": "param"}
TYPE_KW = {"KW_STRING": "string", "KW_INT": "int", "KW_NUMBER": "number", "KW_BOOL": "bool"}
REL = {"EQ": "==", "NE": "!=", "LT": "<", "LE": "<=", "GT": ">", "GE": ">="}
PRETTY = {"SEMI": "';'", "LBRACE": "'{'", "RBRACE": "'}'", "COLON": "':'", "LPAREN": "'('", "RPAREN": "')'",
          "IDENT": "identifier", "STRING": "string literal", "REGEX": "regex literal", "NUMBER": "number",
          "DOTDOT": "'..'", "ASSIGN": "'='", "GT": "'>'", "LT": "'<'", "EOF": "end of file"}


def pretty(kind: str) -> str:
    if kind in PRETTY:
        return PRETTY[kind]
    if kind.startswith("KW_"):
        return f"'{kind[3:].lower()}'"
    return kind


class Parser:
    def __init__(self, tokens: List[Token], diags: DiagnosticBag):
        self.t = tokens
        self.i = 0
        self.d = diags

    # ---- helpers ------------------------------------------------------
    @property
    def cur(self) -> Token:
        return self.t[self.i]

    def peek(self, k=1) -> Token:
        return self.t[min(self.i + k, len(self.t) - 1)]

    def at(self, *kinds) -> bool:
        return self.cur.kind in kinds

    def advance(self) -> Token:
        tok = self.cur
        if tok.kind != "EOF":
            self.i += 1
        return tok

    def expect(self, kind: str, context: str = "") -> Token:
        if self.cur.kind == kind:
            return self.advance()
        found = self.cur
        what = f"'{found.text}'" if found.text else pretty(found.kind)
        msg = f"Expected {pretty(kind)}{' ' + context if context else ''}, found {what}"
        hint = ""
        if kind == "SEMI":
            hint = "Every statement ends with ';'."
            prev = self.t[self.i - 1] if self.i else found
            self.d.error("P001", msg, prev.end, prev.end + 1, "parser", hint)
            raise ParseError()
        self.d.error("P001", msg, found.start, max(found.end, found.start + 1), "parser", hint)
        raise ParseError()

    def sync(self, top_level: bool = False):
        while not self.at("EOF"):
            if top_level and (self.at("KW_RULE", "KW_CONST", "KW_PATTERN") or (self.at("IDENT") and self.cur.text == "protocol")):
                return
            if not top_level and self.at("RBRACE"):
                return
            if self.at("SEMI"):
                self.advance()
                return
            self.advance()

    # ---- program ------------------------------------------------------
    def parse(self) -> A.Program:
        items = []
        while not self.at("EOF"):
            start_i = self.i
            try:
                if self.at("KW_RULE"):
                    items.append(self.rule())
                elif self.at("KW_CONST"):
                    items.append(self.const())
                elif self.at("KW_PATTERN"):
                    items.append(self.pattern())
                elif self.at("IDENT") and self.cur.text == "protocol":
                    from ..protocol.parser import parse_protocol
                    items.append(parse_protocol(self))
                else:
                    tok = self.cur
                    self.d.error("P002", f"Expected 'rule', 'const', 'pattern' or 'protocol' at top level, found '{tok.text}'",
                                 tok.start, tok.end, "parser")
                    raise ParseError()
            except ParseError:
                if self.i == start_i:
                    self.advance()
                self.sync(top_level=True)
        return A.Program(0, self.cur.end, items)

    def const(self) -> A.ConstDecl:
        kw = self.advance()
        name = self.expect("IDENT", "for constant name")
        self.expect("ASSIGN")
        e = self.expr()
        end = self.expect("SEMI").end
        return A.ConstDecl(kw.start, end, name.value, name.start, name.end, e)

    def pattern(self) -> A.PatternDecl:
        kw = self.advance()
        name = self.expect("IDENT", "for pattern name")
        self.expect("ASSIGN")
        rx = self.expect("REGEX", "after 'pattern NAME ='")
        end = self.expect("SEMI").end
        return A.PatternDecl(kw.start, end, name.value, name.start, name.end,
                             A.RegexLit(rx.start, rx.end, rx.value[0], rx.value[1]))

    def rule(self) -> A.Rule:
        kw = self.advance()
        name = self.expect("IDENT", "for rule name")
        self.expect("KW_ON", "after rule name")
        m = self.cur
        if m.kind == "IDENT" and m.value.upper() in METHODS:
            self.advance()
            method = m.value.upper()
        else:
            self.d.error("P003", f"Expected an HTTP method (GET, POST, PUT, PATCH, DELETE, ANY), found '{m.text}'",
                         m.start, m.end, "parser")
            raise ParseError()
        path = self.expect("STRING", "for route path")
        self.expect("LBRACE")
        body: List[A.Node] = []
        while not self.at("RBRACE", "EOF"):
            start_i = self.i
            try:
                body.append(self.stmt())
            except ParseError:
                if self.i == start_i:
                    self.advance()
                self.sync()
        end = self.expect("RBRACE", f"to close rule '{name.value}'").end
        return A.Rule(kw.start, end, name.value, name.start, name.end, method, path.value,
                      path.start, path.end, body)

    # ---- statements ---------------------------------------------------
    def stmt(self):
        k = self.cur.kind
        if k in FIELD_SOURCES:
            return self.field_stmt()
        if k == "KW_WHEN":
            return self.when_stmt()
        if k == "KW_BLOCK":
            return self.block_stmt()
        if k == "KW_BODY":
            return self.body_stmt()
        tok = self.cur
        self.d.error("P004", f"Unknown statement starting with '{tok.text}'", tok.start, tok.end, "parser",
                     "Statements are: header, query, field, param, when, block, body.")
        raise ParseError()

    def name(self, what="name") -> A.Ref:
        first = self.expect("IDENT", f"for {what}")
        parts = [first.value]
        end = first.end
        while self.at("DOT") and self.peek().kind == "IDENT":
            self.advance()
            t = self.advance()
            parts.append(t.value)
            end = t.end
        return A.Ref(first.start, end, ".".join(parts))

    def field_stmt(self) -> A.FieldDecl:
        kw = self.advance()
        src = FIELD_SOURCES[kw.kind]
        if src == "header" and self.at("STRING"):
            t = self.advance()
            nm = A.Ref(t.start, t.end, t.value)
        else:
            nm = self.name(f"{src} name")
        optional = False
        if self.at("QMARK"):
            self.advance()
            optional = True
        self.expect("COLON", f"after {src} '{nm.name}'")
        ty = self.type_()
        cons = []
        while not self.at("SEMI", "EOF", "RBRACE"):
            cons.append(self.constraint())
        end = self.expect("SEMI").end
        return A.FieldDecl(kw.start, end, src, nm.name, nm.start, nm.end, optional, ty, cons)

    def type_(self) -> A.TypeNode:
        t = self.cur
        if t.kind in TYPE_KW:
            self.advance()
            return A.TypeNode(t.start, t.end, TYPE_KW[t.kind])
        if t.kind == "KW_LIST":
            self.advance()
            self.expect("LT", "after 'list'")
            elem = self.type_()
            gt = self.expect("GT", "to close list<...>")
            return A.TypeNode(t.start, gt.end, "list", elem)
        if t.kind == "KW_ENUM":
            self.advance()
            self.expect("LPAREN", "after 'enum'")
            vals = [self.expect("STRING", "as enum member").value]
            while self.at("COMMA"):
                self.advance()
                vals.append(self.expect("STRING", "as enum member").value)
            rp = self.expect("RPAREN", "to close enum(...)")
            return A.TypeNode(t.start, rp.end, "enum", None, vals)
        self.d.error("P005", f"Expected a type (string, int, number, bool, list<T>, enum(...)), found '{t.text}'",
                     t.start, max(t.end, t.start + 1), "parser")
        raise ParseError()

    def pat(self):
        t = self.cur
        if t.kind == "REGEX":
            self.advance()
            return A.RegexLit(t.start, t.end, t.value[0], t.value[1])
        if t.kind == "IDENT":
            self.advance()
            return A.PatternRef(t.start, t.end, t.value)
        self.d.error("P006", f"Expected a regex literal /.../ or a pattern name, found '{t.text}'",
                     t.start, max(t.end, t.start + 1), "parser")
        raise ParseError()

    def constraint(self) -> A.Constraint:
        t = self.cur
        k = t.kind
        if k == "KW_MATCHES":
            self.advance()
            p = self.pat()
            return A.Constraint(t.start, p.end, "matches", [p])
        if k in ("KW_MINLEN", "KW_MAXLEN", "KW_MINITEMS", "KW_MAXITEMS"):
            self.advance()
            e = self.expr()
            return A.Constraint(t.start, e.end, k[3:].lower(), [e])
        if k == "KW_IN":
            self.advance()
            lo = self.expr()
            self.expect("DOTDOT", "in range 'in lo..hi'")
            hi = self.expr()
            return A.Constraint(t.start, hi.end, "range", [lo, hi])
        self.d.error("P007", f"Unknown constraint '{t.text}'", t.start, max(t.end, t.start + 1), "parser",
                     "Constraints are: matches, minlen, maxlen, in a..b, minitems, maxitems.")
        raise ParseError()

    def when_stmt(self) -> A.WhenStmt:
        kw = self.advance()
        cond = self.expr()
        if self.at("KW_REQUIRE", "KW_FORBID"):
            action = self.advance().value
        else:
            t = self.cur
            self.d.error("P008", f"Expected 'require' or 'forbid' after condition, found '{t.text}'",
                         t.start, max(t.end, t.start + 1), "parser")
            raise ParseError()
        targets = [self.name("field to " + action)]
        while self.at("COMMA"):
            self.advance()
            targets.append(self.name("field to " + action))
        end = self.expect("SEMI").end
        return A.WhenStmt(kw.start, end, cond, action, targets)

    def block_stmt(self) -> A.BlockStmt:
        kw = self.advance()
        self.expect("KW_IF", "after 'block'")
        t = self.cur
        target = None
        ts, te = t.start, t.end
        if t.kind == "KW_ANY":
            self.advance()
            f = self.expect("KW_FIELD", "after 'any'")
            scope, te = "any", f.end
        elif t.kind == "KW_HEADER":
            self.advance()
            h = self.cur
            if h.kind in ("STRING", "IDENT"):
                self.advance()
                scope, target, ts, te = "header", h.value, h.start, h.end
            else:
                self.expect("STRING", "for header name")
        elif t.kind == "KW_PATH":
            self.advance()
            scope = "path"
        elif t.kind == "KW_BODY":
            self.advance()
            scope = "body"
        else:
            r = self.name("field to scan")
            scope, target, ts, te = "field", r.name, r.start, r.end
        self.expect("KW_MATCHES", "in block rule")
        p = self.pat()
        label = ""
        if self.at("KW_AS"):
            self.advance()
            label = self.expect("STRING", "as block label").value
        end = self.expect("SEMI").end
        return A.BlockStmt(kw.start, end, scope, target, ts, te, p, label)

    def body_stmt(self) -> A.BodyStmt:
        kw = self.advance()
        limits = []
        while self.at("KW_MAX_DEPTH", "KW_MAX_SIZE", "KW_MAX_KEYS"):
            t = self.advance()
            e = self.expr()
            limits.append(A.BodyLimit(t.start, e.end, t.kind[3:].lower(), e))
        if not limits:
            t = self.cur
            self.d.error("P009", "Expected 'max_depth', 'max_size' or 'max_keys' after 'body'",
                         t.start, max(t.end, t.start + 1), "parser")
            raise ParseError()
        end = self.expect("SEMI").end
        return A.BodyStmt(kw.start, end, limits)

    # ---- expressions --------------------------------------------------
    def expr(self):
        return self.or_()

    def or_(self):
        left = self.and_()
        while self.at("OR", "KW_OR"):
            self.advance()
            right = self.and_()
            left = A.Binary(left.start, right.end, "||", left, right)
        return left

    def and_(self):
        left = self.not_()
        while self.at("AND", "KW_AND"):
            self.advance()
            right = self.not_()
            left = A.Binary(left.start, right.end, "&&", left, right)
        return left

    def not_(self):
        if self.at("NOT", "KW_NOT"):
            t = self.advance()
            e = self.not_()
            return A.Unary(t.start, e.end, "!", e)
        return self.cmp()

    def cmp(self):
        left = self.add()
        if self.cur.kind in REL:
            op = REL[self.advance().kind]
            right = self.add()
            return A.Binary(left.start, right.end, op, left, right)
        return left

    def add(self):
        left = self.mul()
        while self.at("PLUS", "MINUS"):
            op = self.advance().value
            right = self.mul()
            left = A.Binary(left.start, right.end, op, left, right)
        return left

    def mul(self):
        left = self.unary()
        while self.at("STAR", "SLASH"):
            op = self.advance().value
            right = self.unary()
            left = A.Binary(left.start, right.end, op, left, right)
        return left

    def unary(self):
        if self.at("MINUS"):
            t = self.advance()
            e = self.unary()
            return A.Unary(t.start, e.end, "-", e)
        return self.primary()

    def primary(self):
        t = self.cur
        if t.kind == "NUMBER":
            self.advance()
            if self.at("IDENT") and self.cur.value in UNITS:
                u = self.advance()
                return A.Num(t.start, u.end, t.value, u.value)
            return A.Num(t.start, t.end, t.value)
        if t.kind == "STRING":
            self.advance()
            return A.Str(t.start, t.end, t.value)
        if t.kind in ("KW_TRUE", "KW_FALSE"):
            self.advance()
            return A.Bool(t.start, t.end, t.kind == "KW_TRUE")
        if t.kind == "KW_EXISTS":
            self.advance()
            r = self.name("field after 'exists'")
            return A.Exists(t.start, r.end, r.name)
        if t.kind == "IDENT":
            return self.name()
        if t.kind == "LPAREN":
            self.advance()
            e = self.expr()
            self.expect("RPAREN", "to close '('")
            return e
        what = f"'{t.text}'" if t.text else "end of file"
        self.d.error("P010", f"Expected an expression, found {what}", t.start, max(t.end, t.start + 1), "parser")
        raise ParseError()
