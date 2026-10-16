/* Guided presentation. All compiler results come from the existing local APIs. */
"use strict";

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const state = {step:1, source:"", before:"", originals:null, compilation:null, comparison:null,
  replay:null, selected:0, requestText:"", busy:null, error:null, ready:false};
const captions = ["A small policy with real consequences.", "Source becomes executable checks.",
  "Inspect the claim—and its coverage.", "An edit becomes a concrete example.", "Same request. Two policy versions."];

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.error === "string" ? data.error : data.error?.message || `Request failed (${response.status})`);
  return data;
}

function announce(text) { $("#demo-announcement").textContent = text; }
function button(label, action, style="button-blue", disabled=false) {
  return `<button type="button" class="button ${style}" data-action="${action}" ${disabled ? "disabled" : ""}>${label}</button>`;
}
function status(message, error=false) {
  return `<div class="result-status${error ? " error" : ""}" role="${error ? "alert" : "status"}">${escapeHtml(message)}</div>`;
}
function pretty(value) { return escapeHtml(JSON.stringify(value, null, 2)); }
function metrics(items) { return `<div class="evidence-metrics">${items.map(([value,label])=>`<div><b>${escapeHtml(value)}</b><span>${escapeHtml(label)}</span></div>`).join("")}</div>`; }
function details(title, value) { return `<details class="evidence-details"><summary>${escapeHtml(title)}</summary><pre>${pretty(value)}</pre></details>`; }
function setStep(step, focus=false) {
  state.step = Math.max(1, Math.min(5, step));
  render();
  if (focus) $(`#step-${state.step}`).focus();
}
function invalidate() {
  state.compilation = null; state.comparison = null; state.replay = null; state.error = null;
}
function render() {
  document.querySelectorAll("[data-step]").forEach((tab)=>{
    const selected = Number(tab.dataset.step) === state.step;
    tab.setAttribute("aria-selected", String(selected)); tab.tabIndex = selected ? 0 : -1;
  });
  $("#demo-panel").setAttribute("aria-labelledby", `step-${state.step}`);
  $("#step-caption").textContent = `0${state.step} / 05 — ${captions[state.step-1]}`;
  $("#demo-panel").innerHTML = (state.error ? status(state.error,true) : "") + [writePanel,compilePanel,verifyPanel,comparePanel,replayPanel][state.step-1]();
}

function writePanel() {
  return `<div class="demo-grid"><div><label class="policy-label" for="demo-source"><span>The policy · editable</span><span>ShieldLang / .shl</span></label>
    <textarea class="code-editor" id="demo-source" spellcheck="false" aria-label="Demo policy source" ${!state.ready ? "disabled" : ""}>${escapeHtml(state.source)}</textarea><span id="edit-status" class="demo-note"></span></div>
    <div class="policy-explain"><div><div class="eyebrow">01 / Declare the intent</div><h3>A rule is more than<br>a regular expression.</h3><p>This example validates account creation. The policy describes an endpoint, the shape of its fields, and limits on the parsed body.</p>
    <ul><li><b>→</b> Lowercase usernames, 3–12 characters</li><li><b>→</b> A tier chosen from basic or team</li><li><b>→</b> Body depth and serialized-size limits</li></ul><p class="demo-note">Those descriptions refer to the bundled example. Edit the source to explore different rules.</p></div>
    <div class="panel-actions">${button("Compile this policy →","compile","button-blue",!state.ready || !!state.busy)}</div></div></div>`;
}

