// Resolves the theme before first paint so the page never flashes the wrong one. Loaded by index.html as a classic,
// synchronous script in <head> (a file, not inline, so a `script-src 'self'` CSP allows it). Same rule as
// src/lib/theme.ts: a stored "light" or "dark" wins; anything else follows the operating system.
(function () {
  var pref = "system";
  try {
    var stored = localStorage.getItem("softwarden.theme");
    if (stored === "light" || stored === "dark") pref = stored;
  } catch (e) { /* storage blocked: follow the system */ }
  var dark = pref === "dark" || (pref === "system" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme-pref", pref);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
})();
