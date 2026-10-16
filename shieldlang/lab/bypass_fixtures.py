"""Bypass Lab: explicitly declared local demonstration experiments.

Every preset candidate below was written by hand. The separate bypass_search module
generates candidates and reduces confirmed findings without reading these presets.
No fixture claims a real-world product vulnerability. Each names its policy-side pipeline, its app-side pipeline (by named stage, from transforms.py),
and an oracle (oracle.py) specified independently of both pipelines. Running a fixture:
  1. applies the policy pipeline to interpret what the policy stage sees;
  2. applies the app pipeline to interpret what the application stage sees;
  3. asks the independent oracle whether the APPLICATION interpretation violates the property;
  4. replays the ORIGINAL raw bytes against a tiny local loopback app (labapp.py) and reads back what actually happened.
A "bypass" requires all three: the fixture's declared policy would accept the raw value, the oracle says the
application-side interpretation violates the property, AND the local replay confirms the same effect. Any of the three
missing is reported plainly; nothing here invents a verdict.
"""
from __future__ import annotations

from typing import Any, Dict, List

FIXTURES: List[Dict[str, Any]] = [
    {
        "id": "E1-nfkc-fullwidth",
        "title": "Unicode-compatibility (NFKC) decoding mismatch",
        "label": "Hand-authored demo fixture. Shows what happens when only one side normalizes Unicode compatibility forms.",
        "scope": {"kind": "body_field", "name": "comment"},
        "policy_stages": [],
        "app_stages": ["nfkc"],
        "oracle": {"kind": "contains_substring", "needle": "<script", "case_insensitive": True},
        "candidates": [
            {"label": "fullwidth less-than + ASCII script", "raw": "＜script＞"},
            {"label": "control case: plain ASCII (should already be caught by policy)", "raw": "<script>"},
        ],
        "replay": {"endpoint": "reflect", "field": "comment"},
    },
    {
        "id": "E2-percent-decode-path",
        "title": "Percent-decoding stage mismatch on a path parameter",
        "label": "Hand-authored demo fixture (classic dot-dot-slash shape).",
        "scope": {"kind": "path_param", "name": "name"},
        "policy_stages": [],
        "app_stages": ["percent_decode"],
        "oracle": {"kind": "path_escapes_root", "base_depth": 0},
        "candidates": [
            {"label": "encoded traversal", "raw": "%2e%2e%2fsecret.txt"},
            {"label": "double-encoded (should NOT bypass a single-decode app)", "raw": "%252e%252e%252fsecret.txt"},
            {"label": "control case: ordinary filename", "raw": "report.txt"},
        ],
        "replay": {"endpoint": "file", "field": "name"},
    },
    {
        "id": "E3-duplicate-key-role",
        "title": "Duplicate-JSON-key adapter mismatch (first-wins vs last-wins)",
        "label": "Hand-authored demo fixture. Both Python json.loads and JS JSON.parse keep the LAST duplicate; the "
                 "'first-wins' side below is a deliberately labeled demo adapter, not a claim about any real parser.",
        "scope": {"kind": "body_field", "name": "role"},
        "policy_stages": ["dup:first"],
        "app_stages": ["dup:last"],
        "oracle": {"kind": "equals_any", "values": ["admin"]},
        "candidates": [
            {"label": "duplicate role, user then admin", "raw": '{"role":"user","role":"admin"}'},
            {"label": "control case: single key", "raw": '{"role":"user"}'},
        ],
        "replay": {"endpoint": "echo_role", "field": None},
    },
    {
        "id": "E4-equivalent-pipelines",
        "title": "Equivalent pipelines (expected: no confirmed bypass)",
        "label": "Hand-authored demo fixture used as a negative control: both sides decode and keep the last duplicate.",
        "scope": {"kind": "body_field", "name": "role"},
        "policy_stages": ["dup:last"],
        "app_stages": ["dup:last"],
        "oracle": {"kind": "equals_any", "values": ["admin"]},
        "candidates": [
            {"label": "duplicate role, user then admin", "raw": '{"role":"user","role":"admin"}'},
        ],
        "replay": {"endpoint": "echo_role", "field": None},
    },
    {
        "id": "E5-nul-stripping",
        "title": "NUL-byte stripping mismatch",
        "label": "Hand-authored demo fixture.",
        "scope": {"kind": "body_field", "name": "comment"},
        "policy_stages": [],
        "app_stages": ["strip_nul"],
        "oracle": {"kind": "contains_substring", "needle": "<script", "case_insensitive": True},
        "candidates": [
            {"label": "NUL spliced into the tag", "raw": "<scr\x00ipt>"},
            {"label": "control case: plain ASCII", "raw": "<script>"},
        ],
        "replay": {"endpoint": "reflect", "field": "comment"},
    },
]

BY_ID = {f["id"]: f for f in FIXTURES}
