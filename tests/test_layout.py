import copy

import pytest

from tests.helpers import EVENT_CODE, set_event_code

from lanparty import create_app
from lanparty import layout as lay

ADMIN_PW = "s3cret"


@pytest.fixture
def app(tmp_path):
    return set_event_code(create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.sqlite3"),
        "ADMIN_PASSWORD": ADMIN_PW,
    }))


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin(app):
    c = app.test_client()
    assert c.post("/api/admin/login", json={"password": ADMIN_PW}).status_code == 200
    return c


def group(gid="g1", label="A", x=0, y=0, tables=4, per_row=2, orientation="horizontal", name=""):
    return {"id": gid, "label": label, "name": name, "x": x, "y": y,
            "tables": tables, "per_row": per_row, "orientation": orientation}


def layout(*objs, width=30, height=20):
    """Build a layout; objects with a "type" key are area elements, the rest groups."""
    return {"area": {"width": width, "height": height},
            "groups": [o for o in objs if "type" not in o],
            "items": [o for o in objs if "type" in o]}


def take_seat(client, seat, name="Alice", email=None):
    email = email or f"{name.lower()}@example.com"
    res = client.post("/api/register", json={"name": name, "email": email, "event_code": EVENT_CODE})
    headers = {"X-User-Token": res.get_json()["token"]}
    assert client.post("/api/me/seat", json={"seat": seat}, headers=headers).status_code == 200
    return headers, res.get_json()["user"]["id"]


# --- geometry ------------------------------------------------------------

def test_footprint_horizontal_and_vertical():
    g = group(tables=5, per_row=2)          # 3 rows of up to 2 tables
    assert lay.footprint(g) == (4, 3)
    g["orientation"] = "vertical"
    assert lay.footprint(g) == (3, 4)


def test_two_seats_per_table():
    g = group(label="C", tables=3)
    assert lay.group_seat_ids(g) == ["C1", "C2", "C3", "C4", "C5", "C6"]


def test_seat_ids_cover_all_groups():
    data = layout(group("g1", "A", tables=1), group("g2", "BB", x=5, tables=2))
    assert lay.seat_ids(data) == ["A1", "A2", "BB1", "BB2", "BB3", "BB4"]


@pytest.mark.parametrize("value,expected", [
    (" a12 ", "A12"), ("BB3", "BB3"), ("ABC999", "ABC999"),
    ("A0", None), ("A012", None), ("ABCD1", None), ("1A", None), (None, None), (3, None),
])
def test_normalize_seat(value, expected):
    assert lay.normalize_seat(value) == expected


# --- validation ----------------------------------------------------------

def test_validate_normalises():
    out = lay.validate(layout(group(label="a", name="  Clan  ", x="2", y=3.0)))
    g = out["groups"][0]
    assert (g["label"], g["name"], g["x"], g["y"]) == ("A", "Clan", 2, 3)


@pytest.mark.parametrize("data,message", [
    ({}, "area"),
    ({"area": {"width": 3, "height": 10}, "groups": []}, "width"),
    ({"area": {"width": 10, "height": 201}, "groups": []}, "height"),
    (layout(group(label="A1")), "label"),
    (layout(group(label="")), "label"),
    (layout(group("g1", "A"), group("g2", "A", x=10)), "twice"),
    (layout(group("g1", "A"), group("g1", "B", x=10)), "unique id"),
    (layout(group(tables=0)), "Tables"),
    (layout(group(tables=61)), "Tables"),
    (layout(group(tables=2, per_row=3)), "per row"),
    (layout(group(orientation="diagonal")), "Orientation"),
    (layout(group(x=-1)), "X position"),
    (layout(group(x=27), width=30), "fit"),            # 4 wide → 27+4 > 30
    (layout(group(y=19), height=20), "fit"),           # 2 deep → 19+2 > 20
    (layout(group("g1", "A"), group("g2", "B", x=3, y=1)), "overlap"),
    ({"area": {"width": 10, "height": 10}, "groups": "nope"}, "list"),
])
def test_validate_rejects(data, message):
    with pytest.raises(lay.LayoutError, match=message):
        lay.validate(data)


def test_touching_groups_do_not_overlap():
    lay.validate(layout(group("g1", "A"), group("g2", "B", x=4)))   # A spans x 0..3


