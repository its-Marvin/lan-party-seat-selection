"use strict";

/* Admin seat-layout editor: drag groups of two-seat tables and area elements
 * (doors, windows, sleeping area, food & drinks, …) on the event area. */
window.LayoutEditor = (() => {
  const byId = (id) => document.getElementById(id);
  const MAX_AREA = 200, MAX_TABLES = 60;

  const ed = {
    saved: null,          // last layout stored on the server
    draft: null,          // layout being edited
    selected: null,       // id of the selected group or item
    occupancy: new Map(), // seat id (saved labels) -> {status, name}
    dirty: false,
    revision: 0,          // bumped on every draft change
    saving: false,        // a PUT is in flight
    eventId: null,        // event the draft belongs to (save target)
    loadingEventId: null, // event whose layout is being fetched; saving waits for it
    loadFailed: false,    // the last load failed: the draft doesn't match the page any more
    readOnly: false,      // archived event: the layout can't be saved
  };

  const seatsPath = (eventId) => `/admin/seats?event=${encodeURIComponent(eventId)}`;
  /** Saving is only allowed while the draft belongs to the event the page shows. */
  const canSave = () => ed.dirty && !ed.saving && !ed.readOnly && ed.loadingEventId === null && !ed.loadFailed;

  const clone = (x) => JSON.parse(JSON.stringify(x));
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const toInt = (v, fallback) => { const n = parseInt(v, 10); return Number.isFinite(n) ? n : fallback; };
  const newId = (prefix) => prefix + "-" + Math.random().toString(36).slice(2, 10);
  const overlap = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

  const findGroup = (id) => ed.draft?.groups.find((g) => g.id === id) || null;
  const findItem = (id) => ed.draft?.items.find((i) => i.id === id) || null;
  /** {kind, obj} for the current selection, or null. */
  function selection() {
    const g = findGroup(ed.selected);
    if (g) return { kind: "group", obj: g };
    const i = findItem(ed.selected);
    return i ? { kind: "item", obj: i } : null;
  }
  /** Bounding box in grid units. */
  function box(kind, obj) {
    if (kind === "group") { const { w, h } = groupGeometry(obj); return { x: obj.x, y: obj.y, w, h }; }
    return { x: obj.x, y: obj.y, w: obj.w, h: obj.h };
  }

  // --- data ------------------------------------------------------------

  /** Load the stored layout of `eventId` (default: the event loaded last), dropping the draft. */
  async function load(eventId = ed.eventId) {
    // The draft keeps its event id until the new layout has arrived: a save in
    // the meantime is blocked instead of writing the old draft to the new event.
    ed.loadingEventId = eventId;
    if (ed.draft) renderSummary();
    let data;
    try {
      data = await api("GET", seatsPath(eventId));
    } catch (err) {
      if (ed.loadingEventId === eventId) {
        ed.loadingEventId = null;
        ed.loadFailed = true;
        if (ed.draft) renderSummary();
      }
      throw err;
    }
    if (ed.loadingEventId !== eventId) return;   // another event was picked meanwhile
    ed.loadingEventId = null;
    ed.loadFailed = false;
    ed.eventId = eventId;
    ed.saved = data.layout;
    ed.draft = clone(data.layout);
    ed.draft.items = ed.draft.items || [];
    if (!selection()) ed.selected = null;
    setOccupancy(data.seats, false);
    ed.dirty = false;
    render();
  }

  /** Occupancy of `eventId` (ignored if the editor shows another event). */
  function setOccupancy(seats, rerender = true, eventId = ed.eventId) {
    if (eventId !== ed.eventId) return;
    ed.occupancy = new Map(seats.filter((s) => s.status !== "free").map((s) => [s.id, s]));
    if (rerender && ed.draft) renderCanvas();
  }

  /**
   * Where each occupied seat ends up in the draft (same rule as the server's
   * plan_seat_moves): seats are resolved through the group id of the saved
   * layout, so people stay in their group when its label changes, and a deleted
   * group's seats are lost even if another group takes over its label.
   * Returns { at: Map(draftSeatId -> occupant), lost: [[seatId, occupant]] }.
   */
  function seatTargets() {
    const oldByLabel = new Map(ed.saved.groups.map((g) => [g.label, g]));
    const newById = new Map(ed.draft.groups.map((g) => [g.id, g]));
    const valid = new Set();
    for (const g of ed.draft.groups) {
      for (let n = 1; n <= g.tables * SEATS_PER_TABLE; n++) valid.add(g.label + n);
    }
    const at = new Map(), lost = [], leftovers = [];
    const place = (seatId, occ, target) => {
      if (!target || !valid.has(target) || at.has(target)) lost.push([seatId, occ]);
      else at.set(target, occ);
    };
    for (const [seatId, occ] of ed.occupancy) {
      const m = /^([A-Z]{1,3})(\d+)$/.exec(seatId);
      const group = m && oldByLabel.get(m[1]);
      if (!group) { leftovers.push([seatId, occ]); continue; }
      const number = Number(m[2]);
      const successor = newById.get(group.id);
      place(seatId, occ, successor && number <= successor.tables * SEATS_PER_TABLE ? successor.label + number : null);
    }
    for (const [seatId, occ] of leftovers) place(seatId, occ, seatId);
    return { at, lost };
  }

  function markDirty() {
    ed.dirty = true;
    ed.revision++;
    renderCanvas();
    renderSummary();
  }

  // --- validation (mirrors the server so problems show up while editing) --

  function problems() {
    const list = [];
    const bad = new Set();
    const { width, height } = ed.draft.area;
    const outside = (b) => b.x + b.w > width || b.y + b.h > height;
    const labels = new Map();
    const groups = ed.draft.groups.map((g) => ({ g, b: box("group", g) }));
    for (const { g, b } of groups) {
      if (!/^[A-Z]{1,3}$/.test(g.label)) { list.push(t("editor.problem.label", { label: g.label })); bad.add(g.id); }
      if (labels.has(g.label)) { list.push(t("editor.problem.labelTwice", { label: g.label })); bad.add(g.id); bad.add(labels.get(g.label)); }
      labels.set(g.label, g.id);
      if (outside(b)) { list.push(t("editor.problem.groupOutside", { label: g.label })); bad.add(g.id); }
    }
    for (let i = 0; i < groups.length; i++) {
      for (let j = i + 1; j < groups.length; j++) {
        if (overlap(groups[i].b, groups[j].b)) {
          list.push(t("editor.problem.groupsOverlap", { a: groups[i].g.label, b: groups[j].g.label }));
          bad.add(groups[i].g.id); bad.add(groups[j].g.id);
        }
      }
    }
    for (const item of ed.draft.items) {
      const b = box("item", item);
      if (outside(b)) { list.push(t("editor.problem.itemOutside", { name: itemName(item) })); bad.add(item.id); }
      for (const { g, b: gb } of groups) {
        if (overlap(b, gb)) {
          list.push(t("editor.problem.itemOverlaps", { name: itemName(item), label: g.label }));
          bad.add(item.id); bad.add(g.id);
        }
      }
    }
    for (const [seatId, occ] of seatTargets().lost) {
      list.push(t("editor.problem.seatRemoved", { seat: seatId, name: occ.name }));
    }
    return { list, bad };
  }

  // --- rendering -------------------------------------------------------

  function render() {
    byId("area-width").value = ed.draft.area.width;
    byId("area-height").value = ed.draft.area.height;
    renderCanvas();
    renderPanel();
    renderSummary();
  }

  function renderSummary() {
    const groups = ed.draft.groups;
    const tables = groups.reduce((n, g) => n + g.tables, 0);
    const items = ed.draft.items.length;
    byId("layout-summary").textContent =
      t("editor.summary", { groups: groups.length, tables, seats: tables * SEATS_PER_TABLE, items }) +
      (ed.dirty ? t("editor.unsaved") : "");
    byId("save-layout").disabled = !canSave();
    byId("save-layout").textContent = t(ed.saving ? "admin.layout.saving" : "admin.layout.save");
    byId("discard-layout").disabled = !ed.dirty || ed.saving;
  }

  function makeInteractive(node, kind, obj, bad, ariaLabel) {
    node.tabIndex = 0;
    node.setAttribute("role", "button");
    node.setAttribute("aria-label", t("editor.keysHint", { label: ariaLabel }));
    node.classList.add("editable");
    if (obj.id === ed.selected) node.classList.add("selected");
    if (bad.has(obj.id)) node.classList.add("invalid");
    node.addEventListener("pointerdown", (e) => startDrag(e, node, kind, obj));
    node.addEventListener("keydown", (e) => onKey(e, kind, obj));
  }

  function renderCanvas() {
    const { list, bad } = problems();
    const { at } = seatTargets();
    const area = buildArea(ed.draft, {
      renderSeat(seat) {
        const occ = at.get(seat.id);
        const title = occ
          ? t("editor.seatTaken", { seat: seat.id, name: occ.name, status: t(occ.status === "paid" ? "editor.statusPaid" : "editor.statusPending") })
          : t("editor.seatFree", { seat: seat.id });
        return el("span", { class: "seat " + (occ ? occ.status : "free"), title }, seat.id);
      },
      decorateGroup(node, group) {
        makeInteractive(node, "group", group, bad, t("editor.groupAria", { label: group.label, tables: group.tables }));
      },
      decorateItem(node, item) {
        makeInteractive(node, "item", item, bad, t("editor.itemAria", { name: itemName(item) }));
        const handle = el("span", { class: "resize-handle", title: t("editor.resize"), "aria-hidden": "true" });
        handle.addEventListener("pointerdown", (e) => startResize(e, node, item));
        node.append(handle);
      },
    });
    area.addEventListener("pointerdown", (e) => {
      if (e.target === area) select(null);
    });
    byId("layout-canvas").replaceChildren(area);
    if (!ed.draft.groups.length && !ed.draft.items.length) {
      byId("layout-canvas").append(el("p", { class: "muted small canvas-empty" }, t("editor.empty")));
    }
    byId("layout-problems").replaceChildren(...list.map((p) => el("li", {}, p)));
  }

  function fillForm(form, obj, keys) {
    for (const key of keys) {
      if (document.activeElement !== form.elements[key]) form.elements[key].value = obj[key];
    }
  }

  function renderPanel() {
    const sel = selection();
    const groupForm = byId("group-form");
    const itemForm = byId("item-form");
    byId("group-hint").hidden = !!sel;
    groupForm.hidden = sel?.kind !== "group";
    itemForm.hidden = sel?.kind !== "item";
    if (!sel) return;

    if (sel.kind === "group") {
      const g = sel.obj;
      byId("group-title").textContent = t("admin.layout.groupTitle", { label: g.label });
      fillForm(groupForm, g, ["label", "name", "tables", "per_row", "orientation", "x", "y"]);
      groupForm.elements.per_row.max = g.tables;
      const { at } = seatTargets();
      const occupied = groupGeometry(g).tables.flatMap((t) => t.seats).filter((s) => at.has(s.id)).length;
      byId("group-info").textContent = t("editor.groupInfo", {
        seats: g.tables * SEATS_PER_TABLE, first: `${g.label}1`, last: `${g.label}${g.tables * SEATS_PER_TABLE}`, occupied,
      });
    } else {
      const item = sel.obj;
      byId("item-title").textContent = itemName(item);
      fillForm(itemForm, item, ["type", "label", "x", "y", "w", "h"]);
      itemForm.elements.label.placeholder = t(ITEM_TYPES[item.type].key);
    }
  }

  function select(id) {
    ed.selected = id;
    document.querySelectorAll("#layout-canvas .editable").forEach((n) => {
      n.classList.toggle("selected", n.dataset.id === id);
    });
    renderPanel();
  }

  function focusNode(id) {
    const node = document.querySelector(`#layout-canvas .editable[data-id="${CSS.escape(id)}"]`);
    if (node) node.focus();
  }

  // --- interaction -----------------------------------------------------

  /** Shared pointer-drag plumbing: calls onMove(dxUnits, dyUnits), then onEnd(). */
  function trackPointer(e, node, onMove, onEnd) {
    const unit = byId("layout-canvas").querySelector(".area").getBoundingClientRect().width / ed.draft.area.width;
    const start = { px: e.clientX, py: e.clientY };
    node.setPointerCapture(e.pointerId);
    node.classList.add("dragging");
    const move = (ev) => onMove(Math.round((ev.clientX - start.px) / unit), Math.round((ev.clientY - start.py) / unit));
    const end = () => {
      node.removeEventListener("pointermove", move);
      node.removeEventListener("pointerup", end);
      node.removeEventListener("pointercancel", end);
      node.classList.remove("dragging");
      onEnd();
    };
    node.addEventListener("pointermove", move);
    node.addEventListener("pointerup", end);
    node.addEventListener("pointercancel", end);
  }

  function startDrag(e, node, kind, obj) {
    if (e.button !== 0) return;
    e.preventDefault();
    select(obj.id);
    node.focus();
    const b = box(kind, obj);
    const origin = { x: obj.x, y: obj.y };
    const maxX = Math.max(0, ed.draft.area.width - b.w);
    const maxY = Math.max(0, ed.draft.area.height - b.h);
    trackPointer(e, node, (dx, dy) => {
      const x = clamp(origin.x + dx, 0, maxX);
      const y = clamp(origin.y + dy, 0, maxY);
      if (x !== obj.x || y !== obj.y) {
        obj.x = x;
        obj.y = y;
        placeUnits(node, x, y);
      }
    }, () => {
      if (obj.x !== origin.x || obj.y !== origin.y) {
        markDirty();
        renderPanel();
        focusNode(obj.id);
      }
    });
  }

  function startResize(e, node, item) {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    select(item.id);
    const origin = { w: item.w, h: item.h };
    trackPointer(e, node, (dx, dy) => {
      const w = clamp(origin.w + dx, 1, ed.draft.area.width - item.x);
      const h = clamp(origin.h + dy, 1, ed.draft.area.height - item.y);
      if (w !== item.w || h !== item.h) {
        item.w = w;
        item.h = h;
        placeUnits(node, item.x, item.y, w, h);
        node.classList.toggle("tall", h > w);
      }
    }, () => {
      if (item.w !== origin.w || item.h !== origin.h) {
        markDirty();
        renderPanel();
        focusNode(item.id);
      }
    });
  }

  function onKey(e, kind, obj) {
    if (e.target !== e.currentTarget) return;
    if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); select(obj.id); deleteSelected(); return; }
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(obj.id); return; }
    const step = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
    if (!step) return;
    e.preventDefault();
    select(obj.id);
    const b = box(kind, obj);
    if (e.shiftKey && kind === "item") {
      // Shift+arrows resize area elements
      obj.w = clamp(obj.w + step[0], 1, ed.draft.area.width - obj.x);
      obj.h = clamp(obj.h + step[1], 1, ed.draft.area.height - obj.y);
    } else {
      obj.x = clamp(obj.x + step[0], 0, Math.max(0, ed.draft.area.width - b.w));
      obj.y = clamp(obj.y + step[1], 0, Math.max(0, ed.draft.area.height - b.h));
    }
    markDirty();
    renderPanel();
    focusNode(obj.id);
  }

  function onGroupInput(e) {
    const g = selection()?.obj;
    if (!g) return;
    const f = byId("group-form").elements;
    const field = e.target.name;
    if (field === "label") {
      const clean = e.target.value.toUpperCase().replace(/[^A-Z]/g, "").slice(0, 3);
      if (clean !== e.target.value) e.target.value = clean;
      g.label = clean;
    } else if (field === "name") {
      g.name = e.target.value;
    } else if (field === "orientation") {
      g.orientation = e.target.value;
    } else if (field === "tables") {
      g.tables = clamp(toInt(e.target.value, g.tables), 1, MAX_TABLES);
      if (g.per_row > g.tables) { g.per_row = g.tables; f.per_row.value = g.per_row; }
    } else if (field === "per_row") {
      g.per_row = clamp(toInt(e.target.value, g.per_row), 1, g.tables);
    } else if (field === "x" || field === "y") {
      g[field] = clamp(toInt(e.target.value, g[field]), 0, MAX_AREA);
    } else {
      return;
    }
    markDirty();
    renderPanel();
  }

  function onItemInput(e) {
    const item = selection()?.obj;
    if (!item) return;
    const field = e.target.name;
    if (field === "type") {
      item.type = e.target.value;
    } else if (field === "label") {
      item.label = e.target.value;
    } else if (field === "x" || field === "y") {
      item[field] = clamp(toInt(e.target.value, item[field]), 0, MAX_AREA);
    } else if (field === "w" || field === "h") {
      item[field] = clamp(toInt(e.target.value, item[field]), 1, MAX_AREA);
    } else {
      return;
    }
    markDirty();
    renderPanel();
  }

  function onAreaInput() {
    ed.draft.area.width = clamp(toInt(byId("area-width").value, ed.draft.area.width), 4, MAX_AREA);
    ed.draft.area.height = clamp(toInt(byId("area-height").value, ed.draft.area.height), 4, MAX_AREA);
    markDirty();
  }

  function nextLabel() {
    const used = new Set(ed.draft.groups.map((g) => g.label));
    const letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
    for (const a of ["", ...letters]) for (const b of letters) {
      if (!used.has(a + b)) return a + b;
    }
    return "ZZZ";
  }

  /**
   * Top-left-most free spot for a w×h box, keeping `gap` units of space to
   * the other boxes. Grows the area downwards if nothing fits.
   */
  function findSpot(selfId, w, h, prefer, gap = 1) {
    const others = [
      ...ed.draft.groups.map((g) => ({ id: g.id, ...box("group", g) })),
      ...ed.draft.items.map((i) => ({ id: i.id, ...box("item", i) })),
    ].filter((o) => o.id !== selfId);
    const fits = (x, y) => x >= 0 && y >= 0 && x + w <= ed.draft.area.width && y + h <= ed.draft.area.height &&
      others.every((o) => !overlap({ x: x - gap, y: y - gap, w: w + 2 * gap, h: h + 2 * gap }, o));
    if (prefer && fits(prefer.x, prefer.y)) return prefer;
    for (let y = 0; y + h <= ed.draft.area.height; y++) {
      for (let x = 0; x + w <= ed.draft.area.width; x++) if (fits(x, y)) return { x, y };
    }
    const bottom = others.reduce((m, o) => Math.max(m, o.y + o.h), 0);
    ed.draft.area.height = clamp(bottom + gap + h, ed.draft.area.height, MAX_AREA);
    ed.draft.area.width = clamp(Math.max(ed.draft.area.width, w), 4, MAX_AREA);
    return { x: 0, y: Math.min(bottom + gap, MAX_AREA - h) };
  }

  function addAndSelect(list, obj) {
    list.push(obj);
    ed.selected = obj.id;
    render();
    markDirty();
    focusNode(obj.id);
  }

  function addGroup() {
    const g = { id: newId("g"), label: nextLabel(), name: "", x: 0, y: 0, tables: 4, per_row: 2, orientation: "horizontal" };
    const { w, h } = groupGeometry(g);
    Object.assign(g, findSpot(g.id, w, h));
    addAndSelect(ed.draft.groups, g);
  }

  function addItem(type) {
    const def = ITEM_TYPES[type];
    const item = { id: newId("i"), type, label: "", x: 0, y: 0, w: def.w, h: def.h };
    Object.assign(item, findSpot(item.id, def.w, def.h, null, 0));
    addAndSelect(ed.draft.items, item);
  }

  function duplicateSelected() {
    const sel = selection();
    if (!sel) return;
    const src = sel.obj;
    const b = box(sel.kind, src);
    if (sel.kind === "group") {
      const g = { ...clone(src), id: newId("g"), label: nextLabel() };
      const prefer = src.orientation === "vertical" ? { x: src.x + b.w + 1, y: src.y } : { x: src.x, y: src.y + b.h + 1 };
      Object.assign(g, findSpot(g.id, b.w, b.h, prefer));
      addAndSelect(ed.draft.groups, g);
    } else {
      const item = { ...clone(src), id: newId("i") };
      Object.assign(item, findSpot(item.id, b.w, b.h, { x: src.x + b.w, y: src.y }, 0));
      addAndSelect(ed.draft.items, item);
    }
  }

  function deleteSelected() {
    const sel = selection();
    if (!sel) return;
    if (sel.kind === "group") {
      const g = sel.obj;
      const { at } = seatTargets();
      const occupied = groupGeometry(g).tables.flatMap((t) => t.seats).filter((s) => at.has(s.id));
      const question = occupied.length
        ? t("editor.confirmDeleteOccupied", { label: g.label, count: occupied.length })
        : t("editor.confirmDelete", { label: g.label });
      if (!confirm(question)) return;
      ed.draft.groups = ed.draft.groups.filter((x) => x.id !== g.id);
    } else {
      ed.draft.items = ed.draft.items.filter((x) => x.id !== sel.obj.id);
    }
    ed.selected = null;
    render();
    markDirty();
  }

  async function save() {
    if (ed.saving) return;
    const msg = byId("layout-msg");
    showMessage(msg, "");
    // Send a snapshot: the editor stays usable while the request is pending, and
    // changes made in the meantime must not be replaced by the reloaded layout.
    if (!canSave()) return;
    const submitted = clone(ed.draft);
    const revision = ed.revision;
    const eventId = ed.eventId;
    ed.saving = true;
    renderSummary();
    try {
      const res = await api("PUT", `/admin/layout?event=${encodeURIComponent(eventId)}`, submitted);
      const moved = Object.keys(res.moved || {}).length;
      const movedText = moved ? t("editor.moved", { count: moved }) : "";
      document.dispatchEvent(new CustomEvent("layout-saved"));
      if (ed.revision === revision) {
        await load();
        showMessage(msg, t("editor.saved", { moved: movedText }), "success");
      } else {
        await refreshSaved();
        showMessage(msg, t("editor.savedChangedMeanwhile", { moved: movedText }), "info");
      }
    } catch (err) {
      if (err.status === 401) {
        // keep the draft; admin.js offers sign-in, then Save can be retried
        showMessage(msg, t("editor.sessionExpired"));
        document.dispatchEvent(new CustomEvent("admin-auth-required"));
        return;
      }
      showMessage(msg, err.message);
    } finally {
      ed.saving = false;
      renderSummary();
    }
  }

  /** Take the stored layout and occupancy from the server but keep the draft. */
  async function refreshSaved() {
    const data = await api("GET", seatsPath(ed.eventId));
    ed.saved = data.layout;
    setOccupancy(data.seats, false);
    renderCanvas();     // not the side panel: the admin may be typing in it
  }

  function discard() {
    if (!confirm(t("editor.confirmDiscard"))) return;
    ed.draft = clone(ed.saved);
    if (!selection()) ed.selected = null;
    ed.dirty = false;
    showMessage(byId("layout-msg"), "");
    render();
  }

  document.addEventListener("DOMContentLoaded", () => {
    byId("group-form").addEventListener("input", onGroupInput);
    byId("group-form").addEventListener("submit", (e) => e.preventDefault());
    byId("item-form").addEventListener("input", onItemInput);
    byId("item-form").addEventListener("submit", (e) => e.preventDefault());
    byId("area-width").addEventListener("change", onAreaInput);
    byId("area-height").addEventListener("change", onAreaInput);
    byId("add-group").addEventListener("click", addGroup);
    document.querySelectorAll("[data-add-item]").forEach((btn) => {
      btn.addEventListener("click", () => addItem(btn.dataset.addItem));
    });
    document.querySelectorAll(".duplicate-selected").forEach((b) => b.addEventListener("click", duplicateSelected));
    document.querySelectorAll(".delete-selected").forEach((b) => b.addEventListener("click", deleteSelected));
    byId("save-layout").addEventListener("click", save);
    byId("discard-layout").addEventListener("click", discard);
    window.addEventListener("beforeunload", (e) => {
      if (ed.dirty) { e.preventDefault(); e.returnValue = ""; }
    });
  });

  /** Archived events: editing still works as a preview, but nothing can be saved. */
  function setReadOnly(readOnly) {
    ed.readOnly = readOnly;
    byId("layout-readonly").hidden = !readOnly;
    if (ed.draft) renderSummary();
  }

  return {
    load, setOccupancy, setReadOnly,
    isDirty: () => ed.dirty,
    isSaving: () => ed.saving,
    eventId: () => ed.eventId,
  };
})();
