"use strict";

/*
 * Shared by the admin pages /admin, /admin/settings, /admin/mail and /admin/security
 * (the events page, admin-events.js, has its own session handling for its drafts).
 *
 * AdminShell.start({ onSession, unsaved }) shows the login card until there is a
 * session, then calls onSession(). Admin data is only shown with a session; a lost
 * session (idle timeout, logout elsewhere, password change) hides it again, while
 * edits in forms stay where they are and can be saved after signing in again.
 */

const $ = (id) => document.getElementById(id);

const SESSION_POLL_MS = 30000;

const AdminShell = {
  onSession: null,
  unsaved: () => false,

  async check() {
    const { admin, actor } = await api("GET", "/admin/session");
    $("login-card").hidden = admin;
    $("admin-area").hidden = !admin;
    $("session-controls").hidden = !admin;
    $("admin-nav").hidden = !admin;
    $("actor").textContent = actor ? t("admin.loggedInAs", { actor }) : "";
    if (!admin) {
      showMessage($("login-msg"), this.unsaved() ? t("admin.unsavedFormKept") : "", "info");
      return false;
    }
    showMessage($("login-msg"), "");
    if (this.onSession) await this.onSession();
    return true;
  },

  /** For catch blocks: a 401 means the session is gone. */
  async handle(err, target) {
    if (err.status === 401) {
      await this.check();
    } else if (target) {
      showMessage(target, err.message);
    } else {
      alert(err.message);
    }
  },

  start({ onSession, unsaved }) {
    this.onSession = onSession;
    if (unsaved) this.unsaved = unsaved;
    markAdminNav();
    $("login-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      showMessage($("login-msg"), "");
      try {
        await api("POST", "/admin/login", { password: event.target.password.value, name: event.target.name.value });
        event.target.password.value = "";
        await this.check();
      } catch (err) {
        showMessage($("login-msg"), err.message);
      }
    });
    $("logout").addEventListener("click", async () => {
      await api("POST", "/admin/logout");
      await this.check();
    });
    $("logout-all").addEventListener("click", async () => {
      if (!confirm(t("admin.confirmLogoutAll"))) return;
      await api("POST", "/admin/logout-all");
      await this.check();
    });
    window.addEventListener("beforeunload", (e) => {
      if (this.unsaved()) { e.preventDefault(); e.returnValue = ""; }
    });
    this.check().catch((err) => showMessage($("login-msg"), err.message));
    // An unattended page must not keep showing admin data after the session ended
    // (idle timeout, logout elsewhere, password change). The poll is marked as
    // background, so it doesn't keep the session alive; form edits stay as they are.
    setInterval(() => this.poll().catch(() => {}), SESSION_POLL_MS);
  },

  async poll() {
    if ($("admin-area").hidden) return;
    const { admin } = await api("GET", "/admin/session", undefined, { background: true });
    if (!admin) await this.check();
  },
};

/* ---- forms for app-wide settings (appsettings.py) --------------------------------
 * The server describes the fields; labels and help texts come from these keys. */

const CONFIG_TEXT = {
  public_url: ["admin.cfg.publicUrl", "admin.cfg.publicUrlHelp"],
  timezone: ["admin.cfg.timezone", "admin.cfg.timezoneHelp"],
  login_code_hours: ["admin.cfg.loginCodeHours", "admin.cfg.loginCodeHoursHelp"],
  mail_enabled: ["admin.cfg.mailEnabled", "admin.cfg.mailEnabledHelp"],
  smtp_host: ["admin.cfg.smtpHost", null],
  smtp_port: ["admin.cfg.smtpPort", "admin.cfg.smtpPortHelp"],
  smtp_security: ["admin.cfg.smtpSecurity", null],
  smtp_username: ["admin.cfg.smtpUsername", null],
  smtp_password: ["admin.cfg.smtpPassword", "admin.cfg.smtpPasswordHelp"],
  mail_from: ["admin.cfg.mailFrom", "admin.cfg.mailFromHelp"],
  email_confirm_minutes: ["admin.cfg.confirmMinutes", "admin.cfg.confirmMinutesHelp"],
  reminder_hours_before: ["admin.cfg.reminderHours", "admin.cfg.reminderHoursHelp"],
  admin_session_idle_minutes: ["admin.cfg.sessionIdle", null],
  admin_session_max_hours: ["admin.cfg.sessionMax", null],
  admin_login_max_per_ip: ["admin.cfg.loginMaxPerIp", null],
  admin_login_max_global: ["admin.cfg.loginMaxGlobal", "admin.cfg.loginMaxGlobalHelp"],
  admin_login_window_minutes: ["admin.cfg.loginWindow", null],
  register_max_per_ip: ["admin.cfg.registerMax", null],
  max_pending_per_ip: ["admin.cfg.pendingMax", null],
  login_code_max_per_ip: ["admin.cfg.loginCodeMax", null],
  event_code_max_per_ip: ["admin.cfg.eventCodeMax", null],
  audit_log_retention_days: ["admin.cfg.auditRetention", "admin.cfg.auditRetentionHelp"],
};
const CONFIG_CHOICES = {
  starttls: "admin.cfg.choice.starttls",
  ssl: "admin.cfg.choice.ssl",
  none: "admin.cfg.choice.none",
};

