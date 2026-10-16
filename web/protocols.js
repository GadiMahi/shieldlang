/* Protocol Studio frontend. Talks only to /api/protocol/*; all state (resources, reservations, timeline, ledger)
   is server-side and re-fetched after every action — nothing here is simulated client-side.
   The editor textarea is id="proto-source" on purpose: app.css styles #source as a full-page overlay (original Studio). */
"use strict";

const state = {
  examples: [], sessionId: null, sessionView: null, compiled: null, dirty: false,
  lastKey: null, refN: 0, requestCount: 0, recovered: false, saved: false,
};
const $ = (id) => document.getElementById(id);
const startBtn = () => document.querySelector("#session-area .js-start");
function setStartEnabled(on) { const b = startBtn(); if (b) b.disabled = !on; }

/* ---- plain-English dictionaries (what the runtime's reason codes mean) ---- */
const REASONS = {
  no_such_resource_or_not_owner: "That order doesn't exist for this caller — either the id is wrong, or it belongs to someone else.",
  blocked_unknown: "That order is frozen (Unknown) because an earlier request's outcome is uncertain. Reconcile it in step 5 first.",
  busy: "Another request currently holds a reservation on this order. Try again once it settles.",
  illegal_transition: "The order is not in a state from which this request is allowed (for example, confirming an order that was never paid).",
  binding_mismatch: "A value in this request doesn't match the value bound earlier (for example, a different card token than the one used to pay).",
};

function setBanner(text, kind) {
  const area = $("banner-area");
  area.innerHTML = "";
  if (text) area.appendChild(banner(text, kind));
}

/* ---- progress tracker + status strip ---- */
function updateFlow() {
  const done = {
    compile: !!(state.compiled && state.compiled.ok) && !state.dirty,
    session: !!state.sessionId,
    request: state.requestCount > 0,
    recover: state.recovered,
    evidence: state.saved,
  };
  let nowSet = false;
  for (const li of document.querySelectorAll("#flow li")) {
    const d = done[li.dataset.step];
    li.classList.toggle("done", !!d);
    const isNow = !d && !nowSet;
    li.classList.toggle("now", isNow);
    if (isNow) nowSet = true;
  }
}

function chip(label, value, kind) {
  return el("span", { class: `chip ${kind || ""}` }, [el("b", { text: label }), value]);
}

function updateStatus() {
  const strip = $("status-strip");
  strip.innerHTML = "";
  const c = state.compiled;
  if (!c) strip.appendChild(chip("Protocol", "not checked yet", "warn"));
  else if (state.dirty) strip.appendChild(chip("Protocol", "edited since last check — press “Check protocol”", "warn"));
  else {
    const errs = (c.diagnostics || []).filter(d => d.severity === "error").length;
    strip.appendChild(chip("Protocol", errs ? `${errs} error(s)` : "checked, no errors", errs ? "bad" : "ok"));
  }
  const s = state.sessionView;
  if (!s) strip.appendChild(chip("Session", "none"));
  else {
    strip.appendChild(chip("Session", `${s.session_id} · ${s.mode === "observe" ? "observe-only" : "enforcing"}`, s.mode === "observe" ? "warn" : "ok"));
    const unknown = s.resources.filter(r => r.unknown_tx).length;
    strip.appendChild(chip("Orders", `${s.resources.length}`));
    strip.appendChild(chip("Frozen (unknown)", String(unknown), unknown ? "bad" : "ok"));
    strip.appendChild(chip("Backend ledger", `${s.ledger.length} effect(s)${s.ledger_violations.length ? ` · ${s.ledger_violations.length} violation(s)` : ""}`, s.ledger_violations.length ? "bad" : ""));
  }
  updateFlow();
}

function setSessionActive(active) {
  for (const [card, fs] of [["request-card", "request-fs"], ["recovery-card", "recovery-fs"]]) {
    $(card).classList.toggle("locked", !active);
    $(fs).disabled = !active;
  }
  $("btn-save-evidence").disabled = !active;
}

