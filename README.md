# ShieldLang

**A compiler for provably safe API security rules.**
BCSE307P Compiler Design Lab project, VIT Vellore. Team: Milind Koul (24BDS0106), Aaditya Bhushan Yeola (24BDS0109), Mahi Gadi (24BAI0227).

ShieldLang is a small domain-specific language for writing the security rules that protect a web API: which requests are allowed, what each field must look like, and which attack patterns are blocked. The ShieldLang compiler checks those rules for mistakes, analyses every regular expression for **ReDoS** (Regular-expression Denial of Service), repairs the dangerous ones with **proofs of equivalence**, optimises the rules, and generates validators in **Python and JavaScript** that are **linear-time by construction**.

ReDoS is a real problem. Cloudflare's global outage (2019) and Stack Overflow's outage (2016) were both caused by a single backtracking regex inside a request filter. ShieldLang prevents this by design, because every pattern is compiled to a DFA.

```text
rule CreatePayment on POST "/api/pay" {
  header Authorization : string matches /^Bearer [A-Za-z0-9._-]+$/ maxlen 512;
  field  upi_id        : string matches /^[a-z0-9.]+@[a-z]+$/ maxlen 64;
  field  amount        : number in 1..100000;
  field  method        : enum("upi", "card");
  field  card_number?  : string matches /^[0-9]{16}$/;

  when method == "card" require card_number;          // cross-field rule
  body max_depth 5 max_size 16 KB;                    // blocks deeply nested JSON bombs
  block if any field matches /(union|select).*from/i as "SQL injection attempt";
}
```

## Quick start

You only need Python 3.9 or newer. There are no packages to install.

```bash
python shieldlang.py                       # opens ShieldLang Studio at http://127.0.0.1:8080
python shieldlang.py test                  # 36 unit, differential and fuzz tests
python shieldlang.py compile examples/payments.shl --emit report
python shieldlang.py compile examples/payments.shl --emit py -o payments_validator.py
python shieldlang.py regex "^([a-zA-Z0-9]+\.?)+@[a-z]+\.[a-z]{2,6}$"
python shieldlang.py bench "^(a+)+$"
python shieldlang.py check examples/payments.shl request.json --trace
```

On Windows you can also double-click `run.bat`. On macOS or Linux you can run `./run.sh`.

## The compiler pipeline

```text
 .shl source
     │
 ┌───▼────┐   ┌────────┐   ┌──────────────────┐   ┌──────────┐   ┌────────────┐   ┌───────────┐
 │ Lexer  ├──▶│ Parser ├──▶│ Semantic analysis├──▶│ IR (SIR) ├──▶│ Optimiser  ├──▶│ Code gen  │──▶ Python / JS
 └────────┘   └────────┘   └────────┬─────────┘   └──────────┘   │ 6 passes   │   └───────────┘
  tokens +     AST, panic-   symbols, types,       three-address  └────────────┘   table-driven
  spans        mode recovery contradictions        code, labels                    DFAs, O(n)
                                    │
                         ┌──────────▼───────────────────────────────────────────────┐
                         │ RegexShield v2 — regex sub-compiler (Review 1, extended)  │
                         │ tokens → AST → Thompson ε-NFA → DFA → minimal DFA         │
                         │ → EDA/IDA ambiguity analysis → attack string → verified   │
                         │   rewrite (DFA equivalence)                               │
                         └───────────────────────────────────────────────────────────┘
```

| Phase | What it does | Where |
|---|---|---|
| Lexical analysis | Maximal-munch scanner. Handles keywords, size units (`16 KB`), string and regex literals (using the JavaScript rule that tells `/` regex from `/` division), and comments. Every token records exact offsets. | `shieldlang/lang/lexer.py` |
| Syntax analysis | Recursive-descent parser for the grammar in `docs/grammar.md`. Panic-mode recovery resynchronises on `;` and `}`, so all syntax errors are reported in one run. | `shieldlang/lang/parser.py` |
| Semantic analysis | Builds the symbol table (a global scope plus one scope per rule), evaluates constants, and type-checks constraints and `when` conditions. Finds contradictions with interval reasoning and DFA intersection: empty ranges, `minlen > maxlen`, patterns that can never fit the length limits, mutually exclusive patterns, impossible enum comparisons, dead conditions, fields that are both required and forbidden, and route conflicts. | `shieldlang/lang/sema.py` |
| Regex sub-compiler | RegexShield v2. Supports character classes, `\d \w \s`, `.`, `{m,n}`, `(?:)` and the `/i` flag. Pipeline: Thompson construction with provenance, subset construction over a compressed alphabet, Moore minimisation, product construction, equivalence checking, and ambiguity analysis. | `shieldlang/regex/` |
| Intermediate code | SIR is a three-address, label-based IR. Each rule becomes a function of basic blocks, and conditions are lowered to `CMP/AND/OR` temporaries. | `shieldlang/ir/ir.py` |
| Optimisation | Six passes: constant folding, dead-code elimination, redundant-check elimination using DFA length bounds, DFA intersection, scan fusion into a tagged product DFA, and cost-based check reordering. | `shieldlang/ir/optimize.py` |
| Code generation | Dependency-free Python and JavaScript validators. Every pattern is emitted as a transition table. | `shieldlang/codegen.py` |
| Execution | A virtual machine runs the IR, counting instructions and DFA steps. The tests use it to cross-check the generated code. | `shieldlang/vm.py` |

