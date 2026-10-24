/* ShieldLang app shell: renders the sidebar navigation into <aside id="app-sidebar"> and wires the
   mobile drawer and the collapse control. Dependency-free; loaded by every page. */
"use strict";
(function () {
  const page = document.body.getAttribute("data-page") || location.pathname;
  const ICONS = {
    home: '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    studio: '<path d="M8 6 3 12l5 6M16 6l5 6-5 6M14 4l-4 16"/>',
    protocol: '<circle cx="5" cy="12" r="2.2"/><circle cx="12" cy="12" r="2.2"/><circle cx="19" cy="12" r="2.2"/><path d="M7.2 12h2.6M14.2 12h2.6"/>',
    traffic: '<path d="M3 17h4l3-10 4 14 3-8h4"/>',
    bypass: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 5 5"/>',
  };
  const NAV = [
    ["Start", [["/", "home", "Home & guided tour", "Learn ShieldLang in 5 steps"]]],
    ["Build", [["/studio", "studio", "Policy Studio", "Write, check and compile rules"]]],
    ["Test", [
      ["/protocols", "protocol", "Protocol Studio", "Enforce the order of requests"],
      ["/traffic", "traffic", "Traffic Lab", "Replay recorded traffic"],
      ["/lab/bypasses", "bypass", "Bypass Lab", "Find encoding tricks"],
    ]],
  ];
  const svg = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;

  const aside = document.getElementById("app-sidebar");
  if (aside) {
    let html = '<nav class="side-nav" aria-label="ShieldLang sections">';
    for (const [group, items] of NAV) {
      html += `<div class="side-group">${group}</div>`;
      for (const [href, icon, title, desc] of items) {
        const cur = href === page ? ' aria-current="page"' : "";
        html += `<a class="side-link" href="${href}"${cur} title="${title} — ${desc}">${svg(icon)}<span class="t">${title}</span><span class="d">${desc}</span></a>`;
      }
    }
    html += "</nav>";
    html += '<div class="side-foot"><div class="side-status"><i></i><span>Local compiler · 127.0.0.1</span></div>' +
      '<button class="side-collapse" type="button" id="side-collapse" aria-label="Collapse sidebar">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 6-6 6 6 6"/></svg>' +
      "<span>Collapse sidebar</span></button></div>";
    aside.innerHTML = html;
  }

  // collapse (desktop) — a per-viewer convenience only
  const KEY = "shieldlang.nav";
  const read = () => { try { return localStorage.getItem(KEY); } catch (e) { return null; } };
  const write = (v) => { try { localStorage.setItem(KEY, v); } catch (e) { /* storage unavailable */ } };
  const apply = (v) => {
    document.body.classList.toggle("nav-rail", v === "rail");
    document.body.classList.toggle("nav-expanded", v === "expanded");
    const b = document.getElementById("side-collapse");
    if (b) b.setAttribute("aria-label", isRail() ? "Expand sidebar" : "Collapse sidebar");
    if (b) b.querySelector("span").textContent = isRail() ? "Expand sidebar" : "Collapse sidebar";
    window.dispatchEvent(new Event("resize"));
  };
  const isRail = () => document.body.classList.contains("nav-rail") ||
    (window.innerWidth < 1280 && !document.body.classList.contains("nav-expanded"));
  const DEFAULT = document.body.classList.contains("studio") ? "rail" : null;
  apply(read() || DEFAULT);
  const collapse = document.getElementById("side-collapse");
  if (collapse) collapse.addEventListener("click", () => { const v = isRail() ? "expanded" : "rail"; write(v); apply(v); });

  // mobile drawer
  const toggle = document.getElementById("nav-toggle");
  const scrim = document.createElement("div");
  scrim.className = "nav-scrim";
  document.body.appendChild(scrim);
  const setOpen = (open) => {
    document.body.classList.toggle("nav-open", open);
    if (toggle) toggle.setAttribute("aria-expanded", String(open));
  };
  if (toggle) toggle.addEventListener("click", () => setOpen(!document.body.classList.contains("nav-open")));
  scrim.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setOpen(false); });
})();
