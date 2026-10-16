/* Injects the shared lab nav bar. Kept tiny and dependency-free so / and /studio can include it too. */
"use strict";
(function () {
  const page = document.body.getAttribute("data-page") || "";
  const items = [["/", "Guided demo"], ["/studio", "Studio"], ["/protocols", "Protocol Studio"],
                 ["/traffic", "Traffic Lab"], ["/lab/bypasses", "Bypass Lab"]];
  const nav = document.createElement("nav");
  nav.className = "lab-nav";
  nav.setAttribute("aria-label", "ShieldLang sections");
  for (const [href, label] of items) {
    const a = document.createElement("a");
    a.href = href; a.textContent = label;
    if (href === page) a.setAttribute("aria-current", "page");
    nav.appendChild(a);
  }
  const mount = document.getElementById("shared-nav");
  if (mount) mount.replaceWith(nav);
})();
