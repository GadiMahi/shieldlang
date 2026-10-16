/* Bounded local experiments: manual evaluation, generated search and replay-preserving reduction. */
"use strict";

let FIXTURES = [];
let RUNNING = false;
let LAST_EVIDENCE = null;

function invalidateEvidence() {
  LAST_EVIDENCE = null;
  document.getElementById("result-area").textContent = "Experiment input changed. Run again for current evidence.";
  document.getElementById("search-status").textContent = "";
}

async function withBusy(message, action) {
  if (RUNNING) return;
  RUNNING = true;
  const controls = [...document.querySelectorAll("main button, main input, main select, main textarea")];
  controls.forEach(c => { c.disabled = true; });
  document.getElementById("search-status").textContent = message;
  try {
    await action();
    setBanner("", "");
  } catch (e) {
    document.getElementById("search-status").textContent = "Experiment failed.";
    setBanner(e.message, "err");
  } finally {
    controls.forEach(c => { c.disabled = false; });
    RUNNING = false;
  }
}

function setBanner(text, kind) {
  const area = document.getElementById("banner-area");
  area.innerHTML = "";
  if (text) area.appendChild(banner(text, kind));
}

function currentFixture() {
  return FIXTURES.find(f => f.id === document.getElementById("fixture-select").value);
}

const STAGE_HELP = {
  percent_decode: "turn %XX escapes into characters (%2e → .)",
  nfkc: "fold look-alike “compatibility” characters (fullwidth ＜ → <)",
  nfc: "canonical Unicode composition",
  strip_nul: "delete NUL (\\0) bytes",
  html_unescape: "turn &lt; style entities into characters",
  lowercase: "lowercase everything", casefold: "aggressive lowercase",
  path_normalize: "collapse ./ and ../ segments",
  json_unescape: "turn \\uXXXX escapes into characters",
  "dup:first": "if a JSON key repeats, keep the FIRST value",
  "dup:last": "if a JSON key repeats, keep the LAST value",
};

function describeOracle(o, scope) {
  const where = scope.kind === "body_field" ? `the “${scope.name}” field of the JSON body` : scope.kind === "path_param" ? `the “${scope.name}” part of the URL path` : `${scope.kind} ${scope.name}`;
  if (o.kind === "contains_substring") return `Violation = ${where} contains “${o.needle}”${o.case_insensitive ? " (any letter case)" : ""}.`;
  if (o.kind === "path_escapes_root") return `Violation = ${where} climbs out of the allowed folder (for example with ../).`;
  if (o.kind === "equals_any") return `Violation = ${where} equals one of: ${o.values.join(", ")}.`;
  return `Oracle: ${o.kind}.`;
}

function pipelineRow(label, stages) {
  const chips = stages.length
    ? stages.map(st => el("span", { class: "badge badge-observe", title: STAGE_HELP[st] || st, style: "text-transform:none" }, [st]))
    : [el("span", { class: "lab-footnote" }, ["(no transformation — reads the text exactly as sent)"])];
  const row = el("div", { class: "lab-row", style: "margin-bottom:4px; align-items:center" }, [el("b", { style: "min-width:92px", text: label }), ...chips]);
  const help = stages.map(st => STAGE_HELP[st] ? `${st}: ${STAGE_HELP[st]}` : null).filter(Boolean);
  return [row, help.length ? el("p", { class: "lab-footnote", style: "margin:0 0 8px 100px" }, [help.join(" · ")]) : ""];
}

function renderFixtureDetail(fx) {
  const area = document.getElementById("fixture-detail");
  area.className = ""; area.innerHTML = "";
  area.appendChild(el("p", { style: "margin:0 0 4px; font-weight:600", text: fx.title }));
  area.appendChild(el("p", { class: "lab-footnote", style: "margin:0 0 10px", text: fx.label }));
  area.append(...pipelineRow("Policy reads", fx.policy_stages), ...pipelineRow("App reads", fx.app_stages));
  area.appendChild(el("div", { class: "lab-explain" }, [el("span", { class: "what" }, ["Independent oracle"]), describeOracle(fx.oracle, fx.scope)]));
  const presets = document.getElementById("candidate-presets");
  presets.innerHTML = "";
  for (const c of fx.candidates) {
    presets.appendChild(el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => { document.getElementById("candidate-input").value = c.raw; invalidateEvidence(); } }, [c.label]));
  }
}

async function loadFixtures() {
  FIXTURES = await labFetch("GET", "/api/bypass/fixtures");
  const sel = document.getElementById("fixture-select");
  sel.innerHTML = "";
  for (const fx of FIXTURES) sel.appendChild(el("option", { value: fx.id, text: `${fx.id} — ${fx.title}` }));
  sel.addEventListener("change", () => {
    const fx = currentFixture();
    renderFixtureDetail(fx);
    document.getElementById("candidate-input").value = fx.candidates[0].raw;
    invalidateEvidence();
  });
  renderFixtureDetail(FIXTURES[0]);
  document.getElementById("candidate-input").value = FIXTURES[0].candidates[0].raw;
}

