# Lab architecture: Protocol Studio, Traffic Lab, Bypass Lab

Scope: how the three lab pages are built on the ShieldLang compiler, what state lives where, what is trusted, and what happens
on failure. Semantics of the protocol runtime itself are normative in `docs/protocol_semantics.md`; project status and what is
*not* done is in `docs/implementation_progress.md`. Nothing here is a claim of novelty; every mechanism is a conventional one.

## 1. Components

```
 browser pages (web/*.html, *.js)         plain JS, no framework; server-side ids are the source of truth
        │  HTTP/JSON, same origin
        ▼
 shieldlang/server.py  ── static files, existing Studio routes (/api/compile …), and the lab routes
        │  dispatch only
        ▼
 shieldlang/lab/api.py ── route handlers for /api/protocol/*, /api/traffic/*, /api/bypass/*
   │            │                │
   │            │                └─ bypass.py + bypass_fixtures.py + transforms.py + rawjson.py + oracle.py + labapp.py
   │            └─ traffic.py (LabEnv, replay, evaluate) ─┐
   └─ sessions.py (live Protocol Studio sessions) ────────┤
                                                          ▼
                       LabEnv = DemoBackend (loopback) + Gateway/GatewayServer (loopback) [+ ProtocolEngine + ProtocolStore]
 compiler: shieldlang/compiler.py → lexer/parser/sema/SIR/optimizer/codegen; protocol DSL parsed by shieldlang/protocol/parser.py
           into ProtocolIR (shieldlang/protocol/ir.py) and checked by analysis.py. The request-validation VM is unchanged.
```

Two independent "judges" exist on purpose: the **protocol runtime** (what the gateway believes about each resource) and the
**backend ledger** (`DemoBackend.ledger()` read by `ledger_violations()`; it never consults the gateway or the engine). Evidence
shown to a person always labels which of the two it came from.

## 2. Request flow (Protocol Studio session and Traffic Lab replay share it)

1. The page POSTs to `/api/protocol/sessions/<id>/request` (or `/api/traffic/run`, which replays a recording the same way).
2. `LabEnv.send` opens a TCP connection to the session's **gateway** on 127.0.0.1 with a fixed demo bearer token per identity
   (alice/bob/carol). The identity table is fixed in the adapter; a client-supplied "who am I" header is never trusted.
3. `httpsubset.read_request` parses a deliberately small HTTP subset (no chunked bodies → 501, `Expect` → 417, oversized → 413).
4. `Gateway.handle`:
   1. stateless validation: the compiled ShieldLang policy runs in the VM against a normalised request view;
   2. if the matched rule is a protocol event: **admission** on the resource (`docs/protocol_semantics.md` §3: ownership,
      Unknown-blocking, busy, legal transition, binding), by an atomic compare-and-set reservation;
   3. enforce mode: any failure ⇒ deny, nothing is forwarded. Observe mode: always forward, record `would_decision`;
   4. **send intent is persisted before** the upstream call; the upstream is a fixed loopback port chosen at construction;
   5. the upstream outcome is classified committed / released / unknown and persisted in one transaction.
5. The response carries `X-ShieldLang-Decision` and a run id; the API returns the gateway record plus a freshly read session view
   (resources, reservations, timeline from the engine; ledger from the backend). Nothing is cached in the browser.

## 3. Persistent versus transient state

| State | Where | Lifetime | Notes |
|---|---|---|---|
| Policy sources, recordings (bundled and imported), run records | `ArtifactStore` SQLite file `<repo>/.shieldlang_data/lab.db` (override with `SHIELDLANG_DATA`) | persistent; bounded (500 sources, 100 runs, 2 MB per run record; oldest pruned) | content-addressed; deletion is explicit (`DELETE /api/traffic/runs/<id>`); export is `POST /api/traffic/export` |
| Protocol resources, reservations, events | per-session SQLite file in a temp dir (`ProtocolStore`, WAL, `synchronous=FULL`) | lives as long as the session object; deleted with it | a *Restart* reopens the same file and runs `recover()` |
| Demo backend orders and ledger | per-session SQLite file in the same temp dir | same | the ledger is the independent evidence |
| Live sessions (gateway, backend, engine handles) | in-memory `Registry` | at most 16, least-recently-used evicted, lost on server restart | a session id is not a durable artifact; an **evidence snapshot** is |
| Page state (form values, what is currently displayed) | the browser tab | until reload | deliberately *not* in `sessionStorage`/`localStorage`; hand-offs use ids in the query string |
| Gateway decision records | in the gateway object (last 2000) | lifetime of the gateway | copied into run documents when a run is stored |

