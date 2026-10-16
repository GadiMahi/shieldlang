# ShieldLang — Review 2 progress

## Current Review 2 scope — 16 October 2026

ShieldLang now includes the compiler/RegexShield work described below plus:

1. **Protocol compilation and runtime:** a resource/state/transition/binding DSL,
   ProtocolIR, static checks, persistent reservations, Unknown outcomes, recovery
   and reconciliation. Protocol Studio includes Checkout and CheckoutUnbound.
2. **Traffic Lab:** supported policy selection, validated recording import,
   observe/enforce replay through a local gateway and independent backend ledger.
3. **Bypass Lab:** bounded oracle-guided mutation search, local HTTP confirmation,
   replay-preserving minimization, persisted evidence and a read-only Traffic Lab
   handoff. This operates on declared fixture predicates, not compiled policies.

The latest additive search integration preserved existing public contracts and
added deadline/error-evidence regressions. That integration passed 181 focused
and regression checks. A broader 32-test lab API run passed 31 checks and hit a
Windows connection reset in the oversized-request rejection test. Other runs
reported in the implementation log have different coverage and environments;
these figures must not be combined into an unqualified full-suite claim.

See [lab architecture](lab_architecture.md), [protocol semantics](protocol_semantics.md),
[bypass search](bypass_search.md), the [demo guide](review2_demo.md), and the dated
[implementation log](implementation_progress.md). The mechanisms are substantial
project extensions; research novelty still requires comparative evaluation.

## Earlier compiler-verification milestone (historical)

The sections below describe the earlier milestone. Its test counts and remaining
work are historical; use the current scope above and dated implementation log for
the expanded project. The guided introduction and connected lab pages now exist.


Updated 16 October 2026. This records implemented work and a proposed submission scope; it does not assert final project completion or proven research novelty.

## Three major capabilities beyond the RegexShield demonstration

| Capability | Demonstrable implementation | Boundary |
|---|---|---|
| API policy compiler | ShieldLang lexer/parser/semantic analysis → SIR → six optimization passes → Python/JavaScript validators, plus VM execution | A restricted validation DSL, not a complete API security platform |
| Optimization verification | Independent bounded graph search checks scalar string fields before/after optimization; a detected difference blocks generation | Field acceptance only; other behaviors are explicitly outside the check |
| Policy change analysis | Compare versions, synthesize field differences, construct requests and replay both full VMs | Returned requests establish concrete verdict changes; no witness does not prove equivalence |

The last two are new in this development session. The underlying automata theory and translation-validation idea are established. Originality of this particular contribution still needs a focused literature comparison.

## Implemented since the audit

- Typed condition-register operands prevent literals such as `"t1"` from being interpreted as registers.
- Unresolved ambiguity, triple-product exhaustion and epsilon-path truncation are represented as incomplete analysis instead of an unsupported LOW safety claim in the covered cases.
- `$` includes Python's final-LF behavior; tests cover the previous block-scan evasion.
- Depth/key traversal is iterative and stops at its configured bound. Failed body limits stop validation before deeper operations.
- Unicode body-size checks use compact UTF-8 serialization; number coercion has a shared decimal grammar; JavaScript object paths require own properties.
- `verification.py` compares supported field languages using synchronized DFA states and a saturated length counter. It does not invoke the optimizer's product construction or length-subsumption logic.
- Compilation exposes coverage and stops generated output on a detected supported-field discrepancy (`V001`). Unsupported and budget-limited fields remain explicitly reported, rather than treated as proofs.
- `compare` CLI and `/api/compare` return field differences and replay-confirmed requests.
- Studio includes Verification, Policy changes, downloadable evidence, a comparison demo, and links into Run. Visible guarantees have been narrowed to their actual scope.

## What the new checker establishes

A supported field's language is the conjunction of presence, scalar string type, length limits, enum membership and all its pattern DFAs. The checker tracks one state per DFA plus input length, saturated beyond the largest relevant bound. It explores one representative per common alphabet class. After saturation, more input cannot change length predicates; the DFA states retain the remaining language behavior.

Exhausting the reachable graph without a discrepancy establishes field-language equality under these assumptions. A differing state supplies a shortest field counterexample. State/transition budgets produce an incomplete result. The checker shares the compiler's regex front end; it is not an independent proof of parsing semantics.

