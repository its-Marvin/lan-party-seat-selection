"use strict";

const $ = (id) => document.getElementById(id);
let settingsCache = null;

// The event shown and edited on this page (null until the event list is loaded).
let currentEvent = null;
let eventsCache = [];
let eventsRendered = null;
let settingsSaving = false;
let themesCache = [];
// Event whose values are in the settings form (the save target); null while
// another event's values are being loaded, and the form is locked meanwhile.
let settingsEventId = null;
// Event that was deleted elsewhere while selected here (asked about only once).
let goneEvent = null;

const forEvent = (path) => `${path}${path.includes("?") ? "&" : "?"}event=${encodeURIComponent(currentEvent)}`;
const currentEventData = () => eventsCache.find((e) => e.id === currentEvent) || null;
const isArchived = () => currentEventData()?.status === "archived";

const statusLabel = (status) => t(`admin.status.${status}`);

// Settings typed into the form but not saved yet. Kept (like an unsaved layout
// draft) across a sign-in after the session expired, instead of being reloaded.
let settingsDirty = false;

function unsavedWork() {
  return [window.LayoutEditor.isDirty() && "layout", settingsDirty && "settings"].filter(Boolean);
}

/** "layout changes", "settings changes" or both, for messages about unsaved work. */
function describeUnsaved(unsaved) {
  return t(unsaved.length === 2 ? "admin.unsaved.both" : `admin.unsaved.${unsaved[0]}`);
}

async function checkSession() {
  const { admin, actor } = await api("GET", "/admin/session");
  // Never show admin data (emails, event code, audit log) without a session;
  // unsaved work stays in memory and is shown again after signing in.
  $("login-card").hidden = admin;
  $("admin-area").hidden = !admin;
  $("session-controls").hidden = !admin;
  $("actor").textContent = actor ? t("admin.loggedInAs", { actor }) : "";
  if (!admin) {
    const unsaved = unsavedWork();
    showMessage($("login-msg"), unsaved.length
      ? t("admin.unsavedKept", { what: describeUnsaved(unsaved) })
      : "", "info");
    if (unsaved.length) $("login-form").password.focus();
    return;
  }
  showMessage($("login-msg"), "");
  await loadEvents();
  await loadSettings();
  if (!window.LayoutEditor.isDirty()) await window.LayoutEditor.load(currentEvent);
  await loadUsers();
  await loadAudit();
}

/** Load the event list; keeps the selected event if it still exists. */
async function loadEvents({ background = false } = {}) {
  const data = await api("GET", "/admin/events", undefined, { background });
  eventsCache = data.events;
  const gone = currentEvent !== null && !eventsCache.some((e) => e.id === currentEvent);
  if (currentEvent === null) currentEvent = data.default_id;
  $("event-timezone").textContent = data.timezone ? t("admin.event.timezone", { tz: data.timezone }) : "";
  // rebuilding the options would close the dropdown if it is open: only on change
  const fingerprint = JSON.stringify([currentEvent, eventsCache.map((e) => [e.id, e.event_name, e.status, e.event_start])]);
  if (fingerprint !== eventsRendered) {
    eventsRendered = fingerprint;
    $("event-select").replaceChildren(...eventsCache.map((e) => el("option", {
    value: e.id, selected: e.id === currentEvent,
    }, e.event_start
      ? t("admin.event.optionDate", { name: e.event_name, status: statusLabel(e.status), date: formatDateTime(e.event_start) })
      : t("admin.event.option", { name: e.event_name, status: statusLabel(e.status) }))));
  }
  renderEventStatus();
  if (gone && goneEvent !== currentEvent) {
    // Deleted by someone else: switch like a manual pick (asks about unsaved
    // work). If the admin keeps the draft, saving it fails instead of silently
    // writing it to another event, because saves target the draft's own event.
    goneEvent = currentEvent;
    await switchEvent(data.default_id);
    showMessage($("event-msg"), t("admin.event.deletedElsewhere"));
  }
}

function setSettingsLocked(locked) {
  for (const control of $("settings-form").elements) control.disabled = locked;
}

