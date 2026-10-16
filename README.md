# ShieldLang

**A compiler and local experimentation workbench for API validation and stateful security policies.**

BCSE307P Compiler Design Lab project, VIT Vellore.

Team: Milind Koul (24BDS0106), Aaditya Bhushan Yeola (24BDS0109), Mahi Gadi (24BAI0227).

ShieldLang compiles a domain-specific policy language into an intermediate representation, executes it on a virtual machine, and generates dependency-free Python and JavaScript request validators. RegexShield is its regex sub-compiler: patterns become DFA tables, so generated regex matching does not backtrack.

The project also includes a stateful protocol compiler/runtime, traffic replay through a local gateway, and bounded experiments that expose differences between policy-side and application-side input interpretation. These tools make decisions, counterexamples and runtime recovery visible in connected browser pages.

## What you can do

| Component | Functionality |
|---|---|
| **Compiler Studio** | Edit policies; inspect tokens, AST, symbols, diagnostics, SIR and optimization passes; execute requests; export Python/JavaScript validators. |
| **RegexShield** | Construct and visualize automata, analyze backtracking ambiguity, generate candidate attack strings, check rewrite language equivalence and benchmark against Python `re`. |
| **Verification and policy comparison** | Check supported scalar string-field languages before/after optimization; synthesize candidate policy differences and replay requests against both policy VMs. |
| **Protocol Studio** | Compile resource states, legal transitions and cross-request bindings; enforce ownership/reservations; inspect ambiguous outcomes, recovery and reconciliation. |
| **Traffic Lab** | Select supported policies, validate/import recordings, replay in observe/enforce mode, and compare gateway decisions with an independent demo-backend ledger. |
| **Bypass Lab** | Search declared local fixtures for interpretation mismatches, confirm findings by HTTP replay, minimize confirmed inputs and save/export the evidence. |

## Run locally

The runtime uses the Python standard library; no runtime packages or frontend build step are required. Python 3.10+ is recommended.

```bash
git clone https://github.com/GadiMahi/shieldlang.git
cd shieldlang
python shieldlang.py
```

Open the URL printed by the server (default `http://127.0.0.1:8080`). On Windows, `run.bat` also provides a launcher; on macOS/Linux, use `bash run.sh`.

| Page | Route |
|---|---|
| Guided introduction and compiler demo | `/` |
| Full compiler Studio | `/studio` |
| Protocol Studio | `/protocols` |
| Traffic Lab | `/traffic` |
| Bypass Lab | `/lab/bypasses` |

To use another port:

```bash
python shieldlang.py serve --port 8090
```

Lab gateways, fixture apps and demo backends run on loopback. Run records, imported recordings and policy sources are stored locally in `.shieldlang_data/` (excluded from Git). Set `SHIELDLANG_DATA` to use another directory. Restarting the web server loses live lab sessions; saved evidence remains in the artifact store.

## The policy language

```text
rule CreatePayment on POST "/api/pay" {
  header Authorization : string matches /^Bearer [A-Za-z0-9._-]+$/ maxlen 512;
  field upi_id         : string matches /^[a-z0-9.]+@[a-z]+$/ maxlen 64;
  field amount         : number in 1..100000;
  field method         : enum("upi", "card");
  field card_number?   : string matches /^[0-9]{16}$/;

  when method == "card" require card_number;
  body max_depth 5 max_size 16 KB;
  block if any field matches /(union|select).*from/i as "SQL injection attempt";
}
```

The lexer and parser build a source-spanned AST. Semantic analysis checks types, constraints, conditions and route conflicts. Lowering creates SIR instructions, six optimization passes transform them, and the VM or generated validators execute the result.

```text
.shl source -> lexer -> parser -> semantic analysis -> SIR -> optimizer -> VM / Python / JavaScript
                           |                           |
                           |                           +-> supported-field verification
                           +-> RegexShield: regex AST -> NFA -> DFA -> minimization / analysis
                           +-> protocol AST -> ProtocolIR -> checks -> stateful runtime
```

Regex rewrites are accepted only after DFA language-equivalence checking and a risk check. Analysis with unresolved or exhausted search budgets reports incomplete/UNKNOWN results. DFA matching has linear input traversal for a fixed compiled automaton; compilation cost and the complete request-processing pipeline have separate limits.

## Stateful protocols

[`examples/orders.shl`](examples/orders.shl) declares request rules and a `Checkout` protocol:

```text
protocol Checkout {
  resource "order";
  states created, paid, confirmed;
  initial created;
  terminal confirmed;
  key id;
  create CreateOrder id "id" ref client_ref probe get "/api/orders/by-ref/{ref}";
  step PayOrder     from created to paid;
  step ConfirmOrder from paid    to confirmed;
  bind PayOrder.card_token == ConfirmOrder.card_token;
  outcome success 200..201;
  outcome failure 402;
  contract "Demo backend returns 402 only when the card was declined and no state changed.";
  quiesce 2;
  reconcile get "/api/orders/{key}" state "state" owner "owner" {
    "created": created, "paid": paid, "confirmed": confirmed
  };
}
```

The local gateway combines stateless validation with protocol admission. It reserves resources atomically, persists send intent, and classifies outcomes as committed, released or Unknown. Ambiguous upstream outcomes block the resource until reconciliation or an explicit, evidence-backed operator resolution. SQLite stores protocol state and recovery records.

