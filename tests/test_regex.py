"""RegexShield v2: lexer, parser, automata and differential testing against Python's re."""
import random
import re
import unittest

from shieldlang.regex.compiler import compile_pattern
from shieldlang.regex.dfa import equivalent, product
from shieldlang.regex.parser import RegexSyntaxError, parse_regex


class RegexFrontEnd(unittest.TestCase):
    def test_tokens_have_spans(self):
        toks, _ = parse_regex(r"^[a-z]+\d{2,4}$")
        kinds = [t.kind for t in toks]
        self.assertEqual(kinds, ["CARET", "CHAR", "PLUS", "CHAR", "REPEAT", "DOLLAR", "EOF"])
        self.assertEqual((toks[1].start, toks[1].end), (1, 6))
        self.assertEqual((toks[4].rmin, toks[4].rmax), (2, 4))

    def test_ast_shape_and_ids(self):
        _, ast = parse_regex("^(a+)+$")
        self.assertEqual(ast.node_type, "Concat")
        outer = ast.items[1]
        self.assertEqual(outer.node_type, "Plus")
        self.assertEqual(outer.child.node_type, "Group")
        self.assertEqual([n.node_id for n in ast.walk()][:3], ["node_0", "node_1", "node_2"])

    def test_syntax_errors_are_located(self):
        cases = {"(a+b": 4, "a**": 2, "[abc": 0, "a{3,1}": 1, r"\1": 0, "(?=a)": 0, "*a": 0, "a)": 1}
        for pat, pos in cases.items():
            with self.subTest(pat=pat):
                with self.assertRaises(RegexSyntaxError) as cm:
                    parse_regex(pat)
                self.assertEqual(cm.exception.position, pos)

    def test_anchor_in_middle_rejected(self):
        with self.assertRaises(RegexSyntaxError):
            compile_pattern("a^b")


PATTERNS = [
    r"^a+$", r"^(a|b)*abb$", r"^[a-c]{2,4}$", r"ab", r"^(ab|a)*c$", r"[0-9]+x", r"^\d{3}-\d{2}$", r"^(a+)+$",
    r"^[^ab]+$", r"(a|aa)+b", r"^\w+@\w+\.[a-z]{2,3}$", r"c.*d", r"^x?y?z?$", r"^(?:ab){2}$", r"^[ab]*a[ab]{2}$",
]
ALPHABET = "ab.c-xyz0@19 d"


class DifferentialAgainstRe(unittest.TestCase):
    """The compiled DFA must agree with Python's re on every input (search semantics)."""

    def test_fuzz(self):
        rnd = random.Random(2026)
        for pat in PATTERNS:
            art = compile_pattern(pat)
            rx = re.compile(pat)
            for _ in range(400):
                s = "".join(rnd.choice(ALPHABET) for _ in range(rnd.randint(0, 9)))
                with self.subTest(pat=pat, s=s):
                    self.assertEqual(art.dfa.matches(s), rx.search(s) is not None)

    def test_case_insensitive(self):
        art = compile_pattern("union.*from", "i")
        self.assertTrue(art.dfa.matches("x UnIoN y FROM z"))
        self.assertFalse(art.dfa.matches("union only"))


class AutomataAlgorithms(unittest.TestCase):
    def test_minimisation_shrinks(self):
        art = compile_pattern(r"^(a|b)*abb$")
        self.assertLessEqual(art.dfa.n, art.raw_dfa.n)
        self.assertEqual(len(art.dfa.accept_states()), 2)  # before/after the optional final LF

    def test_equivalence_and_counterexample(self):
        a = compile_pattern(r"^(a+)+$").dfa
        b = compile_pattern(r"^a+$").dfa
        c = compile_pattern(r"^a*$").dfa
        self.assertEqual(equivalent(a, b), (True, None))
        same, cex = equivalent(a, c)
        self.assertFalse(same)
        self.assertEqual(cex, "")

    def test_length_bounds(self):
        self.assertEqual(compile_pattern(r"^\d{6}$").length_bounds, (6, 7))
        self.assertEqual(compile_pattern(r"^a{2,4}b?$").length_bounds, (2, 6))
        self.assertEqual(compile_pattern(r"^a+$").length_bounds, (1, None))

    def test_intersection_empty(self):
        a = compile_pattern(r"^[a-z]+$").dfa
        b = compile_pattern(r"^[0-9]+$").dfa
        self.assertTrue(product([a, b], "and").is_empty())

    def test_tagged_product_reports_components(self):
        a = compile_pattern("cat").dfa
        b = compile_pattern("dog").dfa
        t = product([a, b], "tagged")
        self.assertEqual(set(t.tags[t.run("hotdog")[0]]), {1})
        self.assertEqual(set(t.tags[t.run("cat & dog")[0]]), {0, 1})
        self.assertEqual(set(t.tags[t.run("bird")[0]]), set())


if __name__ == "__main__":
    unittest.main()