async function runCandidate() {
  const fx = currentFixture();
  const raw = document.getElementById("candidate-input").value;
  if (!raw) { setBanner("Type or pick a candidate first, or use Search this experiment.", "warn"); return; }
  await withBusy("Evaluating and checking local replay…", async () => {
    const out = await labFetch("POST", "/api/bypass/run", { fixture_id: fx.id, raw, replay: document.getElementById("replay-toggle").checked });
    renderEvidence(out);
    document.getElementById("search-status").textContent = "Evaluation complete.";
    await loadRunsList();
  });
}

async function searchCandidates() {
  await withBusy("Searching declared transformations and confirming findings locally…", async () => {
    const out = await labFetch("POST", "/api/bypass/search", {
      fixture_id: currentFixture().id,
      budget: Number(document.getElementById("search-budget").value),
      max_depth: Number(document.getElementById("search-depth").value),
      seed: Number(document.getElementById("search-seed").value),
      minimize: document.getElementById("search-reduce").checked,
    });
    renderEvidence(out);
    document.getElementById("search-status").textContent = `${out.findings.length} confirmed findings from ${out.evaluated} evaluations. Stopped: ${out.stop_reason}.`;
    await loadRunsList();
  });
}

async function minimizeCandidate() {
  await withBusy("Checking the candidate, then reducing while preserving local replay…", async () => {
    const out = await labFetch("POST", "/api/bypass/minimize", {
      fixture_id: currentFixture().id, raw: document.getElementById("candidate-input").value,
    });
    renderEvidence(out);
    document.getElementById("search-status").textContent = `Reduced ${out.original_bytes} to ${out.reduced_bytes} UTF-8 bytes. Stopped: ${out.stop_reason}.`;
    await loadRunsList();
  });
}

function renderEvidence(out) {
  LAST_EVIDENCE = out;
  if (out.operation === "search") {
    const area = document.getElementById("result-area");
    area.className = "";
    area.replaceChildren(el("h3", { text: `${out.findings.length} replay-confirmed findings` }),
      el("p", { text: `${out.evaluated} candidates evaluated; ${out.replay_count} local replays. Stopped: ${out.stop_reason}.` }),
      el("p", { class: "lab-footnote", text: `Fixture ${out.fixture_hash}. ${out.scope_note}` }));
    for (const finding of out.findings) {
      const reduction = finding.reduction;
      const result = reduction && reduction.result ? reduction.result : finding.result;
      const box = el("div", { class: "lab-explain" }, [
        pre(JSON.stringify(result.raw)), el("p", { text: `Generated by: ${finding.provenance.join(" → ") || "seed"}` }),
        reduction ? el("p", { class: "lab-footnote", text: `${reduction.stop_reason}. ${reduction.minimality}` }) : "",
        el("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
          await withBusy("Replaying the selected finding for an individual evidence record…", async () => {
            const selected = await labFetch("POST", "/api/bypass/run", {
              fixture_id: out.fixture_id, raw: result.raw, replay: true,
            });
            document.getElementById("candidate-input").value = result.raw;
            renderEvidence(selected);
            document.getElementById("search-status").textContent = "Selected finding replayed and saved.";
            await loadRunsList();
          });
        } }, ["Replay and inspect this finding"]),
      ]);
      area.appendChild(box);
    }
    area.appendChild(el("details", {}, [el("summary", { text: "Seeds, budgets and experiment record" }), pre(out)]));
  } else if (out.operation === "minimize") {
    renderResult({ ...out.result, run_id: out.run_id });
    document.getElementById("result-area").prepend(el("div", { class: "lab-explain" }, [
      el("b", { text: `Reduced ${out.original_bytes} → ${out.reduced_bytes} UTF-8 bytes` }),
      el("p", { text: `${out.evaluations} deletion checks; ${out.stop_reason}. ${out.minimality}` }),
      el("details", {}, [el("summary", { text: "Original input" }), pre(JSON.stringify(out.original_raw))]),
      el("button", { class: "btn btn-ghost btn-sm", onclick: () => {
        document.getElementById("candidate-input").value = out.raw;
      } }, ["Use reduced candidate"]),
    ]));
  } else {
    renderResult(out);
  }
  document.getElementById("result-area").appendChild(el("button", {
    class: "btn btn-ghost btn-sm", onclick: () => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(out, null, 2)], { type: "application/json" }));
      const link = el("a", { href: url, download: `${out.run_id || "bypass-evidence"}.json` });
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
  }, ["Download evidence JSON"]));
}

