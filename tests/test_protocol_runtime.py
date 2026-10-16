"""Runtime tests over real loopback HTTP: hand traces, failure injection, concurrency, crash recovery (subprocess),
and independently authored mutants that the same checks must catch."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from shieldlang.lab.demo_backend import ledger_violations
from shieldlang.lab.traffic import LabEnv
from shieldlang.protocol.engine import ProtocolEngine, ProtocolStore

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "examples" / "orders.shl").read_text().replace("quiesce 2;", "quiesce 1;")
TOK = {"card_token": "tok_a"}


def jb(b):
    return json.loads(b) if b else None


class Base(unittest.TestCase):
    def env(self, **kw):
        e = LabEnv(SRC, protocol="Checkout", **kw)
        self.addCleanup(e.close)
        return e

    def create(self, env, who="alice", ref="r1", fault=None):
        h = {"X-Demo-Fault": fault} if fault else None
        s, _, b = env.send("POST", "/api/orders", identity=who, body={"client_ref": ref, "item": "x"}, headers=h)
        return s, jb(b)

    def op(self, env, who, op, oid, fault=None, token="tok_a"):
        h = {"X-Demo-Fault": fault} if fault else None
        s, _, b = env.send("POST", f"/api/orders/{oid}/{op}", identity=who, body={"card_token": token}, headers=h)
        return s, jb(b)

    @staticmethod
    def reason(resp):
        return resp.get("reason") if isinstance(resp, dict) else None

    def state(self, env, key):
        return next((r for r in env.engine.resources() if r["key"] == key), None)


class HandTracesOverHttp(Base):
    def test_H1_happy_path_version_3(self):
        env = self.env()
        _, o = self.create(env)
        self.assertEqual(self.op(env, "alice", "pay", o["id"])[0], 200)
        self.assertEqual(self.op(env, "alice", "confirm", o["id"])[0], 200)
        r = self.state(env, o["id"])
        self.assertEqual((r["state"], r["version"]), ("confirmed", 3))
        self.assertEqual(ledger_violations(env.backend.ledger()), [])

    def test_H2_skip_payment_denied_and_no_effect(self):
        env = self.env()
        _, o = self.create(env)
        s, b = self.op(env, "alice", "confirm", o["id"])
        self.assertEqual((s, self.reason(b)), (409, "illegal_transition"))
        self.assertEqual([x["op"] for x in env.backend.ledger()], ["create"])

    def test_H3_failed_payment_releases(self):
        env = self.env()
        _, o = self.create(env)
        self.assertEqual(self.op(env, "alice", "pay", o["id"], fault="decline")[0], 402)
        r = self.state(env, o["id"])
        self.assertEqual((r["state"], r["version"], r["reserved_tx"], r["unknown_tx"]), ("created", 2, None, None))
        self.assertEqual(self.reason(self.op(env, "alice", "confirm", o["id"])[1]), "illegal_transition")
        self.assertEqual(self.op(env, "alice", "pay", o["id"])[0], 200)   # retry allowed after definitive failure

    def test_H4_cross_principal(self):
        env = self.env()
        _, o = self.create(env)
        s, b = self.op(env, "bob", "pay", o["id"])
        self.assertEqual((s, self.reason(b)), (403, "no_such_resource_or_not_owner"))
        self.assertEqual(len(env.backend.ledger()), 1)

    def test_H5_cross_resource_substitution(self):
        env = self.env()
        _, o1 = self.create(env, ref="a")
        _, o2 = self.create(env, ref="b")
        self.assertEqual(self.op(env, "alice", "pay", o2["id"])[0], 200)
        self.assertEqual(self.reason(self.op(env, "alice", "confirm", o1["id"])[1]), "illegal_transition")
        self.assertEqual(self.state(env, o1["id"])["state"], "created")

    def test_H6_terminal_replay(self):
        env = self.env()
        _, o = self.create(env)
        self.op(env, "alice", "pay", o["id"])
        self.op(env, "alice", "confirm", o["id"])
        self.assertEqual(self.reason(self.op(env, "alice", "confirm", o["id"])[1]), "illegal_transition")
        self.assertEqual(self.state(env, o["id"])["version"], 3)

    def test_H7_busy_while_reserved(self):
        env = self.env()
        _, o = self.create(env)
        out = {}
        t = threading.Thread(target=lambda: out.update(first=self.op(env, "alice", "pay", o["id"], fault="delay_commit:700")))
        t.start()
        time.sleep(0.25)
        s, b = self.op(env, "alice", "pay", o["id"])
        self.assertEqual((s, self.reason(b)), (409, "busy"))
        t.join()
        self.assertEqual(out["first"][0], 200)
        self.assertEqual(sum(1 for x in env.backend.ledger() if x["op"] == "pay"), 1)

    def test_binding_mismatch(self):
        env = self.env()
        _, o = self.create(env)
        self.op(env, "alice", "pay", o["id"])
        s, b = self.op(env, "alice", "confirm", o["id"], token="tok_b")
        self.assertEqual((s, self.reason(b)), (422, "binding_mismatch"))

    def test_unauthenticated_cannot_use_protocol_events(self):
        env = self.env()
        s, _, b = env.send("POST", "/api/orders", body={"client_ref": "z", "item": "x"})
        self.assertEqual(s, 401)
        self.assertEqual(env.backend.ledger(), [])


class UnknownAndReconcile(Base):
    def test_H8_lost_response_blocks_then_H9_reconcile_commits(self):
        env = self.env()
        _, o = self.create(env)
        s, b = self.op(env, "alice", "pay", o["id"], fault="drop_after_commit")
        self.assertEqual((s, b["decision"]), (502, "unknown"))
        s, b = self.op(env, "alice", "pay", o["id"])
        self.assertEqual((s, self.reason(b)), (409, "blocked_unknown"))
        self.assertEqual(sum(1 for x in env.backend.ledger() if x["op"] == "pay"), 1)   # no blind retry
        res = env.engine.reconcile_key(o["id"], env.prober())
        self.assertEqual(res["result"], "committed")
        self.assertEqual(res["evidence"]["source"], "probe")
        self.assertEqual(self.state(env, o["id"])["state"], "paid")
        self.assertEqual(self.op(env, "alice", "confirm", o["id"])[0], 200)

    def test_H10_release_only_after_quiesce(self):
        env = self.env()
        _, o = self.create(env)
        s, b = self.op(env, "alice", "pay", o["id"], fault="error500_no_effect")
        self.assertEqual(b["decision"], "unknown")             # 5xx is never a failure class
        r1 = env.engine.reconcile_key(o["id"], env.prober())
        self.assertEqual(r1["result"], "inconclusive")
        self.assertEqual(self.reason(self.op(env, "alice", "pay", o["id"])[1]), "blocked_unknown")
        real = time.time
        env.engine.clock = lambda: real() + 5
        r2 = env.engine.reconcile_key(o["id"], env.prober())
        self.assertEqual(r2["result"], "released")
        self.assertEqual(self.op(env, "alice", "pay", o["id"])[0], 200)

    def test_reconcile_conflict_on_other_state(self):
        env = self.env()
        _, o = self.create(env)
        self.op(env, "alice", "pay", o["id"], fault="drop_after_commit")
        # out-of-band mutation not seen by the monitor: backend confirms directly (violates complete mediation)
        env.send  # keep linter quiet
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", env.backend.port)
        c.request("POST", f"/api/orders/{o['id']}/confirm", body=json.dumps(TOK), headers={"X-Gateway-Principal": "alice"})
        c.getresponse().read()
        res = env.engine.reconcile_key(o["id"], env.prober())
        self.assertEqual(res["result"], "conflict")
        self.assertEqual(self.reason(self.op(env, "alice", "pay", o["id"])[1]), "blocked_unknown")

    def test_operator_resolution_requires_evidence_and_is_labelled(self):
        env = self.env()
        _, o = self.create(env)
        self.op(env, "alice", "pay", o["id"], fault="drop_after_commit")
        tx = next(r for r in env.engine.reservations() if r["status"] == "unknown")["tx"]
        with self.assertRaises(ValueError):
            env.engine.operator_resolve(tx, "committed", "")
        out = env.engine.operator_resolve(tx, "committed", "ledger shows pay effect for this order (checked by hand)")
        self.assertEqual(out["evidence"]["source"], "operator")
        self.assertEqual(self.state(env, o["id"])["state"], "paid")

    def test_H11_lost_create_response(self):
        env = self.env()
        s, b = self.create(env, ref="lost", fault="drop_after_commit")
        self.assertEqual(s, 502)
        s, b = self.create(env, ref="lost")
        self.assertEqual((s, self.reason(b)), (409, "blocked_unknown"))
        self.assertEqual(sum(1 for x in env.backend.ledger() if x["op"] == "create"), 1)
        res = env.engine.reconcile_ref("alice", "lost", env.prober())
        self.assertEqual(res["result"], "committed")
        self.assertEqual(self.state(env, res["key"])["owner"], "alice")
        self.assertEqual(self.op(env, "alice", "pay", res["key"])[0], 200)

    def test_connect_failure_releases_without_unknown(self):
        env = self.env()
        _, o = self.create(env)
        env.backend.stop()
        s, b = self.op(env, "alice", "pay", o["id"])
        self.assertEqual(b["decision"], "not_delivered")
        r = self.state(env, o["id"])
        self.assertEqual((r["state"], r["reserved_tx"], r["unknown_tx"]), ("created", None, None))

    def test_client_disconnect_does_not_erase_outcome(self):
        import socket
        env = self.env()
        _, o = self.create(env)
        body = json.dumps(TOK).encode()
        s = socket.create_connection(("127.0.0.1", env.server.port))
        s.sendall((f"POST /api/orders/{o['id']}/pay HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer demo-token-alice\r\n"
                   f"Content-Type: application/json\r\nX-Demo-Fault: delay_commit:400\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
        s.close()                                   # client goes away before the response exists
        deadline = time.time() + 5
        while time.time() < deadline and self.state(env, o["id"])["state"] != "paid":
            time.sleep(0.05)
        self.assertEqual(self.state(env, o["id"])["state"], "paid")
        self.assertEqual(sum(1 for x in env.backend.ledger() if x["op"] == "pay"), 1)


class Concurrency(Base):
    def test_H15_concurrent_confirmations(self):
        for _ in range(3):
            env = self.env()
            _, o = self.create(env)
            self.op(env, "alice", "pay", o["id"])
            results = []
            lock = threading.Lock()
            gate = threading.Barrier(8)

            def go():
                gate.wait()
                r = self.op(env, "alice", "confirm", o["id"], fault="delay_commit:150")
                with lock:
                    results.append(r)
            ts = [threading.Thread(target=go) for _ in range(8)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            ok = [r for r in results if r[0] == 200]
            self.assertEqual(len(ok), 1, results)
            self.assertEqual(sum(1 for x in env.backend.ledger() if x["op"] == "confirm"), 1)
            self.assertEqual(ledger_violations(env.backend.ledger()), [])


CRASH_SCRIPT = textwrap.dedent("""
    import json, os, sys
    sys.path.insert(0, {root!r})
    from shieldlang.lab.traffic import LabEnv
    src = open({src!r}).read().replace("quiesce 2;", "quiesce 1;")
    env = LabEnv(src, protocol="Checkout", workdir={work!r})
    s, _, b = env.send("POST", "/api/orders", identity="alice", body={{"client_ref": "r1", "item": "x"}})
    oid = json.loads(b)["id"]
    print(oid, flush=True)
    os.environ["SHIELDLANG_CRASH_POINT"] = {point!r}
    env.send("POST", "/api/orders/%s/pay" % oid, identity="alice", body={{"card_token": "tok_a"}})
    print("NOT CRASHED", flush=True)
