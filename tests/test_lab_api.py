"""End-to-end HTTP tests for the Protocol Studio / Traffic Lab / Bypass Lab API routes in server.py."""
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ["SHIELDLANG_DATA"] = tempfile.mkdtemp(prefix="shieldlang-api-test-")  # keep this test's store off any mounted FS

from shieldlang.server import Handler

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "examples" / "orders.shl").read_text()


class ApiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.t = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, body=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        r = c.getresponse()
        data = r.read()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data


class LoopbackGuardAndLimits(ApiCase):
    def test_lab_api_refuses_non_loopback_client_address(self):
        import unittest.mock as mock
        with mock.patch("shieldlang.server.Handler._loopback_client", return_value=False):
            s, r = self.call("GET", "/api/protocol/examples")
        self.assertEqual(s, 403)
        self.assertEqual(r["error"], "forbidden")

    def test_oversized_post_body_rejected(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/api/protocol/compile")
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", str(5 * 1024 * 1024))
        c.endheaders()
        try:
            c.send(b"x" * (64 * 1024))  # enough for the server to read Content-Length and reject before we finish
        except (BrokenPipeError, ConnectionResetError):
            pass
        r = c.getresponse()
        self.assertEqual(r.status, 413)


class ExistingRoutesStillWork(ApiCase):
    def test_studio_and_demo_pages_and_compile(self):
        self.assertEqual(self.call("GET", "/")[0], 200)
        self.assertEqual(self.call("GET", "/studio")[0], 200)
        s, r = self.call("POST", "/api/compile", {"source": 'rule R on GET "/x" { field a: string maxlen 5; }'})
        self.assertEqual(s, 200)
        self.assertTrue(r["ok"])


class ProtocolStudioApi(ApiCase):
    def setUp(self):
        s, sess = self.call("POST", "/api/protocol/sessions", {"source": SRC, "protocol": "Checkout", "mode": "enforce"})
        self.assertEqual(s, 200, sess)
        self.sid = sess["session_id"]
        self.addCleanup(lambda: self.call("DELETE", f"/api/protocol/sessions/{self.sid}"))

    def test_pages_exist(self):
        self.assertEqual(self.call("GET", "/protocols")[0], 200)

    def test_examples_list_includes_checkout(self):
        s, r = self.call("GET", "/api/protocol/examples")
        self.assertEqual(s, 200)
        self.assertIn("Checkout", r[0]["protocols"])

    def test_compile_reports_hash_and_diagnostics(self):
        s, r = self.call("POST", "/api/protocol/compile", {"source": SRC})
        self.assertEqual(s, 200)
        self.assertTrue(r["ok"])
        self.assertEqual(r["protocols"][0]["hash"][:7], "sha256:")

    def test_compile_error_source_rejected_for_session(self):
        s, r = self.call("POST", "/api/protocol/sessions", {"source": "garbage {{{", "protocol": "Checkout"})
        self.assertEqual(s, 400)

    def test_unknown_protocol_name_rejected(self):
        s, r = self.call("POST", "/api/protocol/sessions", {"source": SRC, "protocol": "NoSuch"})
        self.assertEqual(s, 400)
        self.assertEqual(r["error"], "no_such_protocol")

    def test_full_lifecycle_through_http(self):
        s, r = self.call("POST", f"/api/protocol/sessions/{self.sid}/request",
                         {"method": "POST", "path": "/api/orders", "identity": "alice",
                          "body": {"client_ref": "r1", "item": "x"}})
        self.assertEqual((s, r["status"], r["decision"]), (200, 201, "allow"))
        oid = r["response"]["id"]

        s, r = self.call("POST", f"/api/protocol/sessions/{self.sid}/request",
                         {"method": "POST", "path": f"/api/orders/{oid}/confirm", "identity": "alice",
                          "body": {"card_token": "tok_a"}})
        self.assertEqual(r["record"]["stages"][-1]["reason"], "illegal_transition")   # H2: skip payment

        s, r = self.call("POST", f"/api/protocol/sessions/{self.sid}/request",
                         {"method": "POST", "path": f"/api/orders/{oid}/pay", "identity": "alice",
                          "body": {"card_token": "tok_a"}, "fault": "drop_after_commit"})
        self.assertEqual(r["decision"], "unknown")
        self.assertEqual(r["session"]["resources"][0]["unknown_tx"][:3], "tx-")

        s, r = self.call("POST", f"/api/protocol/sessions/{self.sid}/request",
                         {"method": "POST", "path": f"/api/orders/{oid}/pay", "identity": "alice", "body": {"card_token": "tok_a"}})
        self.assertEqual(r["record"]["stages"][-1]["reason"], "blocked_unknown")      # no blind retry

        s, r = self.call("POST", f"/api/protocol/sessions/{self.sid}/reconcile", {"key": oid})
        self.assertEqual(r["result"]["result"], "committed")
        self.assertEqual(r["session"]["resources"][0]["state"], "paid")

        s, r = self.call("POST", f"/api/protocol/sessions/{self.sid}/request",
                         {"method": "POST", "path": f"/api/orders/{oid}/confirm", "identity": "alice", "body": {"card_token": "tok_a"}})
        self.assertEqual((s, r["status"]), (200, 200))

        s, r = self.call("GET", f"/api/protocol/sessions/{self.sid}")
        self.assertEqual(r["ledger_violations"], [])

        s, r = self.call("POST", f"/api/protocol/sessions/{self.sid}/evidence", {})
        self.assertEqual(s, 200)
        s2, saved = self.call("GET", f"/api/protocol/runs/{r['run_id']}")
        self.assertEqual(s2, 200)
        self.assertEqual(saved["resources"][0]["state"], "confirmed")

    def test_operator_resolution_through_http(self):
        s, r = self.call("POST", f"/api/protocol/sessions/{self.sid}/request",
                         {"method": "POST", "path": "/api/orders", "identity": "alice", "body": {"client_ref": "r9", "item": "x"}})
        oid = r["response"]["id"]
        self.call("POST", f"/api/protocol/sessions/{self.sid}/request",
                 {"method": "POST", "path": f"/api/orders/{oid}/pay", "identity": "alice", "body": {"card_token": "tok_a"}, "fault": "drop_after_commit"})
        s, r = self.call("GET", f"/api/protocol/sessions/{self.sid}")
        tx = next(x["tx"] for x in r["reservations"] if x["status"] == "unknown")
        s, r = self.call("POST", f"/api/protocol/sessions/{self.sid}/operator", {"tx": tx, "decision": "committed", "evidence_text": ""})
        self.assertEqual(s, 400)   # evidence text required
        s, r = self.call("POST", f"/api/protocol/sessions/{self.sid}/operator",
                         {"tx": tx, "decision": "committed", "evidence_text": "checked backend ledger by hand"})
        self.assertEqual(s, 200)
        self.assertEqual(r["session"]["resources"][0]["state"], "paid")

    def test_restart_runs_recovery_pass(self):
        s, r = self.call("POST", f"/api/protocol/sessions/{self.sid}/restart", {})
        self.assertEqual(s, 200)
        self.assertIn("recovery", r)
        self.assertIn("recovery_note", r)

    def test_stale_session_detection_uses_hash_mismatch(self):
        s, r = self.call("POST", "/api/protocol/compile", {"source": SRC})
        original_hash = r["protocols"][0]["hash"]
        edited = SRC.replace("quiesce 2;", "quiesce 9;")
        s, r2 = self.call("POST", "/api/protocol/compile", {"source": edited})
        self.assertNotEqual(original_hash, r2["protocols"][0]["hash"])
        s, sess = self.call("GET", f"/api/protocol/sessions/{self.sid}")
        self.assertEqual(sess["protocol_hash"], original_hash)   # this session's evidence still names the version it was built from

    def test_closing_twice_is_404(self):
        s, _ = self.call("DELETE", f"/api/protocol/sessions/{self.sid}")
        self.assertEqual(s, 200)
        s, _ = self.call("DELETE", f"/api/protocol/sessions/{self.sid}")
        self.assertEqual(s, 404)
        self.sid = None

    def tearDown(self):
        if self.sid:
            self.call("DELETE", f"/api/protocol/sessions/{self.sid}")


class TrafficLabApi(ApiCase):
    def test_pages_exist(self):
        self.assertEqual(self.call("GET", "/traffic")[0], 200)

    def test_recordings_listed(self):
        s, r = self.call("GET", "/api/traffic/recordings")
        self.assertEqual(s, 200)
        self.assertIn("checkout_attacks", [x["name"] for x in r])

    def test_run_enforce_and_observe_distinguishable(self):
        s, enf = self.call("POST", "/api/traffic/run", {"policy": "orders", "recording": "checkout_attacks",
                                                         "mode": "enforce", "protocol": "Checkout"})
        self.assertEqual(s, 200)
        self.assertEqual(enf["mode"], "enforce")
        self.assertTrue(all(e["decision"] in ("allow", "deny") for e in enf["entries"]))

        s, obs = self.call("POST", "/api/traffic/run", {"policy": "orders", "recording": "checkout_attacks",
                                                         "mode": "observe", "protocol": "Checkout"})
        self.assertEqual(obs["mode"], "observe")
        self.assertTrue(all(e["decision"] not in ("allow", "deny") for e in obs["entries"]))  # observe never reports an enforced verdict
        self.assertNotEqual(enf["versions"]["recording"], None)
        self.assertEqual(enf["versions"]["recording"], obs["versions"]["recording"])

    def test_run_lifecycle_get_and_delete(self):
        s, r = self.call("POST", "/api/traffic/run", {"policy": "orders", "recording": "stateless_demo", "mode": "enforce"})
        rid = r["run_id"]
        s, fetched = self.call("GET", f"/api/traffic/runs/{rid}")
        self.assertEqual(fetched["versions"], r["versions"])
        s, lst = self.call("GET", "/api/traffic/runs")
        self.assertIn(rid, [x["run_id"] for x in lst])
        s, _ = self.call("DELETE", f"/api/traffic/runs/{rid}")
        self.assertEqual(s, 200)
        s, _ = self.call("GET", f"/api/traffic/runs/{rid}")
        self.assertEqual(s, 404)

    def test_bad_recording_rejected(self):
        s, r = self.call("POST", "/api/traffic/run", {"policy": "orders", "recording": {"entries": [{"id": "a"}]}})
        self.assertEqual(s, 400)

    def test_export(self):
        self.call("POST", "/api/traffic/run", {"policy": "orders", "recording": "stateless_demo"})
        s, r = self.call("POST", "/api/traffic/export", {"kind": "traffic"})
        self.assertEqual(s, 200)
        self.assertEqual(r["format"], "shieldlang-lab-export/1")
        self.assertGreaterEqual(len(r["runs"]), 1)

    def test_policies_lists_bundled_and_flags_support(self):
        s, r = self.call("GET", "/api/traffic/policies")
        self.assertEqual(s, 200)
        byname = {p["name"]: p for p in r}
        self.assertIn("orders", byname)
        self.assertTrue(byname["orders"]["supported"])
        self.assertIn("Checkout", byname["orders"]["protocols"])
        # regex_lab.shl exists in examples/ and does not target the demo order backend at all
        if "regex_lab" in byname:
            self.assertFalse(byname["regex_lab"]["supported"])
            self.assertTrue(byname["regex_lab"]["reason"])

    def test_recording_check_reports_every_problem_without_storing(self):
        bad = {"entries": [
            {"id": "ok", "method": "POST", "path": "/api/orders", "body": {"client_ref": "r1"}},
            {"id": "ok", "method": "POST", "path": "/api/orders"},            # duplicate id
            {"id": "bad path", "method": "POST", "path": "orders"},          # bad id chars + path not under /api/
            {"id": "c3", "method": "PUT", "path": "/api/orders"},            # bad method
            {"id": "c4", "method": "GET", "path": "/api/orders/{{missing}}"},  # undefined variable
            {"id": "c5", "method": "GET", "path": "/api/orders/1", "fault": "nope"},  # bad fault
            {"id": "c6", "method": "GET", "path": "/api/orders/1", "label": {"legitimate": "yes"}},  # bad label
            {"id": "c7", "method": "GET", "path": "/api/orders/1", "headers": {"X-Evil": "1"}},  # disallowed header
        ]}
        s, r = self.call("POST", "/api/traffic/recordings/check", {"recording": bad})
        self.assertEqual(s, 200)
        paths = [e["path"] for e in r["errors"]]
        self.assertGreaterEqual(len(r["errors"]), 7)
        self.assertTrue(any("entries[1].id" in p for p in paths))
        self.assertTrue(any("entries[4].path" in p for p in paths))
        self.assertTrue(any("entries[5].fault" in p for p in paths))
        self.assertTrue(any("entries[6].label" in p for p in paths))
        self.assertTrue(any("entries[7].headers" in p for p in paths))
        s, lst = self.call("GET", "/api/traffic/recordings")
        self.assertNotIn("bad path", [x.get("name") for x in lst])  # check never stores anything

    def test_recording_import_then_run_then_delete(self):
        good = {"name": "my_import", "entries": [
            {"id": "c1", "identity": "alice", "method": "POST", "path": "/api/orders",
             "body": {"client_ref": "imp1", "item": "pen"}, "save_as": "o", "label": {"legitimate": True}},
            {"id": "c2", "identity": "alice", "method": "POST", "path": "/api/orders/{{o}}/pay",
             "body": {"card_token": "tok_a"}, "label": {"legitimate": True}},
        ]}
        s, r = self.call("POST", "/api/traffic/recordings", {"recording": good})
        self.assertEqual(s, 200)
        h = r["hash"]
        s, lst = self.call("GET", "/api/traffic/recordings")
        self.assertIn(h, [x["hash"] for x in lst if x.get("source") == "imported"])
        s, run = self.call("POST", "/api/traffic/run", {"policy": "orders", "recording": h, "mode": "enforce"})
        self.assertEqual(s, 200)
        self.assertEqual(len(run["entries"]), 2)
        s, _ = self.call("DELETE", f"/api/traffic/recordings/{h}")
        self.assertEqual(s, 200)
        s, lst2 = self.call("GET", "/api/traffic/recordings")
        self.assertNotIn(h, [x["hash"] for x in lst2 if x.get("source") == "imported"])

    def test_recording_import_rejects_unsafe_path(self):
        bad = {"entries": [{"id": "c1", "method": "GET", "path": "/__reset"}]}
        s, r = self.call("POST", "/api/traffic/recordings", {"recording": bad})
        self.assertEqual(s, 400)
        self.assertTrue(any("api/" in e["message"] for e in r["errors"]))

    def test_bypass_handoff_preserves_fixture_evidence_and_states_the_limitation(self):
        s, out = self.call("POST", "/api/bypass/run", {"fixture_id": "E2-percent-decode-path", "raw": "%2e%2e%2fsecret.txt"})
        self.assertEqual(s, 200)
        rid = out["run_id"]
        s, handoff = self.call("GET", f"/api/traffic/bypass/{rid}")
        self.assertEqual(s, 200)
        self.assertEqual(handoff["fixture_id"], "E2-percent-decode-path")
        self.assertEqual(handoff["app_interpretation"], out["app_interpretation"])
        self.assertEqual(handoff["oracle"], out["oracle"])
        self.assertEqual(handoff["confirmed_bypass"], out["confirmed_bypass"])
        self.assertIn("not", handoff["gateway_limitation"].lower())

    def test_bypass_handoff_rejects_non_bypass_run_id(self):
        s, run = self.call("POST", "/api/traffic/run", {"policy": "orders", "recording": "stateless_demo"})
        s, r = self.call("GET", f"/api/traffic/bypass/{run['run_id']}")
        self.assertEqual(s, 404)


class BypassLabApi(ApiCase):
    def test_search_and_minimize_are_stored_and_reopenable(self):
        s, out = self.call("POST", "/api/bypass/search", {"fixture_id": "E5-nul-stripping", "max_findings": 1})
        self.assertEqual(s, 200, out)
        self.assertEqual(out["operation"], "search")
        self.assertEqual(len(out["findings"]), 1)
        s, saved = self.call("GET", f"/api/bypass/runs/{out['run_id']}")
        self.assertEqual(saved["fixture_hash"], out["fixture_hash"])
        self.assertEqual(saved["findings"], out["findings"])
        s, reduced = self.call("POST", "/api/bypass/minimize", {
            "fixture_id": "E5-nul-stripping", "raw": "padding <scr\u0000ipt> suffix"})
        self.assertEqual(s, 200, reduced)
        self.assertTrue(reduced["result"]["confirmed_bypass"])
        self.assertLess(reduced["reduced_bytes"], reduced["original_bytes"])
        self.assertEqual(reduced["raw"], reduced["result"]["raw"])
        self.assertTrue(reduced["confirmed_bypass"])

    def test_search_api_rejects_invalid_parameters(self):
        for body in ({"fixture_id": "nope"}, {"fixture_id": []},
                     {"fixture_id": "E1-nfkc-fullwidth", "budget": 100000},
                     {"fixture_id": "E1-nfkc-fullwidth", "seeds": [1]}):
            self.assertEqual(self.call("POST", "/api/bypass/search", body)[0], 400)
        self.assertEqual(self.call("POST", "/api/bypass/minimize", {
            "fixture_id": "E5-nul-stripping", "raw": "harmless"})[0], 400)

    def test_pages_exist(self):
        self.assertEqual(self.call("GET", "/lab/bypasses")[0], 200)

    def test_fixtures_listed(self):
        s, r = self.call("GET", "/api/bypass/fixtures")
        self.assertEqual(s, 200)
        self.assertEqual(len(r), 5)

    def test_manual_candidate_confirmed_and_control_case_not(self):
        s, r = self.call("POST", "/api/bypass/run", {"fixture_id": "E3-duplicate-key-role",
                                                      "raw": '{"role":"user","role":"admin"}'})
        self.assertEqual(s, 200)
        self.assertTrue(r["confirmed_bypass"])
        s2, r2 = self.call("GET", f"/api/bypass/runs/{r['run_id']}")
        self.assertEqual(r2["confirmed_bypass"], True)

        s, r = self.call("POST", "/api/bypass/run", {"fixture_id": "E4-equivalent-pipelines",
                                                      "raw": '{"role":"user","role":"admin"}'})
        self.assertFalse(r["confirmed_bypass"])

    def test_empty_candidate_rejected_not_generated(self):
        s, r = self.call("POST", "/api/bypass/run", {"fixture_id": "E1-nfkc-fullwidth", "raw": ""})
        self.assertEqual(s, 400)

    def test_unknown_fixture_rejected(self):
        s, r = self.call("POST", "/api/bypass/run", {"fixture_id": "nope", "raw": "x"})
        self.assertEqual(s, 400)


if __name__ == "__main__":
    unittest.main()