Whole-program behavior, conditions, scans, error codes/order, numeric/list fields and emitted backend code are outside this check. “Equivalent fields” must not be presented as “the complete compiler is verified.”

## What policy comparison establishes

Field differences seed candidate requests. The tool fills other required supported fields, prefers values common to both versions, and runs both complete VMs. Only requests whose final allowed/denied verdicts differ are listed as request witnesses.

Guards, scans, body limits, routing and unsupported fields can mask a candidate. Request construction is incomplete, and absence of a witness is never a whole-program equivalence claim. A full-request witness is concrete execution evidence, not an exhaustive proof about either backend.

## Evidence and reproduction

Run these commands from `C:\Users\Shadow\RegexShield\shieldlang`:

```text
python shieldlang.py test
python shieldlang.py compile examples/payments.shl --emit verification
python shieldlang.py compare examples/policy_before.shl examples/policy_after.shl
python tools/review2_evaluation.py --output docs/review2_evidence.json
```

`tests/test_review2.py` covers audited failures, supported/unsupported/inconclusive results, injected optimizer faults, the generation gate, policy comparison and Studio handler contracts. Existing tests also compare generated Python/JavaScript against the VM on bundled examples. Backend agreement alone is not a semantic proof.

The complete suite passed: **67 tests, 0 failures, 0 skips**, including Node.js checks. The captured run took 18.528 seconds in this environment; `Review2_Test_Log.txt` records the output.

`docs/review2_evidence.json` records four synthetic pilot examples, three deliberately injected faults and the account-policy comparison. All three selected faults were detected in this run. The demo produced three replay-confirmed requests: removal of `pro`, addition of `team`, and relaxation of the username length limit. This small pilot is not a large-corpus evaluation or a comparison against the state of the art.

The Studio workflow has now been verified in the actual browser, including field coverage, all three comparison witnesses, JSON export and the same request accepted after/rejected before the policy change. A narrow-layout overlap and the stale-server message were corrected. Desktop width 1366 and narrow width 615 showed no page-width overflow or editor/results overlap. See `Review2_Browser_QA.md` for the record. The frontend changes are functional integration, not the complete visual redesign discussed earlier.

## Alignment with the teacher's Review 2 rubric

| Criterion | Evidence to present | Remaining preparation |
|---|---|---|
| Prototype progress — 5 marks | The three capabilities, source pipeline, runnable examples, tests and pilot results | Rehearse the complete demo and confirm the teacher's interpretation of the 75% milestone |
| Novelty — 2 marks | A precise question about checking restricted policy transformations and exposing replayed changes | Compare closely related approaches; distinguish integration from a new algorithm |
| Documentation/progress — 1 mark | This report, grammar, demo guide and captured evidence | Add team-owned work records and teacher feedback |
| Presentation/Q&A — 2 marks | Explain one policy through the pipeline; show a counterexample and an unsupported result | Ensure each member can defend their actual work |

## Initial literature positioning

| Related work | Relevant overlap | Comparison needed |
|---|---|---|
| [OPA/Rego compilation](https://www.openpolicyagent.org/docs/wasm) | Policies compiled to executable artifacts | Scope, guarantees and integration; code generation alone is not novel |
| [Cedar](https://cedarpolicy.com/) | Policies designed for automated analysis | Authorization versus request validation; existing analysis capabilities |
| [VULCANBOOST, USENIX Security 2025](https://www.usenix.org/system/files/usenixsecurity25-li-yeting.pdf) | ReDoS repair with equivalence-oriented evaluation | Regex repair is prior work; the rewrite pipeline is not itself an established new invention |

This is a starting comparison, not a complete literature survey or patent search.

## Remaining work before the final assessment

1. Establish the precise contribution after a focused literature review. Treat a manuscript as a research target, not promised acceptance or patentability.
2. Expand the pilot to a documented evaluation set, relevant baselines, mutation families and ablations.
3. Address remaining backend boundaries: numeric representation/serialization, body scans and malformed request shapes. Unicode serialization and decimal-coercion regressions are covered; universal backend agreement is not proved.
4. Audit compilation resource budgets, including nested bounded repetitions. DFA matching guarantees do not bound compilation cost.
5. Finish the public introduction and browser-tested Studio workflow.
6. Maintain truthful individual contribution records and reproducible artifacts. A member should claim only work they can explain and support with evidence.
