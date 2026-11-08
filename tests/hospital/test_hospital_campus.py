"""The staff-pager campus: two buildings, walkway and bridge, hospital room names, and the pager's tools and phrase
rules on it (levels 1-4 of the challenge, offline: no Anthropic key needed)."""

import json

import pytest
from hospital import pager_tools, voice
from hospital.campus import build
from hospital.pager_tools import PagerError
from hospital.state import HOSPITALS, Hospital

from klinikum import API, FakeClock

CAMPUS = HOSPITALS / "demo-campus"
SCENARIO = json.loads((CAMPUS / "scenario.json").read_text(encoding="utf-8"))
IDS = SCENARIO["ids"]
PAGER = IDS["staff_pagers"][0]  # Dr. Weber, starts in radiology (Building A, ground floor)


@pytest.fixture
def campus() -> Hospital:
    return Hospital.load(CAMPUS, clock=FakeClock())


@pytest.fixture
def campus_client(campus):
    hospital_api = pytest.importorskip("app.hospital_api")
    server = pytest.importorskip("app.server")
    from fastapi.testclient import TestClient

    hospital_api.set_hospital(campus)
    try:
        with TestClient(server.app) as client:
            yield client
    finally:
        hospital_api.stop_sim()


def names_on_route(h: Hospital, start: str, goal: str, step_free: bool = False) -> list[str]:
    route = h.router.route(h.router.place_for_space(start), h.router.place_for_space(goal), step_free)
    names: list[str] = []
    for space_id in route.spaces:
        name = h.bmap.spaces[space_id]["name"]
        if not names or names[-1] != name:
            names.append(name)
    return names


def test_generated_files_are_current():
    building, scenario = build()
    assert json.loads((CAMPUS / "building.json").read_text(encoding="utf-8")) == json.loads(json.dumps(building))
    assert json.loads((CAMPUS / "scenario.json").read_text(encoding="utf-8")) == json.loads(json.dumps(scenario))


def test_two_buildings_staff_only(campus):
    assert {b["id"] for b in campus.bmap.data["buildings"]} == {"A", "B"}
    assert {s.get("building") for s in campus.bmap.spaces.values()} == {"A", "B", None}
    assert campus.scenario["patients"] == []
    assert all(p.kind == "staff" for p in campus.patients.values())
    assert len(IDS["staff_pagers"]) >= 3


def test_hospital_names_and_numbers(campus):
    spaces = {s["name"]: s for s in campus.bmap.spaces.values()}
    for name in ("Emergency Department (ED)", "ED Resus", "CT", "MRI", "Theatre 1", "Recovery",
                 "Dirty utility 2A", "Nurses' station 1A", "Room A2.07"):
        assert name in spaces, name
    assert spaces["CT"]["ref"] == "A0.33" and spaces["Theatre 1"]["ref"] == "B1.01"


def test_routes_change_building_by_walkway_bridge_lift_and_stairs(campus):
    ed, theatre = IDS["emergency"], IDS["theatre_1"]
    walking = names_on_route(campus, ed, theatre)
    assert "Covered walkway A–B" in walking and "Stairs B" in walking
    step_free = names_on_route(campus, ed, theatre, step_free=True)
    assert "Bridge A–B" in step_free and "Lift A" in step_free and "Stairs B" not in step_free
    campus.bmap.closed.add(IDS["bridge"])
    detour = names_on_route(campus, ed, theatre, step_free=True)
    assert "Covered walkway A–B" in detour and "Lift C" in detour
    campus.bmap.closed.add(IDS["walkway"])
    with pytest.raises(ValueError):
        names_on_route(campus, ed, theatre, step_free=True)


# ---------- pager tools (what the agent calls) ----------

def test_find_shows_without_navigating(campus):
    result = pager_tools.find(campus, PAGER, "MRI")
    assert "MRI" in result["say"] and "Building B" in result["say"]
    assert IDS["mri"] in result["view"]["spaces"]
    assert campus.patient_by_pager(PAGER).destination is None


def test_go_to_step_free_via_stop_across_buildings(campus):
    result = pager_tools.go_to(campus, PAGER, "Theatre 1", step_free=True, via=["Pharmacy"])
    assert result["say"].startswith("To Theatre 1, floor 1, Building B via Pharmacy, step-free")
    holder = campus.patient_by_pager(PAGER)
    assert holder.step_free and holder.destination["space_id"] == IDS["theatre_1"]
    assert "Stairs" not in (result["choice"] or "")


