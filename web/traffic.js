/* Traffic Lab frontend: every run is a real replay against a real local gateway + backend (see /api/traffic/run),
   stored server-side with a run_id. Nothing here is a canned animation, and observe-mode rows are never labeled
   allow/deny — only 'forwarded', with a separate 'would_decision'. */
"use strict";

const state = { lastRun: null, policies: [] };
const $ = (id) => document.getElementById(id);

const REASONS = {
  no_such_resource_or_not_owner: "order doesn't exist for this caller / not the owner",
  blocked_unknown: "order frozen: an earlier outcome is uncertain",
  busy: "another request holds the order",
  illegal_transition: "not allowed from the order's current state",
  binding_mismatch: "value differs from the one bound earlier",
  shadow_diverged: "observe-mode shadow state no longer matches the backend",
};

function explainMode(mode) {
  const box = $("mode-explain");
  box.innerHTML = "";
  if (mode === "observe") {
    box.appendChild(el("span", { class: "what" }, ["Observe-only: nothing is blocked."]));
    box.append("Every request is forwarded to the backend. The “Would” column shows what enforcement would have done — a prediction, not an outcome — so the ledger may contain effects enforcement would have prevented.");
  } else {
    box.appendChild(el("span", { class: "what" }, ["Enforce: violations are blocked."]));
    box.append("A denied request is never forwarded. The ledger should therefore contain only effects the policy allows; any “violation” there means enforcement failed.");
  }
}

function setBanner(text, kind) {
  const area = document.getElementById("banner-area");
  area.innerHTML = "";
  if (text) area.appendChild(banner(text, kind));
}

async function loadPolicies() {
  const list = await labFetch("GET", "/api/traffic/policies");
  const sel = $("policy-select");
  const previous = sel.value;
  sel.innerHTML = "";
  for (const p of list) {
    const opt = el("option", { value: p.value, text: `${p.name}${p.source === "imported" ? " (imported)" : ""}${p.supported ? "" : " — not usable here"}` });
    opt.disabled = !p.supported;
    sel.appendChild(opt);
  }
  if ([...sel.options].some(o => o.value === previous)) sel.value = previous;
  state.policies = list;
  explainPolicy();
}

function explainPolicy() {
  const sel = $("policy-select");
  const p = (state.policies || []).find(x => x.value === sel.value);
  const hint = $("policy-hint");
  const psel = $("protocol-select");
  const prevProto = psel.value;
  psel.innerHTML = '<option value="">(none — rules only, stateless)</option>';
  if (!p) { hint.textContent = ""; return; }
  hint.textContent = p.supported ? `${p.relevant_rules.length} rule(s) match the demo backend.` : p.reason;
  hint.style.color = p.supported ? "" : "var(--wine-2)";
  for (const name of p.protocols) psel.appendChild(el("option", { value: name, text: `${name} (also enforce request order)` }));
  if ([...psel.options].some(o => o.value === prevProto)) psel.value = prevProto;
}

async function loadRecordings() {
  const list = await labFetch("GET", "/api/traffic/recordings");
  const sel = $("recording-select");
  const previous = sel.value;
  sel.innerHTML = "";
  for (const r of list) sel.appendChild(el("option", { value: r.value, text: `${r.name || r.hash.slice(0, 12)} (${r.entries || "?"} entries)${r.source === "imported" ? " — imported" : ""}` }));
  if ([...sel.options].some(o => o.value === previous)) sel.value = previous;
  renderImportedList(list.filter(r => r.source === "imported"));
}

function renderImportedList(imported) {
  const area = $("imported-list");
  area.className = imported.length ? "" : "lab-empty";
  area.innerHTML = "";
  if (!imported.length) { area.textContent = "No imported recordings yet — use \u201cImport your own recording\u201d above."; return; }
  for (const r of imported) {
    area.appendChild(el("div", { class: "lab-row", style: "justify-content:space-between; margin-bottom:6px" }, [
      el("span", { text: `${r.name || r.hash.slice(0, 14)} (${r.entries} entries)` }),
      el("button", { class: "btn btn-ghost btn-sm", onclick: () => deleteRecording(r.hash) }, ["Delete"]),
    ]));
  }
}

async function deleteRecording(hash) {
  await labFetch("DELETE", `/api/traffic/recordings/${hash}`);
  await loadRecordings();
}

