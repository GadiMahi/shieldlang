/* ShieldLang Studio — front end (vanilla JS, no build step) */
"use strict";

/* ------------------------------------------------------------------ utils */
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 1800);
}

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) throw Object.assign(new Error(data.error?.message || data.error || r.statusText), { data });
  return data;
}

function download(name, text) {
  const a = h("a", { href: URL.createObjectURL(new Blob([text], { type: "text/plain" })), download: name });
  document.body.append(a);
  a.click();
  a.remove();
}

const fmtMs = (ms) => (ms == null ? "—" : ms < 1 ? `${ms.toFixed(2)} ms` : ms < 1000 ? `${ms.toFixed(1)} ms` : `${(ms / 1000).toFixed(2)} s`);
const risk = (r) => h("span", { class: `risk ${r}` }, r === "HIGH" ? "● HIGH" : r === "MEDIUM" ? "▲ MEDIUM" : r === "LOW" ? "✓ LOW" : r);

/* ------------------------------------------------------------------ state */
const S = {
  examples: [], ex: null, name: "rules.shl", source: "", res: null, tab: "overview", compiling: false,
  pending: false, focus: null, rxSel: null, rxProbe: null, rxView: "dfa", irView: "opt", codeLang: "py",
  runText: "", runRes: null, preset: -1, bench: null, benchSel: null, benchBusy: false, roundtrip: 0,
};

/* ----------------------------------------------------------- highlighter */
const KW = new Set("rule on const pattern header query field param body when require forbid block if any as exists and or not path".split(" "));
const TYPES = new Set("string int number bool list enum".split(" "));
const CONS = new Set("matches minlen maxlen in maxitems minitems max_depth max_size max_keys".split(" "));
const LITS = new Set(["true", "false"]);
const METHODS = new Set("GET POST PUT PATCH DELETE HEAD OPTIONS ANY".split(" "));
const UNITS = new Set(["B", "KB", "MB"]);

function lexShl(src) {
  const out = [];
  const n = src.length;
  let i = 0, prevOperand = false;
  while (i < n) {
    const c = src[i];
    if (c === " " || c === "\t" || c === "\n" || c === "\r") { i++; continue; }
    if (src.startsWith("//", i) || c === "#") {
      let j = src.indexOf("\n", i); if (j < 0) j = n;
      out.push([i, j, "com"]); i = j; continue;
    }
    if (src.startsWith("/*", i)) {
      let j = src.indexOf("*/", i + 2); j = j < 0 ? n : j + 2;
      out.push([i, j, "com"]); i = j; continue;
    }
    if (/[A-Za-z_]/.test(c)) {
      let j = i; while (j < n && /\w/.test(src[j])) j++;
      const w = src.slice(i, j);
      const k = KW.has(w) ? "kw" : TYPES.has(w) ? "type" : CONS.has(w) ? "cons" : LITS.has(w) ? "lit"
        : METHODS.has(w) ? "method" : UNITS.has(w) && prevOperand ? "unit" : "id";
      out.push([i, j, k]); prevOperand = k === "id" || k === "unit"; i = j; continue;
    }
    if (/[0-9]/.test(c)) {
      let j = i; while (j < n && /[0-9_]/.test(src[j])) j++;
      if (src[j] === "." && /[0-9]/.test(src[j + 1] || "")) { j++; while (j < n && /[0-9]/.test(src[j])) j++; }
      out.push([i, j, "num"]); prevOperand = true; i = j; continue;
    }
    if (c === '"') {
      let j = i + 1;
      while (j < n && src[j] !== '"' && src[j] !== "\n") { if (src[j] === "\\") j++; j++; }
      if (src[j] === '"') j++;
      out.push([i, j, "str"]); prevOperand = true; i = j; continue;
    }
    if (c === "/" && !prevOperand) {
      let j = i + 1, cls = false;
      while (j < n && src[j] !== "\n") {
        const d = src[j];
        if (d === "\\") { j += 2; continue; }
        if (d === "[") cls = true; else if (d === "]") cls = false; else if (d === "/" && !cls) { j++; break; }
        j++;
      }
      while (j < n && /[a-z]/i.test(src[j])) j++;
      out.push([i, j, "re"]); prevOperand = false; i = j; continue;
    }
    out.push([i, i + 1, "punc"]); prevOperand = c === ")"; i++;
  }
  return out;
}

function lineStarts(src) {
  const ls = [0];
  for (let i = 0; i < src.length; i++) if (src[i] === "\n") ls.push(i + 1);
  return ls;
}
function lineOf(ls, off) {
  let lo = 0, hi = ls.length - 1;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; if (ls[m] <= off) lo = m; else hi = m - 1; }
  return lo + 1;
}

/* ----------------------------------------------------------------- editor */
const ta = () => $("#source");

function renderEditor() {
  const src = S.source;
  const n = src.length;
  const cls = new Array(n).fill("");
  for (const [a, b, k] of lexShl(src)) for (let i = a; i < b && i < n; i++) cls[i] = k;
  const dg = new Uint8Array(n);
  const rank = { info: 1, warning: 2, error: 3 };
  const diags = S.res?.diagnostics || [];
  for (const d of diags) {
    const a = Math.min(d.span.start, Math.max(0, n - 1));
    const b = Math.min(Math.max(d.span.end, a + 1), n);
    for (let i = a; i < b; i++) dg[i] = Math.max(dg[i], rank[d.severity] || 0);
  }
  const f = S.focus;
  let html = "", run = "", key = null;
  const dgName = ["", "dg-info", "dg-warning", "dg-error"];
  const flush = () => {
    if (!run) return;
    const [k, d, fc] = key.split("|");
    const c = [k ? `tk-${k}` : "", dgName[+d], fc === "1" ? "focus" : ""].filter(Boolean).join(" ");
    html += c ? `<span class="${c}">${esc(run)}</span>` : esc(run);
    run = "";
  };
  for (let i = 0; i < n; i++) {
    const ch = src[i];
    const k = ch === "\n" ? `||0` : `${cls[i]}|${dg[i]}|${f && i >= f[0] && i < f[1] ? 1 : 0}`;
    if (k !== key) { flush(); key = k; }
    run += ch;
  }
  flush();
  $("#highlight").innerHTML = html + "\n ";
  // gutter
  const ls = lineStarts(src);
  const marks = {};
  for (const d of diags) {
    const ln = lineOf(ls, d.span.start);
    if ((rank[d.severity] || 0) > (rank[marks[ln]] || 0)) marks[ln] = d.severity;
  }
  const g = $("#gutter");
  let gh = "";
  for (let i = 1; i <= ls.length; i++) gh += `<div class="${marks[i] ? "m-" + marks[i] : ""}">${i}</div>`;
  g.innerHTML = gh;
  syncScroll();
}

function syncScroll() {
  const t = ta();
  $("#highlight").scrollTop = t.scrollTop;
  $("#highlight").scrollLeft = t.scrollLeft;
  $("#gutter").scrollTop = t.scrollTop;
}

function updateCursor() {
  const t = ta();
  const before = t.value.slice(0, t.selectionStart);
  const ln = before.split("\n").length;
  const col = before.length - before.lastIndexOf("\n");
  $("#cursor-pos").textContent = `Ln ${ln}, Col ${col}`;
}

function focusRange(start, end, scroll = true) {
  S.focus = start == null ? null : [start, Math.max(end, start + 1)];
  renderEditor();
  if (scroll && start != null) {
    const t = ta();
    const ln = S.source.slice(0, start).split("\n").length;
    const lh = 21;
    const top = (ln - 1) * lh;
    if (top < t.scrollTop + 20 || top > t.scrollTop + t.clientHeight - 60) t.scrollTop = Math.max(0, top - t.clientHeight / 3);
    syncScroll();
  }
}

function gotoOffset(start, end) {
  const t = ta();
  t.focus();
  t.setSelectionRange(start, end ?? start);
  focusRange(start, end ?? start + 1);
  updateCursor();
}

function hoverable(el, span) {
  if (!span) return el;
  const [a, b] = Array.isArray(span) ? span : [span.start, span.end];
  el.addEventListener("mouseenter", () => focusRange(a, b));
  el.addEventListener("mouseleave", () => focusRange(null));
  el.addEventListener("click", (e) => { e.stopPropagation(); gotoOffset(a, b); });
  return el;
}

let compileTimer = null;
function onInput() {
  S.source = ta().value;
  S.focus = null;
  $(".file-tab").classList.add("dirty");
  renderEditor();
  if ($("#live-toggle").checked) {
    clearTimeout(compileTimer);
    compileTimer = setTimeout(compile, 550);
  }
}