/* ---- loading examples ---- */
async function loadExamples() {
  state.examples = await labFetch("GET", "/api/protocol/examples");
  const sel = $("example-select");
  sel.innerHTML = "";
  for (const ex of state.examples) sel.appendChild(el("option", { value: ex.name, text: `${ex.name} (${ex.protocols.join(", ")})` }));
  if (state.examples.length) { sel.value = state.examples[0].name; loadExampleInto(state.examples[0]); }
}

function loadExampleInto(ex) {
  $("proto-source").value = ex.source;
  const psel = $("protocol-select");
  psel.innerHTML = "";
  for (const p of ex.protocols) psel.appendChild(el("option", { value: p, text: p }));
}

/* ---- step 1: diagnostics ---- */
function renderDiagnostics(result) {
  const area = $("diag-area");
  area.innerHTML = "";
  const diags = result.diagnostics || [];
  const n = (sev) => diags.filter(d => d.severity === sev).length;
  area.appendChild(el("p", { class: "lab-footnote", style: "margin:0 0 6px" }, [
    `Result: ${n("error")} error(s), ${n("warning")} warning(s), ${n("info")} note(s). `,
    "Errors block starting a session. Warnings are worth a look. Notes say what a check did — and did not — prove.",
  ]));
  if (!result.ok) area.appendChild(banner(`${n("error")} error(s) — fix them before starting a session.`, "err"));
  else area.appendChild(banner("No errors — this protocol can be run.", "ok"));
  if (!diags.length) return;
  const table = el("table", { class: "lab-table" }, [
    el("thead", {}, [el("tr", {}, ["Severity", "Line", "Code", "Message"].map(h => el("th", { text: h })))]),
  ]);
  const body = el("tbody");
  for (const d of diags) {
    const lineLink = el("button", { type: "button", class: "lab-linecell", title: "Jump to this line in the editor below",
      onclick: () => jumpToSourceLine(d.span.line) }, [String(d.span.line)]);
    const row = el("tr", { class: "lab-diagrow", title: "Click to jump to this line" }, [
      el("td", {}, [badge(d.severity, d.severity === "error" ? "deny" : d.severity === "warning" ? "unknown" : "muted")]),
      el("td", { class: "mono" }, [lineLink]), el("td", { class: "mono", text: d.code }), el("td", { text: d.message }),
    ]);
    row.addEventListener("click", (ev) => { if (ev.target !== lineLink) jumpToSourceLine(d.span.line); });
    body.appendChild(row);
  }
  table.appendChild(body);
  area.appendChild(el("div", { class: "lab-scroll" }, [table]));
  area.appendChild(el("p", { class: "lab-footnote", style: "margin-top:6px" }, ["Click any row (or its line number) to jump to that line in the editor."]));
}

/* Jump the editor's caret to a 1-based source line and scroll it into view, without leaving
   step 1 — this is a plain textarea, so "jump" means placing the selection and adjusting scrollTop
   by the line's pixel offset, not a real editor API. */
function jumpToSourceLine(line1) {
  const ta = $("proto-source");
  const text = ta.value;
  const lines = text.split("\n");
  const idx = Math.max(0, Math.min(lines.length - 1, line1 - 1));
  let offset = 0;
  for (let i = 0; i < idx; i++) offset += lines[i].length + 1;
  const lineText = lines[idx] || "";
  ta.focus();
  ta.setSelectionRange(offset, offset + lineText.length);
  const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 18;
  ta.scrollTop = Math.max(0, lineHeight * (idx - 3));
  const flash = () => { ta.classList.add("lab-flash"); setTimeout(() => ta.classList.remove("lab-flash"), 600); };
  flash();
}

