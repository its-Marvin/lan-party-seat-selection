"use strict";

/* /admin/settings: app-wide settings that apply to every event. */

let general = null;

document.addEventListener("DOMContentLoaded", () => {
  document.title = t("admin.settingsPage.pageTitle");
  general = configForm({
    section: "general", form: $("general-form"), fieldset: $("general-fields"), message: $("general-msg"),
    onLoaded: (data) => {
      $("server-time").textContent = t("admin.settingsPage.serverTime", { time: data.server_time.replace("T", " ") });
      const url = $("general-form").elements.public_url;
      // first visit: suggest the address this page was opened with
      if (!url.value && data.suggested_public_url) url.placeholder = data.suggested_public_url;
    },
  });
  AdminShell.start({ onSession: () => general.load(), unsaved: () => general.dirty });
});
