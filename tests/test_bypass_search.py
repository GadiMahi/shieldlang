"""Independent expected outcomes for bounded local search and reduction."""
import copy
import unittest
from unittest.mock import patch

from shieldlang.lab.bypass import run_fixture
from shieldlang.lab.bypass_fixtures import BY_ID
from shieldlang.lab.bypass_search import search_fixture, minimize_fixture, fixture_version


class SearchExperiments(unittest.TestCase):
    def test_search_discovers_without_reading_preset_candidates(self):
        for fid in ("E1-nfkc-fullwidth", "E2-percent-decode-path", "E3-duplicate-key-role", "E5-nul-stripping"):
            fx = copy.deepcopy(BY_ID[fid])
            fx["candidates"] = []
            with self.subTest(fixture=fid), patch.dict(BY_ID, {fid: fx}):
                result = search_fixture(fid, max_findings=1, minimize=False)
                self.assertEqual(len(result["findings"]), 1)
                finding = result["findings"][0]
                self.assertTrue(finding["result"]["confirmed_bypass"])
                self.assertTrue(finding["provenance"])
                self.assertTrue(run_fixture(fid, finding["raw"])["confirmed_bypass"])

    def test_identical_pipelines_do_not_report_findings(self):
        out = search_fixture("E4-equivalent-pipelines", budget=32)
        self.assertEqual(out["findings"], [])
        self.assertEqual(out["replay_count"], 0)
        self.assertIn("not a safety proof", out["scope_note"])

    def test_seed_reproduces_order_and_findings(self):
        args = {"seed": 91, "max_findings": 2, "minimize": False}
        a = search_fixture("E5-nul-stripping", **args)
        b = search_fixture("E5-nul-stripping", **args)
        self.assertEqual(a["findings"], b["findings"])
        self.assertEqual(a["evaluated"], b["evaluated"])

    def test_budget_and_depth_are_reported_honestly(self):
        out = search_fixture("E1-nfkc-fullwidth", budget=2, max_depth=0)
        self.assertEqual(out["findings"], [])
        self.assertEqual(out["evaluated"], 2)
        self.assertEqual(out["stop_reason"], "space_exhausted")
        out = search_fixture("E1-nfkc-fullwidth", seeds=["<script"], budget=1)
        self.assertEqual(out["evaluated"], 1)
        self.assertEqual(out["stop_reason"], "candidate_budget")

    def test_custom_seed_is_evaluated_not_replaced_by_fixture_presets(self):
        out = search_fixture("E1-nfkc-fullwidth", seeds=["ordinary text"], budget=16)
        self.assertEqual(out["findings"], [])
        self.assertEqual(out["seeds"], ["ordinary text"])

    def test_search_and_reducer_reject_invalid_budgets_and_inputs(self):
        for options in ({"budget": True}, {"budget": 1025}, {"max_depth": 4}, {"time_budget_ms": 0},
                        {"seed": -1}, {"seeds": "text"}, {"seeds": []}, {"minimize": "yes"},
                        {"seeds": ["x" * 2049]}, {"seeds": ["\ud800"]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                search_fixture("E1-nfkc-fullwidth", **options)

    def test_fixture_snapshot_hash_changes_with_semantics(self):
        fid = "E1-nfkc-fullwidth"
        before = fixture_version(fid)
        fx = copy.deepcopy(BY_ID[fid])
        fx["app_stages"] = []
        with patch.dict(BY_ID, {fid: fx}):
            self.assertNotEqual(before, fixture_version(fid))


class ReductionExperiments(unittest.TestCase):
    def test_reduction_keeps_effect_and_removes_irrelevant_text(self):
        out = minimize_fixture("E5-nul-stripping", "irrelevant <scr\x00ipt> suffix", budget=100)
        self.assertTrue(out["result"]["confirmed_bypass"])
        self.assertLess(out["reduced_bytes"], out["original_bytes"])
        self.assertEqual(out["result"]["app_interpretation"], "<script")
        self.assertEqual(out["stop_reason"], "deletion_fixed_point")
        self.assertTrue(run_fixture("E5-nul-stripping", out["raw"])["confirmed_bypass"])

    def test_duplicate_member_order_and_json_syntax_are_preserved(self):
        out = minimize_fixture("E3-duplicate-key-role", '{ "extra": 123, "role":"guest", "role":"admin" }', budget=150)
        self.assertTrue(out["result"]["confirmed_bypass"])
        self.assertEqual(out["result"]["app_interpretation"], "admin")
        self.assertNotEqual(out["result"]["policy_interpretation"], "admin")

    def test_budget_exhaustion_does_not_claim_minimality(self):
        out = minimize_fixture("E5-nul-stripping", "padding <scr\x00ipt> suffix", budget=1)
        self.assertLessEqual(out["evaluations"], 1)
        self.assertEqual(out["stop_reason"], "evaluation_budget")
        self.assertIn("incomplete", out["minimality"])
        self.assertTrue(out["result"]["confirmed_bypass"])

    def test_harmless_and_already_rejected_inputs_cannot_be_minimized(self):
        for raw in ("hello", "<script>"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                minimize_fixture("E5-nul-stripping", raw)

    def test_replay_failure_does_not_create_a_finding(self):
        from shieldlang.lab import bypass_search
        original = run_fixture

        def no_confirmation(*args, **kwargs):
            result = original(*args, **kwargs)
            result["confirmed_bypass"] = False
            return result

        with patch.object(bypass_search, "run_fixture", no_confirmation):
            out = search_fixture("E5-nul-stripping", budget=8, minimize=False)
        self.assertEqual(out["findings"], [])


if __name__ == "__main__":
    unittest.main()
