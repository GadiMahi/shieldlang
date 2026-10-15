"""
ShieldLang command-line interface.

  python shieldlang.py                         launch ShieldLang Studio (web UI) on :8080
  python shieldlang.py serve [--port N] [--no-browser]
  python shieldlang.py compile FILE [--emit py|js|ir|opt-ir|tokens|ast|symbols|report|passes] [-o OUT]
  python shieldlang.py check FILE REQUEST.json        validate a request with the compiled rules
  python shieldlang.py regex PATTERN [--flags i]      RegexShield v2 analysis of one pattern
  python shieldlang.py bench PATTERN [--flags i]      backtracking vs DFA benchmark
  python shieldlang.py test                           run the test suite
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .diagnostics import SourceMap

RISK_MARK = {"HIGH": "✖ HIGH", "MEDIUM": "▲ MEDIUM", "LOW": "✔ LOW", "UNKNOWN": "? UNKNOWN"}


def _print_diags(res):
    sm = SourceMap(res.source)
    for d in res.diags.items:
        print(d.render(sm))
        print()


def cmd_compile(a) -> int:
    from .compiler import compile_source
    src = Path(a.file).read_text(encoding="utf-8")
    res = compile_source(src, Path(a.file).name)
    _print_diags(res)
    if not res.ok:
        print(f"✖ compilation failed with {len(res.diags.errors)} error(s)")
        return 1
    out = None
    if a.emit == "py":
        out = res.python
    elif a.emit == "js":
        out = res.js
    elif a.emit == "ir":
        out = res.ir.text()
    elif a.emit == "opt-ir":
        out = res.opt_ir.text()
    elif a.emit == "tokens":
        out = "\n".join(f"{t.start:5}-{t.end:<5} {t.kind:12} {t.text}" for t in res.tokens)
    elif a.emit == "ast":
        out = json.dumps(res.program.to_json(), indent=2)
    elif a.emit == "symbols":
        out = json.dumps(res.model.symbol_table(SourceMap(src)), indent=2)
    elif a.emit == "passes":
        lines = []
        for p in res.passes:
            lines.append(f"[{p.name}] instrs {p.instrs_before}→{p.instrs_after}, cost {p.cost_before}→{p.cost_after}")
            lines += [f"    • {c}" for c in p.changes] or ["    (no change)"]
        out = "\n".join(lines)
    elif a.emit == "report":
        rep = res.report()
        lines = [f"ShieldLang security report — {res.name}", "=" * 60]
        for r in rep["regexes"]:
            lines.append(f"{RISK_MARK[r['risk']]:10} {r['complexity']:8} /{r['source']}/{r['flags']}")
            if r["witness"]:
                lines.append(f"{'':20}attack: {r['witness']['example'][:60]!r}")
            if r["rewrite"] and r["rewrite"]["changed"]:
                lines.append(f"{'':20}fix (verified equivalent): /{r['rewrite']['final']}/  → {r['rewrite']['final_risk']}")
        lines.append("")
        lines.append(rep["guarantee"])
        out = "\n".join(lines)
    if a.output:
        Path(a.output).write_text(out, encoding="utf-8")
        print(f"✔ wrote {a.output}")
    else:
        print(out)
    t = sum(res.timings.values()) * 1000
    print(f"\n✔ compiled {len(res.model.rules)} rule(s), {len(res.model.regexes)} regex(es) in {t:.1f} ms", file=sys.stderr)
    return 0


def cmd_check(a) -> int:
    from .compiler import compile_source
    res = compile_source(Path(a.file).read_text(encoding="utf-8"), Path(a.file).name)
    if not res.ok:
        _print_diags(res)
        return 1
    req = json.loads(Path(a.request).read_text(encoding="utf-8"))
    out = res.vm().run(req, trace=a.trace)
    print(json.dumps(out, indent=2))
    return 0 if out["allowed"] else 2


def cmd_regex(a) -> int:
    from .regex.compiler import compile_pattern
    from .regex.parser import RegexSyntaxError
    try:
        art = compile_pattern(a.pattern, a.flags)
    except RegexSyntaxError as exc:
        print(f"✖ {exc.stage}: {exc.message} at position {exc.position}")
        print("  " + a.pattern)
        print("  " + " " * exc.position + "^")
        return 1
    r = art.redos
    lo, hi = art.length_bounds
    print(f"Pattern     /{art.source}/{art.flags}")
    print(f"Tokens      {len(art.tokens) - 1}")
    print(f"ε-NFA       {art.nfa.size} states")
    print(f"DFA         {art.raw_dfa.n} states → minimised {art.dfa.n} ({len(art.dfa.partition)} symbol classes)")
    print(f"Length      {lo}..{'∞' if hi is None else hi}")
    print(f"Verdict     {RISK_MARK[r.risk]}  {r.complexity}  — {r.summary}")
    for f in r.findings:
        print(f"  • [{f.layer}/{f.kind}/{f.severity}] {f.message}")
    if r.witness:
        print(f"Attack      prefix={r.witness.prefix!r} pump={r.witness.pump!r} suffix={r.witness.suffix!r}")
    if art.rewrite and art.rewrite.changed:
        print(f"Rewrite     /{art.rewrite.final}/  ({art.rewrite.final_risk}, proved equivalent by DFA comparison)")
        for s in art.rewrite.steps:
            print(f"  • {s.rule}: {s.description}")
    for n in r.notes:
        print(f"Note        {n}")
    return 0


def cmd_bench(a) -> int:
    from .bench import run_benchmark
    res = run_benchmark(a.pattern, a.flags)
    print(f"/{a.pattern}/  risk={res['risk']} {res['complexity']}  pump={res['pump']!r}")
    print(f"{'n':>6} {'length':>8} {'backtracking (re)':>20} {'ShieldLang DFA':>16}")
    for r in res["rows"]:
        bt = "TIMEOUT" if r["timeout"] else f"{r['backtracking_s'] * 1000:.3f} ms"
        print(f"{r['n']:>6} {r['length']:>8} {bt:>20} {r['dfa_s'] * 1000:>13.3f} ms")
    return 0


def cmd_test(a) -> int:
    import unittest
    root = Path(__file__).resolve().parent.parent
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), top_level_dir=str(root))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="shieldlang", description="ShieldLang — a compiler for safe API security rules")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="launch ShieldLang Studio")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--no-browser", action="store_true")
    c = sub.add_parser("compile", help="compile a .shl file")
    c.add_argument("file")
    c.add_argument("--emit", default="report",
                   choices=["py", "js", "ir", "opt-ir", "tokens", "ast", "symbols", "report", "passes"])
    c.add_argument("-o", "--output")
    k = sub.add_parser("check", help="validate a JSON request against compiled rules")
    k.add_argument("file")
    k.add_argument("request")
    k.add_argument("--trace", action="store_true")
    r = sub.add_parser("regex", help="analyse one regular expression")
    r.add_argument("pattern")
    r.add_argument("--flags", default="")
    b = sub.add_parser("bench", help="benchmark backtracking vs DFA")
    b.add_argument("pattern")
    b.add_argument("--flags", default="")
    sub.add_parser("test", help="run the test suite")
    a = p.parse_args(argv)
    if a.cmd in (None, "serve"):
        from .server import serve
        serve(getattr(a, "host", "127.0.0.1"), getattr(a, "port", 8080), not getattr(a, "no_browser", False))
        return 0
    return {"compile": cmd_compile, "check": cmd_check, "regex": cmd_regex, "bench": cmd_bench,
            "test": cmd_test}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