function setupEditor() {
  const t = ta();
  t.addEventListener("input", onInput);
  t.addEventListener("scroll", syncScroll);
  t.addEventListener("keyup", updateCursor);
  t.addEventListener("click", updateCursor);
  t.addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      e.preventDefault();
      const s = t.selectionStart, en = t.selectionEnd;
      t.value = t.value.slice(0, s) + "  " + t.value.slice(en);
      t.selectionStart = t.selectionEnd = s + 2;
      onInput();
    } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      compile();
    }
  });
}

/* --------------------------------------------------------------- compile */
async function compile() {
  if (S.compiling) { S.pending = true; return; }
  S.compiling = true;
  renderPipeline(true);
  const t0 = performance.now();
  try {
    S.res = await api("/api/compile", { source: S.source, name: S.name });
    S.roundtrip = performance.now() - t0;
    $(".file-tab").classList.remove("dirty");
    if (S.rxSel && !(S.res.regexes || {})[S.rxSel]) S.rxSel = null;
  } catch (e) {
    toast("Compile request failed: " + e.message);
  } finally {
    S.compiling = false;
  }
  renderAll();
  if (S.pending) { S.pending = false; compile(); }
}

/* -------------------------------------------------------------- pipeline */
const PHASES = [
  ["lexer", "Lexer", "tokens"], ["parser", "Parser", "ast"], ["semantic", "Semantic", "symbols"],
  ["regex", "RegexShield", "regex"], ["ir", "IR", "ir"], ["optimizer", "Optimiser", "opt"], ["codegen", "Codegen", "code"],
];

function renderPipeline(busy = false) {
  const p = $("#pipeline");
  p.innerHTML = "";
  const r = S.res;
  PHASES.forEach(([key, label, tab], idx) => {
    let st = "skip", icon = idx + 1;
    if (r) {
      const ds = r.diagnostics.filter((d) => d.phase === key);
      const t = r.timings_ms[key];
      if (ds.some((d) => d.severity === "error")) { st = "err"; icon = "!"; }
      else if (t != null && ds.some((d) => d.severity === "warning")) { st = "warn"; icon = "✓"; }
      else if (t != null) { st = "ok"; icon = "✓"; }
    }
    if (busy) icon = "…";
    const t = r?.timings_ms?.[key];
    const el = h("button", { class: `phase ${st} ${PHASES.find((x) => x[2] === S.tab)?.[0] === key ? "active" : ""}`, onclick: () => setTab(tab) },
      h("span", { class: "ph-icon" }, icon),
      h("span", {}, h("div", { class: "ph-name" }, label), h("div", { class: "ph-time" }, t != null ? fmtMs(t) : st === "err" ? "errors" : "—")));
    p.append(el);
    if (idx < PHASES.length - 1) p.append(h("span", { class: "phase-arrow" }, "→"));
  });
}

/* ------------------------------------------------------------ diagnostics */
function renderDiagnostics() {
  const list = $("#diag-list");
  list.innerHTML = "";
  const ds = S.res?.diagnostics || [];
  const ne = ds.filter((d) => d.severity === "error").length;
  const nw = ds.filter((d) => d.severity === "warning").length;
  $("#diag-counts").innerHTML = `<span class="e">${ne} error${ne === 1 ? "" : "s"}</span><span class="w">${nw} warning${nw === 1 ? "" : "s"}</span>`;
  $("#diag-summary").textContent = S.res ? `${ds.length} item${ds.length === 1 ? "" : "s"}` : "";
  if (!ds.length) {
    list.append(h("div", { class: "empty-ok" }, "✓ No problems found — every rule type-checks and every pattern is analysed."));
    return;
  }
  const sorted = [...ds].sort((a, b) => a.span.start - b.span.start);
  for (const d of sorted) {
    const el = h("div", { class: `diag ${d.severity}` },
      h("span", { class: "sev" }),
      h("span", { class: "code" }, d.code),
      h("div", {}, h("div", { class: "msg" }, d.message), d.hint ? h("div", { class: "hint" }, "hint: " + d.hint) : null),
      h("span", { class: "loc" }, `${d.span.line}:${d.span.col}`));
    hoverable(el, d.span);
    list.append(el);
  }
}

/* ------------------------------------------------------------------ tabs */
const TABS = [
  ["overview", "Overview"], ["tokens", "Tokens"], ["ast", "AST"], ["symbols", "Symbols"], ["regex", "RegexShield"],
  ["ir", "IR"], ["opt", "Optimiser"], ["code", "Code"], ["run", "Run"], ["bench", "Benchmark"],
];

function setTab(t) {
  S.tab = t;
  renderTabs();
  renderPanel();
  renderPipeline();
}

function renderTabs() {
  const el = $("#tabs");
  el.innerHTML = "";
  const r = S.res;
  for (const [key, label] of TABS) {
    let badge = null;
    if (r && key === "tokens") badge = h("span", { class: "badge-n" }, r.tokens.length - 1);
    if (r && key === "regex" && r.report) {
      const bad = (r.report.counts.HIGH || 0) + (r.report.counts.MEDIUM || 0);
      badge = h("span", { class: `badge-n ${bad ? "wine" : ""}` }, bad ? `${bad} risky` : Object.keys(r.regexes || {}).length);
    }
    el.append(h("button", { class: `tab ${S.tab === key ? "active" : ""}`, role: "tab", onclick: () => setTab(key) }, label, badge));
  }
}

function renderPanel() {
  const panel = $("#panel");
  const scroll = panel.scrollTop;
  panel.innerHTML = "";
  if (!S.res) { panel.append(h("div", { class: "empty" }, h("span", { class: "spinner" }), " Compiling…")); return; }
  const fn = { overview: pOverview, tokens: pTokens, ast: pAst, symbols: pSymbols, regex: pRegex, ir: pIr, opt: pOpt,
    code: pCode, run: pRun, bench: pBench }[S.tab];
  panel.append(fn());
  panel.scrollTop = renderPanel.keepScroll ? scroll : 0;
  renderPanel.keepScroll = false;
}

function renderAll() {
  renderEditor();
  renderPipeline();
  renderDiagnostics();
  renderTabs();
  renderPanel();
}

function title(t, sub, right) {
  return h("div", { class: "section-title" }, h("div", {}, h("h2", {}, t), sub ? h("p", {}, sub) : null), right || null);
}

const needOk = (what) => h("div", { class: "empty" },
  h("div", { class: "serif", style: "font-size:18px;color:var(--wine);margin-bottom:6px" }, "Compilation stopped"),
  `Fix the ${S.res.diagnostics.filter((d) => d.severity === "error").length} error(s) in the Diagnostics panel to generate ${what}.`);

