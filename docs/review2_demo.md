# Review 2 demonstration and Q&A

## Current presentation sequence

1. Start `python shieldlang.py serve --port 8090`. Open `/` for the introduction,
   then `/studio` to show a rule passing through AST, diagnostics, SIR and codegen.
2. Open `/protocols`, load `orders.shl` and create a Checkout session. Demonstrate
   a legal create/pay/confirm sequence, a confirm-before-pay refusal and a mismatched
   card-token refusal. Load `orders_unbound.shl` in a new session to show the
   mismatched token accepted without the explicit binding. Explain ownership,
   reservation, send intent and Unknown/reconciliation from the timeline.
3. Open `/traffic`. Select a supported order policy and a bundled recording. Run
   observe and enforce modes and compare decisions with the backend ledger. Check
   an invalid recording, then import and replay a valid bounded recording.
4. Open `/lab/bypasses`. Search the NFKC or NUL-stripping fixture, inspect the
   raw/policy/app interpretations and local replay, then minimize a finding. Show
   the equivalent-pipeline negative control. Follow the Traffic Lab evidence link
   and explain that it displays the original fixture evidence, not a gateway replay.
5. Return to Studio for supported-field verification and policy comparison if time
   permits. State the supported scope and demonstrate an incomplete/unsupported case.
6. Close with documented test coverage, known limits, team contributions and the
   evaluation work still needed to establish a manuscript contribution.

Rehearse locally with fresh sessions and a restarted server. The fixture predicates
in Bypass Lab are not `.shl` policies; protocol binding, traffic replay and compiler
verification provide different kinds of evidence. Explain those distinctions.

## Earlier compiler-focused presentation (additional material)


## Opening statement

“Review 1 analysed individual regexes. ShieldLang now compiles API validation policies into Python and JavaScript. This review adds checks for selected compiler transformations and shows concrete requests whose verdict changes between policy versions.”

## Demonstration sequence

1. Run `python shieldlang.py` and open the printed local URL. Use `payments.shl` to show a policy, diagnostics, SIR and generated Python/JavaScript. Explain how field checks become instructions and regex checks become DFA tables.
2. Open **Verification**. Show an equivalent string field and the unsupported numeric/list fields. Explain that the checker is narrower than the compiler and does not verify whole-program behavior.
3. Open **Optimiser** to show the passes, then return to Verification. Explain that graph search checks the resulting field languages rather than trusting a pass's own explanation.
4. Open **Policy changes → Load comparison demo → Compare with main editor**. The baseline has usernames of length 3–8 and tiers basic/pro. The new policy allows length 3–12 and tiers basic/team.
5. Show the three requests: `pro` loses access, `team` gains access, and a nine-character username becomes accepted with the common tier `basic`. Each displayed request was run against both complete VMs.
6. Use **Open request in Run**, then validate against the current policy. Open the baseline source separately or use the CLI comparison to explain the previous verdict.
7. Run `python shieldlang.py test`. Show the mutation tests and a deliberate budget-exhaustion test: incomplete search must not be called equivalent.
8. Close with the pilot evaluation and the explicit final-review work. Do not claim complete safety, universal equivalence, or proven novelty.

The Studio workflow has been exercised in a browser: verification coverage, all three comparison requests, JSON export and request replay under both policy versions. A narrow-layout overlap was corrected. Rehearse locally before presenting; use a fresh port if an older server is still responding on the default port.

## CLI fallback

```text
python shieldlang.py compile examples/payments.shl --emit verification
python shieldlang.py compare examples/policy_before.shl examples/policy_after.shl
python tools/review2_evaluation.py --output docs/review2_evidence.json
```

## Questions to prepare

**What makes this a compiler rather than a regex wrapper?**
The DSL has a lexer, AST, semantic checks, an intermediate representation, transformations and two code generators. Regex analysis is one sub-compiler.

**What is independently checked?**
Acceptance for supported scalar string fields before and after optimization. The checker traverses synchronized DFA states and a bounded/saturated length state. It does not reuse the optimizer's product or length-elimination algorithm, but it shares the regex front end.

**Does equivalent mean the complete validator is correct?**
No. Error behavior, guards, scans, numeric/list fields and generated code are outside that check. The UI reports this boundary.

**Why not just test the VM against generated code?**
They can share the same lowering bug. Regression tests also assert independently specified expected behavior, and the optimization checker examines a separate mathematical representation of supported field acceptance.

**What if the search is too expensive?**
There are state and transition budgets. The result is inconclusive. Unsupported or incomplete analysis is never described as an equality proof.

**What does a policy-change witness establish?**
A concrete request receives different final verdicts when executed on the two policy VMs. Absence of a witness does not establish equality, and the witness is not a proof that the backends are universally correct.

**Is DFA matching a new invention?**
No. The research direction is how a restricted policy compiler exposes transformation checks and actionable change examples. The originality claim still needs literature comparison and evaluation.

**What exactly remains?**
Broader evaluation, baseline comparisons, stronger handling of numeric/backend boundaries, compiler resource limits and the final frontend presentation.

## Individual contribution record

Each member should fill their own evidence. Do not attribute generated or pre-existing work to a member without a truthful record.

| Member | Component personally understood/implemented | Files/commits | Test or demo personally explained | Remaining responsibility |
|---|---|---|---|---|
| To complete | | | | |
| To complete | | | | |
| To complete | | | | |
