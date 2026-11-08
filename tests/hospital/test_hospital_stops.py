import itertools
import re

import pytest
from hospital import dispatch, stops

from klinikum import CAFE, F0, F2, LIFTS, PHARMACY, RADIOLOGY, TOILET_F2

PAGER = "PG-01"


def coffee_case(scenario) -> dict:
    case = scenario["demo_cases"]["coffee"]
    assert case["pager_id"] == PAGER
    return case


def cost(hospital, a, b, step_free: bool) -> float:
    return hospital.router.route(a, b, step_free).cost_m


def test_coffee_picks_min_detour_not_nearest(hospital, scenario):
    case = coffee_case(scenario)
    result = stops.set_stops(hospital, PAGER, [{"category": "coffee"}], dry_run=True)
    assert [s["id"] for s in result["stops"]] == [case["best_detour"]]
    assert result["stops"][0]["category"] == "coffee"
    assert case["nearest_to_start"] != case["best_detour"]


def test_coffee_case_is_real(hospital, scenario):
    case = coffee_case(scenario)
    patient = hospital.patient_by_pager(PAGER)
    assert patient.room_id == case["start"]
    assert patient.appointment.space_id == case["goal"] == RADIOLOGY
    start = hospital.place_of(patient)
    goal = hospital.router.place_for_space(RADIOLOGY)
    options = stops.candidates(hospital, "coffee", 180)
    ids = {c["id"] for c in options}
    assert {case["nearest_to_start"], case["best_detour"]} <= ids
    to_stop = {c["id"]: cost(hospital, start, c["place"], patient.step_free) for c in options}
    total = {c["id"]: to_stop[c["id"]] + cost(hospital, c["place"], goal, patient.step_free) for c in options}
    assert min(to_stop, key=to_stop.get) == case["nearest_to_start"]
    assert min(total, key=total.get) == case["best_detour"]
    assert total[case["best_detour"]] < total[case["nearest_to_start"]]


def test_unordered_stops_minimise_total(hospital):
    patient = hospital.patient_by_pager(PAGER)
    start = hospital.place_of(patient)
    goal = hospital.router.place_for_space(RADIOLOGY)
    wanted = [{"id": CAFE}, {"id": TOILET_F2}, {"id": PHARMACY}]
    chosen = stops.resolve_stops(hospital, start, goal, wanted, ordered=False, step_free=patient.step_free)
    assert sorted(c["id"] for c in chosen) == sorted(s["id"] for s in wanted)
    places = {c["id"]: c["place"] for c in chosen}

    def total(order) -> float:
        route = [start, *(places[i] for i in order), goal]
        return sum(cost(hospital, a, b, patient.step_free) for a, b in itertools.pairwise(route))

    best = min(total(order) for order in itertools.permutations(places))
    assert total([c["id"] for c in chosen]) == pytest.approx(best, abs=0.05)
    assert chosen[0]["id"] == TOILET_F2


def test_ordered_stops_keep_given_order(hospital):
    wanted = [{"id": CAFE}, {"id": TOILET_F2}]
    ordered = stops.set_stops(hospital, PAGER, wanted, ordered=True, dry_run=True)
    unordered = stops.set_stops(hospital, PAGER, wanted, ordered=False, dry_run=True)
    assert [s["id"] for s in ordered["stops"]] == [CAFE, TOILET_F2]
    assert [s["id"] for s in unordered["stops"]] == [TOILET_F2, CAFE]
    assert unordered["route"]["cost_m"] <= ordered["route"]["cost_m"] + 1e-6
    waypoints = [step for step in ordered["route"]["instructions"] if step["maneuver"] == "waypoint"]
    assert len(waypoints) == 2


def test_ordered_categories_keep_given_order(hospital):
    wanted = [{"category": "pharmacy"}, {"category": "toilet"}]
    ordered = stops.set_stops(hospital, PAGER, wanted, ordered=True, dry_run=True)
    unordered = stops.set_stops(hospital, PAGER, wanted, dry_run=True)
    assert [s["category"] for s in ordered["stops"]] == ["pharmacy", "toilet"]
    assert unordered["route"]["cost_m"] <= ordered["route"]["cost_m"] + 1e-6


