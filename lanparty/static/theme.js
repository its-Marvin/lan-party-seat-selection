"use strict";

/*
 * Light/dark theme. "auto" (the default) follows the browser/system setting via
 * prefers-color-scheme; "light" and "dark" override it for this browser.
 * Loaded without `defer` in <head> so the stored choice applies before the
 * first paint (no flash of the wrong theme).
 */
(function () {
  const KEY = "lanparty.theme";
  const CHOICES = ["auto", "light", "dark"];

  function stored() {
    try {
      const value = localStorage.getItem(KEY);
      return CHOICES.includes(value) ? value : "auto";
    } catch {
      return "auto";   // storage blocked (private mode, sandbox): just follow the system
    }
  }

  function apply(choice) {
    if (choice === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", choice);
  }

  apply(stored());

  document.addEventListener("DOMContentLoaded", () => {
    const select = document.getElementById("theme-select");
    if (!select) return;
    select.value = stored();
    select.addEventListener("change", () => {
      const choice = CHOICES.includes(select.value) ? select.value : "auto";
      try {
        if (choice === "auto") localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, choice);
      } catch { /* not remembered, but still applied for this page */ }
      apply(choice);
    });
  });
})();
