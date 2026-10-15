# Architecture

## Data flow

| Stage | Input → output | Key data structures |
|---|---|---|
| Lexer | text → `Token[]` | `Token(kind, value, start, end)`; `DiagnosticBag` |
| Parser | tokens → `Program` AST | `Rule`, `FieldDecl`, `Constraint`, `WhenStmt`, `BlockStmt`, expression nodes |
| Semantic | AST → `SemanticModel` | symbol table, `RuleInfo`, `FieldInfo`, `RegexEntry` (per regex literal) |
| RegexShield v2 | pattern → `RegexArtifact` | `CharSet` / `Partition`, regex AST, `NFA` (+ provenance), `DFA`, `ReDoSReport`, `RewriteResult` |
| Lowering | model → `Module` | `Function` → `Block` → `Instr`; DFA table; error table |
| Optimiser | `Module` → `Module` + `PassReport[]` | cost model `COST[op]` |
| Codegen | `Module` → Python / JS source | DFA transition tables |
| VM | `Module` × request → verdict + trace | registers, labels, step counters |

## Automata details

* **Alphabet compression.** All character sets in an NFA are refined into disjoint *symbol classes* (minterms). DFA tables are indexed by class, not by character, so `[A-Za-z0-9._-]+` needs only a handful of columns. At run time, a binary search over the class boundaries maps each character to its class.
* **Subset construction** works over classes and is capped at 4000 states. A larger DFA produces a diagnostic and never hangs the compiler.
* **Moore minimisation** is tag-aware: the initial partition separates states by their accept tags. Equal columns are merged afterwards (alphabet re-compression).
* **Product construction** supports `and`, `or`, `xor`, `diff` and an N-way `tagged` mode. `xor` + emptiness = equivalence, with the shortest counter-example. `and` + emptiness = detecting mutually exclusive patterns. `tagged` = scan fusion that still reports which rule fired.
* **Ambiguity analysis** runs on an ε-free NFA that keeps the *multiplicity* of ε-paths. This matters because `(a*)*` is ambiguous only through distinct ε-paths that reach the same transition.

## IR instruction set (SIR)

| Op | Meaning |
|---|---|
| `t = LOAD src, name` | read a header / query / param / body field (`MISSING` if absent) |
| `REQUIRE t !E -> L` | error E if missing, then jump to the block end |
| `JMP_IF_MISSING t -> L` | optional field: skip its checks |
| `CHECK_TYPE t, ty` | type check (scalars from header/query/param are coerced first) |
| `CHECK_LEN / CHECK_RANGE / CHECK_ITEMS t, lo, hi` | interval checks (`.each` = per list element) |
| `CHECK_ENUM t, {…}` | membership check |
| `MATCH t, dfaK` | run DFA K over the string (one table lookup per character) |
| `CHECK_DEPTH / CHECK_SIZE / CHECK_KEYS n` | body limits |
| `t = CMP a, op, b` · `AND` · `OR` · `NOT` · `EXISTS` · `BIN` | three-address condition code |
| `JMP_IF_FALSE t -> L` | conditional branch |
| `FAIL_IF_MISSING / FAIL_IF_PRESENT t !E` | `require` / `forbid` |
| `SCAN scope, target, dfaK !E` | block rule: run a (possibly fused, tagged) DFA over every string in scope |

## Optimisation passes

1. **Constant folding.** Named constants, arithmetic and size units are folded into literals.
2. **Dead-code elimination.** `when` blocks that semantic interval/enum analysis proved to be always false are removed. Conditions that are always true are dropped.
3. **Redundant check elimination.** `CHECK_LEN` is removed when the pattern's DFA length bounds already imply it. For example, `/^[0-9]{16}$/` makes `maxlen 32` redundant.
4. **DFA intersection.** Several `MATCH` instructions on one value are merged into one product DFA, so the string is read once instead of k times.
5. **Scan fusion.** `SCAN`s over the same scope are merged into one tagged product DFA.
6. **Check reordering.** Cheap checks run before automata, and field blocks are ordered by cost.

## Testing strategy

* Unit tests for every phase.
* **Differential fuzzing**: compiled DFAs are compared with Python's `re` on thousands of random strings.
* **Translation validation**: every rewrite is checked again for DFA equivalence.
* **Generated-code cross-checks**: the optimised VM, the unoptimised VM, the generated Python and the generated JavaScript must report identical error sets on every sample request.
* A real timing test shows that the exponential witness really does explode in `re`.
