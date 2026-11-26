"use strict";

/*
 * Colour mode (light/dark) and theme preview.
 *
 * Mode: "auto" (the default) follows the browser/system setting and updates
 * when it changes; "light" and "dark" override it for this browser. The
 * resolved mode is always set as data-mode="light|dark" on <html>; style.css
 * and every event theme key their colours on it. Loaded without `defer` in
 * <head> so the mode applies before the first paint (no flash).
 *
 * Theme preview: /?theme=<id> shows the public page with that theme instead of
 * the active event's one (for admins trying out a theme before choosing it).
 */
(function () {
  const KEY = "lanparty.theme";       // historic name: stores the mode choice
  const CHOICES = ["auto", "light", "dark"];
  const system = window.matchMedia("(prefers-color-scheme: light)");

  function stored() {
    try {
      const value = localStorage.getItem(KEY);
      return CHOICES.includes(value) ? value : "auto";
    } catch {
      return "auto";   // storage blocked (private mode, sandbox): just follow the system
    }
  }

  function apply(choice) {
    const mode = choice === "auto" ? (system.matches ? "light" : "dark") : choice;
    document.documentElement.setAttribute("data-mode", mode);
  }

  apply(stored());
  system.addEventListener("change", () => apply(stored()));

  const preview = new URLSearchParams(location.search).get("theme");
  const themeLink = document.getElementById("event-theme");
  if (themeLink && preview && /^[a-z0-9][a-z0-9_-]{0,39}$/.test(preview)) {
    themeLink.href = `/themes/${preview}/theme.css`;
  }

  document.addEventListener("DOMContentLoaded", () => {
    const select = document.getElementById("mode-select");
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