function renderEventStatus() {
  const e = currentEventData();
  if (!e) return;
  const badge = el("span", { class: `badge status-${e.status}` }, statusLabel(e.status));
  const dates = { opens: formatDateTime(e.registration_opens), end: formatDateTime(e.event_end) };
  const text = {
    active: t(e.event_end ? "admin.event.statusActiveEnd" : "admin.event.statusActiveNoEnd", dates),
    upcoming: t("admin.event.statusUpcoming", dates),
    draft: t("admin.event.statusDraft"),
    archived: t("admin.event.statusArchived", dates),
  }[e.status];
  $("event-status").replaceChildren(badge, " ", text, " ",
    el("span", { class: "muted" }, t("admin.event.counts", { registered: e.registered, seated: e.seated, paid: e.paid })));
  $("delete-event").disabled = e.status === "active" || eventsCache.length <= 1;
  $("delete-event").title = e.status === "active" ? t("admin.event.cantDeleteActive") : "";
  window.LayoutEditor.setReadOnly(e.status === "archived");
}

/** Show another event; `force` skips the unsaved-changes question (e.g. after deleting). */
async function switchEvent(eventId, force = false) {
  const unsaved = force ? [] : unsavedWork();
  if (window.LayoutEditor.isSaving() || settingsSaving) {
    $("event-select").value = currentEvent;
    showMessage($("event-msg"), t("admin.event.waitSaving"), "info");
    return;
  }
  if (unsaved.length && !confirm(t("admin.event.confirmDiscard", { what: describeUnsaved(unsaved) }))) {
    $("event-select").value = currentEvent;
    return;
  }
  currentEvent = eventId;
  settingsDirty = false;
  // until this event's values are in the form, nothing can be typed or saved
  settingsEventId = null;
  setSettingsLocked(true);
  showMessage($("event-msg"), "");
  for (const id of ["settings-msg", "layout-msg", "users-msg"]) showMessage($(id), "");
  renderEventStatus();
  // Load every panel even if one fails: a panel that couldn't load stays locked
  // (settings form, layout save) instead of keeping the previous event's data.
  const results = await Promise.allSettled([loadSettings(), window.LayoutEditor.load(currentEvent), loadUsers()]);
  const failed = results.find((r) => r.status === "rejected");
  if (failed) {
    showMessage($("event-msg"), t("admin.event.loadFailed", { error: failed.reason.message }));
    if (failed.reason.status === 401) await checkSession();
  }
}

async function createEvent() {
  const base = currentEventData();
  if (!confirm(t("admin.event.confirmNew", { name: base.event_name }))) return;
  try {
    const { event } = await api("POST", "/admin/events", { copy_from: base.id });
    await loadEvents();
    $("event-select").value = event.id;
    await switchEvent(event.id);
    showMessage($("event-msg"), t("admin.event.created"), "success");
    await loadAudit();
  } catch (err) {
    showMessage($("event-msg"), err.message);
    if (err.status === 401) await checkSession();
  }
}

async function deleteEvent() {
  const e = currentEventData();
  const question = e.registered
    ? t("admin.event.confirmDeleteWithPeople", { name: e.event_name, count: e.registered })
    : t("admin.event.confirmDelete", { name: e.event_name });
  if (!confirm(question)) return;
  try {
    await api("DELETE", `/admin/events/${e.id}`);
    settingsDirty = false;
    currentEvent = null;
    await loadEvents();
    await switchEvent(currentEvent, true);
    showMessage($("event-msg"), t("admin.event.deleted", { name: e.event_name }), "success");
    await loadAudit();
  } catch (err) {
    showMessage($("event-msg"), err.message);
    if (err.status === 401) await checkSession();
  }
}

/** Give every cell its column name, shown as a label when tables stack on phones. */
function labelCells(tbody) {
  const heads = Array.from(tbody.closest("table").querySelectorAll("thead th"), (th) => th.textContent);
  for (const row of tbody.rows) {
    Array.from(row.cells).forEach((cell, i) => { if (cell.colSpan === 1) cell.dataset.label = heads[i] ?? ""; });
  }
}

