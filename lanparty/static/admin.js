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

const STATUS_LABELS = { active: "active", upcoming: "upcoming", draft: "draft", archived: "archived" };

// Settings typed into the form but not saved yet. Kept (like an unsaved layout
// draft) across a sign-in after the session expired, instead of being reloaded.
let settingsDirty = false;

function unsavedWork() {
  return [window.LayoutEditor.isDirty() && "layout", settingsDirty && "settings"].filter(Boolean);
}

async function checkSession() {
  const { admin, actor } = await api("GET", "/admin/session");
  // Never show admin data (emails, event code, audit log) without a session;
  // unsaved work stays in memory and is shown again after signing in.
  $("login-card").hidden = admin;
  $("admin-area").hidden = !admin;
  $("session-controls").hidden = !admin;
  $("actor").textContent = actor ? `Logged in as ${actor}` : "";
  if (!admin) {
    const unsaved = unsavedWork();
    showMessage($("login-msg"), unsaved.length
      ? `Your unsaved ${unsaved.join(" and ")} changes are kept in this tab. Sign in again, then save them.`
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
  $("event-timezone").textContent = data.timezone ? `Dates and times are in ${data.timezone}.` : "";
  // rebuilding the options would close the dropdown if it is open: only on change
  const fingerprint = JSON.stringify([currentEvent, eventsCache.map((e) => [e.id, e.event_name, e.status, e.event_start])]);
  if (fingerprint !== eventsRendered) {
    eventsRendered = fingerprint;
    $("event-select").replaceChildren(...eventsCache.map((e) => el("option", {
    value: e.id, selected: e.id === currentEvent,
    }, `${e.event_name} – ${STATUS_LABELS[e.status]}${e.event_start ? ` (${formatDateTime(e.event_start)})` : ""}`)));
  }
  renderEventStatus();
  if (gone && goneEvent !== currentEvent) {
    // Deleted by someone else: switch like a manual pick (asks about unsaved
    // work). If the admin keeps the draft, saving it fails instead of silently
    // writing it to another event, because saves target the draft's own event.
    goneEvent = currentEvent;
    await switchEvent(data.default_id);
    showMessage($("event-msg"), "The event you were editing was deleted, probably by another admin.");
  }
}

function setSettingsLocked(locked) {
  for (const control of $("settings-form").elements) control.disabled = locked;
}

function renderEventStatus() {
  const e = currentEventData();
  if (!e) return;
  const badge = el("span", { class: `badge status-${e.status}` }, STATUS_LABELS[e.status]);
  const text = {
    active: `Registration is open since ${formatDateTime(e.registration_opens)}` +
      (e.event_end ? `; the event is archived automatically when it ends (${formatDateTime(e.event_end)}).` : "; no end is set, so it stays open."),
    upcoming: `Registration opens ${formatDateTime(e.registration_opens)}. Until then only admins see this event; it's listed publicly as upcoming.`,
    draft: "Not scheduled yet: set \"Registration opens\" in the settings below. Only admins see drafts.",
    archived: `The event ended ${formatDateTime(e.event_end)}. Participants and the seat plan are read-only; settings can still be corrected.`,
  }[e.status];
  $("event-status").replaceChildren(badge, " ", text, " ",
    el("span", { class: "muted" }, `${e.registered} registered · ${e.seated} seated · ${e.paid} paid`));
  $("delete-event").disabled = e.status === "active" || eventsCache.length <= 1;
  $("delete-event").title = e.status === "active" ? "The active event can't be deleted" : "";
  window.LayoutEditor.setReadOnly(e.status === "archived");
}

/** Show another event; `force` skips the unsaved-changes question (e.g. after deleting). */
async function switchEvent(eventId, force = false) {
  const unsaved = force ? [] : unsavedWork();
  if (window.LayoutEditor.isSaving() || settingsSaving) {
    $("event-select").value = currentEvent;
    showMessage($("event-msg"), "Please wait until saving has finished.", "info");
    return;
  }
  if (unsaved.length && !confirm(`Discard your unsaved ${unsaved.join(" and ")} changes?`)) {
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
    showMessage($("event-msg"), `${failed.reason.message} – pick the event again to retry.`);
    if (failed.reason.status === 401) await checkSession();
  }
}

async function createEvent() {
  const base = currentEventData();
  if (!confirm(`Create a new event based on "${base.event_name}"?\n\n` +
               "It copies the settings and the seat layout, but not the dates, the event code or any participants.")) return;
  try {
    const { event } = await api("POST", "/admin/events", { copy_from: base.id });
    await loadEvents();
    $("event-select").value = event.id;
    await switchEvent(event.id);
    showMessage($("event-msg"), "New event created. Set its dates and event code in the settings below.", "success");
    await loadAudit();
  } catch (err) {
    showMessage($("event-msg"), err.message);
    if (err.status === 401) await checkSession();
  }
}

async function deleteEvent() {
  const e = currentEventData();
  const people = e.registered ? ` and its ${e.registered} participant(s) with their seats and payments` : "";
  if (!confirm(`Delete "${e.event_name}"${people} permanently? This can't be undone.`)) return;
  try {
    await api("DELETE", `/admin/events/${e.id}`);
    settingsDirty = false;
    currentEvent = null;
    await loadEvents();
    await switchEvent(currentEvent, true);
    showMessage($("event-msg"), `"${e.event_name}" was deleted.`, "success");
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
    tbody.append(el("tr", {}, el("td", { colspan: 6, class: "muted" }, "Nobody registered yet.")));
  }
  for (const u of users) {
    const select = el("select", { "aria-label": `Move ${u.name}` },
      el("option", { value: "" }, "— no seat —"),
      ...[u.seat, ...freeSeats].filter(Boolean).sort(seatOrder)
        .map((id) => el("option", { value: id, selected: id === u.seat }, id)),
    );
    const moveBtn = el("button", {
      type: "button",
      onclick: () => act(() => api("POST", `/admin/users/${u.id}/move`, { seat: select.value || null })),
    }, "Move");
    const payBtn = el("button", {
      type: "button",
      class: u.paid ? "" : "primary",
      onclick: () => act(() => api("POST", `/admin/users/${u.id}/paid`, { paid: !u.paid })),
    }, u.paid ? "Mark unpaid" : "Confirm payment");
    const removeBtn = el("button", {
      type: "button",
      class: "danger",
      onclick: () => {
        if (confirm(`Remove ${u.name} (${u.email}) completely?`)) {
          act(() => api("DELETE", `/admin/users/${u.id}`));
        }
      },
    }, "Remove");
    const linkBtn = el("button", {
      type: "button",
      title: "Create a one-time login link for this person (e.g. if they lost access)",
      onclick: () => act(async () => {
        const data = await api("POST", `/admin/users/${u.id}/login-link`);
        prompt(`One-time login link for ${u.name} – send it only to ${u.email}.\n` +
               `Works once, valid until ${formatDateTime(data.expires_at)}.`,
               `${location.origin}/#login=${data.code}`);
      }),
    }, "Login link");

    const status = !u.seat
      ? el("span", { class: "badge" }, "no seat")
      : u.paid
        ? el("span", { class: "badge paid", title: u.paid_at ? `paid ${u.paid_at} UTC` : "" }, "paid")
        : el("span", {
            class: "badge reserved",
            title: u.reserved_until ? `released automatically at ${formatDateTime(u.reserved_until)}` : "",
          }, u.reserved_until ? `pending until ${formatDateTime(u.reserved_until)}` : "pending");

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
  $("stats").textContent =
    `${users.length} registered · ${seated.length}/${seatData.seats.length} seats taken · ` +
    `${paid.length} paid · ${formatMoney(received)} received` +
    (settingsCache ? `, ${formatMoney(seated.filter((u) => !u.paid).length * amount)} outstanding` : "");
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
  $("audit").replaceChildren(...(rows.length ? rows : [el("tr", {}, el("td", { colspan: 4, class: "muted" }, "No entries yet."))]));
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
  select.replaceChildren(...data.themes.map((t) => el("option", { value: t.id }, t.builtin ? t.name : `${t.name} (custom)`)));
  if (current) addMissingTheme(current);
  select.value = current;
  $("themes-dir").textContent = data.themes_dir;
  $("theme-problems").replaceChildren(...data.problems.map((p) =>
    el("li", {}, `Theme folder "${p.id}" can't be used: ${p.problem}`)));
}

/** An event's theme stays listed even if its folder is gone. */
function addMissingTheme(id) {
  const select = $("theme-choice");
  if (id && !Array.from(select.options).some((o) => o.value === id)) {
    select.append(el("option", { value: id }, `${id} (missing – the default theme is shown)`));
  }
}

function renderThemeChoice() {
  const id = $("theme-choice").value;
  $("theme-preview").href = `/?theme=${encodeURIComponent(id)}`;
  $("theme-description").textContent = themesCache.find((t) => t.id === id)?.description ?? "";
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
    showMessage(msg, "These settings belong to another event. Pick that event again or reload the page.");
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
    showMessage(msg, "Settings saved.", "success");
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
    if (!confirm("Log out ALL admin sessions on all devices (including this one)?")) return;
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
