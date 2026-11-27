"use strict";

/* /admin/security: checks, admin password and sessions, participants' access,
 * limits and the audit log. */

let limitsForm = null;

async function loadChecks() {
  const { checks } = await api("GET", "/admin/security/checks");
  $("checks").replaceChildren(...checks.map((c) => el("li", { class: `check ${c.level}` }, c.text)));
}

async function loadSessions() {
  const { sessions, password } = await api("GET", "/admin/sessions");
  $("password-source").textContent = password.source === "admin"
    ? t("admin.securityPage.sourceAdmin", { date: password.changed_at ? formatDateTime(password.changed_at.replace(" ", "T") + "Z") : "?" })
    : t("admin.securityPage.sourceEnv");
  $("sessions").replaceChildren(...sessions.map((s) => el("tr", {},
    el("td", {}, s.current ? t("admin.securityPage.you", { actor: s.actor }) : s.actor),
    el("td", { class: "small" }, formatDateTime(s.created_at)),
    el("td", { class: "small" }, formatDateTime(s.last_seen)),
    el("td", { class: "actions-cell" }, s.current ? "" : el("button", {
      type: "button",
      onclick: async () => {
        if (!confirm(t("admin.securityPage.confirmEnd", { actor: s.actor }))) return;
        try {
          await api("DELETE", `/admin/sessions/${s.id}`);
          await loadSessions();
        } catch (err) {
          await AdminShell.handle(err, $("sessions-msg"));
        }
      },
    }, t("admin.securityPage.end"))),
  )));
  labelRows($("sessions"));
}

async function loadAccessEvents() {
  const [{ events, default_id: defaultId }, settings] = await Promise.all([
    api("GET", "/admin/events"), api("GET", "/admin/config/mail")]);
  const select = $("access-event");
  const current = select.value;
  select.replaceChildren(...events.map((e) => el("option", { value: e.id },
    t("admin.event.option", { name: e.event_name, status: t(`admin.status.${e.status}`) }))));
  select.value = current || defaultId;
  $("resend-row").hidden = !settings.enabled;
}

const ACCESS_CONFIRM = {
  login_links: "admin.securityPage.confirmLoginLinks",
  personal_links: "admin.securityPage.confirmPersonal",
  devices: "admin.securityPage.confirmDevices",
};
const ACCESS_DONE = {
  login_links: "admin.securityPage.doneLoginLinks",
  personal_links: "admin.securityPage.donePersonal",
  devices: "admin.securityPage.doneDevices",
};

async function resetAccess(action) {
  const msg = $("access-msg");
  showMessage(msg, "");
  const select = $("access-event");
  const name = select.options[select.selectedIndex]?.textContent ?? "";
  if (!confirm(t(ACCESS_CONFIRM[action], { event: name }))) return;
  try {
    const resend = action === "personal_links" && !$("resend-row").hidden && $("resend-links").checked;
    const { count, mailed } = await api("POST", `/admin/participant-access?event=${encodeURIComponent(select.value)}`,
      { action, resend });
    showMessage(msg, t(ACCESS_DONE[action], { count }) + (mailed ? " " + t("admin.securityPage.mailed") : ""), "success");
    await loadAudit();
  } catch (err) {
    await AdminShell.handle(err, msg);
  }
}

async function onChangePassword(event) {
  event.preventDefault();
  const f = event.target;
  const msg = $("password-msg");
  showMessage(msg, "");
  if (f.new.value !== f.repeat.value) {
    showMessage(msg, t("admin.securityPage.mismatch"));
    return;
  }
  try {
    await api("POST", "/admin/password", { current: f.current.value, new: f.new.value });
    f.reset();
    showMessage(msg, t("admin.securityPage.changed"), "success");
    await Promise.all([loadSessions(), loadChecks(), loadAudit()]);
  } catch (err) {
    await AdminShell.handle(err, msg);
  }
}

/** Phone layout: each cell shows its column name (table.stack). */
function labelRows(tbody) {
  const heads = Array.from(tbody.closest("table").querySelectorAll("thead th"), (th) => th.textContent);
  for (const row of tbody.rows) {
    Array.from(row.cells).forEach((cell, i) => { if (cell.colSpan === 1) cell.dataset.label = heads[i] ?? ""; });
  }
}
const labelCells = labelRows;

const SECURITY_ACTIONS = new Set(["admin.login_failed", "admin.logout_all", "user.login_link_created",
  "event_code.limit_reached", "admin.password_changed", "admin.password_change_failed", "admin.password_reset",
  "admin.session_ended", "participants.access_reset", "mail.test_sent"]);

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
      (e.action === "settings.changed" && ("paypal_address" in e.details || "amount_cents" in e.details)) ||
      (e.action === "config.changed" && e.details.section !== "general");
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

document.addEventListener("DOMContentLoaded", () => {
  document.title = t("admin.securityPage.pageTitle");
  limitsForm = configForm({ section: "security", form: $("limits-form"), fieldset: $("limits-fields"),
    message: $("limits-msg") });
  $("password-form").addEventListener("submit", onChangePassword);
  for (const button of document.querySelectorAll("[data-action]")) {
    button.addEventListener("click", () => resetAccess(button.dataset.action));
  }
  $("refresh-checks").addEventListener("click", () => loadChecks().catch((err) => AdminShell.handle(err)));
  $("refresh-audit").addEventListener("click", () => loadAudit().catch((err) => AdminShell.handle(err)));
  AdminShell.start({
    onSession: () => Promise.all([loadChecks(), loadSessions(), loadAccessEvents(), limitsForm.load(), loadAudit()]),
    unsaved: () => limitsForm.dirty,
  });
});