async function loadUsers() {
  const eventId = currentEvent;
  const [{ users }, seatData] = await Promise.all([api("GET", forEvent("/admin/users")), api("GET", forEvent("/admin/seats"))]);
  if (eventId !== currentEvent) return;   // another event was picked meanwhile
  const readOnly = isArchived();
  window.LayoutEditor.setOccupancy(seatData.seats, true, eventId);
  const freeSeats = seatData.seats.filter((s) => s.status === "free").map((s) => s.id);
  const tbody = $("users");
  tbody.replaceChildren();

  if (!users.length) {
    tbody.append(el("tr", {}, el("td", { colspan: 6, class: "muted" }, t("admin.users.none"))));
  }
  for (const u of users) {
    const select = el("select", { "aria-label": t("admin.users.moveLabel", { name: u.name }) },
      el("option", { value: "" }, t("admin.users.noSeat")),
      ...[u.seat, ...freeSeats].filter(Boolean).sort(seatOrder)
        .map((id) => el("option", { value: id, selected: id === u.seat }, id)),
    );
    const moveBtn = el("button", {
      type: "button",
      onclick: () => act(() => api("POST", `/admin/users/${u.id}/move`, { seat: select.value || null })),
    }, t("admin.users.move"));
    const payBtn = el("button", {
      type: "button",
      class: u.paid ? "" : "primary",
      onclick: () => act(() => api("POST", `/admin/users/${u.id}/paid`, { paid: !u.paid })),
    }, t(u.paid ? "admin.users.markUnpaid" : "admin.users.confirmPayment"));
    const removeBtn = el("button", {
      type: "button",
      class: "danger",
      onclick: () => {
        if (confirm(t("admin.users.confirmRemove", { name: u.name, email: u.email }))) {
          act(() => api("DELETE", `/admin/users/${u.id}`));
        }
      },
    }, t("admin.users.remove"));
    const linkBtn = el("button", {
      type: "button",
      title: t("admin.users.linkTitle"),
      onclick: () => act(async () => {
        const data = await api("POST", `/admin/users/${u.id}/login-link`);
        prompt(t("admin.users.linkPrompt", { name: u.name, email: u.email, date: formatDateTime(data.expires_at) }),
               `${location.origin}/#login=${data.code}`);
      }),
    }, t("admin.users.link"));

    const status = !u.seat
      ? el("span", { class: "badge" }, t("admin.users.badgeNoSeat"))
      : u.paid
        ? el("span", { class: "badge paid", title: u.paid_at ? t("admin.users.paidAt", { date: u.paid_at }) : "" },
          t("admin.users.badgePaid"))
        : el("span", {
            class: "badge reserved",
            title: u.reserved_until ? t("admin.users.releasedAt", { date: formatDateTime(u.reserved_until) }) : "",
          }, u.reserved_until
            ? t("admin.users.pendingUntil", { date: formatDateTime(u.reserved_until) })
            : t("admin.users.pending"));

    const actions = readOnly ? [removeBtn] : [payBtn, el("span", { class: "move" }, select, moveBtn), linkBtn, removeBtn];
    tbody.append(el("tr", {},
      el("td", {}, u.name),
      el("td", {}, el("a", { href: `mailto:${u.email}` }, u.email)),
      el("td", {}, u.seat || "–"),
      el("td", {}, status),
      el("td", { class: "small" }, u.created_at + " UTC"),
      el("td", { class: "actions-cell" }, ...actions),
    ));
  }

  labelCells(tbody);
  const seated = users.filter((u) => u.seat);
  const paid = users.filter((u) => u.paid);
  const amount = settingsCache ? settingsCache.amount_cents : 0;
  // received = amounts actually confirmed; outstanding = open reservations at the current fee
  const received = paid.reduce((sum, u) => sum + (u.paid_amount_cents ?? 0), 0);
  $("stats").textContent = t("admin.users.stats", {
    registered: users.length, seated: seated.length, seats: seatData.seats.length,
    paid: paid.length, received: formatMoney(received),
  }) + (settingsCache
    ? t("admin.users.outstanding", { amount: formatMoney(seated.filter((u) => !u.paid).length * amount) }) : "");
}

