"use strict";

/* /admin/security: checks, admin password and sessions, participants' access,
 * limits and the audit log. */

let limitsForm = null;

async function loadChecks() {
  const { checks } = await api("GET", "/admin/security/checks");
  $("checks").replaceChildren(...checks.map((c) => el("li", { class: `check ${c.level}` }, c.text)));
}

const utcDate = (text) => (text ? formatDateTime(text.replace(" ", "T") + "Z") : "–");

let startMode = false;

async function loadAdmins() {
  const data = await api("GET", "/admin/admins");
  startMode = data.start_mode;
  const mode = $("admins-mode");
  mode.textContent = startMode ? t("admin.securityPage.startMode") : t("admin.securityPage.accounts", { count: data.admins.length });
  mode.classList.toggle("warning", startMode);
  $("add-admin-legend").textContent = t(startMode ? "admin.securityPage.firstLegend" : "admin.securityPage.addLegend");
  $("add-admin-help").textContent = startMode ? t("admin.securityPage.firstHelp") : "";
  $("add-admin-help").hidden = !startMode;
  $("add-admin-button").textContent = t(startMode ? "admin.securityPage.createFirst" : "admin.securityPage.addAdmin");
  $("own-password-card").hidden = startMode;
  $("admins").replaceChildren(...(data.admins.length ? data.admins.map((a) => el("tr", {},
    el("td", {}, a.current ? t("admin.securityPage.you", { actor: a.name }) : a.name),
    el("td", { class: "small" }, utcDate(a.last_login_at)),
    el("td", { class: "small" }, t("admin.securityPage.createdOn", { date: utcDate(a.created_at), by: a.created_by || "–" })),
    el("td", { class: "actions-cell" }, a.current ? "" : el("button", {
      type: "button", class: "danger", onclick: () => removeAdmin(a),
    }, t("admin.securityPage.remove"))),
  )) : [el("tr", {}, el("td", { colspan: 4, class: "muted" }, t("admin.securityPage.noAccounts")))]));
  labelRows($("admins"));
}

async function onAddAdmin(event) {
  event.preventDefault();
  const f = event.target;
  const msg = $("admins-msg");
  showMessage(msg, "");
  if (f.password.value !== f.repeat.value) {
    showMessage(msg, t("admin.securityPage.mismatch"));
    return;
  }
  const current = $("admins-current").value;
  if (!current) {
    showMessage(msg, t("admin.securityPage.needCurrent"));
    $("admins-current").focus();
    return;
  }
  try {
    const result = await api("POST", "/admin/admins", { name: f.name.value, password: f.password.value, current });
    f.reset();
    $("admins-current").value = "";
    if (result.start_mode_ended) {
      // start mode is over: everyone (including this session) logs in with an account now
      await AdminShell.check();
      showMessage($("login-msg"), t("admin.securityPage.firstCreated", { name: result.name }), "success");
      $("login-form").name.value = result.name;
      $("login-form").password.focus();
      return;
    }
    showMessage(msg, t("admin.securityPage.added", { name: result.name }), "success");
    await Promise.all([loadAdmins(), loadChecks(), loadAudit()]);
  } catch (err) {
    await AdminShell.handle(err, msg);
  }
}

async function removeAdmin(admin) {
  const msg = $("admins-msg");
  showMessage(msg, "");
  const current = $("admins-current").value;
  if (!current) {
    showMessage(msg, t("admin.securityPage.needCurrent"));
    $("admins-current").focus();
    return;
  }
  if (!confirm(t("admin.securityPage.confirmRemove", { name: admin.name }))) return;
  try {
    await api("DELETE", `/admin/admins/${admin.id}`, { current });
    $("admins-current").value = "";
    showMessage(msg, t("admin.securityPage.removed", { name: admin.name }), "success");
    await Promise.all([loadAdmins(), loadSessions(), loadChecks(), loadAudit()]);
  } catch (err) {
    await AdminShell.handle(err, msg);
  }
}

async function loadSessions() {
  const { sessions } = await api("GET", "/admin/sessions");
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
    showMessage(msg, t("admin.securityPage.changedOwn"), "success");
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
  "admin.session_ended", "participants.access_reset", "mail.test_sent", "admin.account_created",
  "admin.account_removed", "admin.accounts_reset", "admin.reauth_failed"]);

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
  $("add-admin-form").addEventListener("submit", onAddAdmin);
  for (const button of document.querySelectorAll("[data-action]")) {
    button.addEventListener("click", () => resetAccess(button.dataset.action));
  }
  $("refresh-checks").addEventListener("click", () => loadChecks().catch((err) => AdminShell.handle(err)));
  $("refresh-audit").addEventListener("click", () => loadAudit().catch((err) => AdminShell.handle(err)));
  AdminShell.start({
    onSession: () => Promise.all([loadChecks(), loadAdmins(), loadSessions(), loadAccessEvents(), limitsForm.load(),
      loadAudit()]),
    unsaved: () => limitsForm.dirty,
  });
});
