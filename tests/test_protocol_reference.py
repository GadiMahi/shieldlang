"""Hand-derived traces H1-H11 (docs/protocol_semantics.md section 7), encoded literally against the reference interpreter."""
import unittest
from pathlib import Path

from shieldlang.compiler import compile_source
from shieldlang.protocol import ast as P
from shieldlang.protocol.reference import Reference

SRC = (Path(__file__).resolve().parent.parent / "examples" / "orders.shl").read_text()


def fresh():
    prog = compile_source(SRC).program
    return Reference([i for i in prog.items if isinstance(i, P.Protocol)][0])


def C(who="alice", ref="r", out="S"):
    return {"op": "create", "who": who, "ref": ref, "out": out}


def E(who, rule, key, out="S", **fields):
    return {"op": "event", "who": who, "rule": rule, "key": key, "fields": fields, "out": out}


TOK = {"card_token": "tok_a"}


class HandTraces(unittest.TestCase):
    def reasons(self, trace):
        ref = fresh()
        return ref, [r.get("reason") for r in ref.run(trace)]

    def test_H1_happy_path(self):
        ref, rs = self.reasons([C(), E("alice", "PayOrder", "k1", **TOK), E("alice", "ConfirmOrder", "k1", **TOK)])
        self.assertEqual(rs, [None, None, None])
        self.assertEqual(ref.snapshot()["k1"], {"owner": "alice", "state": "confirmed", "version": 3, "unresolved": None})

    def test_H2_skip_payment(self):
        _, rs = self.reasons([C(), E("alice", "ConfirmOrder", "k1", **TOK)])
        self.assertEqual(rs, [None, "illegal_transition"])

    def test_H3_failed_payment_releases(self):
        ref, rs = self.reasons([C(), E("alice", "PayOrder", "k1", "F", **TOK), E("alice", "ConfirmOrder", "k1", **TOK)])
        self.assertEqual(rs, [None, None, "illegal_transition"])
        self.assertEqual(ref.snapshot()["k1"]["state"], "created")
        self.assertEqual(ref.snapshot()["k1"]["version"], 2)

    def test_H4_cross_principal(self):
        _, rs = self.reasons([C(), E("bob", "PayOrder", "k1", **TOK)])
        self.assertEqual(rs, [None, "no_such_resource_or_not_owner"])

    def test_H5_cross_resource_substitution(self):
        ref, rs = self.reasons([C(ref="a"), C(ref="b"), E("alice", "PayOrder", "k2", **TOK), E("alice", "ConfirmOrder", "k1", **TOK)])
        self.assertEqual(rs, [None, None, None, "illegal_transition"])
        self.assertEqual(ref.snapshot()["k2"]["state"], "paid")
        self.assertEqual(ref.snapshot()["k1"]["state"], "created")

    def test_H6_terminal_replay(self):
        ref, rs = self.reasons([C(), E("alice", "PayOrder", "k1", **TOK), E("alice", "ConfirmOrder", "k1", **TOK),
                                E("alice", "ConfirmOrder", "k1", **TOK)])
        self.assertEqual(rs[-1], "illegal_transition")
        self.assertEqual(ref.snapshot()["k1"]["version"], 3)

    def test_H7_busy(self):
        ref, rs = self.reasons([C(), E("alice", "PayOrder", "k1", "P", **TOK), E("alice", "PayOrder", "k1", **TOK)])
        self.assertEqual(rs, [None, None, "busy"])
        self.assertEqual(ref.snapshot()["k1"]["unresolved"], "reserved")

    def test_H8_unknown_blocks(self):
        _, rs = self.reasons([C(), E("alice", "PayOrder", "k1", "U", **TOK), E("alice", "PayOrder", "k1", **TOK)])
        self.assertEqual(rs, [None, None, "blocked_unknown"])

    def test_H9_reconcile_commit(self):
        ref = fresh()
        ref.run([C(), E("alice", "PayOrder", "k1", "U", **TOK)])
        self.assertEqual(ref.run([{"op": "reconcile", "key": "k1", "observed": "paid"}])[0]["result"], "committed")
        self.assertIsNone(ref.run([E("alice", "ConfirmOrder", "k1", **TOK)])[0]["reason"])

    def test_H10_release_only_after_quiesce(self):
        ref = fresh()
        ref.run([C(), E("alice", "PayOrder", "k1", "U", **TOK)])
        self.assertEqual(ref.run([{"op": "reconcile", "key": "k1", "observed": "created", "quiesced": False}])[0]["result"], "inconclusive")
        self.assertEqual(ref.run([E("alice", "PayOrder", "k1", **TOK)])[0]["reason"], "blocked_unknown")
        self.assertEqual(ref.run([{"op": "reconcile", "key": "k1", "observed": "created", "quiesced": True}])[0]["result"], "released")
        self.assertIsNone(ref.run([E("alice", "PayOrder", "k1", **TOK)])[0]["reason"])

    def test_H11_unknown_create(self):
        ref = fresh()
        ref.run([C(ref="r1", out="U")])
        self.assertEqual(ref.run([C(ref="r1")])[0]["reason"], "blocked_unknown")
        res = ref.run([{"op": "reconcile_ref", "who": "alice", "ref": "r1", "found": True}])[0]
        self.assertEqual(res["result"], "committed")
        self.assertEqual(ref.snapshot()[res["key"]]["state"], "created")

    def test_binding_mismatch_and_conflict(self):
        _, rs = self.reasons([C(), E("alice", "PayOrder", "k1", card_token="tok_a"), E("alice", "ConfirmOrder", "k1", card_token="tok_b")])
        self.assertEqual(rs, [None, None, "binding_mismatch"])
        ref = fresh()
        ref.run([C(), E("alice", "PayOrder", "k1", "U", **TOK)])
        self.assertEqual(ref.run([{"op": "reconcile", "key": "k1", "observed": "confirmed"}])[0]["result"], "conflict")


if __name__ == "__main__":
    unittest.main()