/* ---- step 2: graph ---- */
function renderGraph(proto) {
  const area = $("graph-area");
  area.className = "";
  area.innerHTML = "";
  if (!proto) { area.className = "lab-empty"; area.textContent = "No protocol could be built from this source — see the checks above."; return; }
  area.appendChild(el("p", { class: "lab-footnote", style: "margin:0 0 8px" }, [
    `${proto.name} · fingerprint ${proto.hash} · ${proto.diagnostics_summary.scope}`,
  ]));
  const graph = el("div", { class: "lab-graph" });
  for (const nd of proto.graph.nodes) {
    const cls = ["lab-node", nd.initial ? "initial" : "", nd.terminal ? "terminal" : ""].filter(Boolean).join(" ");
    graph.appendChild(el("span", { class: cls }, [nd.id]));
  }
  area.appendChild(graph);
  area.appendChild(el("div", { class: "lab-legend" }, [
    el("span", {}, [el("i", { class: "initial" }), "blue ring = where a new resource starts"]),
    el("span", {}, [el("i", { class: "terminal" }), "wine ring + bold = final state"]),
  ]));
  const list = el("ul", { class: "lab-edges" });
  for (const e of proto.graph.edges) {
    const isNew = !e.from || e.from === "(new)";
    list.appendChild(el("li", {}, [
      el("code", { text: e.rule }),
      isNew ? ` creates a new resource in state “${e.to}”` : ` moves a resource from “${e.from}” to “${e.to}”`,
      badge(e.kind === "create" ? "create" : "step", "muted"),
    ]));
  }
  area.appendChild(list);
}

async function compile() {
  const source = $("proto-source").value;
  let result;
  try { result = await labFetch("POST", "/api/protocol/compile", { source }); }
  catch (e) { setBanner(`Could not check the protocol: ${e.message}`, "err"); return {}; }
  state.compiled = result; state.dirty = false;
  renderDiagnostics(result);
  const psel = $("protocol-select");
  const previous = psel.value;
  const names = (result.protocols || []).map(p => p.name);
  psel.innerHTML = "";
  for (const nm of names) psel.appendChild(el("option", { value: nm, text: nm }));
  const chosen = names.includes(previous) ? previous : names[0];
  if (chosen) psel.value = chosen;
  const proto = (result.protocols || []).find(p => p.name === chosen);
  renderGraph(proto);
  setStartEnabled(!!(result.ok && proto) && !state.sessionId);
  if (state.sessionView && state.sessionView.protocol_hash !== (proto && proto.hash)) {
    setBanner("The source has changed since the running session was built, so that session's evidence describes the OLD protocol. Close it and start a new one to use the edited protocol.", "warn");
  } else setBanner("", "");
  updateStatus();
  return { result, proto };
}

/* ---- step 3: session ---- */
async function startSession() {
  const source = $("proto-source").value;
  const protocol = $("protocol-select").value;
  const mode = $("mode-select").value;
  try {
    const sess = await labFetch("POST", "/api/protocol/sessions", { source, protocol, mode });
    state.sessionId = sess.session_id; state.sessionView = sess;
    state.requestCount = 0; state.recovered = false; state.saved = false; state.lastKey = null;
    setSessionActive(true);
    $("request-result").innerHTML = ""; $("reconcile-result").innerHTML = ""; $("operator-area").innerHTML = ""; $("scenario-expect").innerHTML = "";
    $("save-evidence-result").textContent = "";
    renderScenarios(source, protocol);
    const link = $("link-traffic-lab"); link.href = sess.handoff.traffic_lab; link.hidden = false;
    renderSession();
    setBanner(sess.mode === "observe"
      ? "Observe-only session: every request will be forwarded to the backend. “Would” decisions show what enforcement would have done — they are predictions, not outcomes."
      : "", sess.mode === "observe" ? "warn" : "ok");
  } catch (e) { setBanner(`Could not start the session: ${e.message}`, "err"); }
}

function renderSession() {
  const area = $("session-area");
  const s = state.sessionView;
  area.innerHTML = "";
  area.appendChild(el("p", { style: "margin:0 0 8px" }, [
    "Running: ", el("b", { text: s.session_id }), " · ", badge(s.mode === "observe" ? "observe-only" : "enforcing", s.mode === "observe" ? "observe" : "allow"),
    ` · protocol ${s.protocol} · fingerprint ${s.protocol_hash.slice(0, 22)}…`,
  ]));
  area.appendChild(el("button", { class: "btn btn-ghost btn-sm", onclick: closeSession }, ["Close session"]));
  renderResources(s); renderReservations(s); renderTimeline(s); renderLedger(s);
  updateStatus();
}