## How ReDoS is detected (RegexShield v2)

1. **Structural indicators** (from Review 1): nested quantifiers, overlapping alternatives under repetition, and adjacent overlapping loops.
2. **Exponential ambiguity (EDA)**: in the pair graph NFA×NFA, a strongly connected component contains a diagonal state `(q,q)` and either an off-diagonal pair or a "split" step. The ε-elimination step keeps track of how many paths lead into each transition, so that `(a*)*`-style ambiguity is not lost. Backtracking cost is O(2ⁿ).
3. **Polynomial ambiguity (IDA)**: states `p≠q` with `p→p`, `p→q`, `q→q` on the same word, found by search in the triple product. A chain of such pairs gives degree *k*, and the cost is O(nᵏ).
4. **Attack string**: built as `prefix · pumpⁿ · suffix`. The suffix is chosen with the DFA so that the whole input is *rejected*, because rejection is what forces backtracking. The benchmark checks this against Python's `re` in a sandboxed subprocess.
5. **Provenance**: the states involved are mapped back through Thompson provenance to the exact sub-expression, which is highlighted in the UI.
6. **Verified rewriting**: there are seven local rules (`(x+)+ → x+`, `(a|aa)+ → a+`, `(x+s?)+ → x+(?:sx+)*s?`, …). A candidate is accepted only if its minimal DFA is language-equivalent to the original (the XOR product is empty) and the risk does not increase.

For example, the real-world email pattern `^([a-zA-Z0-9]+\.?)+@[a-z]+\.[a-z]{2,6}$` is classified **HIGH (O(2ⁿ))**, with the attack string `a.aa.aa.aa…_`. It is rewritten to `^[a-zA-Z0-9]+(?:\.[a-zA-Z0-9]+)*\.?@[a-z]+\.[a-z]{2,6}$`, which is proved equivalent and classified **LOW**.

## ShieldLang Studio (web interface)

`python shieldlang.py` starts a zero-dependency server and opens the Studio. It has the following views:

- A **live editor** with syntax highlighting, inline error and warning squiggles, and gutter markers. It recompiles as you type.
- A **pipeline bar** showing each phase's status and timing.
- An **Overview** with a security report, KPIs and a compile-time breakdown.
- **Tokens**, **AST** and **Symbols** views. Hover over any item to highlight its source span.
- **RegexShield**: per-pattern analysis, the culprit sub-expression, the attack string, the verified rewrite, and drawings of the ε-NFA, the subset DFA and the minimal DFA. You can also analyse any regex you type.
- **IR**, unoptimised and optimised, with the DFA and error tables.
- **Optimiser**: every pass with what it changed and the change in cost.
- **Code**: the generated Python and JavaScript, which you can copy or download.
- **Run**: validates sample or custom requests on the IR virtual machine, with an execution trace.
- **Benchmark**: Python `re` against the ShieldLang DFA on the attack string, as a log-scale chart.

## Project layout

```text
shieldlang/            compiler package (standard library only)
  lang/                lexer, parser, AST, semantic analysis
  regex/               RegexShield v2: charset, parser, NFA, DFA, ReDoS, rewrite
  ir/                  IR lowering and optimisation passes
  codegen.py vm.py bench.py compiler.py server.py __main__.py
web/                   ShieldLang Studio (HTML/CSS/JS, no build step)
examples/              payments.shl, regex_lab.shl, diagnostics_demo.shl (+ sample requests)
tests/                 unittest suite (unit, differential vs re, generated-code cross-checks)
docs/                  grammar, architecture, Review 2 status
```

## Relation to Review 1

Review 1 delivered **RegexShield**, a lexer, parser, typed AST, Thompson ε-NFA and structural ReDoS heuristics for a restricted regex language. In ShieldLang it becomes the regex sub-compiler, and it is extended with character classes, bounded repetition, DFA construction and minimisation, automata-based ambiguity analysis, attack generation and verified repair. See `docs/review2_status.md`.