function renderImportIssues(check) {
  const area = $("import-result");
  area.innerHTML = "";
  if (check.errors.length) {
    area.appendChild(banner(`${check.errors.length} problem(s) found \u2014 nothing was imported.`, "err"));
    const t = el("table", { class: "lab-table" }, [el("thead", {}, [el("tr", {}, ["Where", "Problem"].map(h => el("th", { text: h })))])]);
    const body = el("tbody");
    for (const e of check.errors) body.appendChild(el("tr", {}, [el("td", { class: "mono", text: e.path }), el("td", { text: e.message })]));
    t.appendChild(body); area.appendChild(el("div", { class: "lab-scroll" }, [t]));
  } else {
    area.appendChild(banner("No problems found.", "ok"));
  }
  for (const w of check.warnings || []) area.appendChild(el("p", { class: "lab-footnote" }, [`Note: ${w.path} \u2014 ${w.message}`]));
}

async function checkRecordingText() {
  let doc;
  try { doc = JSON.parse($("import-text").value); } catch (e) { $("import-result").innerHTML = ""; $("import-result").appendChild(banner(`Not valid JSON: ${e.message}`, "err")); return null; }
  const check = await labFetch("POST", "/api/traffic/recordings/check", { recording: doc });
  renderImportIssues(check);
  return check.errors.length ? null : doc;
}

async function importRecordingText() {
  const doc = await checkRecordingText();
  if (!doc) return;
  try {
    const out = await labFetch("POST", "/api/traffic/recordings", { recording: doc });
    $("import-result").appendChild(el("p", { class: "lab-footnote" }, [`Imported as ${out.hash} (${out.entries} entries). It now appears in the Recording dropdown.`]));
    await loadRecordings();
    $("recording-select").value = out.hash;
  } catch (e) {
    $("import-result").appendChild(banner(`Import failed: ${e.message}`, "err"));
  }
}

function renderBypassHandoff(h) {
  const card = $("bypass-handoff-card");
  card.hidden = false;
  const area = $("bypass-handoff-area");
  area.innerHTML = "";
  area.appendChild(banner(h.gateway_limitation, "warn"));
  area.appendChild(el("p", { style: "margin:10px 0 0" }, [
    el("b", { text: h.title }), ` \u00b7 run ${h.run_id} \u00b7 `, h.confirmed_bypass ? badge("confirmed (fixture)", "deny") : badge("not confirmed", "allow"),
  ]));
  area.appendChild(el("div", { class: "lab-scroll" }, [el("table", { class: "lab-table" }, [el("tbody", {}, [
    el("tr", {}, [el("td", { text: "Raw candidate" }), el("td", {}, [pre(h.raw)])]),
    el("tr", {}, [el("td", { text: "Policy-side interpretation" }), el("td", {}, [pre(h.policy_interpretation)])]),
    el("tr", {}, [el("td", { text: "Application-side interpretation" }), el("td", {}, [pre(h.app_interpretation)])]),
    el("tr", {}, [el("td", { text: "Independent oracle" }), el("td", { text: JSON.stringify(h.oracle) })]),
    el("tr", {}, [el("td", { text: "Fixture's own local replay" }), el("td", {}, [pre(h.replay)])]),
  ])])]));
}

async function run() {
  const payload = {
    policy: document.getElementById("policy-select").value,
    protocol: document.getElementById("protocol-select").value || undefined,
    recording: document.getElementById("recording-select").value,
    mode: document.getElementById("mode-select").value,
  };
  try {
    const doc = await labFetch("POST", "/api/traffic/run", payload);
    state.lastRun = doc;
    render(doc);
    setBanner("", "");
    loadRunsList();
  } catch (e) {
    setBanner(`Run failed: ${e.message}`, "err");
  }
}

function renderBypassHandoff(h) {
  const card = $("bypass-handoff-card");
  card.hidden = false;
  const area = $("bypass-handoff-area");
  area.innerHTML = "";
  area.appendChild(banner(h.gateway_limitation, "warn"));
  area.appendChild(el("p", { style: "margin:10px 0 0" }, [
    el("b", { text: h.title }), ` · run ${h.run_id} · `, h.confirmed_bypass ? badge("confirmed (fixture)", "deny") : badge("not confirmed", "allow"),
  ]));
  area.appendChild(el("div", { class: "lab-scroll" }, [el("table", { class: "lab-table" }, [el("tbody", {}, [
    el("tr", {}, [el("td", { text: "Raw candidate" }), el("td", {}, [pre(h.raw)])]),
    el("tr", {}, [el("td", { text: "Policy-side interpretation" }), el("td", {}, [pre(h.policy_interpretation)])]),
    el("tr", {}, [el("td", { text: "Application-side interpretation" }), el("td", {}, [pre(h.app_interpretation)])]),
    el("tr", {}, [el("td", { text: "Independent oracle" }), el("td", { text: JSON.stringify(h.oracle) })]),
    el("tr", {}, [el("td", { text: "Fixture's own local replay" }), el("td", {}, [pre(h.replay)])]),
  ])])]));
}

