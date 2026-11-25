"use strict";

const $ = (id) => document.getElementById(id);
let settingsCache = null;

async function checkSession() {
  const { admin, actor } = await api("GET", "/admin/session");
  $("login-card").hidden = admin;
  $("admin-area").hidden = !admin;
  $("session-controls").hidden = !admin;
  $("actor").textContent = actor ? `Logged in as ${actor}` : "";
  if (admin) {
    await loadSettings();
    await window.LayoutEditor.load();
    await loadUsers();
    await loadAudit();
  }
}

async function loadUsers() {
  const [{ users }, seatData] = await Promise.all([api("GET", "/admin/users"), api("GET", "/seats")]);
  window.LayoutEditor.setOccupancy(seatData.seats);
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

    tbody.append(el("tr", {},
      el("td", {}, u.name),
      el("td", {}, el("a", { href: `mailto:${u.email}` }, u.email)),
      el("td", {}, u.seat || "–"),
      el("td", {}, status),
      el("td", { class: "small" }, u.created_at + " UTC"),
      el("td", { class: "actions-cell" }, payBtn, el("span", { class: "move" }, select, moveBtn), linkBtn, removeBtn),
    ));
  }

  const seated = users.filter((u) => u.seat);
  const paid = users.filter((u) => u.paid);
  const amount = settingsCache ? settingsCache.amount_cents : 0;
  $("stats").textContent =
    `${users.length} registered · ${seated.length}/${seatData.seats.length} seats taken · ` +
    `${paid.length} paid` + (settingsCache ? ` · ${formatMoney(paid.length * amount)} received, ` +
    `${formatMoney(seated.filter((u) => !u.paid).length * amount)} outstanding` : "");
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
}

async function loadSettings() {
  settingsCache = await api("GET", "/admin/settings");
  const f = $("settings-form");
  for (const key of ["event_name", "event_start", "event_end", "location", "description", "paypal_address", "reservation_hours", "event_code"]) {
    f.elements[key].value = settingsCache[key];
  }
  f.elements.amount.value = (settingsCache.amount_cents / 100).toFixed(2);
  $("event-code-hint").classList.toggle("warning", !settingsCache.event_code);
  $("closed-banner").hidden = !!settingsCache.event_code;
}

async function onSaveSettings(event) {
  event.preventDefault();
  const f = event.target;
  const msg = $("settings-msg");
  const payload = Object.fromEntries(new FormData(f).entries());
  try {
    await api("PUT", "/admin/settings", payload);
    await loadSettings();
    await loadUsers();
    await loadAudit();
    showMessage(msg, "Settings saved.", "success");
  } catch (err) {
    showMessage(msg, err.message);
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

document.addEventListener("layout-saved", () => loadUsers().then(loadAudit).catch(() => {}));

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", onLogin);
  $("settings-form").addEventListener("submit", onSaveSettings);
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
  });
  $("refresh-audit").addEventListener("click", () => loadAudit().catch((err) => alert(err.message)));
  checkSession().catch((err) => showMessage($("login-msg"), err.message));
});
