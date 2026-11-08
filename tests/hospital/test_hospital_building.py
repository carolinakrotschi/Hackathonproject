from collections import Counter

import pytest
from core.building_map import BuildingMap
from core.walk import WalkRouter
from shapely.geometry import Point, Polygon

from klinikum import DEMO, ENTRANCE, ER, LIFTS, RADIOLOGY, STAIRS


@pytest.fixture(scope="module")
def bmap() -> BuildingMap:
    return BuildingMap.load(DEMO)


def test_building_loads_with_three_floors(bmap):
    assert len(bmap.floors) >= 3
    assert len({floor["level"] for floor in bmap.floors.values()}) >= 3


def test_required_room_types(bmap):
    room_types = Counter(space.get("room_type") for space in bmap.spaces.values())
    for required in ("lobby", "emergency", "radiology", "ward", "patient_room", "nurse_station", "toilet", "cafe",
                     "triage"):
        assert room_types[required] >= 1, required
    assert bmap.spaces[ENTRANCE]["room_type"] == "lobby"
    assert bmap.spaces[ER]["room_type"] == "emergency"
    assert bmap.spaces[RADIOLOGY]["room_type"] == "radiology"
    upper = {space["floor_id"] for space in bmap.spaces.values() if space.get("room_type") == "patient_room"}
    assert all(bmap.floors[floor_id]["level"] > 0 for floor_id in upper)


def test_entrance_opens_to_outside(bmap):
    assert any(ENTRANCE in o["space_ids"] and "outside" in o["space_ids"] for o in bmap.openings.values())


def test_lifts_and_stairs(bmap):
    lifts = [c for c in bmap.connectors.values() if c["kind"] == "lift" and c["step_free"]]
    assert len(lifts) >= 2
    assert {c["id"] for c in lifts} >= set(LIFTS)
    assert bmap.connectors[STAIRS]["kind"] != "lift"
    assert not bmap.connectors[STAIRS]["step_free"]
    all_floors = set(bmap.floors)
    for connector_id in (*LIFTS, STAIRS):
        assert set(bmap.connectors[connector_id]["floor_ids"]) == all_floors


def test_required_assets(bmap):
    types = Counter(asset["type"] for asset in bmap.assets.values())
    assert types["infusion_pump"] >= 6
    for required in ("bed", "stretcher", "wheelchair", "chair", "cabinet"):
        assert types[required] >= 1, required
    assert types["coffee_machine"] + types["vending_machine"] >= 1
    pump_floors = {a["floor_id"] for a in bmap.assets.values() if a["type"] == "infusion_pump"}
    assert len(pump_floors) >= 2
    assert all(a.get("movable") is False for a in bmap.assets.values() if a["type"] == "cabinet")
    for asset in bmap.assets.values():
        assert asset["space_id"] in bmap.spaces
        assert asset["floor_id"] == bmap.spaces[asset["space_id"]]["floor_id"]
        assert asset.get("footprint"), asset["id"]


def test_every_space_reachable_from_entrance(bmap):
    router = WalkRouter(bmap)
    start = router.place_for_space(ENTRANCE)
    entrance_shape = Polygon(bmap.spaces[ENTRANCE]["polygon"])
    for space_id, space in bmap.spaces.items():
        walk = router.route(start, router.place_for_space(space_id))
        assert walk.spaces[0] == ENTRANCE, space_id
        assert walk.spaces[-1] == space_id, space_id
        first, last = walk.points[0], walk.points[-1]
        assert entrance_shape.buffer(0.5).contains(Point(first["x"], first["y"]))
        assert last["floor_id"] == space["floor_id"], space_id
        assert Polygon(space["polygon"]).buffer(0.5).contains(Point(last["x"], last["y"])), space_id


def test_every_space_step_free_reachable(bmap):
    router = WalkRouter(bmap)
    start = router.place_for_space(ENTRANCE)
    for space_id in bmap.spaces:
        walk = router.route(start, router.place_for_space(space_id), step_free=True)
        assert walk.spaces[-1] == space_id


def test_hospital_scenario_consistent(hospital, scenario):
    assert hospital.entrance_id == ENTRANCE
    for patient in hospital.patients.values():
        assert patient.room_id in hospital.bmap.spaces
        if patient.appointment:
            assert patient.appointment.space_id in hospital.bmap.spaces
    for beacon in hospital.beacons.values():
        assert beacon["floor_id"] in hospital.bmap.floors
        assert beacon["space_id"] in hospital.bmap.spaces
    assert hospital.in_use <= set(hospital.bmap.assets)
    pumps = {a["id"] for a in hospital.bmap.assets.values() if a["type"] == "infusion_pump"}
    assert hospital.in_use & pumps
    assert pumps - hospital.in_use
    cases = scenario["demo_cases"]
    assert cases["coffee"]["nearest_to_start"] != cases["coffee"]["best_detour"]
    assert len(cases["lift"]["pager_ids"]) >= 5