function compilePanel() {
  if (state.busy === "compile") return status("Compiling the policy with the local ShieldLang compiler…");
  const result = state.compilation;
  if (!result) return `<div class="eyebrow">02 / Follow the transformation</div><h3>Let the compiler do the work.</h3><p>Compile the policy to inspect actual phases, intermediate instructions, and generated targets.</p>${button("Compile policy →","compile","button-blue",!state.ready || !!state.busy)}`;
  if (!result.ok) return `<h3>The compiler found a problem.</h3>${status("Compilation stopped. Edit the policy and compile again.",true)}${details("Compiler diagnostics",result.diagnostics)}<div class="panel-actions">${button("Return to the policy","step-1","button-outline")}</div>`;
  const phases = [["lexer","Lexer"],["parser","Parser"],["semantic","Semantic"],["regex","RegexShield"],["ir","Intermediate code"],["optimizer","Optimizer"],["verification","Verification"],["codegen","Code generation"]];
  const rail = phases.map(([key,label])=>`<div class="phase-cell"><span>${label}</span><b>${result.timings_ms[key] == null ? "Unavailable" : `${escapeHtml(result.timings_ms[key].toFixed(2))} ms`}</b></div>`).join("");
  const changes = (result.passes || []).filter(pass=>pass.changes.length);
  const transformations = changes.length ? `<div class="scope-box"><strong>Transformations applied</strong>${changes.map(pass=>`<p><b>${escapeHtml(pass.title)}</b> — ${escapeHtml(pass.changes.join("; "))}</p>`).join("")}</div>` : '<p class="demo-note">This source did not trigger an optimization change. Verification still reports its supported field checks.</p>';
  const count = result.diagnostics.filter(d=>d.severity==="warning").length;
  return `<div class="eyebrow">02 / Follow the transformation</div><h3>Intent becomes instructions.</h3><p>The compiler parses the policy, checks its meaning, lowers it to SIR, and generates standalone validators. The timings below are reported by this compilation—not a preset benchmark.</p>
    ${metrics([[result.rules?.length || 0,"ROUTES"],[result.ir?.instructions ?? "—","IR INSTRUCTIONS BEFORE"],[result.opt_ir?.instructions ?? "—","IR INSTRUCTIONS AFTER"],[count,"WARNINGS"]])}
    <div class="phase-rail">${rail}</div>${transformations}<details class="evidence-details"><summary>Look at the optimized intermediate code</summary><pre class="code-output">${escapeHtml(result.opt_ir?.text || "No IR available")}</pre></details>
    ${result.diagnostics.length ? details("Inspect diagnostics",result.diagnostics) : ""}
    <div class="panel-actions">${button("Inspect verification →","step-3")}${button("Edit policy","step-1","button-outline")}</div>`;
}

function verifyPanel() {
  const result = state.compilation;
  if (!result?.ok) return `<div class="eyebrow">03 / Check the transformation</div><h3>First, compile a valid policy.</h3><p>Verification results belong to a particular compiled source. Editing the source clears its previous results.</p>${button("Compile policy →","compile","button-blue",!state.ready || !!state.busy)}`;
  const report = result.verification;
  if (!report?.checks) return status("This server session does not provide field verification. Restart Studio with the updated backend, then compile again.",true);
  const rows = report.checks.map(c=>`<tr><td>${escapeHtml(c.source)} ${escapeHtml(c.field)}</td><td>${escapeHtml(c.status)}</td><td>${escapeHtml(c.reason || `${c.states ?? 0} states${c.complete ? " · complete search" : ""}`)}</td></tr>`).join("");
  return `<div class="eyebrow">03 / Check the transformation</div><h3>Did optimization change<br>what the field accepts?</h3><p>A separate graph search compares supported fields before and after optimization. A discovered difference stops code generation.</p>
    ${metrics([[report.counts.equivalent,"EQUIVALENT FIELDS"],[report.counts.different,"DIFFERENCES"],[report.counts.inconclusive,"INCONCLUSIVE"],[report.counts.unsupported,"UNSUPPORTED"]])}
    <div class="result-table-wrap"><table class="result-table"><thead><tr><th>Field</th><th>Result</th><th>Coverage</th></tr></thead><tbody>${rows || '<tr><td colspan="3">No string fields to check.</td></tr>'}</tbody></table></div>
    <div class="scope-box"><strong>A precise claim, not a blanket guarantee.</strong><p>${escapeHtml(report.scope)}. Outside this check: ${escapeHtml(report.exclusions.join("; "))}.</p></div>
    <div class="panel-actions">${button("Compare two versions →","step-4")}${button("Download field evidence","export-verification","button-outline")}</div>`;
}

function comparePanel() {
  const result = state.comparison;
  const editor = `<label class="policy-label" for="baseline-source"><span>Earlier policy · editable</span><span>Compare with step 01</span></label><textarea class="code-editor" id="baseline-source" spellcheck="false" aria-label="Earlier policy source" ${!state.ready ? "disabled" : ""}>${escapeHtml(state.before)}</textarea>`;
  let output = "";
  if (result?.status === "compile_error") output = status("One policy has compilation errors. Check the diagnostics below.",true) + details("Earlier policy diagnostics",result.before) + details("Current policy diagnostics",result.after);
  else if (result) {
    const witnesses = result.witnesses || [];
    if (witnesses.length) {
      const chosen = witnesses[Math.min(state.selected,witnesses.length-1)];
      output = `<div class="comparison-summary">${status(`${witnesses.length} request examples confirmed by replay on both policy VMs.`)}<div class="witness-layout"><div class="witness-choices">${witnesses.map((w,i)=>`<button class="witness-choice" data-witness="${i}" aria-pressed="${i===state.selected}"><strong>${w.allowed_before?"Allowed":"Denied"}<span class="change-arrow">→</span>${w.allowed_after?"Allowed":"Denied"}</strong><span>Candidate field: ${escapeHtml(w.field)}</span></button>`).join("")}</div><div class="witness-detail"><label class="policy-label">The request</label><pre>${pretty(chosen.request)}</pre><div class="panel-actions">${button("Replay this request →","use-witness")}</div></div></div></div>`;
    } else output = `<div class="comparison-summary">${status("No full-request verdict change was found. This does not establish that the policies are equivalent.")}</div>`;
    output += details("Field-level comparison and unsupported cases",result.checks || []);
  }
  return `<div class="eyebrow">04 / Make a change concrete</div><h3>What does a policy edit<br>actually change?</h3><p>The bundled baseline allows shorter names and offers basic/pro tiers. The current example allows longer names and basic/team. Compare them to find requests affected by those edits.</p>${editor}
    <div class="panel-actions">${button(state.busy==="compare"?"Comparing…":"Find changed requests →","compare","button-blue",!state.ready || !!state.busy)}${button("Edit current policy","step-1","button-outline")}</div>
    <div id="comparison-result" style="margin-top:24px">${output}</div><div class="scope-box"><strong>A returned request proves a concrete verdict difference.</strong><p>Search and request construction are incomplete. Field differences can be masked by other checks. No witness is not a proof of whole-program equivalence.</p></div>`;
}