/* -------------------------------------------------------------- overview */
function pOverview() {
  const r = S.res;
  const root = h("div", { class: "panel-inner" });
  const errs = r.diagnostics.filter((d) => d.severity === "error");
  const totalMs = Object.values(r.timings_ms).reduce((a, b) => a + b, 0);
  if (r.ok) {
    root.append(h("div", { class: "banner ok" }, h("div", { class: "b-icon" }, "✓"),
      h("div", {}, h("div", { class: "b-title" }, `Compiled ${r.rules.length} rule${r.rules.length === 1 ? "" : "s"} into linear-time validators`),
        h("div", { class: "b-sub" }, `${r.name} · ${r.tokens.length - 1} tokens · ${fmtMs(totalMs)} compile time · Python + JavaScript output`))));
  } else {
    root.append(h("div", { class: "banner bad" }, h("div", { class: "b-icon" }, "!"),
      h("div", {}, h("div", { class: "b-title" }, `Compilation failed: ${errs.length} error${errs.length === 1 ? "" : "s"}`),
        h("div", { class: "b-sub" }, "The front end recovered from every error and kept checking, so all problems are listed at once."))));
  }
  const rep = r.report || { counts: {}, regexes: [], fixed: 0 };
  const risky = (rep.counts.HIGH || 0) + (rep.counts.MEDIUM || 0);
  const kp = h("div", { class: "kpis" },
    kpi("Rules", r.rules?.length ?? 0, "routes compiled", "dark"),
    kpi("Patterns analysed", rep.regexes.length, "via RegexShield v2"),
    kpi("ReDoS-vulnerable", risky, `${rep.counts.HIGH || 0} exponential · ${rep.counts.MEDIUM || 0} polynomial`, risky ? "wine" : "blue"),
    kpi("Auto-repaired", rep.fixed, "verified equivalent", "blue"),
    r.opt_ir ? kpi("IR instructions", `${r.ir.instructions}→${r.opt_ir.instructions}`, `cost ${r.ir.cost}→${r.opt_ir.cost} (−${Math.round(100 - (100 * r.opt_ir.cost) / Math.max(1, r.ir.cost))}%)`) : null,
    kpi("Diagnostics", `${errs.length} / ${r.diagnostics.length - errs.length}`, "errors / warnings & info"));
  root.append(kp);
  if (r.ok) root.append(h("div", { class: "guarantee" }, h("b", {}, "Guarantee"),
    "Every generated validator matches patterns with minimised DFAs: one table lookup per character, worst case O(n), no backtracking — even for the patterns flagged below."));

  // security report
  if (rep.regexes.length) {
    const tb = h("tbody");
    for (const x of rep.regexes) {
      const fix = x.rewrite && x.rewrite.changed ? h("span", { class: "mono-cell", style: "color:var(--blue-2)" }, `/${x.rewrite.final}/`) : h("span", { class: "muted" }, x.risk === "LOW" ? "not needed" : "—");
      tb.append(h("tr", { class: "click", onclick: () => { S.rxSel = x.rid; S.rxProbe = null; setTab("regex"); } },
        h("td", {}, risk(x.risk)),
        h("td", { class: "mono" }, `/${x.source}/${x.flags}`),
        h("td", { class: "mono" }, x.complexity),
        h("td", { class: "small muted" }, x.used_by.join(", ")),
        h("td", {}, fix)));
    }
    root.append(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Security report"), h("span", { class: "small muted" }, "click a row to inspect the automata")),
      h("div", { class: "table-wrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Risk", "Pattern", "Backtracking cost", "Used by", "Verified fix"].map((c) => h("th", {}, c)))), tb))));
  }
  // phase timing bar
  const phases = PHASES.filter(([k]) => r.timings_ms[k] != null);
  const colors = ["#121212", "#3b3732", "#6f665b", "#7b1e3c", "#2448d8", "#5b74e6", "#a99f90"];
  const bar = h("div", { class: "bar", style: "height:14px" });
  phases.forEach(([k], i) => bar.append(h("div", { style: `width:${(100 * r.timings_ms[k]) / Math.max(totalMs, 0.001)}%;background:${colors[i]}`, title: `${k}: ${fmtMs(r.timings_ms[k])}` })));
  const legend = h("div", { class: "flex small muted", style: "margin-top:8px" },
    phases.map(([k, label], i) => h("span", {}, h("i", { style: `display:inline-block;width:9px;height:9px;border-radius:2px;background:${colors[i]};margin-right:5px` }), `${label} ${fmtMs(r.timings_ms[k])}`)));
  root.append(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Where compile time goes"), h("span", { class: "small muted" }, `server ${fmtMs(totalMs)} · round-trip ${fmtMs(S.roundtrip)}`)),
    h("div", { class: "card-body" }, bar, legend)));
  // rules
  if (r.rules?.length) {
    const tb = h("tbody");
    for (const ru of r.rules) {
      tb.append(h("tr", {}, h("td", { class: "mono" }, ru.name), h("td", {}, h("span", { class: "pill ink" }, ru.method)), h("td", { class: "mono" }, ru.path),
        h("td", {}, ru.fields.length), h("td", {}, ru.whens.length), h("td", {}, ru.blocks.length),
        h("td", { class: "small muted mono" }, Object.entries(ru.limits).map(([k, v]) => `${k}=${v}`).join(" ") || "—")));
    }
    root.append(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Rules")),
      h("div", { class: "table-wrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Rule", "Method", "Path", "Fields", "When", "Block", "Body limits"].map((c) => h("th", {}, c)))), tb))));
  }
  return root;
}

function kpi(label, value, sub, cls = "") {
  return h("div", { class: `kpi ${cls}` }, h("div", { class: "k-label" }, label), h("div", { class: "k-value" }, value), sub ? h("div", { class: "k-sub" }, sub) : null);
}

/* ---------------------------------------------------------------- tokens */
function pTokens() {
  const r = S.res;
  const toks = r.tokens.filter((t) => t.kind !== "EOF");
  const cat = (k) => (k.startsWith("KW_") ? "kw" : k === "REGEX" ? "re" : k === "STRING" || k === "NUMBER" ? "lit" : k === "IDENT" ? "id" : "punc");
  const counts = {};
  toks.forEach((t) => (counts[cat(t.kind)] = (counts[cat(t.kind)] || 0) + 1));
  const grid = h("div", { class: "token-grid" });
  for (const t of toks) {
    const el = h("div", { class: `tok k-${cat(t.kind)}`, title: `${t.kind}  [${t.start}..${t.end})` },
      h("span", { class: "tk" }, t.kind.replace("KW_", "")), h("span", { class: "tv" }, t.text.length > 46 ? t.text.slice(0, 44) + "…" : t.text));
    grid.append(hoverable(el, [t.start, t.end]));
  }
  return h("div", { class: "panel-inner" },
    title("Lexical analysis", "A hand-written maximal-munch scanner turns the source into typed tokens with exact character offsets. Regex literals are recognised with the JavaScript rule: '/' starts a regex unless the previous token can end an operand. Hover a token to locate it in the editor."),
    h("div", { class: "stats" }, h("div", { class: "stat" }, "tokens", h("b", {}, toks.length)),
      h("div", { class: "stat" }, "keywords", h("b", {}, counts.kw || 0)), h("div", { class: "stat" }, "identifiers", h("b", {}, counts.id || 0)),
      h("div", { class: "stat" }, "literals", h("b", {}, counts.lit || 0)), h("div", { class: "stat" }, "regex literals", h("b", {}, counts.re || 0)),
      h("div", { class: "stat" }, "punctuation", h("b", {}, counts.punc || 0))),
    h("div", { class: "card card-body" }, grid));
}

/* ------------------------------------------------------------------- AST */
const AST_ATTRS = ["name", "method", "path", "source", "optional", "kind", "op", "value", "unit", "pattern", "flags", "scope", "target", "label", "action", "rid"];

function astTree(obj, depth = 0, edge = "") {
  const kids = [];
  for (const [k, v] of Object.entries(obj)) {
    if (k === "node" || k === "span") continue;
    if (v && typeof v === "object" && !Array.isArray(v) && v.node) kids.push([k, v]);
    else if (Array.isArray(v) && v.some((x) => x && typeof x === "object" && x.node)) v.forEach((x, i) => x && x.node && kids.push([`${k}[${i}]`, x]));
  }
  const attrs = AST_ATTRS.filter((a) => obj[a] !== undefined && obj[a] !== "" && obj[a] !== null && typeof obj[a] !== "object")
    .map((a) => h("span", { class: "n-attr" }, `${a}=`, h("span", { class: "n-val" }, typeof obj[a] === "string" ? JSON.stringify(obj[a]) : String(obj[a]))));
  if (obj.node === "TypeNode" && obj.values?.length) attrs.push(h("span", { class: "n-attr" }, "values=", h("span", { class: "n-val" }, JSON.stringify(obj.values))));
  const li = h("li", { class: depth >= 3 && kids.length ? "collapsed" : "" });
  const node = h("span", { class: "node" }, h("span", { class: "twisty" }, kids.length ? (depth >= 3 ? "▸" : "▾") : "·"),
    edge ? h("span", { class: "n-attr" }, edge + ":") : null, h("span", { class: "n-type" }, obj.node), attrs);
  node.addEventListener("mouseenter", () => obj.span && focusRange(obj.span[0], obj.span[1]));
  node.addEventListener("mouseleave", () => focusRange(null));
  node.addEventListener("click", (e) => {
    e.stopPropagation();
    if (kids.length) {
      li.classList.toggle("collapsed");
      node.firstChild.textContent = li.classList.contains("collapsed") ? "▸" : "▾";
    }
  });
  li.append(node);
  if (kids.length) li.append(h("ul", {}, kids.map(([k, v]) => astTree(v, depth + 1, k))));
  return li;
}

function pAst() {
  const r = S.res;
  return h("div", { class: "panel-inner" },
    title("Abstract syntax tree", "Produced by a recursive-descent parser with panic-mode recovery (it synchronises on ';' and '}' so one mistake never hides the next). Click to expand, hover to see the source span."),
    r.ast ? h("div", { class: "card card-body tree" }, h("ul", {}, astTree(r.ast))) : h("div", { class: "empty" }, "No AST"));
}