def test_too_many_stops_rejected(hospital):
    with pytest.raises(ValueError):
        stops.set_stops(hospital, PAGER, [{"category": "toilet"}] * (stops.MAX_STOPS + 1), dry_run=True)


def test_unknown_category_rejected(hospital):
    with pytest.raises(ValueError):
        stops.set_stops(hospital, PAGER, [{"category": "sauna"}], dry_run=True)


def test_coffee_fits_with_margin(hospital, clock):
    patient = hospital.patient_by_pager(PAGER)
    result = stops.set_stops(hospital, PAGER, [{"category": "coffee"}], dry_run=True)
    assert {"stops", "detour_min", "arrival_margin_min", "fits", "route", "say"} <= result.keys()
    assert result["detour_min"] > 0
    assert result["fits"] is True
    assert result["arrival_margin_min"] > 0
    leave_now_arrival = clock.t + result["route"]["seconds"]
    assert result["arrival_margin_min"] == pytest.approx((patient.appointment.start - leave_now_arrival) / 60, abs=0.2)
    assert "Coffee" in result["say"]
    assert "fits" in result["say"]
    said = re.search(r"(\d+) min before your appointment", result["say"])
    assert said and abs(int(said.group(1)) - result["arrival_margin_min"]) <= 1
    assert patient.stops == []


def test_coffee_too_late(hospital, clock):
    patient = hospital.patient_by_pager(PAGER)
    patient.appointment.start = clock.t + 240
    result = stops.set_stops(hospital, PAGER, [{"category": "coffee"}], dry_run=True)
    assert result["fits"] is False
    assert result["arrival_margin_min"] < 0
    assert "Coffee costs" in result["say"]
    assert "late" in result["say"]
    said = re.search(r"(\d+) min late", result["say"])
    assert said and abs(int(said.group(1)) + result["arrival_margin_min"]) <= 1


def test_lift_slots_count_in_margin(hospital, clock):
    patient = hospital.patient_by_pager("PG-02")
    blocked = 1800.0
    for connector_id in LIFTS:
        hospital.lifts.reserve(connector_id, "blocker", clock.t, blocked, F2, F0)
    result = stops.set_stops(hospital, "PG-02", [{"category": "coffee"}], dry_run=True)
    assert result["fits"] is False
    assert result["arrival_margin_min"] <= (patient.appointment.start - clock.t - blocked) / 60


def test_guidance_uses_stops(hospital, clock, scenario):
    case = coffee_case(scenario)
    without = dispatch.guidance(hospital, PAGER)
    assert without["status"] == "go"
    stops.set_stops(hospital, PAGER, [{"category": "coffee"}])
    patient = hospital.patient_by_pager(PAGER)
    assert [s["id"] for s in patient.stops] == [case["best_detour"]]

    with_coffee = dispatch.guidance(hospital, PAGER)
    assert [s["id"] for s in with_coffee["stops"]] == [case["best_detour"]]
    maneuvers = [step["maneuver"] for step in with_coffee["route"]["instructions"]]
    assert "waypoint" in maneuvers
    assert with_coffee["route"]["points"] != without["route"]["points"]

    going = with_coffee
    stairs = next(step for step in going["route"]["instructions"] if "connector" in step)
    landing = going["route"]["points"][stairs["index"] + 1]
    hospital.set_position(patient.pager_tag_id, landing["floor_id"], landing["x"], landing["y"])
    near_stop = dispatch.guidance(hospital, PAGER)
    turns = near_stop["next_turns"]
    assert any(turn["maneuver"] == "waypoint" for turn in turns)
    waypoint = next(turn for turn in turns if turn["maneuver"] == "waypoint")
    assert "Coffee" in waypoint["text"]


def test_clear_stops(hospital):
    stops.set_stops(hospital, PAGER, [{"category": "coffee"}])
    stops.clear_stops(hospital, PAGER)
    patient = hospital.patient_by_pager(PAGER)
    assert patient.stops == []
    assert dispatch.guidance(hospital, PAGER)["stops"] == []
