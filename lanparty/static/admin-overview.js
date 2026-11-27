"use strict";

/* /admin: login, then a summary of each admin area. */

async function loadOverview() {
  const data = await api("GET", "/admin/overview");
  const e = data.event;
  $("ov-event").textContent = e
    ? t("admin.overview.event", { name: e.name, status: t(`admin.status.${e.status}`), registered: e.registered,
        seated: e.seated, paid: e.paid })
    : t("admin.overview.noEvent", { count: data.events });
  $("ov-settings").textContent = data.settings.public_url
    ? t("admin.overview.settings", { url: data.settings.public_url, zone: data.settings.timezone || t("admin.overview.serverTime") })
    : t("admin.overview.settingsMissing");
  $("ov-settings").classList.toggle("warning", !data.settings.public_url && data.mail.switched_on);
  const m = data.mail;
  $("ov-mail").textContent = !m.switched_on ? t("admin.overview.mailOff")
    : m.problems.length ? t("admin.overview.mailIncomplete")
    : t("admin.overview.mailOn", { sent: m.sent_day, queued: m.queued, failed: m.failed });
  $("ov-mail").classList.toggle("warning", m.switched_on && (m.problems.length > 0 || m.failed > 0));
  $("ov-security").textContent = data.warnings
    ? t("admin.overview.warnings", { count: data.warnings }) : t("admin.overview.noWarnings");
  $("ov-security").classList.toggle("warning", data.warnings > 0);
}

document.addEventListener("DOMContentLoaded", () => {
  document.title = t("admin.pageTitle");
  AdminShell.start({ onSession: loadOverview });
});