function render(doc) {
  document.getElementById("run-versions").textContent =
    `· run ${doc.run_id} · policy ${doc.versions.policy.slice(0, 14)} · recording ${doc.versions.recording.slice(0, 14)}` +
    (doc.versions.protocol ? ` · protocol ${doc.versions.protocol.slice(0, 14)}` : "");
  document.getElementById("mode-footnote").textContent = doc.evidence_note;
  explainMode(doc.mode);
  renderEntries(doc);
  renderEval(doc.evaluation);
  renderLatency(doc.latency_ms);
  renderLedger(doc);
  renderProto(doc);
  const link = document.getElementById("link-studio");
  if (doc.handoff) { link.href = doc.handoff.protocol_studio; link.hidden = false; } else { link.hidden = true; }
}

function whyText(e) {
  const reasons = (e.would_reasons && e.would_reasons.length) ? e.would_reasons
    : (e.stages || []).filter(st => st && st.reason).map(st => st.reason);
  return [...new Set(reasons)].map(r => REASONS[r] || r).join("; ");
}

function renderEntries(doc) {
  const area = $("entries-area");
  area.className = ""; area.innerHTML = "";
  const t = el("table", { class: "lab-table" }, [el("thead", {}, [el("tr", {}, [
    ["#", "Request id"], ["Request", "Method and path"], ["HTTP", "Status the caller received"], ["Decision", "What actually happened to the request"],
    ["Would", "Observe-only: what enforcement would have done"], ["Why", "Reason given for a denial"], ["Label", "Recording author's judgement"], ["Time", "Latency"],
  ].map(([h, tip]) => el("th", { title: tip, text: h })))])]);
  const body = el("tbody");
  for (const e of doc.entries) {
    body.appendChild(el("tr", {}, [
      el("td", { class: "mono", text: String(e.id) }), el("td", { class: "mono", text: `${e.method} ${e.path}` }), el("td", { text: String(e.status) }),
      el("td", {}, [decisionBadge(e.decision)]),
      el("td", {}, [e.would_decision ? badge(e.would_decision, e.would_decision === "deny" ? "deny" : "allow") : "—"]),
      el("td", { text: whyText(e) || "—" }),
      el("td", {}, [e.label ? badge(e.label.legitimate ? "legitimate" : "attack", e.label.legitimate ? "allow" : "deny") : "—"]),
      el("td", { text: `${e.latency_ms} ms` }),
    ]));
  }
  t.appendChild(body); area.appendChild(el("div", { class: "lab-scroll" }, [t]));
}

function renderEval(ev) {
  const area = $("eval-area");
  area.className = ""; area.innerHTML = "";
  const cell = (n, label, good) => el("td", { style: `background:${good ? "#e3f3e6" : "var(--wine-soft)"}` }, [el("b", { text: String(n) }), label]);
  area.appendChild(el("table", { class: "lab-matrix" }, [
    el("thead", {}, [el("tr", {}, [el("th"), el("th", { text: "Blocked" }), el("th", { text: "Let through" })])]),
    el("tbody", {}, [
      el("tr", {}, [el("th", { text: "Attack" }), cell(ev.true_positive, "TP — attack stopped", true), cell(ev.false_negative, "FN — attack got through", false)]),
      el("tr", {}, [el("th", { text: "Legitimate" }), cell(ev.false_positive, "FP — good request blocked", false), cell(ev.true_negative, "TN — good request served", true)]),
    ]),
  ]));
  if (ev.unpredicted) area.appendChild(el("p", { class: "lab-footnote" }, [`${ev.unpredicted} labelled entr${ev.unpredicted === 1 ? "y was" : "ies were"} left out because the observe-mode shadow state had diverged from the backend (its “allow” would not be a real prediction).`]));
  area.appendChild(el("p", { class: "lab-footnote" }, [ev.note]));
}

function renderLatency(lat) {
  const area = $("latency-area");
  area.className = ""; area.innerHTML = "";
  area.appendChild(el("p", { style: "margin:0" }, [`Median ${lat.median} ms per request · slowest ${lat.max} ms · whole run ${lat.wall} ms.`]));
}

function renderLedger(doc) {
  const area = $("ledger-area");
  area.className = ""; area.innerHTML = "";
  if (!doc.ledger.length) { area.className = "lab-empty"; area.textContent = "The backend recorded no effects — nothing reached it, or nothing it received changed state."; return; }
  const t = el("table", { class: "lab-table" }, [el("thead", {}, [el("tr", {}, ["#", "Operation", "Order", "By"].map(h => el("th", { text: h })))])]);
  const body = el("tbody");
  for (const e of doc.ledger) body.appendChild(el("tr", {}, [el("td", { text: String(e.seq) }), el("td", { text: e.op }), el("td", { class: "mono", text: e.order_id }), el("td", { text: e.principal })]));
  t.appendChild(body); area.appendChild(el("div", { class: "lab-scroll" }, [t]));
  if (doc.ledger_violations.length) {
    area.appendChild(banner(`${doc.ledger_violations.length} ledger violation(s): ${doc.ledger_violations.map(v => v.violation).join(", ")}. ` +
      (doc.mode === "observe" ? "Expected in observe-only mode: nothing was blocked, so effects enforcement would have prevented really happened."
                              : "This is a real failure: enforcement did not stop a bad effect."), doc.mode === "observe" ? "warn" : "err"));
  } else area.appendChild(el("p", { class: "lab-footnote" }, ["No violations: every effect in this ledger is one the protocol allows."]));
}