function verdict(label,result) {
  const out = result.optimized;
  return `<div class="verdict${out.allowed?"":" deny"}"><span>${label}</span><strong>${out.allowed?"Allowed":"Denied"}</strong><p>${out.allowed?"The request passed this policy.":escapeHtml(out.errors.map(e=>e.message).join(" "))}</p><div class="tiny">${escapeHtml(out.instructions)} IR instructions · ${escapeHtml(out.dfa_steps)} DFA steps<br>Unoptimized verdict: ${result.unoptimized.allowed?"allowed":"denied"}</div></div>`;
}

function replayPanel() {
  const ready = state.compilation?.ok;
  const example = state.requestText || JSON.stringify({method:"POST",path:"/api/accounts",body:{username:"aaaaaaaaa",tier:"basic"}},null,2);
  state.requestText = example;
  return `<div class="eyebrow">05 / Run the evidence</div><h3>One request.<br>Two independently executed policies.</h3><p>Edit the JSON or use an example from the comparison. Both versions run through the real VM; the output below is an execution result.</p>
    <label class="policy-label" for="replay-request">Request JSON</label><textarea class="code-editor" id="replay-request" aria-label="Request to replay" spellcheck="false">${escapeHtml(example)}</textarea>
    <div class="panel-actions">${button(state.busy==="replay"?"Replaying…":"Replay on both versions →","replay","button-blue",!state.ready || !!state.busy)}</div>
    ${state.replay?`<div class="verdict-pair">${verdict("Earlier policy",state.replay.before)}${verdict("Current policy",state.replay.after)}</div>${details("Inspect both execution results",state.replay)}`:""}
    <div class="scope-box"><strong>Take the policy beyond the demo.</strong><p>Export its generated validator, or open the source in Studio to inspect diagnostics, the AST, SIR, automata and traces. Generated code is available after a successful compilation of the current source.</p></div>
    <div class="panel-actions">${button("Download Python ↓","export-py","button-outline",!ready)}${button("Download JavaScript ↓","export-js","button-outline",!ready)}${button("Download session evidence ↓","export-session","button-outline",!state.compilation && !state.comparison && !state.replay)}</div>`;
}

async function compilePolicy() {
  if (state.busy || !state.ready) return;
  const source = state.source;
  state.busy="compile"; state.error=null; state.step=2; render(); announce("Compilation started.");
  try {
    const result = await api("/api/compile",{source,name:"guided_policy.shl"});
    if (source === state.source) state.compilation=result;
    announce(result.ok?"Policy compiled. Verification coverage is ready.":"Compilation found errors.");
  } catch(error) { state.error=error.message; announce("Compilation failed: "+error.message); }
  finally {state.busy=null; render();}
}

async function comparePolicies() {
  if (state.busy || !state.ready) return;
  const before=state.before, after=state.source;
  state.busy="compare"; state.error=null; state.comparison=null; state.replay=null; state.selected=0; render(); announce("Comparing policy versions.");
  try {
    const result=await api("/api/compare",{before,after});
    if (state.before===before && state.source===after) state.comparison=result;
    announce(`${result.witnesses?.length || 0} request witnesses found. Whole-program equivalence was not checked.`);
  } catch(error) {state.error=error.message; announce("Comparison failed: "+error.message);}
  finally {state.busy=null;render();}
}

