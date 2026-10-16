# Guided demo and original Studio

The presentation is a separate frontend backed by the existing compiler APIs. The original workbench remains available.

| URL | Purpose |
|---|---|
| `/` or `/demo` | Introduction and guided walkthrough |
| `/studio` | Original ShieldLang Studio |
| `/static/demo.html` | Direct presentation asset, also useful during development |
| `/static/index.html` | Direct original Studio asset |

Run `python shieldlang.py serve --port 8090` and open the printed demo URL. Server routing changes require restarting a previously running Python server. Static HTML/CSS/JS edits are loaded on browser refresh.

## Five steps

1. **Write a policy.** Edit the account policy loaded from the bundled examples. The earlier policy remains available for comparison.
2. **Compile.** Call `/api/compile` and inspect actual phase timings, instructions, transformations and optional SIR output. The example's two compatible patterns are intersected into a single scan (12 instructions before, 11 after).
3. **Verify.** Display the compiler's string-field verification results, state counts and explicit exclusions. The default example has two equivalent supported fields.
4. **Compare.** Call `/api/compare` on the editable baseline and current policy. The bundled pair yields three request witnesses, with each request verified by execution against both policy VMs.
5. **Replay and export.** Edit/select request JSON and call `/api/run` for each version. Show actual before/after verdicts, instruction counts, DFA steps and expandable execution evidence. Export generated Python/JavaScript after a successful compilation of the current source, or download the session evidence.

The account endpoint is illustrative. All compiler results, comparisons and replay verdicts are obtained from the live backend. The homepage's pipeline artwork is explicitly an illustration, not a generated automaton or a performance measurement.

## Transfer to Studio

The walkthrough's **Open this policy in Studio** link transfers the source, baseline, relevant view, and valid request JSON through same-origin session storage. Studio consumes the transfer once. This does not create an account, publish a policy or send source to an external service.

Editing a policy invalidates its previous compilation, verification, comparison and replay. Generated validator export stays disabled until the edited source compiles. Editing the baseline clears old comparison evidence. Invalid policies and malformed request JSON display errors; previous verdicts are not reused as current results.

## Presentation and accessibility

The demo uses the existing cream/ink/blue identity, local font fallbacks, code-native pipeline artwork and a distinct explanatory layout. It is buildless and has no external media or font dependency. Narrow layouts stack naturally; the walkthrough tabs scroll horizontally when needed. Tabs support arrow keys, Home and End, and the page supplies focus styles, a skip link and reduced-motion support.

## Scope

The presentation does not expand the compiler's guarantees. Field equivalence is restricted to supported scalar strings and their declared constraints; conditions, scans, error behavior, numeric/list fields, full-program behavior and emitted code remain outside that check. Missing policy-change witnesses do not prove equivalence. A manuscript or patent contribution is not established by this frontend.