function table(headers, rows) {
  const t = el("table", { class: "lab-table" }, [el("thead", {}, [el("tr", {}, headers.map(h => el("th", { text: h })))])]);
  const body = el("tbody");
  for (const r of rows) body.appendChild(el("tr", {}, r));
  t.appendChild(body);
  return el("div", { class: "lab-scroll" }, [t]);
}
const td = (x, cls) => el("td", cls ? { class: cls } : {}, [x instanceof Node ? x : String(x)]);

function renderResources(s) {
  const area = $("resources-area"); area.className = ""; area.innerHTML = "";
  if (!s.resources.length) { area.className = "lab-empty"; area.textContent = "No orders yet — send a “Create an order” request in step 4."; return; }
  area.appendChild(table(["Order", "Owner", "State", "Version", "Status"], s.resources.map(r => {
    const status = r.unknown_tx ? badge("unknown", "unknown") : r.reserved_tx ? badge("reserved", "observe") : badge("settled", "allow");
    return [td(r.key, "mono"), td(r.owner), td(r.state, "mono"), td(r.version), td(el("span", {}, [status, r.diverged ? badge("diverged", "unknown") : ""]))];
  })));
}

function renderReservations(s) {
  const area = $("reservations-area"); area.className = ""; area.innerHTML = "";
  if (!s.reservations.length) { area.className = "lab-empty"; area.textContent = "No attempts yet."; return; }
  area.appendChild(table(["Attempt", "Rule", "Order", "Move", "How it ended", "Evidence"], s.reservations.slice().reverse().map(r => {
    let ev = "";
    try { ev = r.evidence ? JSON.parse(r.evidence).reason || "" : ""; } catch (e) { /* noop */ }
    const kind = r.status === "unknown" ? "unknown" : r.status === "committed" ? "allow" : r.status === "released" ? "muted" : "observe";
    return [td(r.tx.slice(0, 10), "mono"), td(r.rule), td(r.key || "(new)", "mono"), td(`${r.frm || "—"} → ${r.to_state}`, "mono"), td(badge(r.status, kind)), td(ev)];
  })));
}

function renderTimeline(s) {
  const area = $("timeline-area"); area.className = ""; area.innerHTML = "";
  if (!s.timeline.length) { area.className = "lab-empty"; area.textContent = "Nothing has happened yet."; return; }
  area.appendChild(table(["Time", "Event", "Order", "Detail"], s.timeline.slice().reverse().map(e => {
    const detail = JSON.stringify(e.detail || {});
    return [td(fmtTime(e.ts)), td(e.type), td(e.key || "—", "mono"), el("td", { class: "mono", title: detail }, [detail.length > 110 ? detail.slice(0, 110) + "…" : detail])];
  })));
}

function renderLedger(s) {
  const area = $("ledger-area"); area.className = ""; area.innerHTML = "";
  if (!s.ledger.length) { area.className = "lab-empty"; area.textContent = "The backend hasn't done anything yet."; return; }
  area.appendChild(table(["#", "Operation", "Order", "By"], s.ledger.map(e => [td(e.seq), td(e.op), td(e.order_id, "mono"), td(e.principal)])));
  if (s.ledger_violations.length) {
    area.appendChild(banner(`${s.ledger_violations.length} ledger violation(s): ${s.ledger_violations.map(v => v.violation).join(", ")} — the backend did something the protocol says must never happen.`, "err"));
  } else {
    area.appendChild(el("p", { class: "lab-footnote" }, ["No violations: every backend effect so far is one the protocol allows."]));
  }
}

