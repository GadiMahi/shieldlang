"""HTTP-backed regression tests for Bypass Lab search and reduction.

Keep the live project's existing tests. Run this module alongside them from the
ShieldLang project root after applying bypass-runtime-timeout.patch.
"""
import unittest
from unittest.mock import patch

from shieldlang.lab import bypass_search as search
from shieldlang.lab.bypass import run_fixture
from shieldlang.lab.labapp import LabApp


class RealLoopbackFixtureTests(unittest.TestCase):
    def test_nfkc_bypass_is_confirmed_over_http(self):
        out = run_fixture("E1-nfkc-fullwidth", "＜script＞", replay=True)
        self.assertTrue(out["confirmed_bypass"], out)
        self.assertEqual(out["replay"]["status"], 200)
        self.assertIn("<script", out["replay"]["stored"])

    def test_percent_encoded_traversal_is_confirmed_over_http(self):
        out = run_fixture("E2-percent-decode-path", "%2e%2e%2fsecret.txt", replay=True)
        self.assertTrue(out["confirmed_bypass"], out)
        self.assertEqual(out["replay"]["path"], "secret.txt")

    def test_duplicate_key_mismatch_is_confirmed_over_http(self):
        out = run_fixture("E3-duplicate-key-role", '{"role":"user","role":"admin"}', replay=True)
        self.assertTrue(out["confirmed_bypass"], out)
        self.assertEqual(out["replay"]["effective"], "admin")

    def test_nul_stripping_bypass_is_confirmed_over_http(self):
        out = run_fixture("E5-nul-stripping", "<scr\x00ipt>", replay=True)
        self.assertTrue(out["confirmed_bypass"], out)
        self.assertIn("<script", out["replay"]["stored"])

    def test_equivalent_pipeline_is_a_negative_control_over_http(self):
        out = run_fixture("E4-equivalent-pipelines", '{"role":"user","role":"admin"}', replay=True)
        self.assertFalse(out["confirmed_bypass"], out)
        self.assertEqual(out["replay"]["effective"], "admin")
        self.assertFalse(out["policy_would_accept"])

    def test_search_discovers_a_candidate_without_fixture_presets(self):
        out = search.search_fixture(
            "E1-nfkc-fullwidth", budget=60, max_depth=2, seed=0,
            max_findings=3, minimize=False, reduction_budget=1, time_budget_ms=8000,
        )
        preset_raw = {item["raw"] for item in search.BY_ID["E1-nfkc-fullwidth"].get("candidates", [])}
        self.assertTrue(out["findings"], out)
        for finding in out["findings"]:
            self.assertNotIn(finding["result"]["raw"], preset_raw)
            self.assertTrue(finding["result"]["confirmed_bypass"])

    def test_reduction_preserves_replay_confirmation(self):
        original = "＜script＞"
        out = search.minimize_fixture("E1-nfkc-fullwidth", original, budget=80, time_budget_ms=8000)
        self.assertLess(len(out["raw"]), len(original))
        self.assertTrue(out["result"]["confirmed_bypass"], out)
        self.assertIn("replay-confirmed", out["minimality"] if "minimality" in out else "")

    def test_one_server_is_reused_for_search_and_its_reducer(self):
        created = []
        real_lab_app = LabApp

        def tracked_app(*args, **kwargs):
            app = real_lab_app(*args, **kwargs)
            created.append(app)
            return app

        with patch.object(search, "LabApp", side_effect=tracked_app):
            out = search.search_fixture(
                "E1-nfkc-fullwidth", budget=60, max_depth=2, seed=0,
                max_findings=1, minimize=True, reduction_budget=12, time_budget_ms=8000,
            )
        self.assertTrue(out["findings"], out)
        self.assertEqual(len(created), 1, "search and reduction must share one loopback server")
        self.assertGreater(out["reduction_replay_count"], 0)
        self.assertLessEqual(out["replay_count"], out["evaluated"] + out["reduction_replay_count"])
        self.assertGreater(out["replay_count"], out["reduction_replay_count"])
        self.assertEqual(out["reduction_evaluations"], out["reduction_replay_count"])


if __name__ == "__main__":
    unittest.main()
