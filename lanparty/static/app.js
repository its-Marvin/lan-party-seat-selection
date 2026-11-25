"use strict";

const state = { info: null, me: null, payment: null, namesVisible: false, codeOk: false };
// fingerprints of what is currently on screen, so polling doesn't rebuild unchanged panels
const rendered = { userId: undefined, me: null, seats: null };
const $ = (id) => document.getElementById(id);

/**
 * One-time login links look like /#login=<code>. The part after "#" is never
 * sent to the server, so the code does not end up in access logs.
 */
async function handleLoginLink() {
  const match = /^#login=([A-Za-z0-9_-]+)$/.exec(location.hash);
  if (!match) return;
  history.replaceState(null, "", location.pathname + location.search);
  const code = match[1];
  try {
    const target = await api("POST", "/login/preview", { code });
    if (getToken()) {
      await loadMe();
      if (state.me && state.me.id === target.user_id) {
        showMessage($("me-msg"), "You are already logged in on this device – the login link was not used.", "info");
        return;
      }
      if (state.me && !confirm(
        `This browser is logged in as "${state.me.name}".\n\n` +
        `The login link belongs to "${target.name}". Switch to that account?`)) {
        return;
      }
    }
    const data = await api("POST", "/login", { code });
    setToken(data.token);
    state.me = null;
  } catch (err) {
    alert(err.message);
  }
}

function formatDeadline(iso) {
  return iso ? formatDateTime(iso) : "";
}

function renderInfo() {
  const info = state.info;
  document.title = `${info.event_name} – Seat Registration`;
  $("event-name").textContent = info.event_name;
  const facts = $("event-facts");
  facts.replaceChildren();
  if (info.event_start) {
    const when = formatDateTime(info.event_start) +
      (info.event_end ? " – " + formatDateTime(info.event_end) : "");
    facts.append(el("li", {}, el("strong", {}, "When: "), when));
  }
  if (info.location) facts.append(el("li", {}, el("strong", {}, "Where: "), info.location));
  facts.append(el("li", {}, el("strong", {}, "Fee: "), formatMoney(info.amount_cents)));
  facts.append(el("li", {}, el("strong", {}, "Seats: "), String(info.seat_count)));

  $("about").hidden = !info.description;
  $("event-description").textContent = info.description;
}

/** Forget a generated login link – it belongs to the account that created it. */
function resetLoginLinkBox() {
  $("login-link").value = "";
  $("login-link-expiry").textContent = "";
  $("login-link-result").hidden = true;
}

function renderMe() {
  const me = state.me;
  const userId = me ? me.id : null;
  if (userId !== rendered.userId) {
    resetLoginLinkBox();
    rendered.userId = userId;
  }
  $("me-card").hidden = !me;
  renderAccess();
  const fingerprint = JSON.stringify(me);
  if (fingerprint === rendered.me) return;
  rendered.me = fingerprint;
  if (!me) return;
  const restoreFocus = keepFocus($("me-details"));

  const details = $("me-details");
  details.replaceChildren(
    el("p", {}, el("strong", {}, me.name), " ", el("span", { class: "muted" }, `<${me.email}>`)),
  );
  if (!me.seat) {
    details.append(el("p", {}, "You have not chosen a seat yet. Click a free seat below."));
  } else {
    const status = me.paid
      ? el("span", { class: "badge paid" }, "paid ✓")
      : el("span", { class: "badge reserved" }, "payment pending");
    details.append(el("p", {}, `Seat `, el("strong", {}, me.seat), " ", status));
    if (!me.paid && me.reserved_until) {
      details.append(el("p", { class: "small" },
        "Reserved until ", el("strong", {}, formatDeadline(me.reserved_until)),
        " – if the payment has not been confirmed by then, the seat is released automatically."));
    }
    if (!me.paid) {
      details.append(el("p", {},
        el("button", { type: "button", dataset: { focusKey: "pay" }, onclick: () => openPayDialog() },
          "Show payment details"), " ",
        el("button", { type: "button", class: "danger", dataset: { focusKey: "release" }, onclick: releaseSeat },
          "Release seat"),
      ));
    }
  }
  restoreFocus();
}

/**
 * Call before replacing the contents of `container`; the returned function puts
 * keyboard focus back on the control with the same data-focus-key afterwards,
 * or on the container itself if that control no longer exists.
 */
