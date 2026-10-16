import json
import socket
import unittest
from pathlib import Path

from shieldlang.lab.demo_backend import ledger_violations
from shieldlang.lab.httpsubset import Limits
from shieldlang.lab.traffic import LabEnv, RecordingError, run_traffic, validate_recording

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "examples" / "orders_rules.shl").read_text()
REC = json.loads((ROOT / "examples" / "recordings" / "stateless_demo.json").read_text())


def raw_exchange(port, data: bytes) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(data)
    out = b""
    try:
        while True:
            c = s.recv(4096)
            if not c:
                break
            out += c
    except socket.timeout:
        pass
    s.close()
    return out


class TrafficStateless(unittest.TestCase):
    def test_enforce_blocks_invalid_and_forwards_valid(self):
        d = run_traffic(SRC, REC, mode="enforce")
        got = {e["id"]: e["decision"] for e in d["entries"]}
        self.assertEqual(got, {"create_ok": "allow", "pay_ok": "allow", "pay_bad_body": "deny"})
        self.assertEqual([x["op"] for x in d["ledger"]], ["create", "pay"])  # denied request produced no backend effect
        self.assertEqual(d["evaluation"]["true_positive"], 1)

    def test_observe_forwards_everything_and_records_would_block(self):
        d = run_traffic(SRC, REC, mode="observe")
        e = {x["id"]: x for x in d["entries"]}
        self.assertEqual(e["pay_bad_body"]["would_decision"], "deny")
        self.assertEqual(e["pay_bad_body"]["decision"], "forwarded")
        self.assertEqual(e["pay_bad_body"]["status"], 400)  # backend itself rejected it; no effect

    def test_versions_are_content_hashes(self):
        a = run_traffic(SRC, REC)["versions"]
        b = run_traffic(SRC + "\n// edit\n", REC)["versions"]
        self.assertEqual(a["recording"], b["recording"])
        self.assertNotEqual(a["policy"], b["policy"])

    def test_http_subset_rejections(self):
        with LabEnv(SRC) as env:
            p = env.server.port
            self.assertIn(b" 501 ", raw_exchange(p, b"POST /api/orders HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n").split(b"\r\n")[0] + b" ")
            r = raw_exchange(p, b"POST /api/orders HTTP/1.1\r\nHost: x\r\nContent-Length: 999999\r\n\r\n")
            self.assertTrue(r.startswith(b"HTTP/1.0 413") or r.startswith(b"HTTP/1.1 413"), r[:40])
            r = raw_exchange(p, b"POST /api/orders HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\nContent-Length: 0\r\n\r\n")
            self.assertIn(b" 417 ", r.split(b"\r\n")[0] + b" ")
            self.assertEqual(env.backend.ledger(), [])

    def test_x_principal_header_is_ignored_and_no_token_is_unauthenticated_backend_side(self):
        with LabEnv(SRC) as env:
            s, h, b = env.send("POST", "/api/orders", body={"client_ref": "z", "item": "i"},
                               headers={"X-Principal": "alice", "X-Gateway-Principal": "alice"})
            self.assertEqual(s, 201)
            self.assertEqual(env.backend.ledger()[0]["principal"], "anonymous")

    def test_loopback_only(self):
        from shieldlang.lab.gateway import GatewayServer
        with LabEnv(SRC) as env:
            with self.assertRaises(ValueError):
                GatewayServer(env.gateway, host="0.0.0.0")

    def test_recording_validation(self):
        for bad in ({}, {"entries": [{"id": "a"}]}, {"entries": [{"id": "a", "method": "GET", "path": "/", "identity": "mallory"}]},
                    {"entries": [{"id": "a", "method": "GET", "path": "/", "label": {"legitimate": "yes"}}]}):
            with self.assertRaises(RecordingError):
                validate_recording(bad)


if __name__ == "__main__":
    unittest.main()


CHECKOUT = (ROOT / "examples" / "orders.shl").read_text()
ATTACKS = json.loads((ROOT / "examples" / "recordings" / "checkout_attacks.json").read_text())


class TrafficProtocol(unittest.TestCase):
    def test_enforce_matches_labels_and_ledger_is_clean(self):
        d = run_traffic(CHECKOUT, ATTACKS, mode="enforce", protocol="Checkout")
        ev = d["evaluation"]
        self.assertEqual((ev["true_positive"], ev["false_negative"], ev["false_positive"], ev["true_negative"]), (5, 0, 0, 4))
        self.assertEqual(d["ledger_violations"], [])
        self.assertEqual(d["versions"]["protocol"][:7], "sha256:")

    def test_observe_records_would_block_but_backend_ledger_shows_real_effects(self):
        d = run_traffic(CHECKOUT, ATTACKS, mode="observe", protocol="Checkout")
        e = {x["id"]: x for x in d["entries"]}
        self.assertEqual(e["skip_payment"]["would_decision"], "deny")
        self.assertEqual(e["skip_payment"]["status"], 200)                       # actually forwarded and executed
        self.assertTrue(any(v["violation"] == "confirm_without_pay" for v in d["ledger_violations"]))
        res = d["protocol_state"]["resources"][0]
        self.assertEqual(res["diverged"], 1)                                     # shadow stopped predicting
        self.assertGreater(d["evaluation"]["unpredicted"], 0)
        self.assertEqual(d["evaluation"]["false_positive"], 0)

    def test_run_documents_are_stored_and_deletable(self):
        import tempfile
        from shieldlang.lab.artifacts import ArtifactStore
        with tempfile.TemporaryDirectory() as t:
            store = ArtifactStore(Path(t) / "lab.db")
            d = run_traffic(CHECKOUT, ATTACKS, mode="enforce", protocol="Checkout", store=store)
            self.assertEqual(store.get_run(d["run_id"])["versions"], d["versions"])
            self.assertTrue(store.delete_run(d["run_id"]))
            self.assertIsNone(store.get_run(d["run_id"]))
