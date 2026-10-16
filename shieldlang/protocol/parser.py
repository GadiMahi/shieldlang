"""Protocol DSL parser (contextual keywords; hooks into the main recursive-descent Parser).

    protocol  → 'protocol' IDENT '{' pstmt* '}'
    pstmt     → 'resource' STRING ';' | 'states' name (',' name)* ';' | 'initial' name ';' | 'terminal' name (',' name)* ';'
              | 'key' name ';'
              | 'create' name 'id' STRING ('ref' name 'probe' 'get' STRING)? ';'
              | 'step' name 'from' name 'to' name ';'
              | 'bind' name '.' name '==' name '.' name ';'
              | 'outcome' ('success'|'failure') range (',' range)* ';'     range → NUMBER ('..' NUMBER)?
              | 'contract' STRING ';' | 'quiesce' NUMBER ';'
              | 'reconcile' 'get' STRING 'state' STRING 'owner' STRING '{' (STRING ':' name) (',' STRING ':' name)* '}' ';'

Words such as `from`, `to`, `state`, `create`, `step` are contextual: they are not reserved by the lexer and remain legal
field/rule names elsewhere. Anything outside this fragment is rejected with PR901 (never silently ignored).
"""
from __future__ import annotations

from . import ast as P

UNSUPPORTED_HINT = {
    "after": "time-based transitions are not supported", "timeout": "timers are not supported",
    "forall": "quantified/multi-resource properties are not supported",
    "parallel": "concurrent regions are not supported", "when": "guards on transitions are not supported; use rule constraints",
    "on": "write `step Rule from A to B;`", "transition": "write `step Rule from A to B;`",
    "ref": "`ref` belongs inside `create`", "counter": "counters/registers are not supported",
    "exists": "multi-resource transitions are not supported", "bind2": "",
}


def parse_protocol(p) -> P.Protocol:
    """`p` is the main Parser positioned at the IDENT 'protocol'."""
    from ..lang.parser import ParseError
    kw = p.advance()
    name = p.expect("IDENT", "for protocol name")
    p.expect("LBRACE")
    proto = P.Protocol(kw.start, kw.end, name.value, name.start, name.end, [])

    def word(context=""):
        tok = p.cur
        if tok.kind == "IDENT" or tok.kind.startswith("KW_"):
            return p.advance()
        p.expect("IDENT", context)

    def nm(context="") -> P.Name:
        t = word(context)
        return P.Name(t.start, t.end, t.text or t.value)

    def kwd(text):
        t = p.cur
        if (t.text or "").lower() == text and (t.kind == "IDENT" or t.kind.startswith("KW_")):
            return p.advance()
        p.expect("IDENT", f"'{text}'")

    def string(ctx=""):
        return p.expect("STRING", ctx).value

    def number(ctx=""):
        return p.expect("NUMBER", ctx).value

    def name_list():
        out = [nm()]
        while p.at("COMMA"):
            p.advance()
            out.append(nm())
        return out

    while not p.at("RBRACE", "EOF"):
        start_i = p.i
        try:
            t = p.cur
            text = (t.text or "").lower() if (t.kind == "IDENT" or t.kind.startswith("KW_")) else None
            s = t.start
            if text == "resource":
                p.advance(); lab = string("after 'resource'"); e = p.expect("SEMI").end
                proto.stmts.append(P.ResourceStmt(s, e, lab))
            elif text == "states":
                p.advance(); ns = name_list(); e = p.expect("SEMI").end
                proto.stmts.append(P.StatesStmt(s, e, ns))
            elif text == "initial":
                p.advance(); n = nm(); e = p.expect("SEMI").end
                proto.stmts.append(P.InitialStmt(s, e, n))
            elif text == "terminal":
                p.advance(); ns = name_list(); e = p.expect("SEMI").end
                proto.stmts.append(P.TerminalStmt(s, e, ns))
            elif text == "key":
                p.advance(); n = nm(); e = p.expect("SEMI").end
                proto.stmts.append(P.KeyStmt(s, e, n))
            elif text == "create":
                p.advance(); rule = nm("for the creating rule"); kwd("id"); idf = string("after 'id'")
                ref = probe = None
                if (p.cur.text or "").lower() == "ref":
                    p.advance(); ref = nm(); kwd("probe"); kwd("get"); probe = string("probe path")
                e = p.expect("SEMI").end
                proto.stmts.append(P.CreateStmt(s, e, rule, idf, ref, probe))
            elif text == "step":
                p.advance(); rule = nm("for the event rule"); kwd("from"); a = nm(); kwd("to"); b = nm(); e = p.expect("SEMI").end
                proto.stmts.append(P.StepStmt(s, e, rule, a, b))
            elif text == "bind":
                p.advance(); ae = nm(); p.expect("DOT"); af = nm(); p.expect("EQ"); be = nm(); p.expect("DOT"); bf = nm()
                e = p.expect("SEMI").end
                proto.stmts.append(P.BindStmt(s, e, ae, af, be, bf))
            elif text == "outcome":
                p.advance(); c = word("success or failure"); cls = (c.text or "").lower()
                if cls not in ("success", "failure"):
                    p.d.error("PR002", f"Outcome class must be 'success' or 'failure', found '{c.text}'", c.start, c.end, "protocol")
                    raise ParseError()
                ranges = []
                while True:
                    lo = number(); hi = lo
                    if p.at("DOTDOT"):
                        p.advance(); hi = number()
                    ranges.append((int(lo), int(hi)))
                    if not p.at("COMMA"):
                        break
                    p.advance()
                e = p.expect("SEMI").end
                proto.stmts.append(P.OutcomeStmt(s, e, cls, ranges))
            elif text == "contract":
                p.advance(); txt = string(); e = p.expect("SEMI").end
                proto.stmts.append(P.ContractStmt(s, e, txt))
            elif text == "quiesce":
                p.advance(); n = number(); e = p.expect("SEMI").end
                proto.stmts.append(P.QuiesceStmt(s, e, float(n)))
            elif text == "reconcile":
                p.advance(); kwd("get"); path = string(); kwd("state"); sf = string(); kwd("owner"); of = string()
                p.expect("LBRACE"); mp = []
                while True:
                    k = string("observed state value"); p.expect("COLON"); mp.append((k, nm()))
                    if not p.at("COMMA"):
                        break
                    p.advance()
                p.expect("RBRACE"); e = p.expect("SEMI").end
                proto.stmts.append(P.ReconcileStmt(s, e, path, sf, of, mp))
            else:
                tok = p.cur
                hint = UNSUPPORTED_HINT.get(text or "", "")
                p.d.error("PR901", f"Unsupported or unknown protocol construct '{tok.text}'", tok.start, max(tok.end, tok.start + 1),
                          "protocol", hint or "Supported statements: resource, states, initial, terminal, key, create, step, bind, outcome, contract, quiesce, reconcile.")
                raise ParseError()
        except ParseError:
            if p.i == start_i:
                p.advance()
            p.sync()
            if p.at("RBRACE"):
                break
    end = p.expect("RBRACE").end
    proto.end = end
    return proto