The original Studio ↔ Guided-demo hand-off uses `sessionStorage` and is untouched. The lab pages never do.

## 4. Artifact versions and invalidation

Every derived object names the exact inputs that produced it, using `content_hash()` = `sha256:` + 16 hex chars of canonical JSON
(or of the raw text for sources): policy hash, protocol hash (hash of the canonical protocol IR), recording hash (hash of its entries).
A run document records `versions: {policy, recording, protocol}`. Editing a source produces a new hash; stored records are never rewritten.
Stale detection: `/api/protocol/compile` returns the current policy hash and the page compares the protocol hash with that of the running
session; on mismatch it banners that the session's evidence describes the *old* protocol. A session's own hash never changes.
The 16-hex-char truncation is adequate against accidents, **not** against an adversary; hashes are labels here, not authentication.

## 5. Trust boundaries

* **Loopback only.** `GatewayServer`, `DemoBackend` and `LabApp` raise `ValueError` for any non-loopback host. The gateway's upstream is
  a port fixed at construction, so request data cannot choose a destination (no open proxy, no SSRF to other hosts).
* **The main web server** (`shieldlang.py serve`) defaults to 127.0.0.1 but accepts `--host`. The lab API refuses clients whose socket
  address is not loopback (§8), so a mistaken `--host 0.0.0.0` does not expose session creation or recording import.
* **Untrusted input:** source text, recordings, candidates, request bodies, and the demo fault header. Sources go through the normal
  compiler (which has size/depth limits and a ReDoS analysis); recordings are validated structurally and bounded (§7); request bodies
  go through the HTTP-subset limits. Fault injection is understood only by the demo backend.
* **Not a security boundary:** the demo identities and tokens, the demo backend, and the Bypass Lab fixture app. They exist to make
  failures reproducible, not to protect anything.
* **No outbound traffic.** No cloud services, no LLM calls, no public-target proxying; the only dependency is the Python standard library
  (Playwright is development-only and used only by `tests/browser/`).

## 6. Failure and recovery behaviour

* **Upstream ambiguity** (timeout after send, reset, 5xx, unreadable answer): outcome **Unknown**; the resource is blocked until
  evidence arrives (§4–§5 of `protocol_semantics.md`). There is no success-reset control.
* **Crash/restart of the runtime:** `recover()` turns *reserved without send-intent* into released and *sent without recorded outcome*
  into Unknown. A crash between writing the intent and sending may therefore yield a false Unknown, never a false release. The
  subprocess crash tests (`tests/test_protocol_runtime.py`, `os._exit(86)` at named crash points) exercise this for real; the page's
  *Restart session* button runs the same recovery code on the same file but does **not** kill a process, and says so.
* **Reconciliation:** probe (with a quiet period) or operator resolution with written evidence (≥ 8 characters). Conflicting evidence
  leaves the resource Unknown.
* **Lost web-server process:** live sessions disappear (in-memory registry); stored runs and evidence snapshots survive in `lab.db`.
  A page showing a vanished session reports "Session is gone" and locks the dependent steps.
* **Durability claim:** SQLite WAL with `synchronous=FULL` on a normal local filesystem. **No claim** is made for network/FUSE file
  systems (observed to fail with `disk I/O error` in the development sandbox) or for every power-loss scenario.
* **Retention:** bounded as in §3; a run larger than 2 MB is stored with its detail dropped and a visible `truncated` marker.

## 7. Traffic Lab inputs (policy selection and recording import)