async function refreshSession() {
  if (!state.sessionId) return;
  try { state.sessionView = await labFetch("GET", `/api/protocol/sessions/${state.sessionId}`); renderSession(); }
  catch (e) { setBanner(`Session is gone: ${e.message}`, "warn"); state.sessionId = null; state.sessionView = null; setSessionActive(false); updateStatus(); }
}

/* ---- step 4: scenarios + request ---- */
/* Checkout (orders.shl): PayOrder.card_token is bound to ConfirmOrder.card_token, so a mismatched
   card at confirm time is refused before the backend ever sees it. */
function checkoutScenarios() {
  const id = () => state.lastKey || "ord_1";
  const ref = () => `r${++state.refN}`;
  return [
    { label: "1 · Create an order", identity: "alice", method: "POST", path: () => "/api/orders", body: () => ({ client_ref: ref(), item: "book" }), fault: "",
      expect: "Should be ALLOWED. The backend creates an order; it appears under Resources in state “created”." },
    { label: "2 · Pay it (good card)", identity: "alice", method: "POST", path: () => `/api/orders/${id()}/pay`, body: () => ({ card_token: "tok_a" }), fault: "",
      expect: "Should be ALLOWED. created → paid, and card tok_a is remembered for the later confirm." },
    { label: "3 · Confirm it (same card)", identity: "alice", method: "POST", path: () => `/api/orders/${id()}/confirm`, body: () => ({ card_token: "tok_a" }), fault: "",
      expect: "Should be ALLOWED. paid → confirmed (the final state)." },
    { label: "Confirm with a DIFFERENT card", identity: "alice", method: "POST", path: () => `/api/orders/${id()}/confirm`, body: () => ({ card_token: "tok_b" }), fault: "",
      expect: "Run on a PAID order. Should be REFUSED (binding_mismatch): the card differs from the one used to pay. The backend never sees the request." },
    { label: "Confirm WITHOUT paying", identity: "alice", method: "POST", path: () => `/api/orders/${id()}/confirm`, body: () => ({ card_token: "tok_a" }), fault: "",
      expect: "Create a NEW order first, then run this. Should be REFUSED (illegal_transition): a created order may not jump straight to confirmed." },
    { label: "Pay someone else's order (bob)", identity: "bob", method: "POST", path: () => `/api/orders/${id()}/pay`, body: () => ({ card_token: "tok_a" }), fault: "",
      expect: "Should be REFUSED (no_such_resource_or_not_owner): the order belongs to alice." },
    { label: "Pay — card declined", identity: "alice", method: "POST", path: () => `/api/orders/${id()}/pay`, body: () => ({ card_token: "tok_a" }), fault: "decline",
      expect: "Run on a CREATED order. Backend answers 402 and changes nothing → the attempt is RELEASED and the order stays “created”." },
    { label: "Pay — response lost", identity: "alice", method: "POST", path: () => `/api/orders/${id()}/pay`, body: () => ({ card_token: "tok_a" }), fault: "drop_after_commit",
      expect: "Run on a CREATED order. The backend applies the payment but the answer never arrives → outcome UNKNOWN, the order freezes. Then go to step 5 and reconcile." },
  ];
}

/* CheckoutUnbound (orders_unbound.shl): the SAME rules and the SAME backend, but the protocol has
   no `bind` line. These scenarios show what that one missing line costs. */
