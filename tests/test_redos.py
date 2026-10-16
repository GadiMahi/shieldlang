"""ReDoS analysis: classification, attack strings, provenance and verified rewriting."""
import re
import time
import unittest

from shieldlang.regex.compiler import compile_pattern
from shieldlang.regex.dfa import equivalent

EXPECTED = {
    r"^(a+)+$": "HIGH",
    r"^(a*)*$": "HIGH",
    r"^(a|aa)+$": "HIGH",
    r"^(\d|[0-9])+$": "HIGH",
    r"^(\w+\s?)*$": "HIGH",
    r"^([a-zA-Z0-9]+\.?)+@[a-z]+\.[a-z]{2,6}$": "HIGH",
    r"^(.*,)*x$": "HIGH",
    r"^\d+\d+$": "MEDIUM",
    r"^a*a*$": "MEDIUM",
    r"(union|select).*from": "MEDIUM",
    r"^a+$": "LOW",
    r"^[a-z0-9.]+@[a-z]+$": "LOW",
    r"^Bearer [A-Za-z0-9._-]+$": "LOW",
    r"^(ab|a)*c$": "LOW",
    r"(a+)+": "UNKNOWN",  # no rejecting witness is not a proof of cheap backtracking
}


class Classification(unittest.TestCase):
    def test_expected_risk(self):
        for pat, risk in EXPECTED.items():
            with self.subTest(pat=pat):
                self.assertEqual(compile_pattern(pat).redos.risk, risk)

    def test_witness_is_rejected_by_the_dfa(self):
        for pat, risk in EXPECTED.items():
            art = compile_pattern(pat)
            if risk in ("HIGH", "MEDIUM"):
                with self.subTest(pat=pat):
                    w = art.redos.witness
                    self.assertIsNotNone(w)
                    self.assertFalse(art.dfa.matches(w.build(10)))

    def test_exponential_witness_really_explodes(self):
        art = compile_pattern(r"^(a+)+$")
        rx = re.compile(r"^(a+)+$")
        t = []
        for n in (14, 18):
            s = art.redos.witness.build(n)
            t0 = time.perf_counter()
            rx.search(s)
            t.append(time.perf_counter() - t0)
        self.assertGreater(t[1], t[0] * 4)  # ~16x expected for 4 more pumps

    def test_polynomial_degree(self):
        self.assertEqual(compile_pattern(r"^\d+\d+$").redos.degree, 2)
        self.assertEqual(compile_pattern(r"^\d+\d+\d+$").redos.degree, 3)

    def test_provenance_points_to_the_culprit(self):
        src = r"^x([a-z]+\.?)+@y$"
        art = compile_pattern(src)
        eda = [f for f in art.redos.findings if f.kind == "EDA" and f.severity == "HIGH"][0]
        span = eda.spans[0]
        self.assertEqual(src[span["start"]:span["end"]], r"([a-z]+\.?)+")


class Rewriting(unittest.TestCase):
    CASES = {
        r"^(a+)+$": r"^a+$",
        r"^(a|aa)+$": r"^a+$",
        r"^(\d|[0-9])+$": r"^\d+$",
        r"^\d+\d+$": r"^\d{2,}$",
        r"^(.*,)*x$": r"^(?:.*,)?x$",
    }

    def test_known_rewrites(self):
        for pat, want in self.CASES.items():
            with self.subTest(pat=pat):
                art = compile_pattern(pat)
                self.assertEqual(art.rewrite.final, want)
                self.assertEqual(art.rewrite.final_risk, "LOW")

    def test_every_rewrite_is_equivalent(self):
        for pat in list(self.CASES) + [r"^([a-zA-Z0-9]+\.?)+@[a-z]+\.[a-z]{2,6}$", r"^(\w+\s?)*$"]:
            art = compile_pattern(pat)
            new = compile_pattern(art.rewrite.final)
            with self.subTest(pat=pat):
                self.assertTrue(equivalent(art.dfa, new.dfa)[0])
                self.assertEqual(new.redos.risk, "LOW")

    def test_safe_patterns_untouched(self):
        self.assertIsNone(compile_pattern(r"^[a-z]+$").rewrite)


if __name__ == "__main__":
    unittest.main()