function renderProto(doc) {
  const area = $("proto-area");
  area.className = ""; area.innerHTML = "";
  if (!doc.protocol_state) { area.className = "lab-empty"; area.textContent = "No protocol was selected for this run (rules only)."; return; }
  const res = doc.protocol_state.resources;
  if (Array.isArray(res) && res.length && typeof res[0] === "object") {
    const keys = ["key", "owner", "state", "version", "reserved_tx", "unknown_tx"].filter(k => k in res[0]);
    const t = el("table", { class: "lab-table" }, [el("thead", {}, [el("tr", {}, keys.map(k => el("th", { text: k })))])]);
    const body = el("tbody");
    for (const r of res) body.appendChild(el("tr", {}, keys.map(k => el("td", { class: "mono", text: String(r[k] === null ? "—" : r[k]) }))));
    t.appendChild(body); area.appendChild(el("div", { class: "lab-scroll" }, [t]));
  } else area.appendChild(pre(res));
}

async function loadRunsList() {
  const area = document.getElementById("runs-list");
  const runs = await labFetch("GET", "/api/traffic/runs");
  area.innerHTML = "";
  if (!runs.length) { area.className = "lab-empty"; area.textContent = "No runs yet."; return; }
  area.className = "";
  for (const r of runs.slice(0, 15)) {
    const row = el("div", { class: "lab-row", style: "justify-content:space-between" }, [
      el("span", { text: `${r.run_id} · ${r.mode === "observe" ? "observe-only" : "enforce"}` }),
      el("span", {}, [
        el("button", { class: "btn btn-ghost btn-sm", onclick: () => openRun(r.run_id) }, ["Open"]),
        el("button", { class: "btn btn-ghost btn-sm", onclick: () => deleteRun(r.run_id) }, ["Delete"]),
      ]),
    ]);
    area.appendChild(row);
  }
}

async function openRun(rid) {
  const doc = await labFetch("GET", `/api/traffic/runs/${rid}`);
  state.lastRun = doc;
  render(doc);
}

async function deleteRun(rid) {
  await labFetch("DELETE", `/api/traffic/runs/${rid}`);
  loadRunsList();
}

async function exportAll() {
  const doc = await labFetch("POST", "/api/traffic/export", { kind: "traffic" });
  const blob = new Blob([JSON.stringify(doc, null, 1)], { type: "application/json" });
  const a = el("a", { href: URL.createObjectURL(blob), download: "shieldlang-traffic-export.json" });
  document.body.appendChild(a); a.click(); a.remove();
}

function wire() {
  document.getElementById("btn-run").addEventListener("click", run);
  document.getElementById("btn-export").addEventListener("click", exportAll);
  document.getElementById("mode-select").addEventListener("change", (e) => explainMode(e.target.value));
  explainMode(document.getElementById("mode-select").value);
  document.getElementById("policy-select").addEventListener("change", explainPolicy);
  document.getElementById("btn-check-recording").addEventListener("click", checkRecordingText);
  document.getElementById("btn-import-recording").addEventListener("click", importRecordingText);
}

async function init() {
  wire();
  await loadPolicies();
  await loadRecordings();
  const params = queryParams();
  if (params.bypass_run) {
    try {
      const h = await labFetch("GET", `/api/traffic/bypass/${params.bypass_run}`);
      renderBypassHandoff(h);
    } catch (e) { setBanner(`Could not load the Bypass Lab evidence (${e.message}).`, "warn"); }
  }
  if (params.protocol) document.getElementById("protocol-select").value = params.protocol;
  if (params.policy_hash) {
    try {
      const src = await labFetch("GET", `/api/protocol/source/${params.policy_hash}`);
      const sel = document.getElementById("policy-select");
      sel.appendChild(el("option", { value: params.policy_hash, text: `handed off policy (${params.policy_hash.slice(0, 14)}…)` }));
      sel.value = params.policy_hash;
      setBanner(`Loaded policy ${params.policy_hash.slice(0, 14)}… handed off from Protocol Studio (${src.source.length} chars).`, "ok");
    } catch (e) { setBanner(`Could not load the handed-off policy (${e.message}).`, "warn"); }
  }
  loadRunsList();
}

init();