function checkoutUnboundScenarios() {
  const id = () => state.lastKey || "ord_1";
  const ref = () => `r${++state.refN}`;
  return [
    { label: "1 · Create an order", identity: "alice", method: "POST", path: () => "/api/orders", body: () => ({ client_ref: ref(), item: "book" }), fault: "",
      expect: "Should be ALLOWED, exactly as in Checkout — creation doesn't involve card tokens." },
    { label: "2 · Pay it (card tok_a)", identity: "alice", method: "POST", path: () => `/api/orders/${id()}/pay`, body: () => ({ card_token: "tok_a" }), fault: "",
      expect: "Should be ALLOWED. created → paid. Unlike Checkout, this protocol does not remember tok_a for later." },
    { label: "3 · Confirm with a DIFFERENT card (tok_b)", identity: "alice", method: "POST", path: () => `/api/orders/${id()}/confirm`, body: () => ({ card_token: "tok_b" }), fault: "",
      expect: "Run on a PAID order. In Checkout this is REFUSED (binding_mismatch). Here it is ALLOWED: nothing ties this card to the one that paid, and the backend applies it anyway — the ledger will show a confirm with tok_b on an order paid with tok_a." },
    { label: "Confirm WITHOUT paying", identity: "alice", method: "POST", path: () => `/api/orders/${id()}/confirm`, body: () => ({ card_token: "tok_a" }), fault: "",
      expect: "Create a NEW order first, then run this. Still REFUSED (illegal_transition): the created→paid→confirmed ORDER of steps is still enforced — only the card-matching guarantee was removed." },
    { label: "Pay someone else's order (bob)", identity: "bob", method: "POST", path: () => `/api/orders/${id()}/pay`, body: () => ({ card_token: "tok_a" }), fault: "",
      expect: "Should be REFUSED (no_such_resource_or_not_owner): ownership is unrelated to binding, so this is the same as in Checkout." },
    { label: "Pay — response lost", identity: "alice", method: "POST", path: () => `/api/orders/${id()}/pay`, body: () => ({ card_token: "tok_a" }), fault: "drop_after_commit",
      expect: "Run on a CREATED order. Same as Checkout: outcome UNKNOWN, the order freezes until you reconcile in step 5." },
  ];
}

const SCENARIO_SETS = { Checkout: checkoutScenarios, CheckoutUnbound: checkoutUnboundScenarios };

function renderScenarios(source, protocolName) {
  const box = $("scenario-chips");
  box.innerHTML = "";
  const build = SCENARIO_SETS[protocolName];
  if (!build) {
    box.appendChild(el("span", { class: "lab-footnote" }, [`Scenario buttons are written for the Checkout and CheckoutUnbound examples; “${protocolName || "this protocol"}” has none, so fill in the form by hand.`]));
    return;
  }
  for (const sc of build()) {
    box.appendChild(el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => {
      $("req-identity").value = sc.identity; $("req-method").value = sc.method; $("req-path").value = sc.path();
      $("req-body").value = JSON.stringify(sc.body()); $("req-fault").value = sc.fault;
      const ex = $("scenario-expect"); ex.innerHTML = "";
      ex.appendChild(el("div", { class: "lab-explain" }, [el("span", { class: "what" }, [`Scenario: ${sc.label}`]), sc.expect,
        state.lastKey ? "" : " (No order yet — the form uses ord_1; create one first.)"]));
    } }, [sc.label]));
  }
}

function explainRequest(out) {
  const rec = out.record || {};
  const stages = rec.stages || [];
  const withReason = stages.slice().reverse().find(s => s && s.reason);
  const reason = withReason && withReason.reason;
  const dec = out.decision;
  let what, why;
  if (dec === "deny") {
    what = "Refused before reaching the backend.";
    why = (REASONS[reason] || (reason ? `Reason: ${reason}.` : "A rule or the protocol rejected it.")) + " The backend ledger is unchanged.";
  } else if (dec === "forwarded") {
    what = "Forwarded (observe-only mode).";
    why = `Nothing was blocked. If this session were enforcing, the gateway would have said: ${rec.would_decision || "?"}${(rec.would_reasons || []).length ? ` (${rec.would_reasons.join(", ")})` : ""}. The backend answered HTTP ${out.status}.`;
  } else if (dec === "unknown" || dec === "upstream_error" || dec === "not_delivered") {
    what = "Outcome UNKNOWN.";
    why = "The request was sent but the runtime cannot tell whether the backend applied it. The order is frozen — later requests on it are refused — until you reconcile it in step 5. Compare the Backend ledger on the right: it shows the truth.";
  } else if (dec === "allow") {
    what = `Allowed — the backend answered HTTP ${out.status}.`;
    why = "See Reservations for how the runtime classified the result: committed (state moved), released (no effect) or unknown.";
  } else { what = `Decision: ${dec}.`; why = ""; }
  return el("div", { class: "lab-explain" }, [el("span", { class: "what" }, [what]), why]);
}

