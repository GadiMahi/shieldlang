# Protocol Studio semantics (normative for this implementation)

Status: implementation spec written *before* the runtime. The hand-derived traces in section 7 are the
independent oracle; tests encode them literally and never regenerate them from the runtime.

## 1. Scope and assumptions

* One protocol per resource type. Single-resource transitions. Bounded scalar string bindings.
  Single host, SQLite store. Unsupported constructs are compile diagnostics (codes PE9xx), never ignored.
* **Complete mediation (assumption, not a guarantee):** every mutation of governed resources passes through
  the gateway or is reported to it (adoption). Out-of-band mutations and backend-internal races are outside the guarantee.
* **Trusted principal:** supplied by the adapter (`Gateway.identities`: fixed demo token table), never read from
  a request header value claimed by the client. Demo identities are labelled test fixtures, not authentication.
* **No exactly-once claim.** The monitor guarantees at most one *unresolved attempt* per resource and conservative
  recovery. Retrying safely needs application idempotency/reconciliation (`reconcile`, `quiesce`, `contract`).
* Declared HTTP status classes are *claims by the protocol author* about the application. They are only as strong as
  the `contract "…"` text the author supplies; the compiler requires one whenever a `failure` class is declared.

## 2. Events and keys

* Events are existing rule names. A rule is an event of at most one protocol.
* `create R id "f" ref r` — resource-creating event; the backend assigns the key, read from top-level JSON response field `f`.
  `ref r` names a request body field used as correlation value for lost-response reconciliation (`probe get "…{ref}…"`).
* `step R from A to B` — transition. Key = path parameter named by `key NAME` (a claim by the client).
* `bind A.f == B.g` — value of body field `f` at the commit of A is stored in the resource record; at admission of B the
  request field `g` must equal it. Scalar strings only, ≤ 256 bytes.

## 3. Request lifecycle (per request)

1. Stateless validation (existing VM). Failure → deny, protocol state untouched.
2. Event lookup by matched rule. Not an event → forwarded under stateless policy only (reported "ungoverned").
3. Admission on key *k* (steps), checks in this order (first failing check is the reason):
   `no_such_resource_or_not_owner` → `blocked_unknown` → `busy` → `illegal_transition` → `binding_mismatch`.
   Success = atomic reservation `(tx, event, from, to, sent=0)` via compare-and-set on the resource row
   (`state=from ∧ reserved_tx IS NULL ∧ unknown_tx IS NULL ∧ owner=principal ∧ version=v`). No state change yet.
   Create events reserve with no key. Deny `blocked_unknown` if an unresolved create has the same `(principal, ref)`.
4. **Send intent** durably recorded (`sent=1`) *before* the upstream call.
5. Upstream outcome classification:
   * connect failure (no bytes sent) → `released` (definitive no effect);
   * status in `success` class (and, for create, a JSON string id present) → **commit**: state←to, version+1, bindings stored
     (create: resource row created with owner=principal, state=initial) in one transaction with the reservation resolution;
   * status in `failure` class → **release** (needs `contract`);
   * anything else after send (timeout, reset, 5xx, unparseable, success without id) → **unknown**.
6. `unknown` blocks all new reservations on that key (and, for creates, that ref) until reconciled.
7. Client disconnect never changes steps 4–6: the outcome is recorded before the reply is written and nothing is retried.

## 4. Reservation states

`reserved → sent → committed | released | unknown`; `unknown → committed | released` only via reconciliation evidence;
`reserved → released` on pre-send abort or recovery. Invariants: I1 ≤1 unresolved reservation per key; I2 committed
transitions form a path from `initial`; I3 version = 1 at creation and increases by exactly 1 at each successful reservation (commit and release do not change it); I4 `unknown` is
absorbing until evidence arrives.

## 5. Recovery and reconciliation

* Crash points (named, injectable by subprocess termination): `after_reserve`, `after_send_intent`,
  `after_upstream_response`, `after_outcome_persisted`.
* `recover()` at start: `reserved` (no send intent) → released; `sent` without outcome → unknown. A crash between the send-intent
  write and the actual send may yield a *false* unknown; it never yields a false release.
* Reconcile (probe): `GET` the declared path with the key (or ref). Observed state == `to` → commit; observed == `from` →
  release **only if** `now − sent_at ≥ quiesce` (otherwise "inconclusive: attempt may still be in flight"); anything else → conflict (stays unknown).
  Observed owner must equal the reservation principal.
* Operator resolution requires non-empty evidence text and is recorded as `operator` evidence, never as probe evidence.
* Adoption: probe the backend, require owner match, create the resource row at the observed state.
* Store: SQLite WAL, `synchronous=FULL`. This is not a claim of durability across every hardware/OS failure.

## 6. Observe-only (shadow) mode

Requests are always forwarded. A separate shadow store records `would_decision`. Shadow state advances only on *actual*
upstream success of requests the shadow would have admitted; a would-deny request that nonetheless succeeded marks the
resource `diverged` and the shadow stops predicting for it (`shadow_diverged`). Shadow and enforced stores never share rows.
False positives/negatives are reported only for entries carrying independently supplied labels.

## 7. Hand-derived expected traces (oracle)

Protocol Checkout: created —PayOrder→ paid —ConfirmOrder→ confirmed (terminal). Principals alice, bob. `k1` = key assigned to alice's order.
Notation: `ok` = admitted, outcome S/F/U = success/failure/unknown.

| # | Steps | Expected |
|---|---|---|
| H1 | alice Create → S(k1); alice Pay k1 → S; alice Confirm k1 → S | all admitted; final state confirmed, version 3 |
| H2 | alice Create S; alice Confirm k1 | denied illegal_transition (created is not paid) |
| H3 | alice Create S; alice Pay k1 → F; alice Confirm k1 | pay released (state created, version 2); confirm denied illegal_transition |
| H4 | alice Create S; bob Pay k1 | denied no_such_resource_or_not_owner |
| H5 | alice Create S (k1); alice Create S (k2); alice Pay k2 S; alice Confirm k1 | confirm denied illegal_transition (k1 still created); k2 is paid |
| H6 | H1 then alice Confirm k1 | denied illegal_transition (terminal); version unchanged |
| H7 | alice Create S; Pay k1 admitted (reserved); second Pay k1 | second denied busy; first still reserved |
| H8 | alice Create S; Pay k1 → U; Pay k1 again | denied blocked_unknown |
| H9 | H8 then reconcile observes `paid` | pay committed; state paid; later Confirm admitted |
| H10 | H8 then reconcile observes `created` before quiesce | inconclusive, still unknown; after quiesce → released, Pay admitted again |
| H11 | alice Create → U (no id); Create with same ref | second denied blocked_unknown; reconcile-by-ref finds order → committed with its key |
| H12 | crash after send intent (state paid not reached) | recovery: reservation unknown; key blocked |
| H13 | crash after reserve, before send intent | recovery: reservation released; Pay admitted again |
| H14 | crash after upstream success, before persist | recovery: unknown; reconcile observes `paid` → committed |
| H15 | two concurrent Confirm on a paid order | exactly one admitted; backend ledger shows ≤1 confirm effect |

*Correction (made before any runtime existed):* H1's version was first written as 4; applying the version rule of I3 (create=1, +1 per reservation) gives 3. The rule, not the runtime, decided this.