/** Build the inputs for ``fields`` into ``container`` (a fieldset). */
function renderConfigFields(container, fields) {
  container.querySelectorAll("label, p.help").forEach((node) => node.remove());
  for (const f of fields) {
    const [labelKey, helpKey] = CONFIG_TEXT[f.key] ?? [f.key, null];
    let input;
    if (f.type === "bool") {
      input = el("input", { type: "checkbox", name: f.key });
      input.checked = f.value;
    } else if (f.type === "choice") {
      input = el("select", { name: f.key }, f.choices.map((c) => el("option", { value: c }, t(CONFIG_CHOICES[c] ?? c))));
      input.value = f.value;
    } else if (f.type === "int") {
      input = el("input", { type: "number", name: f.key, min: f.min, max: f.max, required: true });
      input.value = f.value;
    } else if (f.type === "secret") {
      input = el("input", {
        type: "password", name: f.key, maxlength: f.max_length, autocomplete: "new-password",
        placeholder: t(f.set ? "admin.cfg.secretSet" : "admin.cfg.secretEmpty"),
      });
    } else {
      input = el("input", { name: f.key, maxlength: f.max_length, spellcheck: "false" });
      input.value = f.value;
    }
    const help = helpKey ? el("small", { class: "muted" }, t(helpKey)) : null;
    container.append(f.type === "bool"
      ? el("label", { class: "checkbox full" }, input, el("span", {}, t(labelKey)), help)
      : el("label", {}, el("span", {}, t(labelKey)), input, help));
    if (f.type === "secret" && f.set) {
      container.append(el("label", { class: "checkbox" },
        el("input", { type: "checkbox", name: `${f.key}_clear` }), el("span", {}, t("admin.cfg.secretClear"))));
    }
  }
}

/** The values of ``fields`` from ``form``, typed for the API (an empty secret = keep it). */
function collectConfig(form, fields) {
  const data = {};
  for (const f of fields) {
    const input = form.elements[f.key];
    if (f.type === "bool") data[f.key] = input.checked;
    else if (f.type === "int") data[f.key] = Number(input.value);
    else data[f.key] = input.value;
    const clear = form.elements[`${f.key}_clear`];
    if (clear && clear.checked) data[`${f.key}_clear`] = true;
  }
  return data;
}

/** A config section bound to a form: load, track edits, save. */
function configForm({ section, form, fieldset, message, onLoaded }) {
  const state = { fields: [], dirty: false };
  form.addEventListener("input", () => (state.dirty = true));
  state.load = async () => {
    const data = await api("GET", `/admin/config/${section}`);
    state.fields = data.fields;
    if (!state.dirty) renderConfigFields(fieldset, data.fields);
    if (onLoaded) onLoaded(data);
    return data;
  };
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    showMessage(message, "");
    const controls = Array.from(form.elements).filter((c) => !c.disabled);
    controls.forEach((c) => (c.disabled = true));
    try {
      const values = collectConfig(form, state.fields);
      controls.forEach((c) => (c.disabled = true));
      const data = await api("PUT", `/admin/config/${section}`, values);
      state.dirty = false;
      state.fields = data.fields;
      renderConfigFields(fieldset, data.fields);
      if (onLoaded) onLoaded(data);
      showMessage(message, t("admin.cfg.saved"), "success");
    } catch (err) {
      await AdminShell.handle(err, message);
    } finally {
      controls.forEach((c) => (c.disabled = false));
    }
  });
  return state;
}