async function sendRequest() {
  if (!state.sessionId) return;
  let body = null;
  const raw = $("req-body").value.trim();
  if (raw) { try { body = JSON.parse(raw); } catch (e) { setBanner(`The body is not valid JSON: ${e.message}`, "err"); return; } }
  const payload = {
    method: $("req-method").value, path: $("req-path").value,
    identity: $("req-identity").value || null, body, fault: $("req-fault").value || undefined,
  };
  try {
    const out = await labFetch("POST", `/api/protocol/sessions/${state.sessionId}/request`, payload);
    const area = $("request-result");
    area.innerHTML = "";
    area.appendChild(el("p", { style: "margin:10px 0 0" }, [`HTTP ${out.status} · `, decisionBadge(out.decision)]));
    area.appendChild(explainRequest(out));
    area.appendChild(el("details", {}, [el("summary", { text: "Raw response body" }), pre(out.response)]));
    const id = out.response && out.response.id;
    if (typeof id === "string") { state.lastKey = id; $("rec-key").value = id; }
    state.requestCount += 1;
    state.sessionView = out.session;
    setBanner("", "");
    renderSession();
  } catch (e) { setBanner(`Request failed: ${e.message}`, "err"); }
}

/* ---- step 5: reconcile / recover ---- */
async function reconcile() {
  const key = $("rec-key").value.trim();
  if (!key || !state.sessionId) { setBanner("Type the order id to reconcile (e.g. ord_1).", "warn"); return; }
  try {
    const out = await labFetch("POST", `/api/protocol/sessions/${state.sessionId}/reconcile`, { key });
    const area = $("reconcile-result");
    area.innerHTML = "";
    const r = out.result || {};
    const text = {
      committed: "The probe saw the order in the target state, so the earlier request DID take effect. It is now committed and the order is unfrozen.",
      released: "The probe saw the order still in its old state after the quiet period, so the request did NOT take effect. It is released and the order is unfrozen.",
      inconclusive: "The probe could not settle it (unreadable answer, or still inside the quiet window). The order stays frozen — wait and retry, or resolve it by hand with written evidence below.",
      conflict: "The probe's evidence contradicts the runtime's records (different owner, or a state it can't map). The order stays frozen; a human must decide, with written evidence.",
      nothing_to_reconcile: "That order isn't frozen — there is nothing to reconcile.",
    }[r.result] || `Result: ${r.result}`;
    area.appendChild(el("div", { class: "lab-explain" }, [el("span", { class: "what" }, [`Reconcile → ${r.result || "?"}`]), text]));
    area.appendChild(el("details", {}, [el("summary", { text: "Raw evidence" }), pre(out.result)]));
    state.recovered = true;
    state.sessionView = out.session;
    renderSession(); renderOperatorArea(out.result);
  } catch (e) { setBanner(`Reconcile failed: ${e.message}`, "err"); }
}

function renderOperatorArea(result) {
  const area = $("operator-area");
  area.innerHTML = "";
  if (result.result !== "inconclusive" && result.result !== "conflict") return;
  area.appendChild(el("p", { class: "lab-help warn" }, [
    "Manual resolution. A person looks at real evidence (the backend's own records, a receipt) and records the decision with at least 8 characters of explanation. There is deliberately no “reset to success” button.",
  ]));
  const tx = el("input", { placeholder: "attempt id (see Reservations table)", "aria-label": "attempt id" });
  const evid = el("input", { placeholder: "evidence text (min 8 chars)", "aria-label": "evidence", style: "min-width:240px" });
  area.appendChild(el("div", { class: "lab-row" }, [tx, evid,
    el("button", { class: "btn btn-ghost btn-sm", onclick: () => operatorResolve(tx.value, "committed", evid.value) }, ["It happened (committed)"]),
    el("button", { class: "btn btn-ghost btn-sm", onclick: () => operatorResolve(tx.value, "released", evid.value) }, ["It did not happen (released)"]),
  ]));
}