function seatOrder(a, b) {
  const [, la, na] = /^([A-Z]+)(\d+)$/.exec(a);
  const [, lb, nb] = /^([A-Z]+)(\d+)$/.exec(b);
  return la === lb ? Number(na) - Number(nb) : la.length - lb.length || la.localeCompare(lb);
}

async function act(fn) {
  showMessage($("users-msg"), "");
  try {
    await fn();
  } catch (err) {
    showMessage($("users-msg"), err.message);
    if (err.status === 401) return checkSession();
  }
  await loadEvents();
  await loadUsers();
  await loadAudit();
}

const SECURITY_ACTIONS = new Set(["admin.login_failed", "admin.logout_all", "user.login_link_created"]);

function describeDetails(details) {
  return Object.entries(details).map(([key, value]) => {
    if (Array.isArray(value) && value.length === 2) return `${key}: ${JSON.stringify(value[0])} → ${JSON.stringify(value[1])}`;
    if (value && typeof value === "object") return `${key}: ${JSON.stringify(value)}`;
    return `${key}: ${value}`;
  }).join(" · ");
}

async function loadAudit() {
  const { entries } = await api("GET", "/admin/audit");
  const rows = entries.map((e) => {
    const sensitive = SECURITY_ACTIONS.has(e.action) ||
      (e.action === "settings.changed" && ("paypal_address" in e.details || "amount_cents" in e.details));
    return el("tr", {},
      el("td", { class: "nowrap" }, e.ts + " UTC"),
      el("td", {}, e.actor),
      el("td", { class: sensitive ? "action-security" : "" }, e.action),
      el("td", { class: "details" }, describeDetails(e.details)),
    );
  });
  $("audit").replaceChildren(...(rows.length ? rows : [el("tr", {}, el("td", { colspan: 4, class: "muted" }, t("admin.audit.none")))]));
  labelCells($("audit"));
}

async function loadSettings() {
  const eventId = currentEvent;
  // Both answers are needed before anything is filled in, and both are checked
  // against the selected event and unsaved edits only once they have arrived.
  const [data, themes] = await Promise.all([api("GET", forEvent("/admin/settings")), api("GET", "/admin/themes")]);
  if (eventId !== currentEvent) return;   // another event was picked meanwhile
  settingsCache = data;
  renderSettingsWarnings();
  renderThemeList(themes);
  // keep edits that weren't saved yet, as long as they belong to this event
  if (settingsDirty && settingsEventId === eventId) return;
  const f = $("settings-form");
  addMissingTheme(settingsCache.theme);
  for (const key of ["theme", "event_name", "registration_opens", "event_start", "event_end", "location", "description", "paypal_address", "reservation_hours", "event_code"]) {
    f.elements[key].value = settingsCache[key];
  }
  f.elements.amount.value = (settingsCache.amount_cents / 100).toFixed(2);
  renderThemeChoice();
  settingsDirty = false;
  settingsEventId = eventId;
  if (!settingsSaving) setSettingsLocked(false);
}

function renderSettingsWarnings() {
  const needsCode = !settingsCache.event_code && ["active", "upcoming"].includes(currentEventData()?.status);
  $("event-code-hint").classList.toggle("warning", needsCode);
  $("closed-banner").hidden = !needsCode;
}

/** Rebuild the theme picker; keeps whatever is selected (it may be an unsaved choice). */
function renderThemeList(data) {
  themesCache = data.themes;
  const select = $("theme-choice");
  const current = select.value;
  select.replaceChildren(...data.themes.map((theme) => el("option", { value: theme.id },
    theme.builtin ? theme.name : t("admin.themes.custom", { name: theme.name }))));
  if (current) addMissingTheme(current);
  select.value = current;
  $("themes-dir").textContent = data.themes_dir;
  $("theme-problems").replaceChildren(...data.problems.map((p) =>
    el("li", {}, t("admin.themes.problem", { id: p.id, problem: p.problem }))));
}

/** An event's theme stays listed even if its folder is gone. */
function addMissingTheme(id) {
  const select = $("theme-choice");
  if (id && !Array.from(select.options).some((o) => o.value === id)) {
    select.append(el("option", { value: id }, t("admin.themes.missing", { id })));
  }
}

