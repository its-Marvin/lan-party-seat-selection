"use strict";

const TOKEN_KEY = "lanparty-token";
const EVENT_CODE_KEY = "lanparty-event-code";

function getEventCode() {
  try { return localStorage.getItem(EVENT_CODE_KEY); } catch { return null; }
}
function setEventCode(code) {
  try {
    if (code) localStorage.setItem(EVENT_CODE_KEY, code);
    else localStorage.removeItem(EVENT_CODE_KEY);
  } catch { /* storage unavailable */ }
}

function getToken() {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* storage unavailable */ }
}

/** `options.background`: a poll the user didn't trigger; it doesn't keep an admin session alive. */
async function api(method, path, body, options = {}) {
  const headers = { "Accept": "application/json" };
  if (options.background) headers["X-Background"] = "1";
  const token = getToken();
  if (token) headers["X-User-Token"] = token;
  const eventCode = getEventCode();
  if (eventCode) headers["X-Event-Code"] = eventCode;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch("/api" + path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const err = new Error(data.error || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function formatMoney(cents) {
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "EUR" })
    .format(cents / 100);
}

function formatDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, {
    weekday: "short", day: "numeric", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function showMessage(target, text, kind = "error") {
  target.textContent = text || "";
  target.className = "message" + (text ? " " + kind : "");
}

/* ---- seat layout ------------------------------------------------------
 * Mirrors lanparty/layout.py: one grid unit = one seat; a table is 2×1
 * (horizontal). Tables in a group are laid out in rows of `per_row`;
 * a vertical group is the same arrangement transposed.
 */
const SEATS_PER_TABLE = 2;

function groupGeometry(g) {
  const rows = Math.ceil(g.tables / g.per_row);
  const vertical = g.orientation === "vertical";
  const tables = [];
  for (let t = 0; t < g.tables; t++) {
    const r = Math.floor(t / g.per_row);
    const c = t % g.per_row;
    const seats = [];
    for (let k = 0; k < SEATS_PER_TABLE; k++) {
      const ux = c * SEATS_PER_TABLE + k, uy = r;
      seats.push({ id: g.label + (t * SEATS_PER_TABLE + k + 1), x: vertical ? uy : ux, y: vertical ? ux : uy });
    }
    tables.push({
      x: vertical ? r : c * SEATS_PER_TABLE,
      y: vertical ? c * SEATS_PER_TABLE : r,
      w: vertical ? 1 : SEATS_PER_TABLE,
      h: vertical ? SEATS_PER_TABLE : 1,
      seats,
    });
  }
  const w = g.per_row * SEATS_PER_TABLE;
  return { w: vertical ? rows : w, h: vertical ? w : rows, tables };
}

const ITEM_TYPES = {
  door:     { name: "Door",          icon: "🚪", w: 2, h: 1, rank: 1 },
  window:   { name: "Window",        icon: "🪟", w: 3, h: 1, rank: 1 },
  sleeping: { name: "Sleeping area", icon: "🛏️", w: 6, h: 4, rank: 0 },
  food:     { name: "Food & drinks", icon: "🍕", w: 4, h: 2, rank: 0 },
  other:    { name: "Other",         icon: "📍", w: 3, h: 2, rank: 0 },
};

function itemName(item) {
  return item.label || (ITEM_TYPES[item.type] || ITEM_TYPES.other).name;
}

function placeUnits(node, x, y, w, h) {
  // CSSOM assignments are allowed by the Content-Security-Policy.
  node.style.left = `calc(var(--u) * ${x})`;
  node.style.top = `calc(var(--u) * ${y})`;
  if (w !== undefined) node.style.width = `calc(var(--u) * ${w})`;
  if (h !== undefined) node.style.height = `calc(var(--u) * ${h})`;
}

/**
 * Build the event area.
 *   renderSeat(seat, group) → Node   – content for one seat cell
 *   decorateGroup(node, group, geometry) – optional hook (e.g. drag handlers)
 *   decorateItem(node, item)              – optional hook for area elements
 */
/** Smallest rectangle (grid units) holding every group and area element, or null if empty. */
function contentBounds(layout) {
  const boxes = [
    ...layout.groups.map((g) => { const geo = groupGeometry(g); return { x: g.x, y: g.y, w: geo.w, h: geo.h }; }),
    ...(layout.items || []).map((i) => ({ x: i.x, y: i.y, w: i.w, h: i.h })),
  ];
  if (!boxes.length) return null;
  const x = Math.min(...boxes.map((b) => b.x));
  const y = Math.min(...boxes.map((b) => b.y));
  return {
    x, y,
    w: Math.max(...boxes.map((b) => b.x + b.w)) - x,
    h: Math.max(...boxes.map((b) => b.y + b.h)) - y,
  };
}

function buildArea(layout, { renderSeat, decorateGroup, decorateItem, view } = {}) {
  // `view` (grid units) shows only part of the area, e.g. just the tables on a phone
  const v = view || { x: 0, y: 0, w: layout.area.width, h: layout.area.height };
  const area = el("div", { class: "area" + (view ? " cropped" : "") });
  area.style.width = `calc(var(--u) * ${v.w})`;
  area.style.height = `calc(var(--u) * ${v.h})`;
  // area elements first so table groups are drawn on top; doors/windows above zones
  const items = [...(layout.items || [])].sort(
    (a, b) => (ITEM_TYPES[a.type]?.rank ?? 0) - (ITEM_TYPES[b.type]?.rank ?? 0));
  for (const item of items) {
    const type = ITEM_TYPES[item.type] || ITEM_TYPES.other;
    const name = itemName(item);
    const node = el("div", {
      class: `item item-${item.type}` + (item.h > item.w ? " tall" : ""),
      title: name,
      dataset: { id: item.id },
    }, el("span", { class: "item-icon", "aria-hidden": "true" }, type.icon), el("span", { class: "item-label" }, name));
    placeUnits(node, item.x - v.x, item.y - v.y, item.w, item.h);
    if (decorateItem) decorateItem(node, item);
    area.append(node);
  }
  for (const g of layout.groups) {
    const geo = groupGeometry(g);
    const groupEl = el("div", { class: "group", dataset: { id: g.id } },
      el("div", { class: "group-label" }, g.label, g.name ? el("span", { class: "group-name" }, " · " + g.name) : null),
    );
    placeUnits(groupEl, g.x - v.x, g.y - v.y, geo.w, geo.h);
    for (const t of geo.tables) {
      const tableEl = el("div", { class: "table" + (t.h > t.w ? " vertical" : "") });
      placeUnits(tableEl, t.x, t.y, t.w, t.h);
      groupEl.append(tableEl);
      for (const seat of t.seats) {
        const node = renderSeat ? renderSeat(seat, g) : el("span", { class: "seat" }, seat.id);
        placeUnits(node, seat.x, seat.y, 1, 1);
        groupEl.append(node);
      }
    }
    if (decorateGroup) decorateGroup(groupEl, g, geo);
    area.append(groupEl);
  }
  return area;
}