def test_close_lift_replans_and_reopen(campus):
    pager_tools.go_to(campus, PAGER, "Theatre 1", step_free=True)
    closed = pager_tools.set_closed(campus, PAGER, "Bridge A–B", True)
    assert "Bridge A–B closed" in closed["say"]
    assert IDS["bridge"] in campus.bmap.closed
    pager_tools.set_closed(campus, PAGER, "bridge a–b", False)
    assert IDS["bridge"] not in campus.bmap.closed
    pager_tools.set_closed(campus, PAGER, "Lift C", True)
    assert IDS["lifts"][2] in campus.bmap.closed


def test_label_room_infers_hospital_type(campus):
    result = pager_tools.label_room(campus, "B0.11", "Plaster room")
    assert "Plaster room" in result["say"]
    result = pager_tools.label_room(campus, "B0.14", "Sluice B0")
    space = campus.bmap.spaces[result["view"]["spaces"][0]]
    assert space["name"] == "Sluice B0" and space["room_type"] == "utility" and space["ref"] == "B0.14"


def test_find_free_equipment_and_move(campus):
    result = pager_tools.find_free(campus, PAGER, "defibrillator")
    assert result["view"]["assets"]
    moved = pager_tools.move_equipment(campus, "Ultrasound unit", "ED Bay 2")
    assert "now in ED Bay 2" in moved["say"]
    with pytest.raises(PagerError):
        pager_tools.move_equipment(campus, "CT scanner", "ED Bay 2")  # fixed


def test_unknown_place_is_said_plainly(campus):
    with pytest.raises(PagerError, match="can't find"):
        pager_tools.go_to(campus, PAGER, "Helipad")


# ---------- phrase rules (offline voice) ----------

@pytest.mark.parametrize("text,intent", [
    ("Where is the MRI?", "find"),
    ("Take me to theatre 1 with a bed", "go_to"),
    ("Take me to CT via the pharmacy", "go_to"),
    ("Lift A is out of order", "set_closed"),
    ("Lift A is working again", "set_closed"),
    ("The bridge A–B is closed", "set_closed"),
    ("Add a door between A0.11 and A0.12", "add_door"),
    ("B0.11 is the plaster room", "label_room"),
    ("Label A0.15 as Bed management office", "label_room"),
    ("The ultrasound unit is now in ED Bay 2", "move_equipment"),
    ("Find the nearest free wheelchair", "find"),
    ("Where am I?", "where"),
])
def test_phrase_rules(campus, text, intent):
    result = voice.handle(campus, PAGER, text)
    assert result["intent"] == intent, result
    assert result["ok"], result


def test_bed_route_is_step_free_and_via_is_a_stop(campus):
    voice.handle(campus, PAGER, "Take me to CT via the pharmacy")
    holder = campus.patient_by_pager(PAGER)
    pharmacy = next(s["id"] for s in campus.bmap.spaces.values() if s["name"] == "Pharmacy")
    assert [s["id"] for s in holder.stops] == [pharmacy]
    assert holder.destination["name"] == "CT" and not holder.step_free
    voice.handle(campus, PAGER, "Take me to theatre 1 with a bed")
    assert campus.patient_by_pager(PAGER).step_free


def test_voice_endpoint_without_key_uses_rules_and_returns_view(campus_client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    body = campus_client.post(f"{API}/pagers/{PAGER}/voice", json={"text": "Where is the MRI?"}).json()
    assert body["intent"] == "find" and body["view"]["spaces"] == [IDS["mri"]]


def test_agent_tools_run_under_lock_and_collect_the_view(campus):
    anthropic = pytest.importorskip("anthropic")
    from anthropic.lib.tools import ToolError
    from hospital.agent import PagerAgent

    session = PagerAgent(campus, PAGER, anthropic.Anthropic(api_key="test-key"))  # no request is sent
    tools = {tool.name: tool for tool in session.tools}
    assert set(tools) >= {"find", "go_to", "find_free", "set_closed", "add_door", "label_room", "move_equipment"}
    assert "\n\n" not in tools["go_to"].description
    reply = json.loads(tools["go_to"].call({"place": "MRI", "step_free": True}))
    assert reply["say"].startswith("To MRI")
    assert IDS["mri"] in session.view["spaces"]
    with pytest.raises(ToolError):
        tools["go_to"].call({"place": "Helipad"})
    assert "Building B" in session.context() or "Building A" in session.context()
