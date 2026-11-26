"use strict";

/*
 * Translations for the browser pages (English and German).
 *
 *   t("key", {name: "Alice"})   → text from the catalog, {name} filled in
 *   t("key", {count: 3})        → picks "key.one" / "key.other" (Intl.PluralRules)
 *
 * Catalogs live in static/locales/<lang>.js and call I18N.add(). Static HTML is
 * translated through attributes, applied on DOMContentLoaded (before the page
 * scripts run their own DOMContentLoaded handlers):
 *
 *   data-i18n="key"                  → textContent
 *   data-i18n-html="key"             → innerHTML (catalog strings only, never user data)
 *   data-i18n-attr="title:key;aria-label:key2"
 *
 * The language comes from the visitor's choice (localStorage), else the
 * browser languages, else English. Loaded without `defer` in <head>.
 */
window.I18N = (() => {
  const KEY = "lanparty.lang";
  const LANGUAGES = ["en-US", "de-DE"];
  const FALLBACK = "en-US";
  const catalogs = {};

  function detect() {
    try {
      const stored = localStorage.getItem(KEY);
      if (LANGUAGES.includes(stored)) return stored;
    } catch { /* storage blocked */ }
    for (const wanted of navigator.languages || [navigator.language || ""]) {
      const base = wanted.toLowerCase().split("-")[0];
      const match = LANGUAGES.find((l) => l.toLowerCase().startsWith(base + "-"));
      if (match) return match;
    }
    return FALLBACK;
  }

  const lang = detect();
  document.documentElement.lang = lang;

  function add(language, messages) {
    catalogs[language] = Object.assign(catalogs[language] || {}, messages);
  }

  function lookup(key) {
    return catalogs[lang]?.[key] ?? catalogs[FALLBACK]?.[key];
  }

  function template(key, params) {
    let text;
    if (typeof params.count === "number") {
      const form = new Intl.PluralRules(lang).select(params.count);
      text = lookup(`${key}.${form}`) ?? lookup(`${key}.other`);
    } else {
      text = lookup(key);
    }
    if (text === undefined) {
      console.warn(`Missing translation: ${key}`);
      return key;
    }
    return text;
  }

  function t(key, params = {}) {
    return template(key, params).replace(/\{(\w+)\}/g, (match, name) => (name in params ? String(params[name]) : match));
  }

  /** Like t(), but returns a list of strings and the given DOM nodes, e.g. for
   *  "Reserved until {deadline}" with deadline = <strong>…</strong>. */
  function nodes(key, params = {}) {
    return template(key, params).split(/(\{\w+\})/).filter(Boolean).map((part) => {
      const name = /^\{(\w+)\}$/.exec(part)?.[1];
      if (!name || !(name in params)) return part;
      return params[name] instanceof Node ? params[name] : String(params[name]);
    });
  }

  function apply(root = document) {
    root.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
    root.querySelectorAll("[data-i18n-html]").forEach((node) => { node.innerHTML = t(node.dataset.i18nHtml); });
    root.querySelectorAll("[data-i18n-attr]").forEach((node) => {
      for (const pair of node.dataset.i18nAttr.split(";")) {
        const [attr, key] = pair.split(":").map((s) => s.trim());
        if (attr && key) node.setAttribute(attr, t(key));
      }
    });
  }

  function setLang(language) {
    if (!LANGUAGES.includes(language) || language === lang) return;
    try { localStorage.setItem(KEY, language); } catch { /* only for this page */ }
    // every panel is rebuilt in the new language; unsaved admin edits trigger the usual warning
    location.reload();
  }

  document.addEventListener("DOMContentLoaded", () => {
    apply();
    const select = document.getElementById("lang-select");
    if (select) {
      select.value = lang;
      select.addEventListener("change", () => {
        setLang(select.value);
        select.value = lang;   // stays unchanged if the reload is cancelled
      });
    }
  });

  return { add, t, nodes, apply, lang, LANGUAGES };
})();

const t = (key, params) => window.I18N.t(key, params);
const tNodes = (key, params) => window.I18N.nodes(key, params);
