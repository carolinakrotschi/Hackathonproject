"""Evaluator: five patients heading to the same upper floor at the same time share lifts without overlap."""
import pytest
from hospital import dispatch

from klinikum import ENTRANCE, F0, F2, LIFT_PAGERS, STAIRS, assert_no_overlap


@pytest.mark.parametrize("all_step_free", [False, True])
def test_five_patients_up_to_same_upper_floor_no_overlapping_lift_slots(hospital, clock, all_step_free):
    x, y = hospital.bmap.anchor(ENTRANCE)
    for pager_id in LIFT_PAGERS:
        patient = hospital.patient_by_pager(pager_id)
        hospital.set_position(patient.pager_tag_id, F0, x, y)
        patient.appointment.space_id = patient.room_id
        assert hospital.bmap.spaces[patient.room_id]["floor_id"] == F2
        patient.appointment.start = clock.t + 900
        if all_step_free:
            patient.step_free = True
    results = {pager_id: dispatch.guidance(hospital, pager_id) for pager_id in LIFT_PAGERS}
    slots = hospital.lifts.slots()
    assert_no_overlap(slots)
    choices = {r["choice"] or "" for r in results.values()}
    assert len(choices) >= 2, choices
    for pager_id, result in results.items():
        patient = hospital.patient_by_pager(pager_id)
        connectors = [step.get("connector") for step in result["route"]["instructions"]]
        assert result["route"]["points"][0]["floor_id"] == F0
        assert result["route"]["points"][-1]["floor_id"] == F2
        if patient.step_free:
            assert STAIRS not in connectors
            assert result["lift_slots"]
        assert result["eta"] <= patient.appointment.start - dispatch.BUFFER_S + 1.0
