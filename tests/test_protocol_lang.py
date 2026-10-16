import unittest
from pathlib import Path

from shieldlang.compiler import compile_source
from shieldlang.diagnostics import SourceMap

ROOT = Path(__file__).resolve().parent.parent
BASE = (ROOT / "examples" / "orders.shl").read_text()


def codes(src):
    r = compile_source(src, "t.shl")
    return r, {(d.code, d.severity) for d in r.diags.items}


def has(src, code, severity=None):
    r, cs = codes(src)
    return any(c == code and (severity is None or s == severity) for c, s in cs)


class ProtocolLang(unittest.TestCase):
    def test_base_compiles_and_hash_is_stable(self):
        r, cs = codes(BASE)
        self.assertTrue(r.ok, cs)
        self.assertEqual(r.protocols[0].hash, compile_source(BASE).protocols[0].hash)
        # a comment/whitespace-only edit does not change the protocol artifact hash; a semantic edit does
        self.assertEqual(r.protocols[0].hash, compile_source(BASE.replace("quiesce 2;", "quiesce 2;   // same")).protocols[0].hash)
        self.assertNotEqual(r.protocols[0].hash, compile_source(BASE.replace("quiesce 2;", "quiesce 3;")).protocols[0].hash)

    def test_contextual_keywords_do_not_break_existing_names(self):
        src = 'rule R on POST "/x" { field from : string maxlen 5; field to : string maxlen 5; field state : string maxlen 5; }'
        self.assertTrue(compile_source(src).ok)

    def test_negative_cases(self):
        cases = [
            ("PE011", BASE.replace("states created, paid, confirmed;", "states created, paid, paid, confirmed;")),
            ("PE012", BASE.replace("step PayOrder     from created to paid;", "step PayOrder     from created to payed;")),
            ("PE012", BASE.replace("initial created;", "initial start;")),
            ("PE002", BASE.replace("initial created;", "")),
            ("PE014", BASE.replace("terminal confirmed;", "")),
            ("PE015", BASE.replace("step ConfirmOrder from paid    to confirmed;",
                                   "step ConfirmOrder from paid    to confirmed;\n step GetOrder from confirmed to paid;")),
            ("PE020", BASE.replace("step PayOrder", "step NoSuchRule")),
            ("PE021", BASE.replace("step ConfirmOrder from paid    to confirmed;", "step ConfirmOrder from paid    to confirmed;\n step CreateOrder from paid to confirmed;")),
            ("PE022", BASE.replace("step ConfirmOrder from paid    to confirmed;",
                                   "step ConfirmOrder from paid    to confirmed;\n step ConfirmOrder from paid to created;")),
            ("PE024", BASE.replace('create CreateOrder id "id" ref client_ref probe get "/api/orders/by-ref/{ref}";', "")),
            ("PE026", BASE.replace("ref client_ref probe", "ref item probe") .replace('field item       : string maxlen 64;', 'field item? : string maxlen 64;')),
            ("PE030", BASE.replace("key id;", "key oid;")),
            ("PE051", BASE.replace("outcome success 200..201;", "")),
            ("PE052", BASE.replace("outcome failure 402;", "outcome failure 200;")),
            ("PE054", BASE.replace("outcome failure 402;", "outcome failure 500;")),
            ("PE056", BASE.replace('contract "Demo backend returns 402 only when the card was declined and no state changed.";', "")),
            ("PE060", BASE.replace('reconcile get "/api/orders/{key}"', 'reconcile get "/api/orders/x"')),
            ("PE070", BASE.replace("bind PayOrder.", "bind GetOrder.")),
            ("PE073", BASE.replace("bind PayOrder.card_token == ConfirmOrder.card_token;", "bind ConfirmOrder.card_token == PayOrder.card_token;")),
            ("PR901", BASE.replace("quiesce 2;", "quiesce 2; after 5 s do nothing;")),
        ]
        for code, src in cases:
            self.assertTrue(has(src, code), f"expected {code}")
            self.assertFalse(compile_source(src).ok if code != "PE056" or True else True, code)

    def test_warnings(self):
        no_recon = BASE[:BASE.index("  reconcile")] + "}\n"
        self.assertTrue(has(no_recon, "PE062", "warning"))
        self.assertTrue(compile_source(no_recon).ok)
        unreachable = BASE.replace("states created, paid, confirmed;", "states created, paid, confirmed, island;")
        self.assertTrue(has(unreachable, "PE016", "warning"))
        deadend = BASE.replace("states created, paid, confirmed;", "states created, paid, confirmed, trap;").replace(
            "step ConfirmOrder from paid    to confirmed;", "step ConfirmOrder from paid    to confirmed;")
        # trap is also unreachable here; make it reachable but terminal-less by adding a step into it
        deadend = deadend.replace("rule GetOrder on GET", "rule Ev on POST \"/api/orders/{id}/trap\" { param id : string maxlen 9; }\nrule GetOrder on GET").replace(
            "step PayOrder     from created to paid;", "step PayOrder     from created to paid;\n step Ev from paid to trap;")
        self.assertTrue(has(deadend, "PE017", "warning"))

    def test_route_ambiguity_and_shadowing(self):
        shadow = BASE.replace('rule PayOrder', 'rule Shadow on POST "/api/orders/{x}/pay" { param x : string maxlen 20; }\nrule PayOrder')
        self.assertTrue(has(shadow, "PE041", "warning"))
        dup = BASE + '\nrule Pay2 on POST "/api/orders/{id}/pay" { param id : string maxlen 9; }\n'
        dup = dup.replace("step ConfirmOrder from paid    to confirmed;", "step ConfirmOrder from paid    to confirmed;\n step Pay2 from created to paid;")
        self.assertTrue(has(dup, "PE040", "error"))

    def test_binding_intersection(self):
        disjoint = BASE.replace('field card_token : string matches /^tok_[a-z0-9]{1,16}$/;\n  body max_depth 3 max_size 2 KB;\n}\nrule ConfirmOrder',
                                'field card_token : string matches /^tok_[a-z0-9]{1,16}$/;\n  body max_depth 3 max_size 2 KB;\n}\nrule ConfirmOrder')
        # confirm's token must start with 'sec_' while pay's starts with 'tok_': empty intersection
        src = BASE.replace('rule ConfirmOrder on POST "/api/orders/{id}/confirm" {\n  param id         : string matches /^ord_[0-9]+$/;\n  field card_token : string matches /^tok_[a-z0-9]{1,16}$/;',
                           'rule ConfirmOrder on POST "/api/orders/{id}/confirm" {\n  param id         : string matches /^ord_[0-9]+$/;\n  field card_token : string matches /^sec_[a-z0-9]{1,16}$/;')
        self.assertNotEqual(src, BASE)
        self.assertTrue(has(src, "PE074", "error"))
        # same prefix, disjoint length ranges: tok_ + 1..3 vs tok_ + 5..9
        src2 = BASE.replace("/^tok_[a-z0-9]{1,16}$/;\n  body max_depth 3 max_size 2 KB;\n}\nrule ConfirmOrder", "/^tok_[a-z0-9]{1,3}$/;\n  body max_depth 3 max_size 2 KB;\n}\nrule ConfirmOrder")
        src2 = src2.replace("field card_token : string matches /^tok_[a-z0-9]{1,16}$/;\n  body max_depth 3 max_size 2 KB;\n}\nrule GetOrder",
                            "field card_token : string matches /^tok_[a-z0-9]{5,9}$/;\n  body max_depth 3 max_size 2 KB;\n}\nrule GetOrder")
        self.assertNotEqual(src2, BASE)
        self.assertTrue(has(src2, "PE074", "error"))
        # positive: overlapping languages -> info PE076, compiles
        self.assertTrue(has(BASE, "PE076", "info"))
        # non-string binding is explicit
        num = BASE.replace("field card_token : string matches /^tok_[a-z0-9]{1,16}$/;\n  body max_depth 3 max_size 2 KB;\n}\nrule ConfirmOrder",
                           "field card_token : int in 1..9;\n  body max_depth 3 max_size 2 KB;\n}\nrule ConfirmOrder")
        self.assertTrue(has(num, "PE072", "error"))

    def test_diagnostics_carry_spans(self):
        r, _ = codes(BASE.replace("initial created;", "initial start;"))
        sm = SourceMap(r.source)
        d = [x for x in r.diags.items if x.code == "PE012"][0].to_json(sm)
        self.assertEqual(r.source[d["span"]["start"]:d["span"]["end"]], "start")


if __name__ == "__main__":
    unittest.main()