async function operatorResolve(tx, decision, evidenceText) {
  try {
    const out = await labFetch("POST", `/api/protocol/sessions/${state.sessionId}/operator`, { tx, decision, evidence_text: evidenceText });
    state.sessionView = out.session; state.recovered = true; renderSession();
    setBanner(`Recorded: attempt ${tx.slice(0, 10)} resolved as ${decision}, with your evidence text.`, "ok");
  } catch (e) { setBanner(`Operator resolution failed: ${e.message}`, "err"); }
}

async function restartSession() {
  if (!state.sessionId) return;
  try {
    const out = await labFetch("POST", `/api/protocol/sessions/${state.sessionId}/restart`, {});
    setBanner(`${out.recovery_note} Recovery pass: ${out.recovery.released} attempt(s) released (never sent), ${out.recovery.unknown} left Unknown (sent, outcome not recorded).`, "ok");
    state.sessionView = out; state.recovered = true; renderSession();
  } catch (e) { setBanner(`Restart failed: ${e.message}`, "err"); }
}

async function closeSession() {
  if (!state.sessionId) return;
  await labFetch("DELETE", `/api/protocol/sessions/${state.sessionId}`).catch(() => {});
  state.sessionId = null; state.sessionView = null;
  $("session-area").innerHTML = "";
  $("session-area").append(el("p", { class: "lab-empty" }, ["Session closed. Start another to keep experimenting."]),
    el("button", { class: "btn btn-primary js-start" }, ["Start session"]));
  for (const id of ["resources-area", "reservations-area", "timeline-area", "ledger-area"]) { $(id).className = "lab-empty"; $(id).textContent = "No session yet."; }
  $("link-traffic-lab").hidden = true;
  setSessionActive(false); setStartEnabled(!!(state.compiled && state.compiled.ok) && !state.dirty); updateStatus();
}

async function saveEvidence() {
  if (!state.sessionId) return;
  try {
    const out = await labFetch("POST", `/api/protocol/sessions/${state.sessionId}/evidence`, {});
    $("save-evidence-result").textContent = ` Saved as ${out.run_id} (kept on the server; exportable from Traffic Lab).`;
    state.saved = true; updateFlow();
  } catch (e) { setBanner(`Could not save evidence: ${e.message}`, "err"); }
}

function wire() {
  $("btn-compile").addEventListener("click", compile);
  $("session-area").addEventListener("click", (ev) => { if (ev.target.closest(".js-start")) startSession(); });
  $("btn-send").addEventListener("click", sendRequest);
  $("btn-reconcile").addEventListener("click", reconcile);
  $("btn-restart").addEventListener("click", restartSession);
  $("btn-save-evidence").addEventListener("click", saveEvidence);
  $("example-select").addEventListener("change", (e) => {
    const ex = state.examples.find(x => x.name === e.target.value);
    if (ex) { loadExampleInto(ex); compile(); }
  });
  $("proto-source").addEventListener("input", () => {
    state.dirty = true; setStartEnabled(false); updateStatus();
  });
}

async function init() {
  wire();
  setSessionActive(false);
  await loadExamples();
  const params = queryParams();
  if (params.policy_hash) {
    try {
      const src = await labFetch("GET", `/api/protocol/source/${params.policy_hash}`);
      $("proto-source").value = src.source;
      await compile();
      setBanner("Loaded the policy handed off from another page, identified by its content hash.", "ok");
    } catch (e) {
      setBanner(`Could not load the handed-off policy (${e.message}); showing the default example instead.`, "warn");
      await compile();
    }
  } else await compile();
  if (params.protocol) {
    const psel = $("protocol-select");
    if ([...psel.options].some(o => o.value === params.protocol)) { psel.value = params.protocol; await compile(); }
  }
  updateStatus();
}

init();
