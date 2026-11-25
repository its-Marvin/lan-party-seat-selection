"""Seat layout: groups of two-seat tables placed on the event area.

Coordinates and sizes are in grid units; one unit is the width of one seat.
A table is 2 units wide and 1 unit deep (horizontal orientation). Inside a
group, tables are arranged in rows of ``per_row`` tables. A vertical group is
the same arrangement transposed (tables stand upright, rows run left→right).

Seat ids are ``<group label><number>``; table ``t`` (0-based) holds seats
``2t+1`` and ``2t+2``.

Besides table groups, the area can hold *items* (doors, windows, sleeping
area, food & drinks, other): plain labelled rectangles. Items may overlap each
other but never a table group.
"""

import json
import math
import re

from .db import get_db

SETTINGS_KEY = "layout"
SEATS_PER_TABLE = 2

LABEL_RE = re.compile(r"[A-Z]{1,3}")
GROUP_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
SEAT_RE = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,2})")

MIN_AREA, MAX_AREA = 4, 200
MAX_GROUPS = 60
MAX_TABLES_PER_GROUP = 60
MAX_SEATS = 1000
ORIENTATIONS = ("horizontal", "vertical")
ITEM_TYPES = ("door", "window", "sleeping", "food", "other")
MAX_ITEMS = 100

DEFAULT_LAYOUT = {
    "area": {"width": 24, "height": 12},
    "groups": [
        {"id": "g-a", "label": "A", "name": "", "x": 2, "y": 2, "tables": 8,
         "per_row": 4, "orientation": "horizontal"},
        {"id": "g-b", "label": "B", "name": "", "x": 2, "y": 7, "tables": 8,
         "per_row": 4, "orientation": "horizontal"},
    ],
    "items": [],
}


class LayoutError(ValueError):
    pass


# --- geometry ------------------------------------------------------------

def footprint(group: dict) -> tuple[int, int]:
    rows = math.ceil(group["tables"] / group["per_row"])
    width, height = group["per_row"] * SEATS_PER_TABLE, rows
    if group["orientation"] == "vertical":
        width, height = height, width
    return width, height


def _overlap(a: tuple, b: tuple) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def group_seat_ids(group: dict) -> list[str]:
    return [f"{group['label']}{n}" for n in range(1, group["tables"] * SEATS_PER_TABLE + 1)]


def seat_ids(layout: dict) -> list[str]:
    return [sid for g in layout["groups"] for sid in group_seat_ids(g)]


def normalize_seat(value) -> str | None:
    if not isinstance(value, str):
        return None
    seat = value.strip().upper()
    return seat if SEAT_RE.fullmatch(seat) else None


def split_seat(seat: str) -> tuple[str, int]:
    match = SEAT_RE.fullmatch(seat)
    return match.group(1), int(match.group(2))


# --- validation ----------------------------------------------------------

def _int(value, name, lo, hi) -> int:
    if isinstance(value, bool):
        raise LayoutError(f"{name} must be a whole number.")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise LayoutError(f"{name} must be a whole number.") from None
    if isinstance(value, float) and value != number:
        raise LayoutError(f"{name} must be a whole number.")
    if not lo <= number <= hi:
        raise LayoutError(f"{name} must be between {lo} and {hi}.")
    return number