Traffic Lab replays a recording of HTTP calls through the real Gateway + DemoBackend pair (§1), so its two inputs — which
compiled policy to enforce, and which recording to replay — are both validated before anything is run against that pair.

**Policy selection and "supportedness."** `GET /api/traffic/policies` (`traffic_policies` in `api.py`) lists every bundled
`.shl` example plus every imported policy artifact, each annotated by `policy_support()` (`lab/traffic.py`): a policy is
*supported* only if at least one of its compiled rules' method+path shape matches one of the demo backend's five fixed routes
(`DEMO_ROUTES` in `traffic.py` — `POST /api/orders`, `GET /api/orders/{id}`, `GET /api/orders/by-ref/{id}`,
`POST /api/orders/{id}/pay`, `POST /api/orders/{id}/confirm`). An unsupported policy is shown disabled in the dropdown with the
reason attached (`explainPolicy()` in `traffic.js`), rather than silently accepted and then failing, or silently hidden —
the person can see *why* a policy they compiled elsewhere isn't usable here. Selecting a supported policy also narrows the
Protocol dropdown to that policy's own protocols, since a protocol only means something layered on its own policy's rules.

**Recording import.** `check_recording()` / `normalized_recording()` (`traffic.py`) validate an imported recording before it is
ever executed, with a JSON-path-located error for every problem (`check_recording` returns `{"errors":[...], "warnings":[...]}`
and the page renders both without discarding the input). The checks that matter for trust, not just shape, are:

* **Every entry's path must start with `/api/`** (`_closest`/path check in `check_recording`). This is deliberate, not
  incidental: the demo backend also answers `/__ledger`, `/__orders` and `/__reset` (§1, §4) for the lab's own bookkeeping, and
  a recording is the one input a person can paste from anywhere — so it is the one place those internal routes must be
  unreachable by construction, not by convention.
* **Headers are restricted to `content-type`** — a recording cannot smuggle `X-Gateway-Principal` or `X-Demo-Fault` (the
  trust-boundary headers in §8) through the replay path; those are set by the gateway itself, never by replayed data.
  size and depth are bounded (`MAX_RECORDING_BYTES`, `MAX_BODY_BYTES`, `MAX_BODY_DEPTH`) before any JSON walk.
* **Everything else** (`ENTRY_KEYS`, `TOP_KEYS`, `METHODS`, identifier patterns) rejects shapes the replay code does not
  expect, so a malformed recording fails at check time with a located message instead of failing confusingly mid-replay or
  succeeding on a wrong interpretation.

An imported recording is stored as a content-addressed artifact like everything else in §3/§4 (`artifacts.py`,
`MAX_RECORDINGS = 50` most-recent retention) and can be deleted (`DELETE /api/traffic/recordings/<hash>`); it never becomes
reachable through anything *but* the checked-and-normalized form — the raw text the person pasted is not what gets replayed.

## 8. Local-only guard for the lab API

Every lab route (`/api/protocol/*`, `/api/traffic/*`, `/api/bypass/*`) is refused with HTTP 403 unless the connecting socket's
peer address is loopback (`Handler._loopback_client()` in `server.py`, checked before dispatch on both `do_GET` and `do_POST`).
This is enforced at the HTTP layer, independent of — and in addition to — the lower-level loopback checks already built into
`Gateway`, `DemoBackend` and `LabApp`'s own constructors (§1): those guarantee the lab's *internal* loopback services never
bind anywhere routable; this guard guarantees the *lab's own HTTP server* (the one serving these pages) also refuses a
non-loopback caller, so the two checks cover different hops of the same "nothing here talks past 127.0.0.1" property.

A request body over `MAX_BODY = 4 MiB` is rejected with HTTP 413 before the body is read into memory (`do_POST`), so an
oversized POST cannot be used to exhaust memory ahead of the loopback or shape checks in §7 running at all.

Together, §7's input validation and this guard are the complete trust boundary for data entering Traffic Lab from outside the
process: a request must originate from loopback, fit the size cap, and (for a recording) pass `check_recording()` before any
of it reaches the Gateway, the DemoBackend, or disk.
