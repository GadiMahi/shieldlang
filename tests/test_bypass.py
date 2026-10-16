"""Bypass Lab: bounded fixture demonstration only (no search/reducer in this build). Verifies the independent oracle,
the two interpretations, and local replay against the loopback LabApp agree for each hand-authored candidate."""
import unittest

from shieldlang.lab.bypass import run_fixture
from shieldlang.lab.bypass_fixtures import BY_ID, FIXTURES
from shieldlang.lab.labapp import LabApp
from shieldlang.lab import rawjson


class FixtureCoverage(unittest.TestCase):
    def test_every_fixture_has_manual_candidates_and_a_label(self):
        for fx in FIXTURES:
            self.assertTrue(fx["candidates"], fx["id"])
            self.assertIn("label", fx)
            self.assertIn("Hand-authored", fx["label"])

    def test_E1_fullwidth_confirmed_plain_ascii_not(self):
        r = run_fixture("E1-nfkc-fullwidth", "＜script＞")
        self.assertTrue(r["confirmed_bypass"])
        self.assertEqual(r["app_interpretation"], "<script>")
        r2 = run_fixture("E1-nfkc-fullwidth", "<script>")
        self.assertFalse(r2["confirmed_bypass"])
        self.assertFalse(r2["policy_would_accept"])  # the policy's OWN view already rejects it: nothing to bypass

    def test_E2_single_decode_confirmed_double_decode_not(self):
        r = run_fixture("E2-percent-decode-path", "%2e%2e%2fsecret.txt")
        self.assertTrue(r["confirmed_bypass"])
        self.assertEqual(r["replay"]["path"], "secret.txt")
        r2 = run_fixture("E2-percent-decode-path", "%252e%252e%252fsecret.txt")
        self.assertFalse(r2["confirmed_bypass"])
        r3 = run_fixture("E2-percent-decode-path", "report.txt")
        self.assertFalse(r3["confirmed_bypass"])
        self.assertFalse(r3["oracle_violation_on_app_view"])

    def test_E3_duplicate_key_mismatch_confirmed(self):
        r = run_fixture("E3-duplicate-key-role", '{"role":"user","role":"admin"}')
        self.assertTrue(r["confirmed_bypass"])
        self.assertEqual(r["policy_interpretation"], "user")
        self.assertEqual(r["app_interpretation"], "admin")
        self.assertEqual(r["replay"]["effective"], "admin")

    def test_E4_equivalent_pipelines_yield_no_confirmed_bypass(self):
        """Exhaustion/agreement means 'no witness found here', not a safety proof; this fixture only shows the two
        declared pipelines agreeing on this one candidate."""
        r = run_fixture("E4-equivalent-pipelines", '{"role":"user","role":"admin"}')
        self.assertFalse(r["confirmed_bypass"])
        self.assertEqual(r["policy_interpretation"], r["app_interpretation"])

    def test_E5_nul_stripping_confirmed(self):
        r = run_fixture("E5-nul-stripping", "<scr\x00ipt>")
        self.assertTrue(r["confirmed_bypass"])
        self.assertEqual(r["app_interpretation"], "<script>")

    def test_unknown_fixture_raises(self):
        with self.assertRaises(KeyError):
            run_fixture("no-such-fixture", "x")

    def test_without_replay_skips_the_http_app(self):
        r = run_fixture("E1-nfkc-fullwidth", "＜script＞", replay=False)
        self.assertTrue(r["replay"]["skipped"])
        self.assertFalse(r["confirmed_bypass"])  # an unconfirmed claim is never reported as a bypass


class RawJsonAdapter(unittest.TestCase):
    def test_duplicate_count_and_policies(self):
        t = '{"a":1,"role":"x","role":"y","role":"z"}'
        self.assertEqual(rawjson.lookup(t, "role", "first"), ("x", 3))
        self.assertEqual(rawjson.lookup(t, "role", "last"), ("z", 3))
        with self.assertRaises(rawjson.RawJsonError):
            rawjson.lookup(t, "role", "reject")

    def test_escaped_key_matches_decoded_name(self):
        t = '{"r\\u006fle":"admin"}'
        self.assertEqual(rawjson.lookup(t, "role", "last"), ("admin", 1))


class LabAppLoopback(unittest.TestCase):
    def test_binds_loopback_only(self):
        with self.assertRaises(ValueError):
            LabApp([], host="0.0.0.0")

    def test_in_bounds_file_served_traversal_blocked_when_app_does_not_decode(self):
        app = LabApp([]).start()   # no percent_decode stage: the server sees the literal %2e%2e%2f text
        try:
            import http.client
            c = http.client.HTTPConnection("127.0.0.1", app.port)
            c.request("GET", "/file?name=report.txt")
            self.assertEqual(c.getresponse().status, 200)
            c = http.client.HTTPConnection("127.0.0.1", app.port)
            c.request("GET", "/file?name=%2e%2e%2fsecret.txt")
            self.assertEqual(c.getresponse().status, 404)
        finally:
            app.stop()


if __name__ == "__main__":
    unittest.main()