async function replayRequest() {
  if (state.busy || !state.ready) return;
  state.error=null;
  const requestText=$("#replay-request").value;
  let request;
  try {request=JSON.parse(requestText); if (!request || typeof request!=="object" || Array.isArray(request)) throw new Error("The request must be a JSON object.");}
  catch(error) {state.error="Invalid request JSON: "+error.message;state.requestText=requestText;render();return;}
  state.requestText=requestText;state.busy="replay";state.replay=null;render();announce("Replaying the request on both policies.");
  const before=state.before,after=state.source;
  try {
    const results=await Promise.all([api("/api/run",{source:before,name:"earlier_policy.shl",request}),api("/api/run",{source:after,name:"guided_policy.shl",request})]);
    if (before===state.before && after===state.source) state.replay={request,before:results[0],after:results[1]};
    announce(`Earlier policy: ${results[0].optimized.allowed?"allowed":"denied"}. Current policy: ${results[1].optimized.allowed?"allowed":"denied"}.`);
  } catch(error) {state.error=error.message;announce("Replay failed: "+error.message);}
  finally {state.busy=null;render();}
}

function download(name,content) {
  const url=URL.createObjectURL(new Blob([content],{type:"text/plain;charset=utf-8"}));
  const link=document.createElement("a");link.href=url;link.download=name;document.body.append(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}

function openStudio(event) {
  if (!state.ready) return;
  const handoff={source:state.source,name:"guided_policy.shl",compareBefore:state.before,
    tab:state.step===3?"verification":state.step===4?"compare":state.step===5?"run":"overview"};
  if (state.requestText) {
    try {handoff.request=JSON.parse(state.requestText);} catch (_) { /* Keep invalid draft out of Studio. */ }
  }
  try {sessionStorage.setItem("shieldlang.demo.handoff",JSON.stringify(handoff));}
  catch (_) {event.preventDefault();state.error="The browser could not transfer this policy to Studio. You can still copy the source and open Studio directly.";render();}
}

async function boot() {
  render();
  document.querySelectorAll("[data-step]").forEach(tab=>{
    tab.addEventListener("click",()=>setStep(Number(tab.dataset.step)));
    tab.addEventListener("keydown",event=>{
      let target;
      if(event.key==="ArrowRight")target=state.step===5?1:state.step+1;
      if(event.key==="ArrowLeft")target=state.step===1?5:state.step-1;
      if(event.key==="Home")target=1;
      if(event.key==="End")target=5;
      if(target){event.preventDefault();setStep(target,true);}
    });
  });
  $("#demo-panel").addEventListener("input",event=>{
    if(event.target.id==="demo-source"){state.source=event.target.value;invalidate();$("#edit-status").textContent="Edited · compile to refresh the evidence.";}
    if(event.target.id==="baseline-source"){state.before=event.target.value;state.comparison=null;state.replay=null;$("#comparison-result")?.remove();}
    if(event.target.id==="replay-request"){state.requestText=event.target.value;state.replay=null;document.querySelector(".verdict-pair")?.remove();document.querySelector(".demo-panel .evidence-details")?.remove();}
  });
  $("#demo-panel").addEventListener("click",event=>{
    const target=event.target.closest("button");if(!target || target.disabled)return;
    if(target.dataset.witness!==undefined){state.selected=Number(target.dataset.witness);render();return;}
    const action=target.dataset.action;
    if(action?.startsWith("step-")){setStep(Number(action.slice(5)));return;}
    if(action==="compile")compilePolicy();
    if(action==="compare")comparePolicies();
    if(action==="replay")replayRequest();
    if(action==="use-witness"){
      const witness=state.comparison?.witnesses[state.selected];
      if(witness){state.requestText=JSON.stringify(witness.request,null,2);state.replay=null;setStep(5);}
    }
    if(action==="export-verification" && state.compilation?.verification)download("field-verification.json",JSON.stringify(state.compilation.verification,null,2));
    if(action==="export-py" && state.compilation?.ok)download("account_validator.py",state.compilation.python);
    if(action==="export-js" && state.compilation?.ok)download("account_validator.js",state.compilation.js);
    if(action==="export-session")download("shieldlang-demo-evidence.json",JSON.stringify({source:state.source,before:state.before,verification:state.compilation?.verification,comparison:state.comparison,replay:state.replay},null,2));
  });
  $("#open-in-studio").addEventListener("click",openStudio);
  $("#reset-demo").addEventListener("click",()=>{
    if(state.busy || !state.originals)return;
    state.source=state.originals.after;state.before=state.originals.before;state.requestText="";state.selected=0;invalidate();setStep(1);announce("Example restored.");
  });
  try {
    const examples=await api("/api/examples");
    const before=examples.find(x=>x.name==="policy_before.shl"),after=examples.find(x=>x.name==="policy_after.shl");
    if(!before || !after)throw new Error("The comparison examples are missing from this server. Start the updated ShieldLang project.");
    state.source=after.source;state.before=before.source;state.originals={before:before.source,after:after.source};state.ready=true;
    $("#connection-status").innerHTML='<span class="signal-dot"></span> Local compiler connected · live API';
  } catch(error) {
    state.error="Could not load the compiler demo: "+error.message;
    $("#connection-status").classList.add("error");$("#connection-status").textContent="Local compiler unavailable";
  }
  render();
}

boot();
