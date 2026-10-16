"""Browser smoke tests for Protocol Studio / Traffic Lab / Bypass Lab (development-only; needs Playwright + Chromium).

Skipped, not passed, when Playwright or a browser is unavailable.
"""
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

os.environ.setdefault("SHIELDLANG_DATA", tempfile.mkdtemp(prefix="shieldlang-browser-test-"))

try:
    from playwright.sync_api import sync_playwright
    _IMPORT_ERR = None
except Exception as exc:  # pragma: no cover - depends on the environment
    sync_playwright = None
    _IMPORT_ERR = f"playwright not importable: {exc}"

from shieldlang.server import Handler

DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 375, "height": 812}


class BrowserCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest(_IMPORT_ERR)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        try:
            exe = os.environ.get("SHIELDLANG_CHROMIUM")
            cls.browser = cls.pw.chromium.launch(executable_path=exe) if exe else cls.pw.chromium.launch()
        except Exception as exc:
            cls.pw.stop(); cls.httpd.shutdown(); cls.httpd.server_close()
            raise unittest.SkipTest(f"no usable Chromium: {exc}")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close(); cls.pw.stop()
        if getattr(cls, "httpd", None):
            cls.httpd.shutdown(); cls.httpd.server_close()

    def open(self, path, viewport=DESKTOP):
        ctx = self.browser.new_context(viewport=viewport)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: self.errors.append(f"console: {m.text}") if m.type == "error" else None)
        page.goto(self.base + path)
        page.wait_for_load_state("networkidle")
        return page

    def assertNoJsErrors(self):
        self.assertEqual(self.errors, [], "JavaScript errors on the page")


