# Review 2: implementation status

| Component | Review 1 (25%) | Review 2 (now) |
|---|---|---|
| Problem scope | ReDoS in one regex | Security rules for whole APIs (ShieldLang DSL), with ReDoS as one analysis |
| Lexer | Regex lexer | Regex lexer v2 (classes, escapes, `{m,n}`, flags) **+ ShieldLang lexer** |
| Parser | Regex recursive descent | Regex parser v2 **+ ShieldLang parser with error recovery** |
| AST | Typed regex AST | Regex AST v2 **+ ShieldLang AST** |
| Semantic analysis | — | Symbol table, constant evaluation, type checking, contradiction detection (interval + automata), 42 diagnostic codes across all phases |
| Automata | Thompson ε-NFA | ε-NFA with provenance, **alphabet compression, subset construction, Moore minimisation, product / equivalence** |
| ReDoS analysis | Structural heuristics | Heuristics **+ EDA/IDA ambiguity analysis, degree, attack strings, source provenance** |
| Repair | Planned | **7 rewrite rules, each verified by DFA equivalence** |
| Intermediate code | — | **SIR three-address IR** |
| Optimisation | — | **6 passes with cost model and explanations** |
| Code generation | — | **Python + JavaScript validators (table-driven DFAs)** |
| Runtime | — | **IR virtual machine with traces** |
| Evaluation | 4 examples | **Benchmark vs Python `re`, differential fuzzing, generated-code cross-checks (36 tests)** |
| Interface | 5-stage viewer | **ShieldLang Studio: live editor + 10 views** |

## Remaining work for the final review (≈25%)

* Grammar-level extensions: nested object schemas (`field address { … }`) and reusable rule templates.
* An HTTP middleware adapter (Flask/Express) that loads the generated validator.
* A larger evaluation on public ReDoS corpora (for example, the regexes collected by Davis et al.) and a comparison with existing detectors.
* More rewrite rules and a counter-example-guided search for repairs.
* A final report and paper-style write-up.