/* --------------------------------------------------------------- symbols */
function pSymbols() {
  const r = S.res;
  if (!r.symbols) return h("div", { class: "empty" }, "No symbol table");
  const g = h("tbody");
  for (const s of r.symbols.global) {
    g.append(h("tr", {}, h("td", { class: "mono" }, s.name), h("td", {}, h("span", { class: `pill ${s.kind === "rule" ? "ink" : s.kind === "pattern" ? "wine" : "blue"}` }, s.kind)),
      h("td", { class: "mono" }, s.type), h("td", { class: "mono" }, String(s.value)), h("td", {}, s.risk ? risk(s.risk) : ""), h("td", { class: "mono muted" }, s.line)));
  }
  const root = h("div", { class: "panel-inner" },
    title("Symbol table & semantic analysis", "A global scope (constants, patterns, rules) and one scope per rule. The analyser evaluates constants, type-checks every constraint and condition, and proves contradictions with interval reasoning and DFA intersection."),
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Global scope")),
      h("div", { class: "table-wrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Name", "Kind", "Type", "Value", "Risk", "Line"].map((c) => h("th", {}, c)))), g))));
  for (const sc of r.symbols.rules) {
    const tb = h("tbody");
    for (const s of sc.symbols) {
      tb.append(h("tr", {}, h("td", { class: "mono" }, s.name + (s.optional ? "?" : "")), h("td", {}, h("span", { class: "pill" }, s.kind)),
        h("td", { class: "mono" }, s.type), h("td", { class: "mono small" }, s.constraints.join(" · ") || "—"), h("td", { class: "mono muted" }, s.line)));
    }
    root.append(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, `rule ${sc.rule}`), h("span", { class: "mono small muted" }, sc.route + (Object.keys(sc.limits).length ? "  ·  " + Object.entries(sc.limits).map(([k, v]) => `${k}=${v}`).join(" ") : ""))),
      h("div", { class: "table-wrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Symbol", "Source", "Type", "Constraints", "Line"].map((c) => h("th", {}, c)))), tb))));
  }
  const sem = r.diagnostics.filter((d) => d.phase === "semantic");
  if (sem.length) {
    root.append(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Semantic findings")),
      ...sem.map((d) => hoverable(h("div", { class: `diag ${d.severity}` }, h("span", { class: "sev" }), h("span", { class: "code" }, d.code), h("div", { class: "msg" }, d.message), h("span", { class: "loc" }, `${d.span.line}:${d.span.col}`)), d.span))));
  }
  return root;
}

/* ----------------------------------------------------------- regexshield */
function pRegex() {
  const r = S.res;
  const entries = Object.values(r.regexes || {});
  const order = { HIGH: 0, MEDIUM: 1, UNKNOWN: 2, LOW: 3, ERROR: 4 };
  if (!S.rxSel && !S.rxProbe && entries.length) S.rxSel = [...entries].sort((a, b) => order[a.risk] - order[b.risk])[0].rid;
  const list = h("div", { class: "rx-list" });
  const input = h("input", { placeholder: "try any regex, e.g. ^(a|aa)+$", value: S.rxProbe?.source || "" });
  const flags = h("input", { placeholder: "flags", style: "flex:0 0 54px", value: S.rxProbe?.flags || "" });
  const go = async () => {
    try {
      const art = await api("/api/regex", { pattern: input.value, flags: flags.value });
      S.rxProbe = art;
      S.rxSel = null;
      renderPanel();
    } catch (e) {
      const d = e.data?.error;
      toast(d ? `${d.stage}: ${d.message} (position ${d.position})` : e.message);
    }
  };
  input.addEventListener("keydown", (e) => e.key === "Enter" && go());
  list.append(h("div", { class: "card card-body", style: "padding:10px;display:flex;flex-direction:column;gap:6px" },
    h("div", { class: "small muted", style: "font-weight:600" }, "Analyse any pattern"), h("div", { class: "rx-probe" }, input, flags), h("button", { class: "btn btn-primary btn-sm", onclick: go }, "Analyse")));
  if (S.rxProbe) list.append(h("button", { class: "rx-item on" }, h("span", { class: "rx-src" }, `/${S.rxProbe.source}/${S.rxProbe.flags}`), h("span", { class: "rx-meta" }, risk(S.rxProbe.risk), "custom pattern")));
  for (const e of entries) {
    list.append(h("button", { class: `rx-item ${S.rxSel === e.rid && !S.rxProbe ? "on" : ""}`, onclick: () => { S.rxSel = e.rid; S.rxProbe = null; renderPanel(); } },
      h("span", { class: "rx-src" }, `/${e.source}/${e.flags}`),
      h("span", { class: "rx-meta" }, risk(e.risk), e.name ? h("b", {}, e.name) : null, h("span", {}, e.rid))));
  }
  let detail;
  if (S.rxProbe) detail = regexDetail(S.rxProbe, null);
  else if (S.rxSel && r.regexes[S.rxSel]?.artifact) detail = regexDetail(r.regexes[S.rxSel].artifact, r.regexes[S.rxSel]);
  else if (S.rxSel) detail = h("div", { class: "empty" }, "This regex did not compile — see Diagnostics.");
  else detail = h("div", { class: "empty" }, "No regex literals in this program. Try the analyser on the left.");
  return h("div", { class: "panel-inner" },
    title("RegexShield v2 — the regex sub-compiler", "Every pattern is compiled tokens → AST → Thompson ε-NFA → DFA (subset construction) → minimal DFA, then checked for exponential (EDA) and polynomial (IDA) ambiguity, the cause of ReDoS. Findings come with a real attack string and, where possible, a rewrite proved equivalent by DFA comparison."),
    h("div", { class: "rx-layout" }, list, detail));
}

function markSpans(src, spans) {
  const ranges = spans.slice(0, 2).map((s, i) => [s.start, s.end, i === 0 ? "cul" : "cul2"]);
  let out = "";
  for (let i = 0; i < src.length; i++) {
    const opens = ranges.filter((r) => r[0] === i);
    const closes = ranges.filter((r) => r[1] === i);
    out += closes.map(() => "</span>").join("");
    out += opens.map((r) => `<span class="${r[2]}">`).join("");
    out += esc(src[i]);
  }
  out += ranges.filter((r) => r[1] >= src.length).map(() => "</span>").join("");
  return out;
}

