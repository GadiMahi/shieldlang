"""Public API contract and inconclusive-replay regression tests."""
import unittest
from unittest.mock import patch

from shieldlang.lab import bypass_search as search
from shieldlang.lab.labapp import LabApp


class SearchContractTests(unittest.TestCase):
    def test_zero_depth_is_valid_and_evaluates_only_seeds(self):
        out = search.search_fixture(
            "E4-equivalent-pipelines", budget=2, max_depth=0, minimize=False,
            reduction_budget=1, time_budget_ms=3000,
        )
        self.assertLessEqual(out["evaluated"], 2)
        self.assertEqual(out["max_depth"], 0)

    def test_oversized_budget_is_rejected_not_clamped(self):
        with self.assertRaisesRegex(ValueError, "exceeds the maximum"):
            search.search_fixture("E1-nfkc-fullwidth", budget=search.MAX_BUDGET + 1)

    def test_minimize_must_be_a_boolean(self):
        with self.assertRaisesRegex(ValueError, "minimize must be a boolean"):
            search.search_fixture("E1-nfkc-fullwidth", minimize="false")

    def test_unpaired_surrogate_is_rejected_before_replay(self):
        with self.assertRaisesRegex(ValueError, "unpaired Unicode surrogate"):
            search.search_fixture("E1-nfkc-fullwidth", seeds=["\ud800"])

    def test_unconfirmed_minimization_input_raises(self):
        with self.assertRaisesRegex(ValueError, "not a replay-confirmed finding"):
            search.minimize_fixture("E4-equivalent-pipelines", '{"role":"user"}', budget=5)

    def test_expired_replay_deadline_cannot_start_http_request(self):
        from shieldlang.lab.bypass import run_fixture
        app = LabApp(search.BY_ID["E1-nfkc-fullwidth"]["app_stages"]).start()
        try:
            with patch("http.client.HTTPConnection") as connection:
                with self.assertRaisesRegex(TimeoutError, "shared replay deadline expired"):
                    run_fixture("E1-nfkc-fullwidth", "＜script＞", app=app,
                                replay_deadline=search.time.monotonic() - 1)
                connection.assert_not_called()
        finally:
            app.stop()

    def test_immediate_replay_error_preserves_original_confirmed_evidence(self):
        app = LabApp(search.BY_ID["E1-nfkc-fullwidth"]["app_stages"]).start()
        started = search.time.monotonic()
        confirmed = {"fixture_id": "E1-nfkc-fullwidth", "raw": "＜script＞", "confirmed_bypass": True}
        try:
            with patch.object(search, "run_fixture", side_effect=OSError("simulated first replay failure")):
                out = search._minimize_fixture_with_app(
                    "E1-nfkc-fullwidth", "＜script＞", budget=10, time_budget_ms=2000,
                    _initial_result=confirmed, _app=app, _session_started=started,
                )
        finally:
            app.stop()
        self.assertIn("inconclusive replay error", out["stop_reason"])
        self.assertIn("No minimality claim", out["minimality"])
        self.assertEqual(out["raw"], "＜script＞")
        self.assertIs(out["result"], confirmed)
        self.assertTrue(out["result"]["confirmed_bypass"])
        self.assertNotEqual(out["trial_error"]["candidate"], out["raw"])
        self.assertIn("simulated first replay failure", out["trial_error"]["evaluation_error"])

    def test_successful_reduction_then_replay_error_preserves_last_confirmed_evidence(self):
        app = LabApp(search.BY_ID["E1-nfkc-fullwidth"]["app_stages"]).start()
        started = search.time.monotonic()
        original = "＜script＞"
        initial = {"fixture_id": "E1-nfkc-fullwidth", "raw": original, "confirmed_bypass": True}
        trials = []
        last_confirmed = {}

        def confirm_once_then_fail(fixture_id, candidate, **kwargs):
            trials.append(candidate)
            if len(trials) == 1:
                last_confirmed.update({
                    "fixture_id": fixture_id,
                    "raw": candidate,
                    "confirmed_bypass": True,
                    "evidence_marker": "last-confirmed-candidate",
                })
                return last_confirmed
            raise OSError("simulated later replay failure")

        try:
            with patch.object(search, "run_fixture", side_effect=confirm_once_then_fail):
                out = search._minimize_fixture_with_app(
                    "E1-nfkc-fullwidth", original, budget=10, time_budget_ms=2000,
                    _initial_result=initial, _app=app, _session_started=started,
                )
        finally:
            app.stop()
        self.assertEqual(len(trials), 2)
        self.assertNotEqual(trials[0], original)
        self.assertNotEqual(trials[1], trials[0])
        self.assertIn("inconclusive replay error", out["stop_reason"])
        self.assertIn("No minimality claim", out["minimality"])
        # The shorter input and evidence must describe the same confirmed replay.
        self.assertEqual(out["raw"], trials[0])
        self.assertIs(out["result"], last_confirmed)
        self.assertEqual(out["result"]["raw"], out["raw"])
        self.assertEqual(out["result"]["evidence_marker"], "last-confirmed-candidate")
        # The failed trial is diagnostic data, not replacement evidence.
        self.assertEqual(out["trial_error"]["candidate"], trials[1])
        self.assertNotEqual(out["trial_error"]["candidate"], out["raw"])
        self.assertIn("simulated later replay failure", out["trial_error"]["evaluation_error"])


if __name__ == "__main__":
    unittest.main()
