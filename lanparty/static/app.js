"use strict";

const state = { info: null, me: null, payment: null, namesVisible: false, codeOk: false };
// fingerprints of what is currently on screen, so polling doesn't rebuild unchanged panels
const rendered = { userId: undefined, me: null, seats: null };
const $ = (id) => document.getElementById(id);

/**
 * Links from mails and admins carry a code in the URL fragment, which is never
 * sent to the server, so codes don't end up in access logs:
 *   #login=<code>    one-time login link
 *   #access=<code>   personal link from the payment confirmation (reusable)
 *   #confirm=<code>  email confirmation after registering
 */
const LINK_RE = /^#(login|access|confirm)=([A-Za-z0-9_-]+)$/;

async function handleLoginLink() {
  const match = LINK_RE.exec(location.hash);
  if (!match) return;
  history.replaceState(null, "", location.pathname + location.search);
  const [, kind, code] = match;
  if (kind === "confirm") return confirmEmail(code);
  const base = kind === "login" ? "/login" : "/access";
  try {
    const target = await api("POST", `${base}/preview`, { code });
    if (getToken()) {
      await loadMe();
      if (state.me && state.me.id === target.user_id) {
        showMessage($("me-msg"), t("pub.loginLink.alreadyIn"), "info");
        return;
      }
      if (state.me && !confirm(t("pub.loginLink.switch", { current: state.me.name, target: target.name }))) {
        return;
      }
    }
    const data = await api("POST", base, { code });
    setToken(data.token);
    state.me = null;
  } catch (err) {
    alert(err.message);
  }
}

/** Confirm the email address; logs this browser in unless it belongs to someone else. */
async function confirmEmail(code) {
  try {
    if (getToken()) await loadMe();
    const current = state.me;
    const data = await api("POST", "/confirm", { code, login: !current });
    if (data.token) setToken(data.token);
    state.me = null;
    if (current && current.id !== data.user.id) {
      showMessage($("seat-msg"), t("pub.confirm.doneOther", { name: data.user.name, current: current.name }), "success");
    } else {
      showMessage($("me-msg"), t(data.user.seat ? "pub.confirm.doneSeat" : "pub.confirm.doneNoSeat"), "success");
    }
  } catch (err) {
    alert(err.message);
  }
}

/** Send the confirmation mail again (the deadline stays the same). */
async function resendConfirmation() {
  const msg = $("me-msg");
  showMessage(msg, "");
  try {
    await api("POST", "/me/confirmation");
    showMessage(msg, t("pub.confirm.resent", { email: state.me.email }), "success");
  } catch (err) {
    showMessage(msg, err.message);
  }
}

function formatDeadline(iso) {
  return iso ? formatDateTime(iso) : "";
}

function formatRange(start, end) {
  return formatDateTime(start) + (end ? " – " + formatDateTime(end) : "");
}

// Open pages follow theme changes (another event opens, an admin picks another
// theme). Not during a ?theme= preview.
let appliedTheme = null;
const themePreview = new URLSearchParams(location.search).has("theme");