function keepFocus(container) {
  const active = document.activeElement;
  if (!active || active === document.body || !container.contains(active)) return () => {};
  const key = active.dataset.focusKey;
  return () => {
    const target = key && container.querySelector(`[data-focus-key="${CSS.escape(key)}"]`);
    if (target && !target.disabled) {
      target.focus({ preventScroll: true });
    } else {
      container.tabIndex = -1;
      container.focus({ preventScroll: true });
    }
  };
}

/** Show the event-code card or the registration form, depending on what the visitor has unlocked. */
function renderAccess() {
  const open = !!state.info?.registration_open;
  const me = !!state.me;
  // Seeing names (admins do) is not the same as holding the registration code.
  $("closed-card").hidden = open || me;
  $("code-card").hidden = !open || me || state.codeOk;
  $("register-card").hidden = !open || me || !state.codeOk;
  $("names-hint").hidden = state.namesVisible;
  $("names-hint").textContent = open
    ? "🔒 Names are hidden. Enter the event code above to see who sits where."
    : "🔒 Names are hidden.";
}

async function loadSeats() {
  const data = await api("GET", "/seats");
  state.namesVisible = data.names_visible;
  state.codeOk = data.event_code_ok;
  if (!data.event_code_ok && getEventCode() && !state.me) {
    // the admin changed the code (or it was never valid)
    setEventCode(null);
    showMessage($("code-msg"), "The event code has changed. Please enter the new one.");
  }
  renderAccess();
  const canPickFree = !state.me || !state.me.paid;
  const fingerprint = JSON.stringify([data.layout, data.seats, canPickFree]);
  if (fingerprint === rendered.seats) return;
  rendered.seats = fingerprint;
  const restoreFocus = keepFocus($("seat-map"));
  const byId = new Map(data.seats.map((s) => [s.id, s]));
  const area = buildArea(data.layout, {
    renderSeat(pos) {
      const seat = byId.get(pos.id);
      const title = seat.status === "free"
        ? `${seat.id} – free`
        : `${seat.id} – ${seat.name ?? "taken"} (${seat.status === "paid" ? "paid" : "payment pending"})`;
      return el("button", {
        type: "button",
        class: "seat " + (seat.mine ? "mine" : seat.status),
        title,
        "aria-label": title,
        disabled: seat.status === "free" && !canPickFree,
        dataset: { focusKey: seat.id },
        onclick: () => onSeatClick(seat),
      }, seat.id);
    },
  });
  $("seat-map").replaceChildren(area);
  if (!data.layout.groups.length) {
    $("seat-map").replaceChildren(el("p", { class: "muted" }, "The seat plan has not been published yet."));
  }
  restoreFocus();
  const taken = data.seats.filter((s) => s.status !== "free").length;
  const paid = data.seats.filter((s) => s.status === "paid").length;
  $("seat-stats").textContent =
    `${taken} of ${data.seats.length} seats taken · ${paid} paid · ${data.seats.length - taken} free`;
}

function onSeatClick(seat) {
  if (seat.mine) return openPayDialog();
  if (seat.status === "free") return pickSeat(seat.id);
  showMessage($("seat-msg"), seat.name
    ? `Seat ${seat.id} is taken by ${seat.name}.`
    : `Seat ${seat.id} is taken. Enter the event code to see by whom.`, "info");
}

async function loadMe() {
  if (!getToken()) { state.me = null; return; }
  try {
    const data = await api("GET", "/me");
    state.me = data.user;
    state.payment = data.payment;
  } catch (err) {
    if (err.status === 401) { setToken(null); state.me = null; state.payment = null; }
    else throw err;
  }
}

async function pickSeat(seatId) {
  const msg = $("seat-msg");
  showMessage(msg, "");
  if (!state.me) {
    if (!state.info?.registration_open) {
      showMessage(msg, "Registration hasn't opened yet.");
      return;
    }
    const needsCode = !$("code-card").hidden;
    showMessage(msg, needsCode
      ? "Please enter the event code and register first (above)."
      : "Please register first (form above).");
    $(needsCode ? "code-card" : "register-card").scrollIntoView({ behavior: "smooth" });
    return;
  }
  const question = state.me.seat
    ? `Move from seat ${state.me.seat} to ${seatId}?`
    : `Reserve seat ${seatId}?`;
  if (!confirm(question)) return;
  try {
    const data = await api("POST", "/me/seat", { seat: seatId });
    state.me = data.user;
    state.payment = data.payment;
    renderMe();
    await loadSeats();
    openPayDialog();
  } catch (err) {
    showMessage(msg, err.message);
    await loadSeats();
  }
}

