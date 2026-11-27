"use strict";

/* /admin/mail: mail server settings, status, test mail and the queue of unsent mails. */

let mailForm = null;

const PROBLEM_KEYS = {
  smtp_host: "admin.mailPage.missingHost",
  mail_from: "admin.mailPage.missingFrom",
  public_url: "admin.mailPage.missingUrl",
};

function renderStatus(s) {
  const items = [];
  const item = (level, text) => items.push(el("li", { class: `check ${level}` }, text));
  if (!s.switched_on) item("info", t("admin.mailPage.off"));
  else if (s.problems.length) item("warn", t("admin.mailPage.incomplete"));
  else item("ok", t("admin.mailPage.on"));
  for (const p of s.problems) if (s.switched_on || p !== "public_url") item("warn", t(PROBLEM_KEYS[p]));
  if (s.dev_backend) item("info", t("admin.mailPage.devBackend", { backend: s.dev_backend }));
  item(s.failed ? "warn" : "ok", t("admin.mailPage.counts", { sent: s.sent_day, queued: s.queued, failed: s.failed }));
  const last = s.scheduler_last_run;
  const running = last && Date.now() - Date.parse(last.replace(" ", "T") + "Z") < 15 * 60000;
  item(running ? "ok" : (s.switched_on ? "warn" : "info"), running
    ? t("admin.mailPage.schedulerOk", { date: formatDateTime(last.replace(" ", "T") + "Z") })
    : t("admin.mailPage.schedulerMissing"));
  $("mail-status").replaceChildren(...items);
  $("test-form").querySelector("button").disabled = !s.enabled;
}

async function loadQueue() {
  const { status, pending } = await api("GET", "/admin/mail/queue");
  renderStatus(status);
  const rows = pending.map((m) => el("tr", {},
    el("td", { class: "nowrap small" }, m.created_at + " UTC"),
    el("td", {}, m.recipient),
    el("td", {}, m.subject),
    el("td", { class: m.given_up ? "warning" : "" }, m.given_up
      ? t("admin.mailPage.givenUp", { error: m.last_error || "?" })
      : m.last_error ? t("admin.mailPage.retrying", { count: m.attempts, error: m.last_error }) : t("admin.mailPage.waiting")),
  ));
  $("queue").replaceChildren(...(rows.length ? rows
    : [el("tr", {}, el("td", { colspan: 4, class: "muted" }, t("admin.mailPage.empty")))]));
  labelRows($("queue"));
  $("retry-queue").disabled = !pending.some((m) => m.given_up);
}

/** Phone layout: each cell shows its column name (table.stack). */
function labelRows(tbody) {
  const heads = Array.from(tbody.closest("table").querySelectorAll("thead th"), (th) => th.textContent);
  for (const row of tbody.rows) {
    Array.from(row.cells).forEach((cell, i) => { if (cell.colSpan === 1) cell.dataset.label = heads[i] ?? ""; });
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.title = t("admin.mailPage.pageTitle");
  mailForm = configForm({
    section: "mail", form: $("mail-form"), fieldset: $("mail-fields"), message: $("mail-msg"),
    onLoaded: (data) => renderStatus(data),
  });
  $("test-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const msg = $("test-msg");
    showMessage(msg, t("admin.mailPage.sending"), "info");
    try {
      await api("POST", "/admin/mail/test", { to: event.target.to.value });
      showMessage(msg, t("admin.mailPage.testSent", { to: event.target.to.value }), "success");
    } catch (err) {
      await AdminShell.handle(err, msg);
    }
  });
  $("refresh-queue").addEventListener("click", () => loadQueue().catch((err) => AdminShell.handle(err, $("queue-msg"))));
  $("retry-queue").addEventListener("click", async () => {
    try {
      const { retried } = await api("POST", "/admin/mail/retry");
      showMessage($("queue-msg"), t("admin.mailPage.retried", { count: retried }), "success");
      await loadQueue();
    } catch (err) {
      await AdminShell.handle(err, $("queue-msg"));
    }
  });
  AdminShell.start({
    onSession: async () => { await mailForm.load(); await loadQueue(); },
    unsaved: () => mailForm.dirty,
  });
});