function regexDetail(a, entry) {
  const rd = a.redos;
  const root = h("div", { class: "panel-inner" });
  const top = rd.findings.filter((f) => f.layer === "automata" && (f.severity === "HIGH" || f.severity === "MEDIUM"));
  const spans = (top.length ? top : rd.findings.filter((f) => f.severity !== "LOW")).flatMap((f) => f.spans);
  root.append(h("div", { class: "rx-head" }, h("div", { class: "flex" }, risk(rd.risk), h("span", { class: "pill ink mono" }, rd.complexity), entry?.name ? h("span", { class: "pill wine" }, entry.name) : null,
    entry ? h("span", { class: "small muted" }, "used by " + entry.used_by.join(", ")) : null),
  entry ? h("button", { class: "btn btn-light btn-sm", onclick: () => gotoOffset(entry.span[0], entry.span[1]) }, "Show in editor") : null));
  root.append(h("div", { class: "rx-source", html: "/" + markSpans(a.source, spans) + "/" + esc(a.flags) }));
  root.append(h("div", { style: "font-size:13.5px" }, rd.summary));
  const lb = a.length_bounds;
  root.append(h("div", { class: "stats" },
    h("div", { class: "stat" }, "tokens", h("b", {}, a.tokens.length - 1)),
    h("div", { class: "stat" }, "ε-NFA states", h("b", {}, a.nfa_states)),
    h("div", { class: "stat" }, "DFA states", h("b", {}, `${a.raw_dfa_states} → ${a.dfa_states}`)),
    h("div", { class: "stat" }, "symbol classes", h("b", {}, a.alphabet_classes)),
    h("div", { class: "stat" }, "length", h("b", {}, `${lb.min ?? "∅"}..${lb.max ?? "∞"}`)),
    h("div", { class: "stat" }, "pair-graph", h("b", {}, `${rd.stats.pair_nodes ?? "—"} nodes`)),
    h("div", { class: "stat" }, "analysis", h("b", {}, fmtMs(Object.values(a.timings_ms).reduce((x, y) => x + y, 0))))));
  // findings
  if (rd.findings.length) {
    root.append(h("div", { style: "display:flex;flex-direction:column;gap:8px" }, rd.findings.map((f) =>
      h("div", { class: `finding ${f.severity}` }, h("div", { class: "f-top" }, risk(f.severity === "INFO" ? "LOW" : f.severity), h("span", { class: "pill" }, f.layer), h("b", { class: "mono" }, f.kind)),
        h("div", { class: "f-msg" }, f.message)))));
  } else {
    root.append(h("div", { class: "finding LOW" }, h("div", { class: "f-msg" }, "No ambiguity: every input has at most one path through the NFA, so a backtracking engine also runs in linear time.")));
  }
  rd.notes.forEach((n) => root.append(h("div", { class: "small muted" }, "Note: " + n)));
  // attack
  if (rd.witness) {
    const w = rd.witness;
    const show = (s) => (s === "" ? "ε" : JSON.stringify(s).slice(1, -1));
    root.append(h("div", { class: "attack" },
      h("div", { class: "flex" }, h("b", {}, "Generated attack string"), h("span", { class: "small", style: "color:#a99f90" }, w.kind === "EDA" ? "each pump doubles backtracking work" : "work grows polynomially with pumps"),
        h("span", { class: "grow" }), h("button", { class: "btn btn-wine btn-sm", onclick: () => { S.benchSel = { source: a.source, flags: a.flags }; S.bench = null; setTab("bench"); runBench(); } }, "Benchmark it →")),
      h("div", { class: "a-parts" }, h("span", { class: "a-part" }, h("small", {}, "prefix"), show(w.prefix)), "·",
        h("span", { class: "a-part pump" }, h("small", {}, "pump × n"), show(w.pump)), "·", h("span", { class: "a-part" }, h("small", {}, "suffix (forces failure)"), show(w.suffix))),
      h("div", { class: "a-ex" }, show(w.example)),
      h("div", { class: "small", style: "color:#a99f90" }, "The suffix is chosen with the DFA so the whole string is rejected — rejection is what forces a backtracking engine to try every path.")));
  }
  // rewrite
  const rw = a.rewrite;
  if (rw && rw.changed) {
    const box = h("div", { class: "rewrite" },
      h("div", { class: "rw-head" }, h("span", {}, "Verified safe rewrite"), h("span", { class: "flex" }, risk(rd.risk), "→", risk(rw.final_risk))),
      h("div", { class: "rw-final" }, `/${rw.final}/${a.flags}`));
    rw.steps.forEach((s) => box.append(h("div", { class: "rw-step" }, h("div", { class: "flex" }, h("span", { class: "pill blue" }, s.rule), h("span", { class: "mono" }, s.description)),
      h("div", { class: "proof" }, "✓ L(original) = L(rewrite): XOR-product DFA is empty", h("span", { class: "muted" }, `(${s.dfa_states[0]} vs ${s.dfa_states[1]} min-DFA states)`)))));
    if (rw.rejected.length) {
      box.append(h("div", { class: "rw-step" }, h("details", {}, h("summary", {}, `${rw.rejected.length} candidate(s) rejected by verification`),
        h("div", { style: "display:grid;gap:4px;margin-top:6px" }, rw.rejected.map((x) => h("div", { class: "small" }, h("span", { class: "mono" }, `/${x.candidate}/`), " — ", h("span", { class: "muted" }, x.reason)))))));
    }
    root.append(box);
  } else if (rd.risk !== "LOW") {
    root.append(h("div", { class: "finding MEDIUM" }, h("div", { class: "f-msg" }, "No rewrite rule produced a provably equivalent safe pattern. ShieldLang still compiles this pattern to a DFA, so the generated validator is linear-time.")));
  }
  // automata
  const seg = h("div", { class: "seg" }, [["nfa", "ε-NFA"], ["raw", "DFA (subset)"], ["dfa", "Minimal DFA"]].map(([k, l]) =>
    h("button", { class: S.rxView === k ? "on" : "", onclick: () => { S.rxView = k; renderPanel.keepScroll = true; renderPanel(); } }, l)));
  const g = S.rxView === "nfa" ? a.nfa : S.rxView === "raw" ? a.raw_dfa : a.dfa;
  root.append(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Automata"), seg),
    h("div", { class: "card-body", style: "display:flex;flex-direction:column;gap:8px" },
      h("div", { class: "graph-legend" }, h("span", { class: "lg-start" }, "start"), h("span", { class: "lg-acc" }, "accepting"), S.rxView === "nfa" ? h("span", { class: "lg-eps" }, "ε-transition") : null,
        h("span", { class: "muted", style: "margin-left:auto" }, g ? `${g.total_states} states · ${g.edges.length} transitions${S.rxView !== "nfa" ? " · dead state hidden" : ""}` : "")),
      g ? drawAutomaton(g, S.rxView === "nfa") : h("div", { class: "empty" }, "Automaton too large to draw"),
      S.rxView !== "nfa" && a.partition ? h("div", { class: "small muted" }, "Alphabet partition (symbol classes): ", h("span", { class: "mono" }, a.partition.join("  ·  "))) : null)));
  // regex AST
  root.append(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Regex AST")), h("div", { class: "card-body tree" }, h("ul", {}, rxAst(a.ast, 0)))));
  return root;
}

function rxAst(n, d) {
  const li = h("li", {});
  li.append(h("span", { class: "node" }, h("span", { class: "twisty" }, n.children.length ? "▾" : "·"), h("span", { class: "n-type" }, n.type),
    h("span", { class: "n-val" }, n.label.slice(n.type.length).trim()), h("span", { class: "n-attr" }, `${n.id} [${n.span.start}..${n.span.end})`)));
  if (n.children.length) li.append(h("ul", {}, n.children.map((c) => rxAst(c, d + 1))));
  return li;
}