def validate(data) -> dict:
    """Validate a full layout and return a normalised copy."""
    if not isinstance(data, dict):
        raise LayoutError("Expected a JSON object.")
    area = data.get("area")
    if not isinstance(area, dict):
        raise LayoutError("'area' is required.")
    width = _int(area.get("width"), "Area width", MIN_AREA, MAX_AREA)
    height = _int(area.get("height"), "Area height", MIN_AREA, MAX_AREA)

    raw_groups = data.get("groups")
    if not isinstance(raw_groups, list):
        raise LayoutError("'groups' must be a list.")
    if len(raw_groups) > MAX_GROUPS:
        raise LayoutError(f"At most {MAX_GROUPS} groups are allowed.")

    groups, ids, labels = [], set(), set()
    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise LayoutError("Each group must be an object.")
        gid = str(raw.get("id") or "")
        if not GROUP_ID_RE.fullmatch(gid) or gid in ids:
            raise LayoutError("Each group needs a unique id.")
        label = str(raw.get("label") or "").strip().upper()
        if not LABEL_RE.fullmatch(label):
            raise LayoutError(f"Group label '{label}' must be 1–3 letters (A–Z).")
        if label in labels:
            raise LayoutError(f"Group label '{label}' is used twice.")
        name = str(raw.get("name") or "").strip()
        if len(name) > 60:
            raise LayoutError(f"Name of group {label} must be at most 60 characters.")
        tables = _int(raw.get("tables"), f"Tables in group {label}", 1, MAX_TABLES_PER_GROUP)
        per_row = _int(raw.get("per_row"), f"Tables per row in group {label}", 1, tables)
        orientation = raw.get("orientation", "horizontal")
        if orientation not in ORIENTATIONS:
            raise LayoutError(f"Orientation of group {label} must be horizontal or vertical.")
        group = {
            "id": gid, "label": label, "name": name,
            "x": _int(raw.get("x"), f"X position of group {label}", 0, MAX_AREA),
            "y": _int(raw.get("y"), f"Y position of group {label}", 0, MAX_AREA),
            "tables": tables, "per_row": per_row, "orientation": orientation,
        }
        w, h = footprint(group)
        if group["x"] + w > width or group["y"] + h > height:
            raise LayoutError(f"Group {label} does not fit inside the event area.")
        ids.add(gid)
        labels.add(label)
        groups.append(group)

    boxes = [(g, (g["x"], g["y"], *footprint(g))) for g in groups]
    for i, (a, abox) in enumerate(boxes):
        for b, bbox in boxes[i + 1:]:
            if _overlap(abox, bbox):
                raise LayoutError(f"Groups {a['label']} and {b['label']} overlap.")

    raw_items = data.get("items", [])
    if not isinstance(raw_items, list):
        raise LayoutError("'items' must be a list.")
    if len(raw_items) > MAX_ITEMS:
        raise LayoutError(f"At most {MAX_ITEMS} area elements are allowed.")
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise LayoutError("Each area element must be an object.")
        iid = str(raw.get("id") or "")
        if not GROUP_ID_RE.fullmatch(iid) or iid in ids:
            raise LayoutError("Each area element needs a unique id.")
        kind = raw.get("type")
        if kind not in ITEM_TYPES:
            raise LayoutError("Area element type must be one of: " + ", ".join(ITEM_TYPES) + ".")
        label = str(raw.get("label") or "").strip()
        if len(label) > 40:
            raise LayoutError("Area element labels must be at most 40 characters.")
        name = label or kind
        item = {
            "id": iid, "type": kind, "label": label,
            "x": _int(raw.get("x"), f"X position of '{name}'", 0, MAX_AREA),
            "y": _int(raw.get("y"), f"Y position of '{name}'", 0, MAX_AREA),
            "w": _int(raw.get("w"), f"Width of '{name}'", 1, MAX_AREA),
            "h": _int(raw.get("h"), f"Height of '{name}'", 1, MAX_AREA),
        }
        box = (item["x"], item["y"], item["w"], item["h"])
        if item["x"] + item["w"] > width or item["y"] + item["h"] > height:
            raise LayoutError(f"'{name}' does not fit inside the event area.")
        for g, gbox in boxes:
            if _overlap(box, gbox):
                raise LayoutError(f"'{name}' overlaps group {g['label']}.")
        ids.add(iid)
        items.append(item)

    layout = {"area": {"width": width, "height": height}, "groups": groups, "items": items}
    if len(seat_ids(layout)) > MAX_SEATS:
        raise LayoutError(f"At most {MAX_SEATS} seats are allowed.")
    return layout


# --- storage -------------------------------------------------------------

def load() -> dict:
    row = get_db().execute(
        "SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,)
    ).fetchone()
    if row is None:
        return json.loads(json.dumps(DEFAULT_LAYOUT))
    layout = json.loads(row["value"])
    layout.setdefault("items", [])  # layouts saved before items existed
    return layout


def plan_seat_moves(old: dict, new: dict, occupied: list[str]) -> tuple[dict, list[str]]:
    """Work out how occupied seats map onto the new layout.

    Seats are resolved through the *group id* of the old layout: people stay in
    their group when its label changes, and a deleted group's seats are lost
    even if another group takes over its label. Returns ``(moves, lost)``:
    ``moves`` maps old→new seat ids that change, ``lost`` lists occupied seats
    that would no longer exist.
    """
    old_by_label = {g["label"]: g for g in old["groups"]}
    new_by_id = {g["id"]: g for g in new["groups"]}
    valid = set(seat_ids(new))
    moves, lost, taken = {}, [], set()

    def place(seat, target):
        if target is None or target not in valid or target in taken:
            lost.append(seat)
            return
        taken.add(target)
        if target != seat:
            moves[seat] = target

    leftovers = []
    for seat in occupied:
        match = SEAT_RE.fullmatch(seat)
        group = old_by_label.get(match.group(1)) if match else None
        if group is None:
            leftovers.append(seat)  # not part of the old layout (e.g. legacy data)
            continue
        number = int(match.group(2))
        successor = new_by_id.get(group["id"])
        fits = successor is not None and number <= successor["tables"] * SEATS_PER_TABLE
        place(seat, f"{successor['label']}{number}" if fits else None)
    for seat in leftovers:  # only after the group-mapped seats have claimed theirs
        place(seat, seat)
    return moves, lost


def save(layout: dict, moves: dict) -> None:
    """Store the layout and apply seat renames; call inside ``write_transaction``."""
    db = get_db()
    # two passes so that swapping labels (A<->B) never violates UNIQUE(seat)
    for old in moves:
        db.execute("UPDATE users SET seat = ? WHERE seat = ?", ("~" + old, old))
    for old, new in moves.items():
        db.execute("UPDATE users SET seat = ? WHERE seat = ?", (new, "~" + old))
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SETTINGS_KEY, json.dumps(layout)),
    )
