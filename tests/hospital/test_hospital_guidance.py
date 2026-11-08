import math

import pytest
from hospital import dispatch

from klinikum import F2, FAR_ROOM_F2, RADIOLOGY, assert_turns, first_connector

GUIDANCE_KEYS = {"patient", "status", "depart_at", "eta", "appointment", "next_turns", "route", "lift_slots", "stops",
                 "say"}


def go(hospital, clock, pager_id: str) -> dict:
    waiting = dispatch.guidance(hospital, pager_id)
    clock.t = max(clock.t, waiting["depart_at"])
    result = dispatch.guidance(hospital, pager_id)
    assert result["status"] == "go"
    return result


def test_guidance_shape(hospital):
    result = dispatch.guidance(hospital, "PG-01")
    assert GUIDANCE_KEYS <= result.keys()
    assert result["patient"]["id"] == "P001"
    assert result["appointment"]["space_id"] == RADIOLOGY
    route = result["route"]
    assert {"points", "instructions", "lift_slots"} <= route.keys()
    assert len(route["points"]) >= 2
    assert route["points"][-1]["floor_id"] == hospital.bmap.spaces[RADIOLOGY]["floor_id"]
    assert isinstance(result["say"], str) and result["say"]
    assert_turns(result["next_turns"])


def test_go_immediately_when_appointment_is_close(hospital, clock):
    patient = hospital.patient_by_pager("PG-01")
    patient.appointment.start = clock.t + 60
    result = dispatch.guidance(hospital, "PG-01")
    assert result["status"] == "go"
    assert result["depart_at"] == pytest.approx(clock.t, abs=1.0)


def test_lift_patient_departs_to_meet_slot(hospital, clock):
    result = dispatch.guidance(hospital, "PG-02")
    assert result["lift_slots"]
    slot = result["lift_slots"][0]
    assert {"connector_id", "holder", "start", "end"} <= slot.keys()
    assert slot["holder"] == "P002"
    assert slot["start"] >= result["depart_at"]
    assert slot["end"] <= result["eta"]
    assert [s for s in hospital.lifts.slots() if s["holder"] == "P002"]


def test_position_on_route_advances_next_turns(hospital, clock):
    before = go(hospital, clock, "PG-01")
    assert_turns(before["next_turns"])
    assert len(before["next_turns"]) == 3
    stairs = first_connector(before["route"])
    assert stairs is not None
    point = before["route"]["points"][stairs["index"]]
    assert point["floor_id"] == F2
    patient = hospital.patient_by_pager("PG-01")
    hospital.set_position(patient.pager_tag_id, point["floor_id"], point["x"], point["y"])

    after = dispatch.guidance(hospital, "PG-01")
    assert after["status"] == "go"
    assert after["route"]["points"] == before["route"]["points"]
    assert_turns(after["next_turns"])
    assert after["next_turns"][0]["maneuver"] == stairs["maneuver"]
    assert after["next_turns"][0] == before["next_turns"][2]
    assert after["next_turns"] != before["next_turns"]


def test_off_route_position_replans_from_new_position(hospital, clock):
    before = go(hospital, clock, "PG-01")
    patient = hospital.patient_by_pager("PG-01")
    far = FAR_ROOM_F2
    x, y = hospital.bmap.anchor(far)
    start = before["route"]["points"][0]
    assert math.dist((x, y), (start["x"], start["y"])) > 10
    hospital.set_position(patient.pager_tag_id, F2, x, y)
    assert hospital.tags[patient.tag_id].space_id == far

    after = dispatch.guidance(hospital, "PG-01")
    assert after["status"] == "go"
    first = after["route"]["points"][0]
    assert first["floor_id"] == F2
    assert math.dist((x, y), (first["x"], first["y"])) <= 1.0
    assert after["route"]["points"] != before["route"]["points"]


def test_arrived_in_goal_space(hospital, clock):
    go(hospital, clock, "PG-01")
    patient = hospital.patient_by_pager("PG-01")
    x, y = hospital.bmap.anchor(RADIOLOGY)
    hospital.set_position(patient.pager_tag_id, hospital.bmap.spaces[RADIOLOGY]["floor_id"], x, y)
    result = dispatch.guidance(hospital, "PG-01")
    assert result["status"] == "arrived"
    assert result["next_turns"] == []


def test_unknown_pager_raises(hospital):
    with pytest.raises(KeyError):
        dispatch.guidance(hospital, "PG-99")