/* automaton drawing: BFS-layered layout in SVG */
function drawAutomaton(g, isNfa) {
  const states = g.states;
  if (states.length > 90) return h("div", { class: "empty" }, `${states.length} states — too many to draw clearly.`);
  const ids = states.map((s) => s.id);
  const adj = {};
  ids.forEach((i) => (adj[i] = []));
  g.edges.forEach((e) => adj[e.from] && adj[e.from].push(e.to));
  const layer = {};
  const startId = g.start;
  const q = [startId];
  layer[startId] = 0;
  while (q.length) {
    const u = q.shift();
    for (const v of adj[u] || []) if (layer[v] === undefined) { layer[v] = layer[u] + 1; q.push(v); }
  }
  let maxL = Math.max(0, ...Object.values(layer));
  ids.forEach((i) => { if (layer[i] === undefined) layer[i] = ++maxL; });
  const cols = {};
  ids.forEach((i) => (cols[layer[i]] = cols[layer[i]] || []).push(i));
  const DX = isNfa ? 92 : 120, DY = 66, R = 15, PAD = 46;
  const maxRows = Math.max(...Object.values(cols).map((c) => c.length));
  const H = PAD * 2 + (maxRows - 1) * DY + 30;
  const pos = {};
  Object.entries(cols).forEach(([l, list]) => {
    const off = (H - (list.length - 1) * DY) / 2;
    list.forEach((id, k) => (pos[id] = { x: PAD + 20 + l * DX, y: off + k * DY }));
  });
  const W = PAD * 2 + 40 + (maxL) * DX;
  // merge parallel edges
  const merged = {};
  for (const e of g.edges) {
    const k = `${e.from}->${e.to}`;
    if (!merged[k]) merged[k] = { ...e, labels: [] };
    merged[k].labels.push(e.label);
  }
  const svgNS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs, text) => {
    const x = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs)) x.setAttribute(k, v);
    if (text != null) x.textContent = text;
    return x;
  };
  const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, "font-family": "JetBrains Mono, monospace" });
  const defs = el("defs", {});
  for (const [id, col] of [["ah", "#121212"], ["ahe", "#958b7e"], ["ahb", "#2448d8"]]) {
    const m = el("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
    m.append(el("path", { d: "M0,0 L10,5 L0,10 z", fill: col }));
    defs.append(m);
  }
  svg.append(defs);
  const edgesG = el("g", {}), labelsG = el("g", {}), nodesG = el("g", {});
  svg.append(edgesG, labelsG, nodesG);
  Object.values(merged).forEach((e) => {
    const a = pos[e.from], b = pos[e.to];
    if (!a || !b) return;
    const eps = e.epsilon || e.labels.every((l) => l === "ε");
    let label = [...new Set(e.labels)].join(", ");
    if (label.length > 22) label = label.slice(0, 21) + "…";
    const stroke = eps ? "#958b7e" : "#121212";
    let d, lx, ly;
    if (e.from === e.to) {
      d = `M ${a.x - 8} ${a.y - R + 2} C ${a.x - 26} ${a.y - 52}, ${a.x + 26} ${a.y - 52}, ${a.x + 8} ${a.y - R + 2}`;
      lx = a.x; ly = a.y - 46;
    } else {
      const forward = layer[e.to] > layer[e.from];
      const dx = b.x - a.x, dy = b.y - a.y;
      const len = Math.hypot(dx, dy) || 1;
      const ux = dx / len, uy = dy / len;
      const sx = a.x + ux * R, sy = a.y + uy * R, tx = b.x - ux * (R + 2), ty = b.y - uy * (R + 2);
      const bend = forward && Math.abs(layer[e.to] - layer[e.from]) === 1 ? 0.12 : 0.35 + Math.min(0.4, Math.abs(layer[e.to] - layer[e.from]) * 0.05);
      const sign = forward ? -1 : 1;
      const mx = (sx + tx) / 2 + sign * -uy * len * bend, my = (sy + ty) / 2 + sign * ux * len * bend;
      d = `M ${sx} ${sy} Q ${mx} ${my} ${tx} ${ty}`;
      lx = (sx + 2 * mx + tx) / 4; ly = (sy + 2 * my + ty) / 4 - 4;
    }
    edgesG.append(el("path", { d, fill: "none", stroke, "stroke-width": eps ? 1.1 : 1.4, "stroke-dasharray": eps ? "4 3" : "", "marker-end": `url(#${eps ? "ahe" : "ah"})` }));
    const tw = label.length * 6.6 + 6;
    labelsG.append(el("rect", { x: lx - tw / 2, y: ly - 9, width: tw, height: 13, rx: 3, fill: "#fffdf9", opacity: 0.92 }));
    labelsG.append(el("text", { x: lx, y: ly + 1.5, "text-anchor": "middle", "font-size": 10.5, fill: eps ? "#958b7e" : "#7b1e3c" }, label));
  });
  states.forEach((s) => {
    const p = pos[s.id];
    const gN = el("g", {});
    const title = el("title", {}, isNfa ? `q${s.id}${s.role ? " — " + s.role : ""}${s.owner ? " (from " + s.owner + ")" : ""}` : `D${s.id}${s.nfa_states ? " = {" + s.nfa_states.join(",") + "}" : ""}`);
    gN.append(title);
    if (s.start) gN.append(el("path", { d: `M ${p.x - R - 26} ${p.y} L ${p.x - R - 2} ${p.y}`, stroke: "#2448d8", "stroke-width": 2, "marker-end": "url(#ahb)" }));
    gN.append(el("circle", { cx: p.x, cy: p.y, r: R, fill: s.start ? "#e6ebfc" : "#ffffff", stroke: s.start ? "#2448d8" : "#121212", "stroke-width": 1.6 }));
    if (s.accept) gN.append(el("circle", { cx: p.x, cy: p.y, r: R - 3.5, fill: "none", stroke: "#121212", "stroke-width": 1.3 }));
    gN.append(el("text", { x: p.x, y: p.y + 3.8, "text-anchor": "middle", "font-size": 10.5, "font-weight": 600, fill: "#121212" }, (isNfa ? "q" : "D") + s.id));
    nodesG.append(gN);
  });
  return h("div", { class: "graph-wrap" }, svg);
}

/* -------------------------------------------------------------------- IR */
function hlIr(line) {
  const re = /(;.*$)|("(?:[^"\\]|\\.)*")|(\bL\d+:)|(\bL\d+\b)|(\bt\d+\b)|(\bdfa\d+\b)|(!E\d+)|(\b[A-Z][A-Z_]{2,}(?:\.each)?\b)/g;
  let out = "", last = 0, m;
  while ((m = re.exec(line))) {
    out += esc(line.slice(last, m.index));
    const c = m[1] ? "ir-com" : m[2] ? "ir-str" : m[3] ? "ir-lbl" : m[4] ? "ir-lbl" : m[5] ? "ir-tmp" : m[6] ? "ir-dfa" : m[7] ? "ir-err" : "ir-op";
    out += `<span class="${c}">${esc(m[0])}</span>`;
    last = re.lastIndex;
  }
  return out + esc(line.slice(last));
}

function codeView(text, hl) {
  const tb = h("tbody");
  text.replace(/\n$/, "").split("\n").forEach((ln, i) => tb.append(h("tr", {}, h("td", { class: "ln" }, i + 1), h("td", { class: "lc", html: hl(ln) || " " }))));
  return h("div", { class: "codeview" }, h("table", {}, tb));
}

