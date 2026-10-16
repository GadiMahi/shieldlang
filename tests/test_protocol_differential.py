"""Differential test: runtime ProtocolEngine vs the independent reference interpreter over
(a) every trace up to depth 3 of a fixed alphabet and (b) seeded random traces of length 8.
Bounded evidence only: it shows agreement on these traces, not equivalence in general."""
import itertools
import json
import random
import tempfile
import unittest
from pathlib import Path

from shieldlang.compiler import compile_source
from shieldlang.protocol import ast as P
from shieldlang.protocol.engine import ProtocolEngine, ProtocolStore
from shieldlang.protocol.reference import Reference

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "examples" / "orders.shl").read_text()
RES = compile_source(SRC)
PROTO = [i for i in RES.program.items if isinstance(i, P.Protocol)][0]
IR = RES.protocols[0]

UP = {"S": ("response", 200), "F": ("response", 402), "U": ("ambiguous", None)}


def alphabet():
    ops = [
        {"op": "create", "who": "alice", "ref": "ok", "out": "S"},
        {"op": "create", "who": "alice", "ref": "u", "out": "U"},
    ]
    for out in "SFUP":
        ops.append({"op": "event", "who": "alice", "rule": "PayOrder", "key": "k1", "fields": {"card_token": "tok_a"}, "out": out})
    for out in "SFU":
        ops.append({"op": "event", "who": "alice", "rule": "ConfirmOrder", "key": "k1", "fields": {"card_token": "tok_a"}, "out": out})
    ops.append({"op": "event", "who": "alice", "rule": "ConfirmOrder", "key": "k1", "fields": {"card_token": "tok_b"}, "out": "S"})
    ops.append({"op": "event", "who": "bob", "rule": "PayOrder", "key": "k1", "fields": {"card_token": "tok_a"}, "out": "S"})
    ops.append({"op": "event", "who": "alice", "rule": "PayOrder", "key": "k2", "fields": {"card_token": "tok_a"}, "out": "S"})
    ops.append({"op": "settle", "key": "k1", "out": "S"})
    for obs, q in (("paid", False), ("created", False), ("created", True), ("confirmed", False)):
        ops.append({"op": "reconcile", "key": "k1", "observed": obs, "quiesced": q})
    ops.append({"op": "reconcile_ref", "who": "alice", "ref": "u", "found": True})
    return ops


class Driver:
    def __init__(self, engine, ref):
        self.e, self.ref, self.pending = engine, ref, {}
        self.base = 1_000_000.0
        engine.clock = lambda: self.base

    def apply(self, op, ref_result):
        e = self.e
        k = op["op"]
        if k == "create":
            adm = e.admit(principal=op["who"], rule="CreateOrder", params={}, body={"client_ref": op["ref"]})
            if adm.allowed and op["out"] != "P":
                self._finish(adm, op["out"], {"id": ref_result.get("key")})
            return {"admitted": adm.allowed, "reason": adm.reason}
        if k == "event":
            adm = e.admit(principal=op["who"], rule=op["rule"], params={"id": op["key"]}, body=op["fields"])
            if adm.allowed:
                if op["out"] == "P":
                    self.pending[op["key"]] = adm
                else:
                    self._finish(adm, op["out"], {})
            return {"admitted": adm.allowed, "reason": adm.reason}
        if k == "settle":
            self._finish(self.pending.pop(op["key"]), op["out"], {})
            return {"admitted": True, "reason": None}
        if k == "reconcile":
            self.base += 100 if op.get("quiesced") else 0
            out = e.reconcile_key(op["key"], lambda path: (200, {"state": op["observed"], "owner": "alice"}))
            self.base -= 100 if op.get("quiesced") else 0
            return {"result": out["result"]}
        if k == "reconcile_ref":
            out = e.reconcile_ref(op["who"], op["ref"], lambda path: (200, {"id": ref_result["key"], "owner": "alice", "state": "created"}))
            return {"result": out["result"]}

    def _finish(self, adm, out, body):
        self.e.mark_sent(adm.tx)
        kind, status = UP[out]
        self.e.settle(adm, kind, status, json.dumps(body).encode())

    def snapshot(self):
        return {r["key"]: {"owner": r["owner"], "state": r["state"], "version": r["version"],
                           "unresolved": "unknown" if r["unknown_tx"] else "reserved" if r["reserved_tx"] else None}
                for r in self.e.resources()}


class Differential(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = ProtocolStore(str(Path(cls.tmp.name) / "d.db"), synchronous="OFF")
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    engine_cls = ProtocolEngine

    def check(self, trace):
        type(self).n += 1
        ref = Reference(PROTO)
        eng = self.engine_cls(IR, self.store, ns=f"d{type(self).n}", recover=False)
        drv = Driver(eng, ref)
        done = []
        for op in trace:
            if not ref.applicable(op):
                continue
            rr = ref.run([op])[0]
            er = drv.apply(op, rr)
            done.append(op)
            if "result" in rr:
                self.assertEqual(rr["result"], er["result"], (done, rr, er))
            else:
                self.assertEqual((rr["admitted"], rr["reason"]), (er["admitted"], er["reason"]), (done, rr, er))
            self.assertEqual(ref.snapshot(), drv.snapshot(), done)

    def test_exhaustive_depth_3(self):
        alpha = alphabet()
        count = 0
        for n in (1, 2, 3):
            for combo in itertools.product(alpha, repeat=n):
                self.check(combo)
                count += 1
        self.assertGreater(count, 3000)

    def test_seeded_random_length_8(self):
        rnd = random.Random(20261015)
        alpha = alphabet()
        for _ in range(600):
            self.check([rnd.choice(alpha) for _ in range(8)])


class ReleaseOnUnknown(ProtocolEngine):
    def classify(self, adm, kind, status, body):
        o, ev, k = super().classify(adm, kind, status, body)
        return ("released", ev, k) if o == "unknown" else (o, ev, k)


class PrematureCommit(ProtocolEngine):
    def classify(self, adm, kind, status, body):
        o, ev, k = super().classify(adm, kind, status, body)
        return ("committed", ev, k) if (o == "released" and adm.key) else (o, ev, k)


class DifferentialSensitivity(Differential):
    """The differential harness must fail for known-wrong engines (otherwise it proves nothing)."""
    def test_mutants_are_caught(self):
        alpha = alphabet()
        for cls in (ReleaseOnUnknown, PrematureCommit):
            self.engine_cls = cls
            with self.assertRaises(AssertionError, msg=cls.__name__):
                for combo in itertools.product(alpha, repeat=2):
                    self.check(combo)
        self.engine_cls = ProtocolEngine

    test_exhaustive_depth_3 = None
    test_seeded_random_length_8 = None


if __name__ == "__main__":
    unittest.main()
