# Browser smoke tests (development only)

These tests drive the three lab pages in a real Chromium through Playwright. **They are not part of ShieldLang's runtime and
ShieldLang itself stays standard-library only**; Playwright is a development dependency.

    pip install -r requirements-dev.txt
    python -m playwright install chromium
    python -m unittest tests.browser.test_smoke -v

If Playwright or a browser is missing, every test reports *skipped* with the reason (they are never silently counted as passing).
Set `SHIELDLANG_CHROMIUM=/path/to/chrome` to use an existing Chromium/Chrome executable.

The test starts the real `shieldlang.server.Handler` on an ephemeral loopback port with a throw-away data directory.
What the tests assert is listed in `test_smoke.py`; pixel appearance is *not* asserted (only geometry, visibility,
disabled/locked state, focus outline, overflow and behaviour of one flow per page).
