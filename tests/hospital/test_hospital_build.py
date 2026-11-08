"""Level 4 for the hospital: draw the campus ground floor as a plan, import it like an upload, and label it by
conversation. Offline: the labels Claude would read are simulated from the drawn text."""

import json

import pytest
from shapely.geometry import Polygon

from core.building_map import BuildingMap
from core.plan_import import KIT_PX_PER_M, assemble, scan_floor
from hospital import nomenclature, state, voice
from hospital.render_plan import MARGIN_PX, TITLE_PX, label_lines, render
from hospital.state import HOSPITALS, Hospital, auto_scenario

from klinikum import API

CAMPUS = json.loads((HOSPITALS / "demo-campus" / "building.json").read_text(encoding="utf-8"))
GROUND = "demo-campus-F0"


@pytest.fixture(scope="module")
def plan(tmp_path_factory):
    ground_only = {**CAMPUS, "floors": [f for f in CAMPUS["floors"] if f["id"] == GROUND]}
    return render(ground_only, tmp_path_factory.mktemp("plans"))[0]


def truth_spaces() -> list[dict]:
    return [s for s in CAMPUS["spaces"] if s["floor_id"] == GROUND]


def simulated_labels(scan) -> dict:
    """What Claude transcribes from the label crops: the printed lines of the room under each label."""
    xs = [x for s in CAMPUS["spaces"] for x, _ in s["polygon"]]
    ys = [y for s in CAMPUS["spaces"] for _, y in s["polygon"]]

    def metres(px, py):
        return (px - MARGIN_PX) / KIT_PX_PER_M + min(xs), max(ys) - (py - MARGIN_PX - TITLE_PX) / KIT_PX_PER_M

    rooms = {}
    for index, room in enumerate(scan.rooms):
        centre = Polygon([metres(*p) for p in room["polygon"]]).representative_point()
        space = next(s for s in truth_spaces() if Polygon(s["polygon"]).contains(centre))
        width = max(x for x, _ in space["polygon"]) - min(x for x, _ in space["polygon"])
        rooms[index] = " ".join(label_lines(space, width))
    return {"title": f"{CAMPUS['building']['name']} - Ground floor", "rooms": rooms}


def door_graph(building: dict) -> set[frozenset]:
    """Room connectivity, Blueprint-Bench style: which rooms (by number, else name) a door or opening joins."""
    names = {s["id"]: s.get("ref") or s["name"] for s in building["spaces"]}
    return {frozenset(names.get(i, i) for i in o["space_ids"]) for o in building["openings"]
            if o["floor_id"].endswith("F0")}


def test_drawn_plan_imports_with_every_room_and_door(plan):
    scan = scan_floor(plan)
    assert len(scan.rooms) == len(truth_spaces())
    assert len(scan.markers) == sum(o["floor_id"] == GROUND for o in CAMPUS["openings"])


def test_import_with_labels_rebuilds_the_hospital_graph(plan):
    scan = scan_floor(plan)
    building = assemble([scan], KIT_PX_PER_M, [simulated_labels(scan)])
    renamed = nomenclature.apply_labels(building)
    assert renamed >= 25
    names = {s["name"]: s for s in building["spaces"]}
    assert names["CT"]["room_type"] == "radiology" and names["CT"]["ref"] == "A0.33"
    assert names["ED Resus"]["room_type"] == "resuscitation"
    assert names["Pharmacy"]["ref"] == "A0.11"
    truth = door_graph(CAMPUS)
    imported = door_graph(building)
    assert truth == imported  # every door joins the same two rooms as in the original

    bmap = BuildingMap(building)
    h = Hospital(HOSPITALS / "imported-test", bmap, auto_scenario(bmap))
    start, goal = names["Main entrance"]["id"], names["MRI"]["id"]
    route = h.router.route(h.router.place_for_space(start), h.router.place_for_space(goal))
    passed = [bmap.spaces[s]["name"] for s in route.spaces]
    assert "Covered walkway A-B" in passed or any("walkway" in name.lower() for name in passed), passed


def test_upload_select_and_label_by_voice(plan, tmp_path, monkeypatch):
    server = pytest.importorskip("app.server")
    hospital_api = pytest.importorskip("app.hospital_api")
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "IMPORTED", tmp_path)
    monkeypatch.setattr(state, "IMPORTED", tmp_path)
    monkeypatch.setattr(hospital_api, "IMPORTED", tmp_path)
    monkeypatch.setattr(server, "read_labels", lambda scans, client: None)  # no Claude: unnamed rooms
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(server.app) as client:
        with plan.open("rb") as image:
            summary = client.post("/api/import", files={"files": ("ground.png", image, "image/png")},
                                  data={"name": "Uploaded hospital"}).json()
        assert summary["rooms"] >= 30 and summary["entrances"] == 3
        assert client.post(f"{API}/buildings/select", json={"building_id": summary["id"]}).status_code == 200
        h = hospital_api.hospital()
        room = next(s for s in h.bmap.spaces.values() if s["type"] == "room")
        number = room["name"].removeprefix("Room ")
        body = client.post(f"{API}/pagers/PG-S01/voice", json={"text": f"{number} is the CT"}).json()
        assert body["intent"] == "label_room" and body["ok"], body
        saved = json.loads((tmp_path / summary["id"] / "building.json").read_text(encoding="utf-8"))
        ct = next(s for s in saved["spaces"] if s["id"] == room["id"])
        assert ct["name"] == "CT" and ct["room_type"] == "radiology"
        body = client.post(f"{API}/pagers/PG-S01/voice", json={"text": "Take me to the CT"}).json()
        assert body["intent"] == "go_to" and body["ok"], body
    hospital_api.set_hospital(Hospital.load(HOSPITALS / "demo-campus"))
    assert voice  # the phrase rules handled both requests