Protocol Studio exposes these steps through runnable scenarios. Its second example, [`orders_unbound.shl`](examples/orders_unbound.shl), deliberately omits the card-token binding: the same mismatched confirmation is refused under Checkout and allowed under CheckoutUnbound. This demonstrates a cross-request guarantee that the demo backend itself does not enforce.

## Traffic and bypass experiments

**Traffic Lab** runs recordings through the compiled-policy gateway and local order backend. It supports policy/protocol selection, observe/enforce comparison, recording validation/import/deletion and saved results. Supported routes and recording format are described in the [lab architecture](docs/lab_architecture.md).

**Bypass Lab** contains five fixtures: Unicode compatibility normalization, percent-decoded paths, duplicate JSON keys, an equivalent-pipeline negative control and NUL stripping. Oracle-derived or caller-supplied seeds feed a bounded mutation search. A finding requires policy acceptance, an application-view property violation and matching local HTTP replay. Character-deletion minimization preserves confirmed evidence; an inconclusive trial is recorded separately.

Bypass experiments use their declared fixture predicates and a standalone fixture app, not imported `.shl` policies or arbitrary external services. The Traffic Lab link displays the original evidence with its scope labeled; it does not replay that candidate through the unrelated order gateway. See [search contracts and limits](docs/bypass_search.md).

## Compiler commands

```bash
python shieldlang.py compile examples/payments.shl --emit report
python shieldlang.py compile examples/payments.shl --emit py -o payments_validator.py
python shieldlang.py compile examples/payments.shl --emit js -o payments_validator.js
python shieldlang.py compile examples/payments.shl --emit verification
python shieldlang.py compare examples/policy_before.shl examples/policy_after.shl
python shieldlang.py check examples/payments.shl request.json --trace
python shieldlang.py regex "^(a+)+$"
python shieldlang.py bench "^(a+)+$"
```

The `check` command expects one request object. The bundled `*.requests.json` files contain example request collections; choose an individual request from Studio or save one request object to a file before using `check`.

## Tests

```bash
python shieldlang.py test
```

The suite includes compiler regressions, VM/generated-code comparisons, protocol static analysis, runtime crash/concurrency/recovery tests, independent-reference differential checks, traffic replay, bypass search/reduction and HTTP API tests. Node.js enables JavaScript backend checks. Exhaustive protocol trace tests can take longer than the focused modules.

For a focused lab integration run:

```bash
python -m unittest tests.test_bypass tests.test_bypass_search tests.test_bypass_search_contracts tests.test_bypass_search_integration tests.test_lab_api
```

Browser tests use optional development dependencies:

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m unittest tests.browser.test_smoke -v
```

Missing browser dependencies are reported as skips. Test counts and platform-specific limitations belong to the dated [implementation log](docs/implementation_progress.md), rather than a permanent all-tests-passing claim. See [browser test instructions](tests/browser/README.md).

## Guarantees and boundaries

- Generated pattern matching uses DFA tables. ReDoS analysis of other backtracking engines is bounded and can be inconclusive.
- Regex equivalence applies to the supported regex language and its modeled alphabet/semantics.
- Optimization verification covers supported scalar string-field acceptance. Conditions, scans, error ordering, numeric/list fields and emitted backend correctness are outside that proof.
- Policy-change witnesses are replayed differences between two VMs; failure to find a witness is not whole-program equivalence.
- Protocol guarantees depend on declared upstream outcome contracts, adapter assumptions and storage behavior. A UI restart exercises recovery without simulating every process or power-loss failure.
- Bypass findings apply to local fixtures and the explored search space. No finding is not a safety proof.

The current work is an academic compiler/security prototype. Manuscript novelty and patentability have not been established.

## Project layout

```text
shieldlang/
  lang/          lexer, parser, AST and semantic analysis
  regex/         RegexShield automata, ambiguity analysis and rewrites
  ir/            SIR lowering and optimization passes
  protocol/      protocol language, IR, static checks, runtime and reference interpreter
  lab/           gateway, demo backend, traffic, fixture experiments and artifact store
  verification.py, compiler.py, codegen.py, vm.py, server.py
web/             guided demo, Studio and connected lab pages; plain HTML/CSS/JS
examples/        policies, protocols, request samples and traffic recordings
tests/           unit, integration, differential and optional browser tests
tools/           evaluation scripts
docs/            grammar, semantics, architecture, demo guides and implementation history
```

## Documentation and Review 2

- [Review 2 status](docs/review2_status.md) and [presentation/demo guide](docs/review2_demo.md)
- [Language grammar](docs/grammar.md) and [compiler architecture](docs/architecture.md)
- [Protocol semantics](docs/protocol_semantics.md) and [lab architecture](docs/lab_architecture.md)
- [Bypass search and minimization](docs/bypass_search.md)
- [Guided demo](docs/guided_demo.md) and [implementation history](docs/implementation_progress.md)

Review 1 delivered RegexShield's regex-language pipeline and structural analysis. ShieldLang extends it into an API policy compiler, with stateful protocol enforcement and evidence-producing local experiments. DFA construction, protocol state machines and mutation testing are established techniques; the project presents their implementation and integration without claiming they are new inventions.