class CommonLayout(BrowserCase):
    PAGES = ["/protocols", "/traffic", "/lab/bypasses"]

    def test_header_visible_and_page_scrolls(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                page = self.open(path)
                top = page.locator("header.lab-topbar").bounding_box()
                self.assertEqual(round(top["y"]), 0)
                self.assertGreater(top["height"], 30)
                overflow = page.evaluate("getComputedStyle(document.body).overflow")
                self.assertNotEqual(overflow, "hidden", "body must not lock scrolling (app.css regression)")
                page.evaluate("window.scrollTo(0, 600)")
                self.assertGreater(page.evaluate("window.scrollY"), 100, "page did not scroll")
                self.assertNoJsErrors()

    def test_no_horizontal_overflow_at_375px(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                page = self.open(path, PHONE)
                sw, cw = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
                self.assertLessEqual(sw, cw, f"horizontal overflow {sw}>{cw}")

    def test_keyboard_focus_is_visible_everywhere(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                page = self.open(path)
                seen = 0
                for _ in range(14):
                    page.keyboard.press("Tab")
                    info = page.evaluate("""() => { const e = document.activeElement; if (!e || e === document.body) return null;
                        const cs = getComputedStyle(e); const r = e.getBoundingClientRect();
                        return {tag: e.tagName, id: e.id, outline: cs.outlineStyle, w: parseFloat(cs.outlineWidth), shadow: cs.boxShadow, vis: r.width > 0 && r.height > 0}; }""")
                    if info and info["vis"]:
                        seen += 1
                        has_ring = (info["outline"] not in ("none", "hidden") and info["w"] > 0) or info["shadow"] != "none"
                        self.assertTrue(has_ring, f"no visible focus indicator on {info}")
                self.assertGreaterEqual(seen, 8, "keyboard focus never reached enough controls")


class ProtocolStudioSmoke(BrowserCase):
    def test_editor_is_a_contained_visible_textarea(self):
        page = self.open("/protocols")
        ed = page.locator("#proto-source")
        card = page.locator("#source-card")
        self.assertTrue(ed.is_visible())
        eb, cb = ed.bounding_box(), card.bounding_box()
        self.assertGreaterEqual(eb["x"], cb["x"] - 1); self.assertLessEqual(eb["x"] + eb["width"], cb["x"] + cb["width"] + 1)
        self.assertGreaterEqual(eb["height"], 200)
        self.assertNotEqual(ed.evaluate("e => getComputedStyle(e).position"), "absolute")
        self.assertIn("protocol Checkout", ed.input_value())
        self.assertNotEqual(ed.evaluate("e => getComputedStyle(e).color"), "rgba(0, 0, 0, 0)", "text must not be transparent")
        self.assertNoJsErrors()

    def test_steps_are_locked_until_a_session_exists(self):
        page = self.open("/protocols")
        for card, fs in (("#request-card", "#request-fs"), ("#recovery-card", "#recovery-fs")):
            self.assertIn("locked", page.locator(card).get_attribute("class"))
            self.assertTrue(page.locator(fs).evaluate("e => e.disabled"), f"{fs} must be a disabled fieldset")
        self.assertTrue(page.locator("#btn-send").is_disabled())
        self.assertTrue(page.locator("#btn-save-evidence").is_disabled())
        for _ in range(60):  # keyboard can never land on a locked control
            page.keyboard.press("Tab")
            self.assertNotIn(page.evaluate("document.activeElement.id"), ("btn-send", "btn-reconcile", "btn-restart", "req-path"))

    def test_start_session_create_and_unknown_flow(self):
        page = self.open("/protocols")
        page.locator("#session-area .js-start").click()
        page.wait_for_selector("#request-fs:not([disabled])")
        self.assertNotIn("locked", page.locator("#request-card").get_attribute("class"))
        page.get_by_role("button", name="1 · Create an order").click()
        page.locator("#btn-send").click()
        page.wait_for_selector("#request-result .lab-explain")
        self.assertIn("Allowed", page.locator("#request-result .lab-explain").inner_text())
        self.assertEqual(page.locator("#ledger-area tbody tr").count(), 1)
        self.assertIn("ord_1", page.locator("#resources-area").inner_text())
        # the backend applies a payment but the response is lost -> Unknown, order frozen
        page.get_by_role("button", name="Pay — response lost").click()
        page.locator("#btn-send").click()
        page.wait_for_function("document.querySelector('#request-result .lab-explain .what').textContent.includes('UNKNOWN')")
        self.assertIn("unknown", page.locator("#resources-area").inner_text().lower())
        self.assertEqual(page.locator("#ledger-area tbody tr").count(), 2, "the backend ledger shows the payment that really happened")
        self.assertNoJsErrors()

    def test_editing_source_invalidates_the_check(self):
        page = self.open("/protocols")
        self.assertIn("checked, no errors", page.locator("#status-strip").inner_text())
        page.locator("#proto-source").press("End"); page.locator("#proto-source").type(" ")
        self.assertIn("edited since last check", page.locator("#status-strip").inner_text())
        self.assertTrue(page.locator("#session-area .js-start").is_disabled())

    def test_diagnostic_rows_jump_the_editor_to_their_line(self):
        page = self.open("/protocols")
        bad = 'rule Transfer on POST "/api/transfer" {\n  field amount : number in 5000..100;\n  field pin : string matches /^[a-z]+$/ matches /^[0-9]+$/;\n}\n'
        page.fill("#proto-source", bad)
        page.locator("#btn-compile").click()
        page.wait_for_selector("#diag-area table tbody tr")
        self.assertGreater(page.locator("#diag-area table tbody tr").count(), 0)
        page.locator("#diag-area table tbody tr").first.locator("button.lab-linecell").click()
        sel = page.evaluate(
            "() => { const ta = document.getElementById('proto-source'); "
            "return ta.selectionEnd > ta.selectionStart; }"
        )
        self.assertTrue(sel, "clicking a diagnostic's line number must select that line in the editor")
        self.assertTrue(page.eval_on_selector("#proto-source", "e => e.classList.contains('lab-flash')"))
        self.assertNoJsErrors()

    def test_second_protocol_example_demonstrates_the_missing_binding(self):
        # orders_unbound.shl is the same rules/backend as orders.shl, minus the Checkout protocol's
        # `bind` line -- this is what that one missing line costs, run for real against the backend.
        page = self.open("/protocols")
        page.select_option("#example-select", "orders_unbound.shl")
        self.assertEqual(page.eval_on_selector_all("#protocol-select option", "els => els.map(e => e.value)"), ["CheckoutUnbound"])
        page.locator("#session-area .js-start").click()
        page.wait_for_selector("#request-fs:not([disabled])")
        page.get_by_role("button", name="1 · Create an order").click()
        page.locator("#btn-send").click()
        page.wait_for_selector("#request-result .lab-explain")
        page.get_by_role("button", name="2 · Pay it (card tok_a)").click()
        page.locator("#btn-send").click()
        page.wait_for_selector("#request-result .lab-explain")
        page.get_by_role("button", name="3 · Confirm with a DIFFERENT card (tok_b)").click()
        page.locator("#btn-send").click()
        page.wait_for_selector("#request-result .lab-explain")
        # Checkout refuses this (binding_mismatch); CheckoutUnbound allows it -- that contrast is the point.
        self.assertIn("Allowed", page.locator("#request-result .lab-explain").inner_text())
        self.assertNoJsErrors()


class TrafficLabSmoke(BrowserCase):
    def test_run_stateless_recording_in_both_modes(self):
        page = self.open("/traffic")
        page.evaluate("""() => { const s = document.getElementById('recording-select'); const o = [...s.options].find(o => /stateless_demo/.test(o.value)); s.value = o.value; }""")
        page.select_option("#protocol-select", "")
        page.locator("#btn-run").click()
        page.wait_for_selector("#entries-area tbody tr")
        self.assertEqual(page.locator("#entries-area tbody tr").count(), 3)
        self.assertIn("Enforce", page.locator("#mode-explain").inner_text())
        self.assertTrue(page.locator("#eval-area .lab-matrix").is_visible())
        page.select_option("#mode-select", "observe")
        self.assertIn("Observe-only", page.locator("#mode-explain").inner_text())
        page.locator("#btn-run").click()
        page.wait_for_function("document.querySelector('#entries-area').innerText.includes('FORWARDED')")
        self.assertNoJsErrors()

    def test_policy_select_and_recording_import_flow(self):
        page = self.open("/traffic")
        page.wait_for_selector("#policy-select option", state="attached")
        page.select_option("#policy-select", "orders")
        self.assertIn("rule(s) match the demo backend", page.locator("#policy-hint").inner_text())
        page.locator("#import-area summary").click()
        bad = '{"entries":[{"id":"a","method":"PUT","path":"orders"}]}'
        page.fill("#import-text", bad)
        page.locator("#btn-check-recording").click()
        page.wait_for_selector("#import-result table tbody tr")
        self.assertGreater(page.locator("#import-result table tbody tr").count(), 0)
        good = ('{"name":"smoke_flow","entries":['
                '{"id":"c1","identity":"alice","method":"POST","path":"/api/orders",'
                '"body":{"client_ref":"smoke1","item":"pen"},"save_as":"o","label":{"legitimate":true}},'
                '{"id":"c2","identity":"alice","method":"POST","path":"/api/orders/{{o}}/pay",'
                '"body":{"card_token":"tok_a"},"label":{"legitimate":true}}]}')
        page.fill("#import-text", good)
        page.locator("#btn-import-recording").click()
        page.wait_for_selector("#import-result >> text=Imported as")
        self.assertIn("smoke_flow", page.locator("#imported-list").inner_text())
        page.evaluate(
            "() => { const s = document.getElementById('recording-select'); "
            "const o = [...s.options].find(o => /smoke_flow/.test(o.textContent)); s.value = o.value; }"
        )
        page.locator("#btn-run").click()
        page.wait_for_selector("#entries-area tbody tr")
        self.assertEqual(page.locator("#entries-area tbody tr").count(), 2)
        self.assertNoJsErrors()


class BypassLabSmoke(BrowserCase):
    def test_fixture_candidate_evaluation_and_negative_control(self):
        page = self.open("/lab/bypasses")
        page.wait_for_selector("#fixture-select option", state="attached")
        page.select_option("#fixture-select", "E2-percent-decode-path")
        page.locator("#candidate-presets button").first.click()
        page.locator("#btn-run").click()
        page.wait_for_selector("#result-area .lab-explain")
        self.assertIn("Confirmed for this fixture", page.locator("#result-area").inner_text())
        page.select_option("#fixture-select", "E4-equivalent-pipelines")
        page.locator("#candidate-presets button").first.click()
        page.locator("#btn-run").click()
        # Wait for the fresh run's own verdict text, not merely for the old "Confirmed" text to
        # disappear -- selecting a new fixture clears the stale result ("Experiment input changed...")
        # well before the new run's fetch resolves, and that intermediate text also satisfies a
        # naive "no longer confirmed" check, causing a race if we only wait for that.
        page.wait_for_function(
            "document.querySelector('#result-area').innerText.toLowerCase().includes('not confirmed')"
        )
        self.assertIn("not confirmed", page.locator("#result-area").inner_text().lower())
        self.assertIn("only the selected fixture's declared transformations", page.locator("main").inner_text().lower())
        self.assertNoJsErrors()

    def test_handoff_link_opens_a_readonly_evidence_panel_in_traffic_lab(self):
        page = self.open("/lab/bypasses")
        page.wait_for_selector("#fixture-select option", state="attached")
        page.select_option("#fixture-select", "E1-nfkc-fullwidth")
        page.locator("#candidate-presets button").first.click()
        page.locator("#btn-run").click()
        page.wait_for_selector("#result-area a:has-text('View this evidence in Traffic Lab')")
        href = page.locator("#result-area a:has-text('View this evidence in Traffic Lab')").get_attribute("href")
        page.goto(self.base + href)
        page.wait_for_selector("#bypass-handoff-card:not([hidden])")
        text = page.locator("#bypass-handoff-area").inner_text().lower()
        self.assertIn("not re-run through this page's gateway", text)
        self.assertNoJsErrors()


if __name__ == "__main__":
    unittest.main()