async function releaseSeat() {
  if (!confirm(`Release seat ${state.me.seat}?`)) return;
  try {
    const data = await api("DELETE", "/me/seat");
    state.me = data.user;
    state.payment = null;
    renderMe();
    await loadSeats();
  } catch (err) {
    showMessage($("me-msg"), err.message);
  }
}

function openPayDialog() {
  if (fillPayDialog()) $("pay-dialog").showModal();
}

/** Fill the payment dialog from the current state; returns false if there is nothing to pay. */
function fillPayDialog() {
  const p = state.payment;
  if (!p || !state.me || !state.me.seat || state.me.paid) return false;
  $("pay-seat").textContent = state.me.seat;
  $("pay-amount").textContent = formatMoney(p.amount_cents);
  $("pay-address").textContent = p.paypal_address || "(not configured yet – please check back later)";
  $("pay-ref").textContent = p.reference;
  $("pay-deadline").textContent = p.reserved_until
    ? `Please pay by ${formatDeadline(p.reserved_until)} – unpaid reservations are released automatically after that.`
    : "";
  return true;
}

/** Keep an open payment dialog in sync (seat moved, expired or paid in the meantime). */
function syncPayDialog() {
  const dialog = $("pay-dialog");
  if (dialog.open && !fillPayDialog()) dialog.close();
}

/** Re-read event info, the participant and the seat plan, and update every panel. */
async function refresh() {
  state.info = await api("GET", "/info");
  renderInfo();
  await loadMe();
  await loadSeats();
  renderMe();
  syncPayDialog();
}

async function createLoginLink() {
  const msg = $("me-msg");
  showMessage(msg, "");
  try {
    const data = await api("POST", "/me/login-link");
    $("login-link").value = `${location.origin}/#login=${data.code}`;
    $("login-link-expiry").textContent =
      `Works once, valid until ${formatDeadline(data.expires_at)}. Don't share it – whoever opens it is logged in as you.`;
    $("login-link-result").hidden = false;
    $("login-link").select();
  } catch (err) {
    showMessage(msg, err.message);
  }
}

async function copy(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    const old = button.textContent;
    button.textContent = "Copied!";
    setTimeout(() => (button.textContent = old), 1500);
  } catch { prompt("Copy this:", text); }
}

async function onRegister(event) {
  event.preventDefault();
  const form = event.target;
  const msg = $("register-msg");
  showMessage(msg, "");
  try {
    const data = await api("POST", "/register", {
      name: form.name.value,
      email: form.email.value,
      event_code: getEventCode() || "",
    });
    setToken(data.token);
    state.me = data.user;
    state.payment = null;
    renderMe();
    await loadSeats();
    showMessage($("me-msg"), "Registered! Now pick a free seat.", "success");
  } catch (err) {
    showMessage(msg, err.message);
  }
}

async function onEventCode(event) {
  event.preventDefault();
  const form = event.target;
  const msg = $("code-msg");
  showMessage(msg, "");
  const code = form.code.value.trim().toUpperCase();
  try {
    await api("POST", "/event-code", { code });
    setEventCode(code);
    form.reset();
    await loadSeats();
    showMessage($("register-msg"), "Unlocked! You can now see who sits where, and register here.", "success");
    $("register-form").name.focus();
  } catch (err) {
    showMessage(msg, err.message);
  }
}

async function init() {
  await handleLoginLink();
  $("register-form").addEventListener("submit", onRegister);
  $("code-form").addEventListener("submit", onEventCode);
  $("create-login-link").addEventListener("click", createLoginLink);
  $("copy-login-link").addEventListener("click", (e) => copy($("login-link").value, e.target));
  $("copy-address").addEventListener("click", (e) => copy(state.payment?.paypal_address || "", e.target));
  try {
    await refresh();
  } catch (err) {
    showMessage($("seat-msg"), err.message);
  }
  // expiry, admin moves, payment confirmation and opened registration show up without a reload
  setInterval(() => refresh().catch(() => {}), 15000);
  // a login link opened in a tab that already shows the page only changes the hash
  window.addEventListener("hashchange", async () => {
    if (!location.hash.startsWith("#login=")) return;
    await handleLoginLink();
    await refresh();
  });
}

document.addEventListener("DOMContentLoaded", init);