function verdict(out) {
  if (out.confirmed_bypass) return ["Confirmed for this fixture.", "The policy would let this through, the independent oracle says the application's reading is a violation, and the local replay reproduced the effect. This demonstrates the fixture's named mismatch — nothing more."];
  if (!out.policy_would_accept) return ["Not a bypass: the policy rejects it.", "The policy's own reading of the candidate already violates the rule, so it would be blocked before reaching the application."];
  if (!out.oracle_violation_on_app_view) return ["Not a bypass: harmless on both sides.", "The policy accepts it, but the application's reading is not a violation either. (If this is the negative-control fixture, that is exactly the expected result.)"];
  return ["Not confirmed.", "The policy accepts it and the oracle flags the application's reading, but the local replay did not reproduce the effect (or replay was switched off), so it is not counted as a confirmed bypass."];
}

function renderResult(out) {
  const area = document.getElementById("result-area");
  area.className = ""; area.innerHTML = "";
  const [head, body] = verdict(out);
  area.appendChild(el("div", { class: "lab-explain", style: "margin-top:0" }, [
    out.confirmed_bypass ? badge("confirmed (this fixture)", "deny") : badge("not confirmed", "allow"), " ",
    el("span", { class: "what", style: "display:inline" }, [head]), el("br"), body,
  ]));
  area.appendChild(el("p", { class: "lab-footnote" }, [`Run ${out.run_id}. ${out.note || ""}`]));
  area.appendChild(el("a", { class: "btn btn-ghost btn-sm", href: `/traffic?bypass_run=${out.run_id}` }, ["View this evidence in Traffic Lab ↗"]));
  area.appendChild(el("p", { class: "lab-footnote" }, ["Opens a read-only panel there — it does not re-run the candidate through the gateway (see why on that page)."]));
  const yn = (v) => badge(v ? "yes" : "no", v ? "unknown" : "muted");
  const row = (k, v, hint) => el("tr", {}, [el("td", {}, [el("b", { text: k }), hint ? el("div", { class: "lab-footnote", text: hint }) : ""]), el("td", {}, [v])]);
  area.appendChild(el("div", { class: "lab-scroll" }, [el("table", { class: "lab-table" }, [el("tbody", {}, [
    row("1 · Raw candidate", pre(JSON.stringify(out.raw)), "JSON-escaped display makes control characters visible."),
    row("2 · Policy's reading", pre(JSON.stringify(out.policy_interpretation)), "After the policy-side pipeline."),
    row("3 · Application's reading", pre(JSON.stringify(out.app_interpretation)), "After the application-side pipeline."),
    row("Would the policy accept it?", yn(out.policy_would_accept), "“yes” = the filter lets it through."),
    row("Does the oracle call the app's reading a violation?", yn(out.oracle_violation_on_app_view), "Independent of the policy."),
    row("4 · Local replay", pre(out.replay), "Sent to the loopback fixture app."),
  ])])]));
}

async function loadRunsList() {
  const area = document.getElementById("runs-list");
  const runs = await labFetch("GET", "/api/bypass/runs");
  area.innerHTML = "";
  if (!runs.length) { area.className = "lab-empty"; area.textContent = "No runs yet."; return; }
  area.className = "";
  for (const r of runs.slice(0, 15)) {
    area.appendChild(el("div", { class: "lab-row", style: "justify-content:space-between" }, [
      el("span", { text: r.run_id }),
      el("button", { class: "btn btn-ghost btn-sm", onclick: () => openRun(r.run_id) }, ["Open"]),
    ]));
  }
}

async function openRun(rid) {
  const out = await labFetch("GET", `/api/bypass/runs/${rid}`);
  renderEvidence(out);
  const sel = document.getElementById("fixture-select");
  if ([...sel.options].some(o => o.value === out.fixture_id)) { sel.value = out.fixture_id; renderFixtureDetail(currentFixture()); }
  document.getElementById("candidate-input").value = out.raw !== undefined ? out.raw : (out.seeds || [""])[0];
}

function wire() {
  document.getElementById("btn-run").addEventListener("click", runCandidate);
  document.getElementById("btn-search").addEventListener("click", searchCandidates);
  document.getElementById("btn-minimize").addEventListener("click", minimizeCandidate);
  document.getElementById("candidate-input").addEventListener("input", invalidateEvidence);
  for (const id of ["search-budget", "search-depth", "search-seed", "search-reduce", "replay-toggle"]) {
    document.getElementById(id).addEventListener("change", invalidateEvidence);
  }
}

async function init() {
  wire();
  await loadFixtures();
  loadRunsList();
  const params = queryParams();
  if (params.run_id) {
    try { await openRun(params.run_id); } catch (e) { setBanner(`Could not load run ${params.run_id}: ${e.message}`, "warn"); }
  }
}

init();
