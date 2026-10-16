"""Review 2 regressions and independent verification/counterexample tests."""
import copy
import itertools
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shieldlang.compiler import compile_source
from shieldlang.ir.optimize import run_passes
from shieldlang.regex.compiler import compile_pattern
from shieldlang.verification import compare_policies, field_language, verify_optimizations


def program(statements):
    return 'rule R on POST "/" { ' + statements + ' }'


def python_validator(result):
    namespace = {"__name__": "review2_validator"}
    exec(result.python, namespace)
    return namespace["validate"]


def js_results(result, requests):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "validator.js"
        path.write_text(result.js, encoding="utf-8")
        script = "const v=require(process.argv[1]).validate; const r=JSON.parse(require('fs').readFileSync(0,'utf8')); console.log(JSON.stringify(r.map(v)));"
        out = subprocess.run(["node", "-e", script, str(path)], input=json.dumps(requests),
                             capture_output=True, text=True, check=True, timeout=10)
        return json.loads(out.stdout)


class AuditRegressions(unittest.TestCase):
    def check_targets(self, source, request, expected):
        result = compile_source(source)
        self.assertTrue(result.ok, result.diags.items)
        for optimized in (False, True):
            self.assertEqual(result.vm(optimized).run(request, trace=False)["allowed"], expected)
        self.assertEqual(python_validator(result)(request)["allowed"], expected)
        if shutil.which("node"):
            self.assertEqual(js_results(result, [request])[0]["allowed"], expected)

    def test_register_shaped_string_cannot_bypass_requirement(self):
        self.check_targets(program('field mode : string; field token? : string; when mode != "t1" require token;'),
                           {"method": "POST", "path": "/", "body": {"mode": "safe"}}, False)

    def test_undeclared_register_shaped_literal_is_a_string(self):
        self.check_targets(program('field mode : string; field token? : string; when mode == "t999" require token;'),
                           {"method": "POST", "path": "/", "body": {"mode": "safe"}}, True)

    def test_register_shaped_constant_is_preserved(self):
        source = 'const VALUE = "t1"; ' + program('field mode : string; field token? : string; when mode != VALUE require token;')
        self.check_targets(source, {"method": "POST", "path": "/", "body": {"mode": "safe"}}, False)

    def test_final_newline_cannot_evade_block(self):
        self.check_targets(program('block if any field matches /^attack$/;'),
                           {"method": "POST", "path": "/", "body": {"x": "attack\n"}}, False)

    def test_anchor_newline_semantics_match_python(self):
        for pattern in ("^a$", "a$", "^$", "^a$|^b$"):
            dfa = compile_pattern(pattern).dfa
            for text in ("", "\n", "a", "a\n", "a\n\n", "ba\n", "b\n", "a\r\n"):
                with self.subTest(pattern=pattern, text=text):
                    self.assertEqual(dfa.matches(text), bool(re.search(pattern, text)))

    def test_fallback_alternative_is_not_claimed_safe(self):
        report = compile_pattern('^(a+)+$|').redos
        self.assertEqual(report.risk, "UNKNOWN")
        self.assertNotIn("proved", " ".join(report.notes))

    def test_triple_budget_exhaustion_is_unknown(self):
        from shieldlang.regex.compiler import _cached
        _cached.cache_clear()
        with patch('shieldlang.regex.redos.TRIPLE_BUDGET', 0):
            report = compile_pattern('^z+z+$', with_rewrite=False).redos
        _cached.cache_clear()
        self.assertEqual(report.risk, 'UNKNOWN')
        self.assertTrue(any('partial' in note for note in report.notes))

    def test_unicode_body_size_is_utf8(self):
        for limit, expected in ((14, True), (13, False)):
            self.check_targets(program(f'body max_size {limit};'),
                               {"method": "POST", "path": "/", "body": {"x": "ééé"}}, expected)

    def test_number_coercion_has_one_decimal_grammar(self):
        result = compile_source(program('query n : number in 0..100;'))
        values = ('0x5', '0b10', '1e1', '1.5', ' 5 ', '+5', 'Infinity', '5\n', '05', '5')
        expected = [False, False, True, True, False, False, False, False, True, True]
        requests = [{"method": "POST", "path": "/", "query": {"n": v}} for v in values]
        self.assertEqual([result.vm().run(r, trace=False)["allowed"] for r in requests], expected)
        self.assertEqual([python_validator(result)(r)["allowed"] for r in requests], expected)
        if shutil.which('node'):
            self.assertEqual([r['allowed'] for r in js_results(result, requests)], expected)

    def test_optional_prototype_name_is_missing(self):
        self.check_targets(program('field __proto__? : string;'), {"method": "POST", "path": "/", "body": {}}, True)

    def test_deep_body_returns_limit_error_without_recursion(self):
        result = compile_source(program('body max_depth 5 max_size 100;'))
        body = {}
        for _ in range(2000):
            body = {"x": body}
        request = {"method": "POST", "path": "/", "body": body}
        for validate in (lambda r: result.vm().run(r, trace=False), python_validator(result)):
            out = validate(request)
            self.assertFalse(out['allowed'])
            self.assertEqual(out['errors'][0]['kind'], 'limit')

    @unittest.skipUnless(shutil.which('node'), 'node not installed')
    def test_javascript_deep_body_returns_limit_error(self):
        result = compile_source(program('body max_depth 5;'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'validator.js'
            path.write_text(result.js, encoding='utf-8')
            script = 'let body={};for(let i=0;i<20000;i++)body={x:body}; console.log(JSON.stringify(require(process.argv[1]).validate({method:"POST",path:"/",body})))'
            out = subprocess.run(['node', '-e', script, str(path)], capture_output=True, text=True, check=True, timeout=10)
            self.assertFalse(json.loads(out.stdout)['allowed'])


class OptimizationVerification(unittest.TestCase):
    def test_supported_optimization_is_verified(self):
        result = compile_source(program('field x : string matches /^a+$/ matches /^[a-z]+$/ maxlen 100;'))
        self.assertEqual(result.verification['status'], 'verified')
        self.assertEqual(result.verification['counts']['equivalent'], 1)

    def test_missing_match_is_detected_and_counterexample_replays(self):
        result = compile_source(program('field x : string matches /^a+$/;'))
        mutated = copy.deepcopy(result.opt_ir)
        block = mutated.functions[0].blocks[0]
        block.instrs = [i for i in block.instrs if i.op != 'MATCH']
        report = verify_optimizations(result.ir, mutated)
        self.assertEqual(report['status'], 'failed')
        example = report['checks'][0]['examples'][0]
        from shieldlang.vm import VM
        request = {'method':'POST', 'path':'/', 'body':{'x':example['value']}}
        self.assertFalse(result.vm(False).run(request, trace=False)['allowed'])
        self.assertTrue(VM(mutated, {}).run(request, trace=False)['allowed'])

    def test_lost_length_bound_is_detected(self):
        result = compile_source(program('field x : string maxlen 2;'))
        mutated = copy.deepcopy(result.opt_ir)
        block = mutated.functions[0].blocks[0]
        block.instrs = [i for i in block.instrs if i.op != 'CHECK_LEN']
        report = verify_optimizations(result.ir, mutated)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(len(report['checks'][0]['examples'][0]['value']), 3)

    def test_presence_change_is_detected(self):
        result = compile_source(program('field x? : string;'))
        mutated = copy.deepcopy(result.opt_ir)
        guard = next(i for i in mutated.functions[0].blocks[0].instrs if i.op == 'JMP_IF_MISSING')
        guard.op = 'REQUIRE'
        report = verify_optimizations(result.ir, mutated)
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(report['checks'][0]['examples'][0]['missing'])

    def test_budget_exhaustion_never_proves_equality(self):
        result = compile_source(program('field x : string minlen 10;'))
        report = verify_optimizations(result.ir, result.opt_ir, budget=1)
        self.assertEqual(report['checks'][0]['status'], 'inconclusive')

    def test_numeric_fields_are_explicitly_unsupported(self):
        result = compile_source(program('field x : number in 0..10;'))
        self.assertEqual(result.verification['status'], 'partial')
        self.assertEqual(result.verification['checks'][0]['status'], 'unsupported')

    def test_empty_policy_is_not_called_verified(self):
        result = compile_source(program('body max_depth 5;'))
        self.assertEqual(result.verification['status'], 'not_applicable')

    def test_failed_verification_stops_code_generation(self):
        def broken(mod, consts):
            out, reports = run_passes(mod, consts)
            for block in out.functions[0].blocks:
                block.instrs = [i for i in block.instrs if i.op != 'MATCH']
            return out, reports
        with patch('shieldlang.compiler.run_passes', broken):
            result = compile_source(program('field x : string matches /^a+$/;'))
        self.assertFalse(result.ok)
        self.assertEqual(result.python, '')
        self.assertEqual(result.js, '')
        self.assertTrue(any(d.code == 'V001' for d in result.diags.items))

    def test_checker_matches_independent_finite_examples(self):
        result = compile_source(program('field x : string matches /^a*b?$/ minlen 1 maxlen 3;'))
        language = field_language(result.ir.functions[0].blocks[0], result.ir)
        for n in range(5):
            for chars in itertools.product('ab\n', repeat=n):
                text = ''.join(chars)
                states = [d.run(text)[0] for d in language.dfas]
                expected = 1 <= len(text) <= 3 and re.search('^a*b?$', text) is not None
                self.assertEqual(language.accepts(states, len(text)), expected)


class PolicyComparison(unittest.TestCase):
    def test_relaxed_policy_produces_replayed_request(self):
        result = compare_policies(program('field x : string maxlen 2;'), program('field x : string maxlen 4;'))
        self.assertEqual(result['status'], 'behavior_change_found')
        witness = result['witnesses'][0]
        self.assertFalse(witness['allowed_before'])
        self.assertTrue(witness['allowed_after'])
        self.assertEqual(len(witness['request']['body']['x']), 3)
        self.assertNotIn('_template', witness['request'])

    def test_enum_change_can_have_both_directions(self):
        result = compare_policies(program('field x : enum("a","b");'), program('field x : enum("b","c");'))
        self.assertEqual({(w['allowed_before'], w['allowed_after']) for w in result['witnesses']}, {(True, False), (False, True)})

    def test_disjoint_enums_still_produce_requests(self):
        result = compare_policies(program('field x : enum("a");'), program('field x : enum("b");'))
        self.assertEqual(len(result['witnesses']), 2)

    def test_equivalent_regex_forms(self):
        result = compare_policies(program('field x : string matches /^(a+)+$/;'), program('field x : string matches /^a+$/;'))
        self.assertEqual(result['checks'][0]['status'], 'equivalent')
        self.assertEqual(result['whole_program_equivalence'], 'not_checked')

    def test_guard_masked_difference_is_not_a_full_request_witness(self):
        a = program('field x : string maxlen 2; field token? : string; when true require token;')
        b = program('field x : string maxlen 4; field token? : string; when true require token;')
        result = compare_policies(a, b)
        self.assertEqual(result['status'], 'field_differences')
        self.assertEqual(result['witnesses'], [])

    def test_invalid_policy_is_reported(self):
        self.assertEqual(compare_policies('rule {', program('field x : string;'))['status'], 'compile_error')

    def test_route_addition_is_not_an_equivalence_claim(self):
        result = compare_policies(program('field x : string;'), 'rule R on GET "/new" { field x : string; }')
        self.assertTrue(all(c['status'] == 'unsupported' for c in result['checks']))
        self.assertEqual(result['whole_program_equivalence'], 'not_checked')


class StudioEndpoints(unittest.TestCase):
    """Exercise handler contracts without requiring a network socket."""
    def post(self, path, data):
        from shieldlang.server import Handler
        handler = object.__new__(Handler)
        handler.path = path
        payload = json.dumps(data).encode()
        handler.headers = {'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        captured = []
        handler._json = lambda result, code=200: captured.append((code, result))
        handler.do_POST()
        return captured[0]

    def test_compile_includes_verification_coverage(self):
        code, result = self.post('/api/compile', {'source': program('field x : string minlen 1;')})
        self.assertEqual(code, 200)
        self.assertEqual(result['verification']['counts']['equivalent'], 1)
        self.assertIn('verification', result['timings_ms'])

    def test_compare_returns_replayed_counterexamples(self):
        code, result = self.post('/api/compare', {'before': program('field x : string maxlen 2;'),
                                                'after': program('field x : string maxlen 4;')})
        self.assertEqual(code, 200)
        self.assertEqual(result['status'], 'behavior_change_found')
        self.assertTrue(result['witnesses'][0]['replayed'])

    def test_comparison_examples_are_available(self):
        from shieldlang.server import examples
        names = {e['name'] for e in examples()}
        self.assertTrue({'policy_before.shl', 'policy_after.shl'} <= names)

    def test_demo_and_original_studio_have_distinct_routes(self):
        from shieldlang.server import Handler
        for route, expected in (('/', b'Rules in.'), ('/demo', b'Rules in.'),
                                ('/studio', b'ShieldLang Studio'), ('/studio/', b'ShieldLang Studio')):
            handler = object.__new__(Handler)
            handler.path = route
            captured = []
            handler._send = lambda code, body, mime: captured.append((code, body, mime))
            handler.do_GET()
            self.assertEqual(captured[0][0], 200)
            self.assertIn(expected, captured[0][1])
            if route.startswith('/studio'):
                self.assertIn(b'id="source"', captured[0][1])
            else:
                self.assertIn(b'id="demo-panel"', captured[0][1])


if __name__ == '__main__':
    unittest.main()