/** The theme /theme.css gave this page (it sets --lp-theme), or null if unknown. */
function loadedTheme() {
  const value = getComputedStyle(document.documentElement).getPropertyValue("--lp-theme").trim();
  return value.replace(/^["']|["']$/g, "") || null;
}

function syncTheme(theme) {
  if (themePreview) return;
  // compare with what the page really got, not with the first /api/info answer:
  // the theme may have changed between loading the stylesheet and that answer
  if (appliedTheme === null) appliedTheme = loadedTheme();
  if (theme === appliedTheme) return;
  $("event-theme").href = `/themes/${encodeURIComponent(theme)}/theme.css`;
  appliedTheme = theme;
}

function renderInfo() {
  const info = state.info;
  syncTheme(info.theme);
  const next = info.upcoming[0];
  const title = info.active ? info.event_name : "LAN Party";
  document.title = t("pub.pageTitle", { name: title });
  $("event-name").textContent = title;
  const facts = $("event-facts");
  facts.replaceChildren();
  if (info.active) {
    if (info.event_start) facts.append(el("li", {}, el("strong", {}, t("pub.facts.when")), formatRange(info.event_start, info.event_end)));
    if (info.location) facts.append(el("li", {}, el("strong", {}, t("pub.facts.where")), info.location));
    facts.append(el("li", {}, el("strong", {}, t("pub.facts.fee")), formatMoney(info.amount_cents)));
    facts.append(el("li", {}, el("strong", {}, t("pub.facts.seats")), String(info.seat_count)));
  }

  $("no-event-card").hidden = info.active;
  $("seat-card").hidden = !info.active;
  $("no-event-text").textContent = next
    ? t("pub.noEvent.next", { name: next.event_name, date: formatDateTime(next.registration_opens) })
    : t("pub.noEvent.text");
  renderUpcoming(info.upcoming);

  const hours = info.login_link_hours || 168;
  const validity = hours % 24 === 0
    ? t("pub.loginLink.days", { count: hours / 24 }) : t("pub.loginLink.hours", { count: hours });
  $("login-link-intro").textContent = t("pub.loginLink.intro", { validity });

  $("about").hidden = !info.active || !info.description;
  $("event-description").textContent = info.description;
}

function renderUpcoming(upcoming) {
  $("upcoming-card").hidden = !upcoming.length;
  $("upcoming-list").replaceChildren(...upcoming.map((e) => el("li", {},
    el("strong", {}, e.event_name),
    e.event_start ? el("span", {}, formatRange(e.event_start, e.event_end)) : null,
    e.location ? el("span", { class: "muted" }, e.location) : null,
    el("span", { class: "small muted" }, t("pub.upcoming.opens", { date: formatDateTime(e.registration_opens) })),
  )));
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
  if (me.confirm_until) {
    // registered with mail: the address must be confirmed before the deadline
    details.append(el("div", { class: "notice" },
      el("p", {}, tNodes("pub.me.confirmPending", {
        email: el("strong", {}, me.email), deadline: el("strong", {}, formatDeadline(me.confirm_until)),
      })),
      el("p", {}, el("button", { type: "button", dataset: { focusKey: "resend" }, onclick: resendConfirmation },
        t("pub.me.resend")))));
  }
  if (!me.seat) {
    details.append(el("p", {}, t("pub.me.noSeat")));
  } else {
    const status = me.paid
      ? el("span", { class: "badge paid" }, t("pub.me.paid"))
      : el("span", { class: "badge reserved" }, t(me.confirm_until ? "pub.me.held" : "pub.me.pending"));
    details.append(el("p", {}, tNodes("pub.me.seat", { seat: el("strong", {}, me.seat), status })));
    if (!me.paid && me.reserved_until) {
      details.append(el("p", { class: "small" },
        tNodes("pub.me.reservedUntil", { deadline: el("strong", {}, formatDeadline(me.reserved_until)) })));
    }
    if (me.paid) {
      details.append(el("p", { class: "small muted" },
        t("pub.me.switchPaid")));
    } else if (me.confirm_until) {
      details.append(el("p", {},
        el("button", { type: "button", class: "danger", dataset: { focusKey: "release" }, onclick: releaseSeat },
          t("pub.me.release"))));
    } else {
      details.append(el("p", {},
        el("button", { type: "button", dataset: { focusKey: "pay" }, onclick: () => openPayDialog() },
          t("pub.me.showPayment")), " ",
        el("button", { type: "button", class: "danger", dataset: { focusKey: "release" }, onclick: releaseSeat },
          t("pub.me.release")),
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
  const active = !!state.info?.active;
  const me = !!state.me;
  // Seeing names (admins do) is not the same as holding the registration code.
  $("closed-card").hidden = open || me || !active;
  $("code-card").hidden = !open || me || state.codeOk;
  $("register-card").hidden = !open || me || !state.codeOk;
  $("names-hint").hidden = state.namesVisible;
  $("names-hint").textContent = t(open ? "pub.seats.namesHiddenCode" : "pub.seats.namesHidden");
}

async function loadSeats() {
  const data = await api("GET", "/seats");
  state.namesVisible = data.names_visible;
  state.codeOk = data.event_code_ok;
  if (!data.event_code_ok && getEventCode() && !state.me) {
    // the admin changed the code (or it was never valid)
    setEventCode(null);
    showMessage($("code-msg"), t("pub.code.changed"));
  }
  renderAccess();
  const fingerprint = JSON.stringify([data.layout, data.seats]);
  if (fingerprint === rendered.seats) return;
  rendered.seats = fingerprint;
  state.seatData = data.layout ? data : null;   // no layout: no event is active
  if (data.layout) renderSeatMap(data);
}

const MIN_SEAT_PX = 26;   // smallest seat size we shrink to on narrow screens

/**
 * How to show the area in the available width: the whole area if it fits;
 * otherwise (phones) only the part with tables and area elements, with seats
 * shrunk down to MIN_SEAT_PX. Anything still too wide scrolls sideways.
 */
function fitSeatMap(layout) {
  const map = $("seat-map");
  map.style.removeProperty("--u");
  const style = getComputedStyle(map);
  const base = parseFloat(style.getPropertyValue("--u")) || 36;
  const available = map.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  if (layout.area.width * base <= available || available <= 0) return { view: null, u: base, overflow: false };
  const box = contentBounds(layout);
  if (!box) return { view: null, u: base, overflow: true };
  const u = Math.max(MIN_SEAT_PX, Math.min(base, Math.floor(available / box.w)));
  return { view: box, u, overflow: box.w * u > available };
}

function renderSeatMap(data) {
  const restoreFocus = keepFocus($("seat-map"));
  const byId = new Map(data.seats.map((s) => [s.id, s]));
  const fit = fitSeatMap(data.layout);
  $("seat-map").style.setProperty("--u", `${fit.u}px`);
  $("map-hint").hidden = !fit.overflow || !data.layout.groups.length;
  const area = buildArea(data.layout, {
    view: fit.view,
    renderSeat(pos) {
      const seat = byId.get(pos.id);
      const title = seat.status === "free"
        ? t("pub.seats.titleFree", { seat: seat.id })
        : t("pub.seats.titleTaken", {
          seat: seat.id,
          name: seat.name ?? t("pub.seats.taken"),
          status: t(seat.status === "paid" ? "pub.seats.statusPaid" : "pub.seats.statusPending"),
        });
      return el("button", {
        type: "button",
        class: "seat " + (seat.mine ? "mine" : seat.status),
        title,
        "aria-label": title,
        dataset: { focusKey: seat.id },
        onclick: () => onSeatClick(seat),
      }, seat.id);
    },
  });
  $("seat-map").replaceChildren(area);
  if (!data.layout.groups.length) {
    $("seat-map").replaceChildren(el("p", { class: "muted" }, t("pub.seats.notPublished")));
  }
  restoreFocus();
  const taken = data.seats.filter((s) => s.status !== "free").length;
  const paid = data.seats.filter((s) => s.status === "paid").length;
  $("seat-stats").textContent = t("pub.seats.stats",
    { taken, total: data.seats.length, paid, free: data.seats.length - taken });
}

function onSeatClick(seat) {
  if (seat.mine) return openPayDialog();
  if (seat.status === "free") return pickSeat(seat.id);
  showMessage($("seat-msg"), seat.name
    ? t("pub.seats.takenBy", { seat: seat.id, name: seat.name })
    : t("pub.seats.takenHidden", { seat: seat.id }), "info");
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
      showMessage(msg, t("pub.seats.notOpen"));
      return;
    }
    const needsCode = !$("code-card").hidden;
    showMessage(msg, t(needsCode ? "pub.seats.codeFirst" : "pub.seats.registerFirst"));
    $(needsCode ? "code-card" : "register-card").scrollIntoView({ behavior: "smooth" });
    return;
  }
  const question = !state.me.seat
    ? t("pub.seats.confirmReserve", { seat: seatId })
    : t(state.me.paid ? "pub.seats.confirmSwitchPaid" : "pub.seats.confirmMove", { from: state.me.seat, to: seatId });
  if (!confirm(question)) return;
  try {
    const data = await api("POST", "/me/seat", { seat: seatId });
    state.me = data.user;
    state.payment = data.payment;
    renderMe();
    await loadSeats();
    if (data.payment) {
      openPayDialog();
    } else if (data.user.confirm_until) {
      showMessage(msg, t("pub.seats.heldUnconfirmed", { seat: seatId, deadline: formatDeadline(data.user.confirm_until) }),
        "info");
    }
  } catch (err) {
    showMessage(msg, err.message);
    await loadSeats();
  }
}

async function releaseSeat() {
  if (!confirm(t("pub.seats.confirmRelease", { seat: state.me.seat }))) return;
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
  $("pay-title").textContent = t("pub.pay.title", { seat: state.me.seat });
  $("pay-amount").textContent = formatMoney(p.amount_cents);
  $("pay-address").textContent = p.paypal_address || t("pub.pay.notConfigured");
  $("pay-ref").textContent = p.reference;
  $("pay-deadline").textContent = p.reserved_until
    ? t("pub.pay.deadline", { date: formatDeadline(p.reserved_until) })
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
    $("login-link-expiry").textContent = t("pub.loginLink.expiry", { date: formatDeadline(data.expires_at) });
    $("login-link-result").hidden = false;
    $("login-link").select();
  } catch (err) {
    showMessage(msg, err.message);
  }
}

/** Log this device out on the server too, so the token can't be used any more. */
async function logout() {
  showMessage($("me-msg"), "");
  if (!confirm(t("pub.logout.confirm"))) return;
  try {
    await api("POST", "/me/logout");
  } catch (err) {
    if (err.status !== 401) { showMessage($("me-msg"), err.message); return; }
  }
  setToken(null);
  state.me = null;
  state.payment = null;
  // hide name, email and any login link right away, even if the refresh fails
  renderMe();
  syncPayDialog();
  try {
    await refresh();
    showMessage($("seat-msg"), t("pub.logout.done"), "success");
  } catch (err) {
    showMessage($("seat-msg"), err.message);
  }
}

/** For a lost or shared device: end every other login of this participant. */
async function logoutOthers() {
  const msg = $("me-msg");
  showMessage(msg, "");
  if (!confirm(t("pub.logout.othersConfirm"))) return;
  try {
    const { devices, new_link_sent: newLink } = await api("POST", "/me/logout-others");
    resetLoginLinkBox();   // its link no longer works
    const text = devices ? t("pub.logout.othersDone", { count: devices }) : t("pub.logout.othersNone");
    showMessage(msg, newLink ? `${text} ${t("pub.logout.newLinkSent")}` : text, "success");
  } catch (err) {
    showMessage(msg, err.message);
  }
}

async function copy(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    const old = button.textContent;
    button.textContent = t("common.copied");
    setTimeout(() => (button.textContent = old), 1500);
  } catch { prompt(t("common.copyPrompt"), text); }
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
    showMessage($("me-msg"), data.confirmation_required
      ? t("pub.register.doneConfirm", { email: data.user.email, deadline: formatDeadline(data.user.confirm_until) })
      : t("pub.register.done"), "success");
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
    showMessage($("register-msg"), t("pub.code.unlocked"), "success");
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
  $("logout").addEventListener("click", logout);
  $("logout-others").addEventListener("click", logoutOthers);
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
    if (!LINK_RE.test(location.hash)) return;
    await handleLoginLink();
    await refresh();
  });
}

// re-fit the seat map when the screen width changes (rotation, resized window)
let resizeTimer = null;
let lastMapWidth = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    const width = $("seat-map").clientWidth;
    if (state.seatData && width !== lastMapWidth) renderSeatMap(state.seatData);
    lastMapWidth = width;
  }, 150);
});

document.addEventListener("DOMContentLoaded", init);