function renderThemeChoice() {
  const id = $("theme-choice").value;
  $("theme-preview").href = `/?theme=${encodeURIComponent(id)}`;
  $("theme-description").textContent = themesCache.find((theme) => theme.id === id)?.description ?? "";
}

async function onSaveSettings(event) {
  event.preventDefault();
  const f = event.target;
  const msg = $("settings-msg");
  showMessage(msg, "");
  const payload = Object.fromEntries(new FormData(f).entries());
  // save to the event the form values belong to, never to one picked meanwhile
  const target = settingsEventId;
  if (target === null || target !== currentEvent) {
    showMessage(msg, t("admin.settings.otherEvent"));
    return;
  }
  // lock the form until the save and reload are done, so no edit made in the
  // meantime is silently replaced by the reloaded values
  const controls = Array.from(f.elements).filter((c) => !c.disabled);
  controls.forEach((c) => (c.disabled = true));
  settingsSaving = true;
  try {
    await api("PUT", `/admin/settings?event=${encodeURIComponent(target)}`, payload);
    settingsDirty = false;
    await loadEvents();               // the status follows the dates
    await loadSettings();
    showMessage(msg, t("admin.settings.saved"), "success");
    await loadUsers();
    await loadAudit();
  } catch (err) {
    if (err.status === 401) {
      await checkSession();           // keeps the form values, offers sign-in
    } else {
      showMessage(msg, err.message);
    }
  } finally {
    settingsSaving = false;
    controls.forEach((c) => (c.disabled = false));
  }
}

async function onLogin(event) {
  event.preventDefault();
  const msg = $("login-msg");
  showMessage(msg, "");
  try {
    await api("POST", "/admin/login", { password: event.target.password.value, name: event.target.name.value });
    event.target.reset();
    await checkSession();
  } catch (err) {
    showMessage(msg, err.message);
  }
}

window.addEventListener("beforeunload", (e) => {
  if (settingsDirty) { e.preventDefault(); e.returnValue = ""; }
});
document.addEventListener("admin-auth-required", () => checkSession().catch(() => {}));
document.addEventListener("layout-saved", () => loadUsers().then(loadAudit).catch(() => {}));
// the status of an event changes with the clock (registration opens, event ends)
setInterval(() => {
  if ($("admin-area").hidden) return;
  loadEvents({ background: true }).catch((err) => {
    // session ended (idle timeout, logout elsewhere, password change): hide the
    // admin data; checkSession() keeps unsaved work and offers sign-in
    if (err.status === 401) checkSession().catch(() => {});
  });
}, 60000);

document.addEventListener("DOMContentLoaded", () => {
  document.title = t("admin.pageTitle");
  setSettingsLocked(true);   // unlocked once an event's values are in the form
  $("login-form").addEventListener("submit", onLogin);
  $("event-select").addEventListener("change", (e) => switchEvent(Number(e.target.value)));
  $("new-event").addEventListener("click", createEvent);
  $("delete-event").addEventListener("click", deleteEvent);
  $("settings-form").addEventListener("submit", onSaveSettings);
  $("settings-form").addEventListener("input", () => (settingsDirty = true));
  $("theme-choice").addEventListener("change", renderThemeChoice);
  $("logout").addEventListener("click", async () => {
    await api("POST", "/admin/logout");
    await checkSession();
  });
  $("logout-all").addEventListener("click", async () => {
    if (!confirm(t("admin.confirmLogoutAll"))) return;
    await api("POST", "/admin/logout-all");
    await checkSession();
  });
  $("generate-code").addEventListener("click", () => {
    // no 0/O/1/I to avoid typos when the code is read out loud
    const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    const bytes = crypto.getRandomValues(new Uint8Array(6));
    $("settings-form").elements.event_code.value = Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
    settingsDirty = true;
  });
  $("refresh-audit").addEventListener("click", () => loadAudit().catch((err) => alert(err.message)));
  checkSession().catch((err) => showMessage($("login-msg"), err.message));
});