def test_seat_limit():
    groups = [group(f"g{i}", chr(65 + i % 26) * (1 + i // 26), x=(i % 6) * 33, y=(i // 6) * 3,
                    tables=16, per_row=16) for i in range(40)]
    with pytest.raises(lay.LayoutError, match="1000 seats"):
        lay.validate(layout(*groups, width=200, height=200))


# --- seat move planning --------------------------------------------------

def test_plan_moves_follow_renamed_group():
    old = layout(group("g1", "A"), group("g2", "B", x=10))
    new = layout(group("g1", "C"), group("g2", "B", x=10))
    moves, lost = lay.plan_seat_moves(old, new, ["A3", "B1"])
    assert moves == {"A3": "C3"} and lost == []


def test_plan_moves_detects_lost_seats():
    old = layout(group("g1", "A", tables=4))
    new = layout(group("g1", "A", tables=2))
    assert lay.plan_seat_moves(old, new, ["A2", "A7"]) == ({}, ["A7"])


def test_plan_moves_deleted_group():
    old = layout(group("g1", "A"), group("g2", "B", x=10))
    new = layout(group("g2", "B", x=10))
    assert lay.plan_seat_moves(old, new, ["A1", "B1"]) == ({}, ["A1"])


# --- API -----------------------------------------------------------------

def test_layout_endpoints_require_admin(client):
    assert client.get("/api/admin/layout").status_code == 401
    assert client.put("/api/admin/layout", json=layout()).status_code == 401


def test_default_layout(admin):
    data = admin.get("/api/admin/layout").get_json()
    assert data == lay.DEFAULT_LAYOUT


def test_save_layout_updates_public_seat_map(client, admin):
    new = layout(
        group("g1", "A", x=1, y=1, tables=3, per_row=3, name="Streamers"),
        group("g2", "B", x=1, y=4, tables=2, per_row=1, orientation="vertical"),
    )
    res = admin.put("/api/admin/layout", json=new)
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["moved"] == {}

    data = client.get("/api/seats").get_json()
    assert data["layout"]["groups"][0]["name"] == "Streamers"
    assert [s["id"] for s in data["seats"]] == ["A1", "A2", "A3", "A4", "A5", "A6", "B1", "B2", "B3", "B4"]
    assert client.get("/api/info").get_json()["seat_count"] == 10


def test_saved_layout_controls_valid_seats(client, admin):
    res = admin.put("/api/admin/layout", json=layout(group("g1", "X", tables=1, per_row=1)))
    assert res.status_code == 200, res.get_json()
    res = client.post("/api/register", json={"name": "Alice", "email": "a@example.com", "event_code": EVENT_CODE})
    headers = {"X-User-Token": res.get_json()["token"]}
    assert client.post("/api/me/seat", json={"seat": "A1"}, headers=headers).status_code == 400
    assert client.post("/api/me/seat", json={"seat": "X2"}, headers=headers).status_code == 200


def test_invalid_layout_is_rejected(admin):
    res = admin.put("/api/admin/layout", json=layout(group("g1", "A"), group("g2", "B", x=1)))
    assert res.status_code == 400 and "overlap" in res.get_json()["error"]
    assert admin.get("/api/admin/layout").get_json() == lay.DEFAULT_LAYOUT


def test_moving_groups_keeps_people(client, admin):
    take_seat(client, "A5")
    current = admin.get("/api/admin/layout").get_json()
    moved = copy.deepcopy(current)
    moved["groups"][0]["x"], moved["groups"][0]["y"] = 14, 8
    moved["groups"][1]["orientation"] = "vertical"
    moved["area"] = {"width": 40, "height": 20}
    assert admin.put("/api/admin/layout", json=moved).status_code == 200
    seats = {s["id"]: s for s in client.get("/api/seats", headers={"X-Event-Code": EVENT_CODE}).get_json()["seats"]}
    assert seats["A5"]["name"] == "Alice"


def test_cannot_remove_occupied_seats(client, admin):
    take_seat(client, "B16")
    current = admin.get("/api/admin/layout").get_json()

    shrunk = copy.deepcopy(current)
    shrunk["groups"][1]["tables"] = 7          # B15/B16 disappear
    res = admin.put("/api/admin/layout", json=shrunk)
    assert res.status_code == 409
    assert "B16 (Alice)" in res.get_json()["error"]

    removed = copy.deepcopy(current)
    removed["groups"] = removed["groups"][:1]  # delete group B
    assert admin.put("/api/admin/layout", json=removed).status_code == 409
    assert admin.get("/api/admin/layout").get_json() == current


def test_renaming_group_moves_its_people(client, admin):
    headers, user_id = take_seat(client, "A3")
    current = admin.get("/api/admin/layout").get_json()
    current["groups"][0]["label"] = "C"
    res = admin.put("/api/admin/layout", json=current)
    assert res.status_code == 200
    assert res.get_json()["moved"] == {"A3": "C3"}
    assert client.get("/api/me", headers=headers).get_json()["user"]["seat"] == "C3"


def test_swapping_group_labels(client, admin):
    h1, _ = take_seat(client, "A1", "Alice")
    h2, _ = take_seat(client, "B1", "Bob")
    current = admin.get("/api/admin/layout").get_json()
    current["groups"][0]["label"], current["groups"][1]["label"] = "B", "A"
    res = admin.put("/api/admin/layout", json=current)
    assert res.status_code == 200, res.get_json()
    assert client.get("/api/me", headers=h1).get_json()["user"]["seat"] == "B1"
    assert client.get("/api/me", headers=h2).get_json()["user"]["seat"] == "A1"


def test_paid_status_survives_rename(client, admin):
    _, user_id = take_seat(client, "A2")
    admin.post(f"/api/admin/users/{user_id}/paid", json={"paid": True})
    current = admin.get("/api/admin/layout").get_json()
    current["groups"][0]["label"] = "Z"
    admin.put("/api/admin/layout", json=current)
    user = admin.get("/api/admin/users").get_json()["users"][0]
    assert (user["seat"], user["paid"]) == ("Z2", True)


def test_admin_can_move_into_new_group(client, admin):
    _, user_id = take_seat(client, "A1")
    current = admin.get("/api/admin/layout").get_json()
    current["groups"].append(group("g-new", "C", x=14, y=2, tables=2, per_row=2))
    assert admin.put("/api/admin/layout", json=current).status_code == 200
    res = admin.post(f"/api/admin/users/{user_id}/move", json={"seat": "C4"})
    assert res.status_code == 200 and res.get_json()["user"]["seat"] == "C4"


def test_empty_layout_is_allowed(client, admin):
    assert admin.put("/api/admin/layout", json=layout()).status_code == 200
    assert client.get("/api/seats").get_json()["seats"] == []


# --- area elements (doors, windows, sleeping area, food & drinks) ---------

def item(iid="i1", kind="door", x=0, y=10, w=2, h=1, label=""):
    return {"id": iid, "type": kind, "label": label, "x": x, "y": y, "w": w, "h": h}


def test_items_default_to_empty_list():
    out = lay.validate(layout(group()))
    assert out["items"] == []


def test_validate_items():
    out = lay.validate(layout(
        group(),
        item("i1", "door", x=0, y=19, w=2, h=1),
        item("i2", "window", x=10, y=0, w=4, h=1, label="  Big window "),
        item("i3", "sleeping", x=20, y=10, w=8, h=6),
        item("i4", "food", x=20, y=2, w=4, h=2, label="Buffet"),
        item("i5", "other", x=24, y=10, w=2, h=2, label="Stage"),  # inside sleeping area: allowed
    ))
    assert [i["type"] for i in out["items"]] == ["door", "window", "sleeping", "food", "other"]
    assert out["items"][1]["label"] == "Big window"


@pytest.mark.parametrize("data,message", [
    (layout(item(kind="pool")), "type"),
    (layout(item(w=0)), "Width"),
    (layout(item(h=201)), "Height"),
    (layout(item(x=29, w=2)), "fit"),
    (layout(item(label="x" * 41)), "40 characters"),
    (layout(item("i1"), item("i1", y=5)), "unique id"),
    (layout(group("same"), item("same")), "unique id"),
    (layout(group(x=0, y=0), item(x=3, y=1, w=2, h=2)), "overlaps group A"),
    ({"area": {"width": 10, "height": 10}, "groups": [], "items": "nope"}, "list"),
    (layout(*[item(f"i{n}", y=n % 20) for n in range(101)]), "100"),
])
def test_validate_rejects_bad_items(data, message):
    with pytest.raises(lay.LayoutError, match=message):
        lay.validate(data)


def test_items_saved_and_public(client, admin):
    current = admin.get("/api/admin/layout").get_json()
    current["items"] = [item("door-1", "door", x=0, y=0), item("food-1", "food", x=18, y=2, w=4, h=2, label="Bar")]
    res = admin.put("/api/admin/layout", json=current)
    assert res.status_code == 200, res.get_json()
    public = client.get("/api/seats").get_json()["layout"]
    assert [(i["id"], i["type"], i["label"]) for i in public["items"]] == [
        ("door-1", "door", ""), ("food-1", "food", "Bar")]
    # items do not create seats
    assert client.get("/api/info").get_json()["seat_count"] == 32


def test_item_overlapping_group_rejected_by_api(admin):
    current = admin.get("/api/admin/layout").get_json()
    current["items"] = [item("s", "sleeping", x=3, y=3, w=4, h=4)]   # group A covers x 2..9, y 2..3
    res = admin.put("/api/admin/layout", json=current)
    assert res.status_code == 400 and "overlaps group A" in res.get_json()["error"]


def test_layout_saved_without_items_loads_with_empty_list(app, admin):
    with app.app_context():
        from lanparty.db import get_db
        db = get_db()
        db.execute("INSERT INTO settings (key, value) VALUES ('layout', ?)",
                   ('{"area": {"width": 10, "height": 10}, "groups": []}',))
        db.commit()
    assert admin.get("/api/admin/layout").get_json()["items"] == []
