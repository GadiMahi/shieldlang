"""IR, optimiser, VM and code generation — including differential testing of the generated code."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shieldlang.compiler import compile_source

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "examples" / "payments.shl").read_text()
REQUESTS = [r["request"] for r in json.loads((ROOT / "examples" / "payments.requests.json").read_text())]
RES = compile_source(SRC, "payments.shl")


def load_python(code):
    d = tempfile.mkdtemp()
    p = Path(d) / "validator.py"
    p.write_text(code)
    spec = importlib.util.spec_from_file_location("validator_under_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def error_codes(result):
    return sorted(e["code"] for e in result["errors"])


class IRTests(unittest.TestCase):
    def test_lowering_produces_one_function_per_rule(self):
        self.assertEqual([f.rule for f in RES.ir.functions], ["CreatePayment", "GetUser", "Signup"])
        self.assertIn("CHECK_RANGE", RES.ir.text())

    def test_every_pass_fires_on_the_example(self):
        fired = {p.name for p in RES.passes if p.changes}
        self.assertEqual(fired, {"constant-folding", "dead-code-elimination", "redundant-check-elim",
                                 "dfa-intersection", "scan-fusion", "check-reordering"})
        self.assertLess(RES.opt_ir.cost(), RES.ir.cost())
        self.assertLess(RES.opt_ir.instr_count(), RES.ir.instr_count())


class ExecutionTests(unittest.TestCase):
    def test_expected_verdicts(self):
        verdicts = [RES.vm().run(r)["allowed"] for r in REQUESTS]
        self.assertEqual(verdicts, [True, False, False, False, False, True, False, False, False, False])

    def test_optimised_and_unoptimised_agree(self):
        for r in REQUESTS:
            with self.subTest(r=r):
                self.assertEqual(error_codes(RES.vm(True).run(r)), error_codes(RES.vm(False).run(r)))

    def test_generated_python_matches_vm(self):
        mod = load_python(RES.python)
        for r in REQUESTS:
            with self.subTest(r=r):
                self.assertEqual(error_codes(mod.validate(r)), error_codes(RES.vm().run(r)))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_generated_javascript_matches_vm(self):
        d = tempfile.mkdtemp()
        js = Path(d) / "validator.js"
        js.write_text(RES.js)
        script = ("const {validate}=require(process.argv[1]);const reqs=JSON.parse(require('fs').readFileSync(0,'utf8'));"
                  "process.stdout.write(JSON.stringify(reqs.map(validate)))")
        out = subprocess.run(["node", "-e", script, str(js)], input=json.dumps(REQUESTS), capture_output=True,
                             text=True, check=True).stdout
        for r, got in zip(REQUESTS, json.loads(out)):
            with self.subTest(r=r):
                self.assertEqual(error_codes(got), error_codes(RES.vm().run(r)))

    def test_redos_attack_is_linear_in_generated_code(self):
        mod = load_python(RES.python)
        req = {"method": "POST", "path": "/api/signup",
               "body": {"email": "a." + "aa." * 5000 + "_", "username": "mahi_27", "password": "Secret123"}}
        import time
        t0 = time.perf_counter()
        out = mod.validate(req)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertFalse(out["allowed"])

    def test_cli_compile(self):
        out = subprocess.run([sys.executable, str(ROOT / "shieldlang.py"), "compile", str(ROOT / "examples" / "payments.shl"),
                              "--emit", "report"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertIn("verified equivalent", out.stdout)


if __name__ == "__main__":
    unittest.main()
