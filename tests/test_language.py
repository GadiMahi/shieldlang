"""ShieldLang front end: lexer, parser (with error recovery) and semantic analysis."""
import unittest
from pathlib import Path

from shieldlang.compiler import compile_source
from shieldlang.diagnostics import DiagnosticBag
from shieldlang.lang.lexer import Lexer
from shieldlang.lang.parser import Parser

ROOT = Path(__file__).resolve().parent.parent


def codes(src):
    return sorted(d.code for d in compile_source(src).diags.items)


class LexerTests(unittest.TestCase):
    def kinds(self, src):
        return [t.kind for t in Lexer(src, DiagnosticBag()).tokenize()]

    def test_regex_vs_division(self):
        self.assertEqual(self.kinds("const A = 10 / 2;"),
                         ["KW_CONST", "IDENT", "ASSIGN", "NUMBER", "SLASH", "NUMBER", "SEMI", "EOF"])
        self.assertEqual(self.kinds("pattern P = /a/b/;")[:4], ["KW_PATTERN", "IDENT", "ASSIGN", "REGEX"])

    def test_range_and_units(self):
        k = self.kinds("in 1..MAX 16 KB")
        self.assertEqual(k, ["KW_IN", "NUMBER", "DOTDOT", "IDENT", "NUMBER", "IDENT", "EOF"])

    def test_lexical_errors(self):
        d = DiagnosticBag()
        Lexer('const X = "oops\nrule @', d).tokenize()
        self.assertEqual(sorted(x.code for x in d.items), ["L001", "L002"])


class ParserTests(unittest.TestCase):
    def test_recovers_and_reports_several_errors(self):
        src = """
        rule A on POST "/a" {
          field x : string maxlen ;
          field y  string;
          field z : int in 1..5;
        }
        rule B on FETCH "/b" { }
        rule C on GET "/c" { field ok : bool; }
        """
        res = compile_source(src)
        parse_errs = [d for d in res.diags.items if d.phase == "parser"]
        self.assertGreaterEqual(len(parse_errs), 3)
        self.assertEqual([r.name for r in res.program.rules], ["A", "C"])
        self.assertEqual([s.name for s in res.program.rules[0].body], ["z"])

    def test_missing_semicolon_points_after_previous_token(self):
        res = compile_source('rule A on GET "/a" { field x : int }')
        d = [x for x in res.diags.items if x.code == "P001"][0]
        self.assertIn("';'", d.message)


class SemanticTests(unittest.TestCase):
    def test_diagnostics_demo_catches_everything(self):
        src = (ROOT / "examples" / "diagnostics_demo.shl").read_text()
        got = set(codes(src))
        for c in ["S001", "S002", "S003", "S006", "S008", "S009", "S012", "S014", "S016", "S017", "S023", "S024",
                  "R001", "R101"]:
            self.assertIn(c, got)

    def test_type_errors_in_conditions(self):
        src = '''rule A on POST "/a" {
          field n : int in 1..10;
          field s? : string;
          when s > 3 require s;
          when n == "x" require s;
        }'''
        self.assertEqual(codes(src).count("S013"), 2)

    def test_static_condition_evaluation_uses_constants(self):
        src = '''const MAX = 100;
        rule A on POST "/a" { field n : int in 1..MAX; field s? : string; when n > MAX require s; }'''
        self.assertIn("S014", codes(src))

    def test_clean_program_has_no_errors(self):
        src = (ROOT / "examples" / "payments.shl").read_text()
        res = compile_source(src)
        self.assertTrue(res.ok)
        self.assertEqual(len(res.model.rules), 3)


if __name__ == "__main__":
    unittest.main()
