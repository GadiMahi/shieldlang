/* Small shared helpers for Protocol Studio / Traffic Lab / Bypass Lab pages. No framework; no state kept only in
   sessionStorage/localStorage (server-side ids are the source of truth; see each page's own comments). */
"use strict";

async function labFetch(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) {
    const msg = (data && (data.message || data.error)) || `${res.status} ${res.statusText}`;
    const err = new Error(msg);
    err.status = res.status; err.body = data;
    throw err;
  }
  return data;
}

function el(tag, attrs, children) {
  const n = document.createElement(tag);
  for (const k in (attrs || {})) {
    if (k === "text") n.textContent = attrs[k];
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), attrs[k]);
    else n.setAttribute(k, attrs[k]);
  }
  for (const c of (children || [])) n.append(c instanceof Node ? c : document.createTextNode(c));
  return n;
}

function badge(text, kind) { return el("span", { class: `badge badge-${kind}` }, [text]); }

function decisionBadge(decision) {
  const map = { allow: "allow", deny: "deny", unknown: "unknown", forwarded: "observe", upstream_error: "unknown",
                not_delivered: "unknown", reject: "deny" };
  return badge(decision || "?", map[decision] || "muted");
}

function qs(params) {
  return Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
}

function queryParams() {
  return Object.fromEntries(new URLSearchParams(location.search).entries());
}

function pre(obj) {
  return el("pre", { class: "lab-pre" }, [typeof obj === "string" ? obj : JSON.stringify(obj, null, 1)]);
}

function banner(text, kind) { return el("div", { class: `lab-banner ${kind}`, role: "status" }, [text]); }

function fmtTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString();
}