function pIr() {
  const r = S.res;
  if (!r.ir) return needOk("intermediate code");
  const cur = S.irView === "opt" ? r.opt_ir : r.ir;
  const seg = h("div", { class: "seg" }, [["raw", "Unoptimised"], ["opt", "Optimised"]].map(([k, l]) =>
    h("button", { class: S.irView === k ? "on" : "", onclick: () => { S.irView = k; renderPanel(); } }, l)));
  const dfas = h("tbody");
  cur.dfas.forEach((d) => dfas.append(h("tr", {}, h("td", { class: "mono" }, d.did), h("td", {}, h("span", { class: `pill ${d.kind.includes("fused") || d.kind.includes("merged") ? "blue" : ""}` }, d.kind)),
    h("td", { class: "mono" }, d.states), h("td", { class: "mono" }, d.classes), h("td", { class: "mono small" }, d.sources.map((s) => `/${s}/`).join(" ∧ ")))));
  const errs = h("tbody");
  cur.errors.forEach((e) => errs.append(h("tr", {}, h("td", { class: "mono" }, e.code), h("td", {}, h("span", { class: "pill" }, e.kind)), h("td", { class: "mono" }, e.target), h("td", {}, e.message))));
  return h("div", { class: "panel-inner" },
    title("Intermediate representation (SIR)", "Three-address, label-based code. Each rule is a function of basic blocks; a failing check records its error code and jumps to the end of its block, so one bad field never hides another. Conditions are lowered to CMP/AND/OR temporaries.", seg),
    h("div", { class: "stats" }, h("div", { class: "stat" }, "instructions", h("b", {}, cur.instructions)), h("div", { class: "stat" }, "static cost", h("b", {}, cur.cost)),
      h("div", { class: "stat" }, "functions", h("b", {}, cur.functions.length)), h("div", { class: "stat" }, "DFAs", h("b", {}, cur.dfas.length)), h("div", { class: "stat" }, "error codes", h("b", {}, cur.errors.length))),
    codeView(cur.text, hlIr),
    h("div", { class: "two-col" },
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "DFA table")), h("div", { class: "table-wrap", style: "max-height:320px" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Id", "Kind", "States", "Classes", "Patterns"].map((c) => h("th", {}, c)))), dfas))),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Error table")), h("div", { class: "table-wrap", style: "max-height:320px" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Code", "Kind", "Target", "Message"].map((c) => h("th", {}, c)))), errs)))));
}

/* ------------------------------------------------------------- optimiser */
function pOpt() {
  const r = S.res;
  if (!r.passes) return needOk("optimised code");
  const pct = Math.round(100 - (100 * r.opt_ir.cost) / Math.max(1, r.ir.cost));
  const root = h("div", { class: "panel-inner" },
    title("Optimiser", "Six passes over the IR, each explaining what it changed. The cost model weights a DFA run or a scan far above a length or type check."),
    h("div", { class: "kpis" }, kpi("Instructions", `${r.ir.instructions} → ${r.opt_ir.instructions}`, "static count", "dark"),
      kpi("Static cost", `${r.ir.cost} → ${r.opt_ir.cost}`, `${pct}% cheaper`, "blue"),
      kpi("DFAs", `${r.ir.dfas.length} → ${r.opt_ir.dfas.length}`, "incl. merged / fused automata"),
      kpi("Passes that fired", r.passes.filter((p) => p.changes.length).length + " / " + r.passes.length, "")),
    h("div", { class: "card card-body" }, h("div", { class: "small muted", style: "margin-bottom:6px" }, "Static cost before → after"),
      h("div", { class: "bar" }, h("div", { style: `width:${100 - pct}%;background:var(--blue)` }), h("div", { style: `width:${pct}%;background:var(--wine-soft)` })),
      h("div", { class: "flex small muted", style: "margin-top:6px" }, h("span", {}, h("b", { style: "color:var(--blue)" }, "■ "), "remaining cost"), h("span", {}, h("b", { style: "color:var(--wine)" }, "■ "), `${pct}% removed`))));
  r.passes.forEach((p, i) => {
    root.append(h("div", { class: `pass ${p.changes.length ? "" : "noop"}` }, h("div", { class: "p-num" }, i + 1),
      h("div", { class: "p-card" }, h("div", { class: "p-title" }, h("h4", {}, p.title), h("span", { class: "delta" }, "instrs ", h("b", {}, `${p.instrs_before}→${p.instrs_after}`), "  cost ", h("b", {}, `${p.cost_before}→${p.cost_after}`))),
        h("div", { class: "p-desc" }, p.description),
        p.changes.length ? h("ul", {}, p.changes.map((c) => h("li", {}, c))) : h("div", { class: "small muted" }, "No opportunities in this program."))));
  });
  return root;
}

/* ------------------------------------------------------------------ code */
function hlCode(line, lang) {
  const kws = lang === "py" ? "def|return|if|elif|else|for|in|not|and|or|is|while|break|import|from|class|None|True|False|pass|lambda|try|except"
    : "function|return|if|else|for|of|in|const|let|new|class|break|do|while|true|false|null|typeof|this";
  const com = lang === "py" ? "#.*$" : "\\/\\/.*$|\\/\\*\\*.*\\*\\/";
  const re = new RegExp(`(${com})|("(?:[^"\\\\]|\\\\.)*"|'(?:[^'\\\\]|\\\\.)*'|\`[^\`]*\`)|\\b(${kws})\\b|\\b(\\d+)\\b|\\b(validate_\\w+|_\\w+|DFA\\d*|route|validate)\\b(?=\\()`, "g");
  let out = "", last = 0, m;
  if (line.length > 600) return esc(line.slice(0, 600)) + `<span class="py-com">  … (${line.length - 600} more chars of table data)</span>`;
  while ((m = re.exec(line))) {
    out += esc(line.slice(last, m.index));
    const c = m[1] ? "py-com" : m[2] ? "py-str" : m[3] ? "py-kw" : m[4] ? "py-num" : "py-fn";
    out += `<span class="${c}">${esc(m[0])}</span>`;
    last = re.lastIndex;
  }
  return out + esc(line.slice(last));
}

function baseName() { return (S.name || "rules.shl").replace(/\.shl$/, ""); }

function pCode() {
  const r = S.res;
  if (!r.python) return needOk("code");
  const text = S.codeLang === "py" ? r.python : r.js;
  const fname = `${baseName()}_validator.${S.codeLang === "py" ? "py" : "js"}`;
  const seg = h("div", { class: "seg" }, [["py", "Python"], ["js", "JavaScript"]].map(([k, l]) =>
    h("button", { class: S.codeLang === k ? "on" : "", onclick: () => { S.codeLang = k; renderPanel(); } }, l)));
  return h("div", { class: "panel-inner" },
    title("Code generation", "The optimised IR is emitted as a dependency-free validator. Every pattern becomes a table-driven DFA (alphabet-class boundaries + transition table), so the firewall is linear-time by construction.", seg),
    h("div", { class: "flex" }, h("span", { class: "pill ink mono" }, fname), h("span", { class: "small muted" }, `${text.split("\n").length} lines · ${(text.length / 1024).toFixed(1)} KB`), h("span", { class: "grow" }),
      h("button", { class: "btn btn-light btn-sm", onclick: () => { navigator.clipboard?.writeText(text); toast("Copied to clipboard"); } }, "Copy"),
      h("button", { class: "btn btn-primary btn-sm", onclick: () => download(fname, text) }, "Download")),
    codeView(text, (l) => hlCode(l, S.codeLang)),
    h("div", { class: "small muted" }, S.codeLang === "py" ? `Run it:  echo '{"method":"POST","path":"/api/pay","body":{}}' | python ${fname}` : `Use it:  const { validate } = require("./${fname}");  validate(request)`));
}

/* ------------------------------------------------------------------- run */
function pRun() {
  const r = S.res;
  if (!r.ok) return needOk("an executable validator");
  const presets = (S.ex && S.ex.source === S.source ? S.ex.requests : S.ex?.requests) || [];
  if (!S.runText) S.runText = JSON.stringify(presets[0]?.request || { method: "POST", path: "/", headers: {}, body: {} }, null, 2);
  const area = h("textarea", { class: "json", spellcheck: "false" });
  area.value = S.runText;
  area.addEventListener("input", () => (S.runText = area.value));
  area.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runRequest(); } });
  const left = h("div", { class: "panel-inner" },
    presets.length ? h("div", { class: "card card-body", style: "padding:10px" }, h("div", { class: "small muted", style: "font-weight:600;margin-bottom:6px" }, "Sample requests"),
      h("div", { class: "presets" }, presets.map((p, i) => h("button", { class: `preset ${S.preset === i ? "on" : ""}`, onclick: () => { S.preset = i; S.runText = JSON.stringify(p.request, null, 2); S.runRes = null; renderPanel(); runRequest(); } }, p.name)))) : null,
    area,
    h("div", { class: "flex" }, h("button", { class: "btn btn-primary", onclick: runRequest }, "Validate request"), h("span", { class: "small muted" }, "Ctrl/⌘ + Enter")));
  const right = h("div", { class: "panel-inner", id: "run-out" });
  renderRunResult(right);
  return h("div", { class: "panel-inner" },
    title("Request console", "Runs the compiled IR on a virtual machine, exactly as the generated validators would, and shows the executed instructions. The same request is also run on the unoptimised IR for comparison."),
    h("div", { class: "run-layout" }, left, right));
}

async function runRequest() {
  let req;
  try { req = JSON.parse(S.runText); } catch (e) { toast("Request is not valid JSON: " + e.message); return; }
  try {
    S.runRes = await api("/api/run", { source: S.source, name: S.name, request: req });
  } catch (e) { toast(e.message); return; }
  const out = $("#run-out");
  if (out) { out.innerHTML = ""; renderRunResult(out); }
}

function renderRunResult(root) {
  const rr = S.runRes;
  if (!rr) { root.append(h("div", { class: "empty" }, "Pick a sample request or edit the JSON, then validate it.")); return; }
  const o = rr.optimized, u = rr.unoptimized;
  root.append(o.allowed
    ? h("div", { class: "banner ok" }, h("div", { class: "b-icon" }, "✓"), h("div", {}, h("div", { class: "b-title" }, "ALLOWED"), h("div", { class: "b-sub" }, `matched rule ${o.rule}${Object.keys(o.params || {}).length ? " · params " + JSON.stringify(o.params) : ""}`)))
    : h("div", { class: "banner bad" }, h("div", { class: "b-icon" }, "✕"), h("div", {}, h("div", { class: "b-title" }, `BLOCKED — ${o.errors.length} violation${o.errors.length === 1 ? "" : "s"}`), h("div", { class: "b-sub" }, o.rule ? `rule ${o.rule}` : "no matching route"))));
  root.append(h("div", { class: "kpis" }, kpi("Instructions run", o.instructions, `unoptimised: ${u.instructions}`, "dark"), kpi("DFA steps", o.dfa_steps, "≤ total input length", "blue"),
    kpi("Backtracking", "0", "DFA matching never backtracks")));
  if (o.errors.length) {
    root.append(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Violations")),
      o.errors.map((e) => h("div", { class: "err-row" }, h("span", { class: "ec" }, e.code), h("span", { class: "ek" }, e.kind), h("span", {}, e.message)))));
  }
  const tb = h("tbody");
  o.trace.forEach((t, i) => tb.append(h("tr", {}, h("td", { class: "muted" }, i + 1), h("td", { style: "white-space:pre" }, t.text),
    h("td", { class: /FAIL|BLOCKED/.test(t.note) ? "fail" : /skip/.test(t.note) ? "skip" : "" }, t.note + (t.jump ? `  → ${t.jump}` : "")))));
  root.append(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Execution trace"), h("span", { class: "small muted" }, `${o.trace.length} steps`)),
    h("div", { class: "table-wrap", style: "max-height:380px" }, h("table", { class: "t trace" }, tb))));
}

/* ------------------------------------------------------------- benchmark */
function benchCandidates() {
  const list = Object.values(S.res?.regexes || {}).filter((e) => e.artifact).map((e) => ({ source: e.source, flags: e.flags, risk: e.risk }));
  if (S.rxProbe) list.unshift({ source: S.rxProbe.source, flags: S.rxProbe.flags, risk: S.rxProbe.risk });
  return list;
}

