# ShieldLang guided demo

Built 16 October 2026 inside `C:\Users\Shadow\RegexShield\shieldlang`.

## Two frontends, one compiler

- `http://127.0.0.1:8090/` — new presentation and guided demo.
- `http://127.0.0.1:8090/studio` — the original workbench, preserved.

The default server uses port 8080; the currently verified user-started instance uses 8090. Run `python shieldlang.py serve --port 8090` from the project if it needs restarting.

## What was added

The new frontend introduces ShieldLang and guides visitors through **Write → Compile → Verify → Compare → Replay & export**. It uses the existing `/api/examples`, `/api/compile`, `/api/compare` and `/api/run` endpoints. No computed compiler results or request verdicts are mocked.

The homepage's pipeline artwork is a labeled illustration. Its role is explanation, not an actual generated automaton. The page uses local assets and font fallbacks, with reduced-motion support.

**Open this policy in Studio** transfers the current policy, earlier baseline, relevant view and valid request JSON into Studio. The transfer is browser-session-local and consumed once.

## Functionalities to describe in Review 2

1. **Policy compiler:** the DSL, semantic checks, SIR, optimizer, VM and Python/JavaScript code generation. This already existed in the reviewed ShieldLang project and is a major extension beyond the original RegexShield demonstration.
2. **Optimization verification:** a new bounded comparison of supported scalar string-field acceptance before/after optimization, with explicit unsupported/incomplete results and generation blocked on a detected discrepancy.
3. **Policy comparison:** new field counterexamples and candidate requests, with displayed request witnesses confirmed by full-VM replay on both versions.

The guided frontend demonstrates these capabilities. It is not itself a new compiler algorithm or an established research novelty claim.

## Verification completed

- Complete Python suite: **68 tests passed**, including the distinct demo/Studio route test and existing generated-backend tests. Captured in `Demo_Test_Log.txt`.
- Both JavaScript entry points passed `node --check`; Git diff whitespace checks passed.
- Actual browser at `/` compiled the bundled example from **12 to 11 IR instructions**, reporting DFA intersection and check reordering.
- Its verification step showed **2 equivalent supported fields**, with explicit exclusions.
- Policy comparison returned **3 replay-confirmed request examples**.
- Paired replay of username `aaaaaaaaa`, tier `basic` showed **denied before / allowed after**, with actual instruction and DFA-step counts.
- Studio transfer preserved the source with both patterns, the earlier baseline and the selected request. The request was then validated successfully in the original workbench.
- Invalid source produced compiler errors; malformed request JSON produced a visible error.
- Editing source cleared previous verification and disabled generated-validator exports; editing the baseline removed old comparison summaries and field details.
- Python and JavaScript validators were downloaded through the browser and executed. Both accepted the nine-character basic-tier request and rejected the thirteen-character and removed pro-tier requests.
- Desktop introduction inspected at width **1366**. Phone introduction checked at width **390**, with no horizontal page overflow. Temporary viewport settings were reset after testing.

## Relevant implementation files

- `web/demo.html`, `web/demo.css`, `web/demo.js` — separate guided frontend.
- `web/index.html`, `web/app.js` — preserved Studio with demo navigation and policy transfer support.
- `shieldlang/server.py` — separate demo and Studio routes, existing API behavior retained.
- `examples/policy_after.shl` — compatible second pattern makes DFA intersection visible in the presentation.
- `docs/guided_demo.md` — usage and scope.

## Remaining boundaries

This is a local, working demonstration. Nothing was published to external hosting. Backend correctness limits from the Review 2 report still apply: field verification is not whole-program or generated-code verification, and failure to find a policy-change witness is not equivalence. Testing covered the listed flows and viewports rather than every browser, device or accessibility condition.
