"""Small reproducible pilot evaluation, not a large-corpus research result.

Run: python tools/review2_evaluation.py --output docs/review2_evidence.json
"""
from __future__ import annotations

import argparse
import copy
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shieldlang.compiler import compile_source
from shieldlang.verification import compare_policies, verify_optimizations


def evaluate():
    cases = {
        "intersection": 'field x : string matches /^a+$/ matches /^[a-z]+$/ maxlen 20;',
        "length_subsumption": 'field x : string matches /^[0-9]{4}$/ minlen 4 maxlen 5;',
        "enum": 'field x : enum("basic", "pro");',
        "mixed_coverage": 'field x : string minlen 1 maxlen 20; field n : number in 0..10;',
    }
    rows = []
    for name, statements in cases.items():
        source = 'rule Demo on POST "/" { ' + statements + ' }'
        started = time.perf_counter()
        result = compile_source(source, name + '.shl')
        elapsed = time.perf_counter() - started
        rows.append({"case": name, "compile_ok": result.ok,
                     "compile_ms": round(elapsed * 1000, 3),
                     "checker_ms": round(result.timings.get('verification', 0) * 1000, 3),
                     "instructions_before": result.ir.instr_count(), "instructions_after": result.opt_ir.instr_count(),
                     "verification": result.verification})
    mutations = []
    for name, statement, op in (
        ('lost_regex', 'field x : string matches /^a+$/;', 'MATCH'),
        ('lost_length_bound', 'field x : string maxlen 2;', 'CHECK_LEN'),
        ('changed_presence', 'field x? : string;', 'JMP_IF_MISSING'),
    ):
        result = compile_source('rule Demo on POST "/" { ' + statement + ' }')
        mutant = copy.deepcopy(result.opt_ir)
        block = mutant.functions[0].blocks[0]
        if op == 'JMP_IF_MISSING':
            next(i for i in block.instrs if i.op == op).op = 'REQUIRE'
        else:
            block.instrs = [i for i in block.instrs if i.op != op]
        report = verify_optimizations(result.ir, mutant)
        mutations.append({"case": name, "detected": report['status'] == 'failed', "report": report})
    comparison = compare_policies((ROOT / 'examples/policy_before.shl').read_text(encoding='utf-8'),
                                  (ROOT / 'examples/policy_after.shl').read_text(encoding='utf-8'))
    return {"evaluation": "Review 2 synthetic pilot; timings from a single run",
            "python": platform.python_version(), "platform": platform.platform(),
            "cases": rows, "mutations": mutations, "policy_comparison": comparison,
            "limitations": ["Four synthetic examples and three hand-selected mutations",
                            "No state-of-the-art comparison or statistical performance claim",
                            "DFA front end is shared with the compiler", "Whole-program equivalence is not established"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = evaluate()
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + '\n', encoding='utf-8')
        print(f"Wrote {args.output}")
    else:
        print(text)
    if not all(r['compile_ok'] for r in result['cases']) or not all(m['detected'] for m in result['mutations']):
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
