import pytest
from hospital import dispatch
from hospital.lifts import LiftScheduler

from klinikum import F0, F2, LIFT_PAGERS, LIFTS, STAIRS, assert_no_overlap


def variant(result: dict) -> str:
    return result["choice"] or ""


def test_demo_lift_case_is_set_up(hospital, scenario):
    assert tuple(scenario["demo_cases"]["lift"]["pager_ids"]) == LIFT_PAGERS
    starts = []
    for pager_id in LIFT_PAGERS:
        patient = hospital.patient_by_pager(pager_id)
        assert hospital.bmap.spaces[patient.room_id]["floor_id"] == F2
        assert hospital.bmap.spaces[patient.appointment.space_id]["floor_id"] == F0
        starts.append(patient.appointment.start)
    assert max(starts) - min(starts) <= 120


def test_demo_lift_case_no_overlapping_slots(hospital):
    results = {pager_id: dispatch.guidance(hospital, pager_id) for pager_id in LIFT_PAGERS}
    slots = hospital.lifts.slots()
    assert slots
    assert_no_overlap(slots)
    for pager_id, result in results.items():
        holder = hospital.patient_by_pager(pager_id).id
        assert [(s["connector_id"], s["start"]) for s in slots if s["holder"] == holder] == \
            [(s["connector_id"], s["start"]) for s in result["lift_slots"]]
        if not hospital.patient_by_pager(pager_id).step_free:
            continue
        assert "stairs" not in variant(result)
        assert result["lift_slots"]
    assert len({variant(r) for r in results.values()}) >= 2


def test_same_departure_all_step_free_spreads_over_lifts(hospital, clock):
    for pager_id in LIFT_PAGERS:
        patient = hospital.patient_by_pager(pager_id)
        patient.step_free = True
        patient.appointment.start = clock.t + 900
    results = [dispatch.guidance(hospital, pager_id) for pager_id in LIFT_PAGERS]
    slots = hospital.lifts.slots()
    assert len(slots) == len(LIFT_PAGERS)
    assert_no_overlap(slots)
    assert {s["connector_id"] for s in slots} == set(LIFTS)
    for result in results:
        assert "stairs" not in variant(result)
        assert result["eta"] <= result["appointment"]["start"] - dispatch.BUFFER_S + 1.0


def test_same_departure_mixed_patients_no_overlap(hospital, clock):
    for pager_id in LIFT_PAGERS:
        hospital.patient_by_pager(pager_id).appointment.start = clock.t + 900
    results = [dispatch.guidance(hospital, pager_id) for pager_id in LIFT_PAGERS]
    assert_no_overlap(hospital.lifts.slots())
    assert len({variant(r) for r in results}) >= 2


def test_busy_lift_switches_to_other_lift(hospital, clock):
    alone = dispatch.guidance(hospital, "PG-02")
    used = alone["lift_slots"][0]["connector_id"]
    other = next(cid for cid in LIFTS if cid != used)
    patient = hospital.patient_by_pager("PG-02")
    patient.plan = None
    hospital.lifts.release(patient.id)
    hospital.lifts.reserve(used, "blocker", clock.t, 3600.0, F2, F0)

    result = dispatch.guidance(hospital, "PG-02")
    assert [s["connector_id"] for s in result["lift_slots"]] == [other]
    assert_no_overlap(hospital.lifts.slots())


def test_both_lifts_busy_takes_stairs_unless_step_free(hospital, clock):
    for connector_id in LIFTS:
        hospital.lifts.reserve(connector_id, "blocker", clock.t, 3600.0, F2, F0)
    patient = hospital.patient_by_pager("PG-02")

    patient.step_free = False
    walker = dispatch.guidance(hospital, "PG-02")
    assert walker["lift_slots"] == []
    assert STAIRS in [step.get("connector") for step in walker["route"]["instructions"]]

    patient.step_free = True
    patient.plan = None
    rider = dispatch.guidance(hospital, "PG-02")
    assert rider["lift_slots"]
    assert rider["lift_slots"][0]["start"] >= clock.t + 3600.0 - 1e-6
    assert STAIRS not in [step.get("connector") for step in rider["route"]["instructions"]]
    assert_no_overlap(hospital.lifts.slots())


def test_scheduler_earliest_and_reserve(hospital):
    lifts = LiftScheduler(hospital.bmap)
    assert set(lifts.lifts()) == set(LIFTS)
    duration = lifts.trip_seconds(LIFTS[0], F2, F0)
    assert duration > 0
    lifts.reserve(LIFTS[0], "a", 100.0, duration, F2, F0)
    assert lifts.earliest(LIFTS[0], 100.0, duration) == pytest.approx(100.0 + duration)
    assert lifts.earliest(LIFTS[1], 100.0, duration) == pytest.approx(100.0)
    with pytest.raises(ValueError):
        lifts.reserve(LIFTS[0], "b", 100.0 + duration / 2, duration, F2, F0)
    lifts.release("a")
    assert lifts.slots() == []