async function runBench() {
  if (!S.benchSel) return;
  S.benchBusy = true;
  if (S.tab === "bench") renderPanel();
  try { S.bench = await api("/api/bench", { pattern: S.benchSel.source, flags: S.benchSel.flags }); } catch (e) { toast(e.message); }
  S.benchBusy = false;
  renderPanel.keepScroll = true;
  if (S.tab === "bench") renderPanel();
}

function pBench() {
  const cands = benchCandidates();
  const order = { HIGH: 0, MEDIUM: 1, UNKNOWN: 2, LOW: 3 };
  if (!S.benchSel && cands.length) S.benchSel = [...cands].sort((a, b) => order[a.risk] - order[b.risk])[0];
  const sel = h("select", { style: "flex:1;min-width:0;padding:7px 9px;border:1px solid var(--line-2);border-radius:6px;font:12.5px var(--mono);background:var(--white)" },
    cands.map((c, i) => h("option", { value: i, selected: S.benchSel && c.source === S.benchSel.source && c.flags === S.benchSel.flags }, `[${c.risk}] /${c.source}/${c.flags}`)));
  sel.addEventListener("change", () => { S.benchSel = cands[+sel.value]; S.bench = null; renderPanel(); });
  const root = h("div", { class: "panel-inner" },
    title("Backtracking vs DFA", "Runs the generated attack string against Python's backtracking `re` engine (in a sandboxed child process with a 2 s timeout) and against the ShieldLang DFA, for growing n."),
    h("div", { class: "flex" }, sel, h("button", { class: "btn btn-wine", onclick: runBench, disabled: S.benchBusy || !S.benchSel }, S.benchBusy ? h("span", { class: "spinner" }) : null, S.benchBusy ? " Running…" : "Run benchmark")));
  const b = S.bench;
  if (!b) { root.append(h("div", { class: "empty" }, S.benchBusy ? "Measuring… exponential patterns need a few seconds." : "Choose a pattern and run the benchmark.")); return root; }
  root.append(h("div", { class: "flex" }, risk(b.risk), h("span", { class: "pill ink mono" }, b.complexity), h("span", { class: "small muted mono" }, `input = ${JSON.stringify(b.prefix)} + ${JSON.stringify(b.pump)} × n + ${JSON.stringify(b.suffix)}`)));
  root.append(h("div", { class: "chart" }, benchChart(b)),
    h("div", { class: "legend" }, h("span", {}, h("i", { style: "background:var(--wine)" }), "Python re (backtracking)"), h("span", {}, h("i", { style: "background:var(--blue)" }), "ShieldLang DFA"), h("span", {}, "✕ = timeout (> 2 s)")));
  const tb = h("tbody");
  b.rows.forEach((r) => tb.append(h("tr", {}, h("td", { class: "mono" }, r.n), h("td", { class: "mono" }, r.length), h("td", { class: "mono", style: r.timeout ? "color:var(--wine);font-weight:600" : "" }, r.timeout ? "TIMEOUT" : fmtMs(r.backtracking_s * 1000)),
    h("td", { class: "mono" }, fmtMs(r.dfa_s * 1000)), h("td", { class: "mono" }, r.dfa_steps), h("td", { class: "mono" }, r.timeout ? "∞" : `${Math.max(1, Math.round(r.backtracking_s / Math.max(r.dfa_s, 1e-7)))}×`))));
  root.append(h("div", { class: "card" }, h("div", { class: "table-wrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["n", "length", "backtracking", "DFA", "DFA steps", "slow-down"].map((c) => h("th", {}, c)))), tb))));
  return root;
}

function benchChart(b) {
  const W = 720, H = 300, L = 64, Rm = 18, T = 16, B = 42;
  const rows = b.rows;
  const xs = rows.map((r) => r.n);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const tmo = b.timeout_s * 1000;
  const vals = [];
  rows.forEach((r) => { if (r.backtracking_s != null) vals.push(r.backtracking_s * 1000); vals.push(r.dfa_s * 1000); });
  if (rows.some((r) => r.timeout)) vals.push(tmo);
  const lo = Math.pow(10, Math.floor(Math.log10(Math.max(1e-4, Math.min(...vals))))), hi = Math.pow(10, Math.ceil(Math.log10(Math.max(...vals))));
  const X = (n) => L + ((n - x0) / Math.max(1, x1 - x0)) * (W - L - Rm);
  const Y = (ms) => T + (1 - (Math.log10(Math.max(ms, lo)) - Math.log10(lo)) / Math.max(1e-9, Math.log10(hi) - Math.log10(lo))) * (H - T - B);
  const ns = "http://www.w3.org/2000/svg";
  const el = (tag, a, txt) => { const x = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(a)) x.setAttribute(k, v); if (txt != null) x.textContent = txt; return x; };
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, "font-family": "Inter, sans-serif", "font-size": 11 });
  for (let e = Math.log10(lo); e <= Math.log10(hi) + 1e-9; e++) {
    const ms = Math.pow(10, e);
    svg.append(el("line", { x1: L, x2: W - Rm, y1: Y(ms), y2: Y(ms), stroke: "#e4d9c7" }));
    svg.append(el("text", { x: L - 8, y: Y(ms) + 4, "text-anchor": "end", fill: "#6f665b" }, ms >= 1000 ? `${ms / 1000} s` : `${+ms.toPrecision(3)} ms`));
  }
  rows.forEach((r) => svg.append(el("text", { x: X(r.n), y: H - B + 18, "text-anchor": "middle", fill: "#6f665b" }, r.n)));
  svg.append(el("text", { x: (L + W - Rm) / 2, y: H - 6, "text-anchor": "middle", fill: "#6f665b" }, "n (number of pump repetitions)"));
  svg.append(el("text", { x: 14, y: (T + H - B) / 2, transform: `rotate(-90 14 ${(T + H - B) / 2})`, "text-anchor": "middle", fill: "#6f665b" }, "match time (log scale)"));
  if (rows.some((r) => r.timeout)) svg.append(el("line", { x1: L, x2: W - Rm, y1: Y(tmo), y2: Y(tmo), stroke: "#7b1e3c", "stroke-dasharray": "4 4", opacity: 0.6 }));
  const line = (pts, col) => {
    if (pts.length > 1) svg.append(el("polyline", { points: pts.map((p) => p.join(",")).join(" "), fill: "none", stroke: col, "stroke-width": 2.4, "stroke-linejoin": "round" }));
    pts.forEach((p) => svg.append(el("circle", { cx: p[0], cy: p[1], r: 3.4, fill: "#fff", stroke: col, "stroke-width": 2 })));
  };
  line(rows.filter((r) => !r.timeout && r.backtracking_s != null).map((r) => [X(r.n), Y(r.backtracking_s * 1000)]), "#7b1e3c");
  line(rows.map((r) => [X(r.n), Y(r.dfa_s * 1000)]), "#2448d8");
  rows.filter((r) => r.timeout).forEach((r) => svg.append(el("text", { x: X(r.n), y: Y(tmo) + 4, "text-anchor": "middle", fill: "#7b1e3c", "font-size": 15, "font-weight": 700 }, "✕")));
  return svg;
}

/* ------------------------------------------------------------------ boot */
function loadExample(ex) {
  S.ex = ex;
  S.name = ex.name;
  S.source = ex.source;
  S.rxSel = null; S.rxProbe = null; S.runRes = null; S.runText = ""; S.preset = -1; S.bench = null; S.benchSel = null;
  $("#file-name").textContent = ex.name;
  ta().value = ex.source;
  ta().scrollTop = 0;
  S.focus = null;
  renderEditor();
  compile();
}

async function boot() {
  setupEditor();
  renderPipeline();
  renderTabs();
  $("#btn-compile").addEventListener("click", compile);
  $("#btn-download-py").addEventListener("click", () => S.res?.python ? download(`${baseName()}_validator.py`, S.res.python) : toast("Fix compile errors first"));
  $("#btn-download-js").addEventListener("click", () => S.res?.js ? download(`${baseName()}_validator.js`, S.res.js) : toast("Fix compile errors first"));
  try {
    S.examples = await api("/api/examples");
  } catch (e) {
    toast("Could not reach the ShieldLang server");
    return;
  }
  const sel = $("#example-select");
  S.examples.forEach((ex, i) => sel.append(h("option", { value: i }, ex.title)));
  sel.addEventListener("change", () => loadExample(S.examples[+sel.value]));
  if (S.examples.length) loadExample(S.examples[0]);
}

boot();
