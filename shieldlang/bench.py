"""
Empirical ReDoS benchmark: backtracking (Python `re`) versus the compiled DFA,
on the attack strings produced by the analyser.

Each backtracking measurement runs in a child process with a hard timeout, so
a catastrophic pattern can never hang the compiler or the web server.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Dict, List, Optional

from .regex.compiler import compile_pattern

_CHILD = r"""
import re, sys, time, json
pat, flags, s = json.loads(sys.stdin.read())
rx = re.compile(pat, re.I if 'i' in flags else 0)
t = time.perf_counter()
rx.search(s)
print(time.perf_counter() - t)
"""


def _backtracking_time(pattern: str, flags: str, text: str, timeout: float) -> Optional[float]:
    try:
        out = subprocess.run([sys.executable, "-c", _CHILD], input=json.dumps([pattern, flags, text]),
                             capture_output=True, text=True, timeout=timeout)
        return float(out.stdout.strip())
    except subprocess.TimeoutExpired:
        return None
    except (ValueError, OSError):
        return None


def run_benchmark(pattern: str, flags: str = "", budget: float = 6.0, timeout: float = 2.0) -> Dict:
    art = compile_pattern(pattern, flags)
    w = art.redos.witness
    if w is None:
        # No attack: still measure on a long benign-looking input to show linear behaviour
        sample = art.dfa.shortest_accepted() or "a"
        sizes = [1000, 4000, 16000, 64000]
        prefix, pump, suffix = "", (sample or "a")[:1] or "a", "!"
        kind = "none"
    else:
        prefix, pump, suffix, kind = w.prefix, w.pump, w.suffix, w.kind
        if kind == "EDA":
            sizes = [4, 8, 12, 14, 16, 18, 20, 22, 24, 26, 28]
        else:
            sizes = [500, 1000, 2000, 4000, 8000, 16000]
    rows: List[Dict] = []
    started = time.perf_counter()
    timed_out = False
    for n in sizes:
        text = prefix + pump * n + suffix
        bt = None if timed_out else _backtracking_time(pattern, flags, text, timeout)
        if bt is None:
            timed_out = True
        t0 = time.perf_counter()
        st, steps = art.dfa.run(text)
        dfa_t = time.perf_counter() - t0
        rows.append({"n": n, "length": len(text), "backtracking_s": bt, "dfa_s": dfa_t, "dfa_steps": steps,
                     "timeout": bt is None})
        if time.perf_counter() - started > budget:
            break
        if timed_out and len(rows) >= 3:
            break
    return {"pattern": pattern, "flags": flags, "kind": kind, "risk": art.redos.risk,
            "complexity": art.redos.complexity, "prefix": prefix, "pump": pump, "suffix": suffix,
            "timeout_s": timeout, "rows": rows}
