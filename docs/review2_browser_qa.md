# Studio browser verification

Verified 16 October 2026 against the updated server at `http://127.0.0.1:8090/`.

## Confirmed in the actual browser

- Payments compiled with a Verification phase. The verification view showed **9 equivalent string fields, 0 differences, 0 inconclusive checks and 4 unsupported fields**, with scope exclusions visible.
- Policy changes loaded the bundled before/after account policies and displayed **3 replay-confirmed request witnesses**.
- The nine-character username `aaaaaaaaa` with tier `basic` was opened in Run and allowed under `policy_after.shl`.
- The identical JSON request was replayed in Run under `policy_before.shl` and rejected with the expected username-length violation.
- Download comparison exported JSON with status `behavior_change_found` and three replayed witnesses. A copy is saved as `Review2_Browser_Comparison.json`.
- Desktop layout at width **1366** had no horizontal page overflow or overlap between the editor and results columns.
- After returning to the normal browser size, the layout at width **615** had no horizontal page overflow; the results section began below the editor/diagnostics section, with no overlap. The temporary viewport override was reset.

## Issues found and corrected

1. An older server was still responding on port 8080 while serving the updated frontend files. The verification view incorrectly said compilation had stopped with zero errors. It now clearly explains that the server needs restarting when a successful response lacks verification support. The updated backend was verified on a fresh port.
2. In narrow windows, the grid/flex layout let the editor overlap the results and intercept tab clicks. The narrow breakpoint now uses a naturally flowing column layout with a fixed-height editor and nonshrinking result sections. Tab navigation and the complete comparison/replay flow worked after the correction.
3. External font stylesheets were removed from the page head so the local Studio does not depend on Google Fonts availability. Existing local font fallbacks remain in the stylesheet.

The latest frontend passed JavaScript syntax and diff-whitespace checks. The previous complete backend/regression run remains **67 passed**; these subsequent changes were confined to frontend layout, messages, font loading and documentation.

This verifies the exercised Review 2 workflow, not all browser/device combinations, accessibility requirements, compiler resource limits, or whole-program correctness. The larger visual redesign remains separate work.