""")


class CrashRecovery(Base):
    def crash(self, point):
        work = tempfile.mkdtemp(prefix="shieldlang-crash-")
        code = CRASH_SCRIPT.format(root=str(ROOT), src=str(ROOT / "examples" / "orders.shl"), work=work, point=point)
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 86, p.stdout + p.stderr)
        oid = p.stdout.split()[0]
        env = LabEnv(SRC, protocol="Checkout", workdir=work)    # restart: engine runs recover()
        self.addCleanup(env.close)
        return env, oid

    def test_H13_crash_after_reserve_is_released(self):
        env, oid = self.crash("after_reserve")
        r = self.state(env, oid)
        self.assertEqual((r["state"], r["reserved_tx"], r["unknown_tx"]), ("created", None, None))
        self.assertEqual(self.op(env, "alice", "pay", oid)[0], 200)

    def test_H12_crash_after_send_intent_is_unknown_then_released_after_quiesce(self):
        env, oid = self.crash("after_send_intent")
        self.assertEqual(self.reason(self.op(env, "alice", "pay", oid)[1]), "blocked_unknown")
        self.assertEqual([x["op"] for x in env.backend.ledger()], ["create"])    # false unknown: backend never saw the pay
        real = time.time
        env.engine.clock = lambda: real() + 5
        self.assertEqual(env.engine.reconcile_key(oid, env.prober())["result"], "released")
        self.assertEqual(self.op(env, "alice", "pay", oid)[0], 200)

    def test_H14_crash_after_upstream_response_then_probe_commits(self):
        env, oid = self.crash("after_upstream_response")
        self.assertEqual(self.reason(self.op(env, "alice", "pay", oid)[1]), "blocked_unknown")
        self.assertEqual(env.engine.reconcile_key(oid, env.prober())["result"], "committed")
        self.assertEqual(self.state(env, oid)["state"], "paid")
        self.assertEqual(sum(1 for x in env.backend.ledger() if x["op"] == "pay"), 1)

    def test_crash_after_outcome_persisted_keeps_commit(self):
        env, oid = self.crash("after_outcome_persisted")
        self.assertEqual(self.state(env, oid)["state"], "paid")
        self.assertEqual(self.op(env, "alice", "confirm", oid)[0], 200)


# ---- mutants: authored here, independent of the runtime's own code paths -------------------------------------------
class NoAtomicReservation(ProtocolEngine):
    """Check-then-act in two separate transactions with a window between them (missing compare-and-set)."""
    def _admit_step(self, principal, rule, key, body, request_id):
        with self.store._read() as c:
            row = c.execute("SELECT * FROM resources WHERE ns=? AND protocol=? AND key=?", (self.ns, self.ir.name, key)).fetchone()
        reason, to = self._check_step(row, principal, rule, body)
        if reason:
            from shieldlang.protocol.engine import Admission
            return Admission(False, reason, key=key)
        time.sleep(0.1)
        import uuid
        from shieldlang.protocol.engine import Admission
        tx = "tx-" + uuid.uuid4().hex[:12]
        with self.store._write() as c:
            c.execute("UPDATE resources SET reserved_tx=?, version=version+1 WHERE ns=? AND protocol=? AND key=?", (tx, self.ns, self.ir.name, key))
            c.execute("INSERT INTO reservations(tx,ns,protocol,key,kind,rule,principal,frm,to_state,fields,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (tx, self.ns, self.ir.name, key, "step", rule, principal, row["state"], to, json.dumps(self._bound_values(rule, body)), "reserved", time.time()))
        return Admission(True, tx=tx, key=key, from_state=row["state"], to_state=to)


class PrematureSuccess(ProtocolEngine):
    """Commits at send intent, before the upstream outcome is known."""
    def mark_sent(self, tx):
        super().mark_sent(tx)
        if any(r["tx"] == tx and r["kind"] == "step" for r in self.reservations()):
            self._apply(tx, "committed", {"source": "mutant"})


class ReleaseOnTimeout(ProtocolEngine):
    """Treats every non-authoritative outcome as 'no effect'."""
    def classify(self, adm, kind, status, body):
        o, ev, k = super().classify(adm, kind, status, body)
        return ("released", ev, k) if o == "unknown" else (o, ev, k)


class Mutants(Base):
    def mutant_env(self, cls):
        return self.env(engine_cls=cls)

    def test_no_atomic_reservation_is_detected(self):
        env = self.mutant_env(NoAtomicReservation)
        _, o = self.create(env)
        self.op(env, "alice", "pay", o["id"])
        ts = [threading.Thread(target=self.op, args=(env, "alice", "confirm", o["id"], "delay_commit:150")) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        confirms = sum(1 for x in env.backend.ledger() if x["op"] == "confirm")
        self.assertGreater(confirms, 1, "mutant should have admitted concurrent confirmations")
        self.assertTrue(any(v["violation"].startswith("repeated_confirm") or v["violation"] == "effect_after_terminal" for v in ledger_violations(env.backend.ledger())))

    def test_premature_success_is_detected(self):
        env = self.mutant_env(PrematureSuccess)
        _, o = self.create(env)
        self.assertEqual(self.op(env, "alice", "pay", o["id"], fault="decline")[0], 402)
        s, _ = self.op(env, "alice", "confirm", o["id"])      # the real engine denies this (H3)
        self.assertEqual(s, 200)
        self.assertTrue(any(v["violation"] == "confirm_without_pay" for v in ledger_violations(env.backend.ledger())))

    def test_release_on_timeout_is_detected(self):
        env = self.mutant_env(ReleaseOnTimeout)
        _, o = self.create(env)
        self.op(env, "alice", "pay", o["id"], fault="drop_after_commit")
        s, _ = self.op(env, "alice", "pay", o["id"])           # the real engine denies this (H8)
        self.assertEqual(s, 200)
        self.assertTrue(any(v["violation"] == "repeated_pay" for v in ledger_violations(env.backend.ledger())))


if __name__ == "__main__":
    unittest.main()
