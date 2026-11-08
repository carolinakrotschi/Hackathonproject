import pytest
from hospital import finder
from shapely.geometry import Point, Polygon

from klinikum import COFFEE_START, ENTRANCE, ER

STARTS = [COFFEE_START, ER, "demo-klinikum-F1-S005", ENTRANCE]


def walking_costs(hospital, start: str, asset_type: str) -> dict[str, float]:
    router = hospital.router
    begin = router.place_for_space(start)
    costs = {}
    for asset in hospital.bmap.assets.values():
        if asset["type"] != asset_type or asset["id"] in hospital.in_use:
            continue
        try:
            costs[asset["id"]] = router.route(begin, router.place_for_asset(asset["id"])).cost_m
        except ValueError:
            continue
    return costs


@pytest.mark.parametrize("start", STARTS)
def test_nearest_pump_is_minimal_walking_cost(hospital, start):
    result = finder.nearest_asset(hospital, "infusion_pump", start)
    costs = walking_costs(hospital, start, "infusion_pump")
    assert result["asset"]["id"] in costs
    assert result["asset"]["id"] not in hospital.in_use
    assert result["route"]["cost_m"] == pytest.approx(min(costs.values()), abs=0.05)
    assert result["asset"]["type"] == "infusion_pump"
    assert {"walk_m", "minutes", "route", "say"} <= result.keys()
    last = result["route"]["points"][-1]
    assert last["floor_id"] == result["asset"]["floor_id"]


def test_in_use_pumps_are_skipped(hospital):
    start = COFFEE_START
    first = finder.nearest_asset(hospital, "infusion_pump", start)
    assert finder.set_in_use(hospital, first["asset"]["id"], True) == {"id": first["asset"]["id"], "in_use": True}
    second = finder.nearest_asset(hospital, "infusion_pump", start)
    assert second["asset"]["id"] != first["asset"]["id"]
    assert second["route"]["cost_m"] >= first["route"]["cost_m"] - 1e-6
    finder.set_in_use(hospital, first["asset"]["id"], False)
    assert finder.nearest_asset(hospital, "infusion_pump", start)["asset"]["id"] == first["asset"]["id"]


def test_scenario_in_use_pump_never_returned(hospital):
    for start in STARTS:
        assert finder.nearest_asset(hospital, "infusion_pump", start)["asset"]["id"] not in hospital.in_use


def test_no_free_pump_raises(hospital):
    for asset in hospital.bmap.assets.values():
        if asset["type"] == "infusion_pump":
            hospital.in_use.add(asset["id"])
    with pytest.raises(LookupError):
        finder.nearest_asset(hospital, "infusion_pump", ER)


def test_unknown_start_raises_key_error(hospital):
    with pytest.raises(KeyError):
        finder.nearest_asset(hospital, "infusion_pump", "no-such-room")


def test_visit_with_right_code(hospital):
    patient = hospital.patients["P001"]
    result = finder.visit(hospital, patient.number, patient.visit_code)
    assert result is not None
    room = hospital.bmap.spaces[patient.room_id]
    assert result["patient"]["room"]["id"] == patient.room_id
    assert result["patient"]["ward"] == patient.ward
    assert result["patient"]["floor"]["id"] == room["floor_id"]
    points = result["route"]["points"]
    entrance = Polygon(hospital.bmap.spaces[ENTRANCE]["polygon"])
    assert points[0]["floor_id"] == hospital.bmap.spaces[ENTRANCE]["floor_id"]
    assert entrance.buffer(0.5).contains(Point(points[0]["x"], points[0]["y"]))
    assert points[-1]["floor_id"] == room["floor_id"]
    assert Polygon(room["polygon"]).buffer(0.5).contains(Point(points[-1]["x"], points[-1]["y"]))
    assert result["visiting_hours"] == hospital.visiting_hours
    assert result["say"]


def test_visit_by_name(hospital):
    patient = hospital.patients["P001"]
    result = finder.visit(hospital, patient.name.lower(), patient.visit_code)
    assert result is not None and result["patient"]["number"] == patient.number


def test_visit_wrong_code_and_unknown_patient_are_none(hospital):
    patient = hospital.patients["P001"]
    assert finder.visit(hospital, patient.number, "0000") is None
    assert finder.visit(hospital, patient.number, "") is None
    assert finder.visit(hospital, "999999", patient.visit_code) is None
    assert finder.visit(hospital, "", patient.visit_code) is None
