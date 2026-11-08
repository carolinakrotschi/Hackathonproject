import math
import time

import pytest
from hospital import dispatch

from klinikum import F2, LIFT_PAGERS, assert_no_overlap

sim = pytest.importorskip("hospital.sim")

TICK_S = 2.0


def keep_only(hospital, pager_ids) -> None:
    keep = {hospital.patient_by_pager(pager_id).id for pager_id in pager_ids}
    for patient_id in list(hospital.patients):
        if patient_id not in keep:
            del hospital.patients[patient_id]


def test_simulator_thread_starts_and_stops(hospital, clock):
    simulator = sim.Simulator(hospital, factor=30.0, tick_s=0.05)
    real0 = time.monotonic()
    try:
        simulator.start()
        assert simulator.running
        assert hospital.clock is not clock
        time.sleep(0.2)
        sim_elapsed = hospital.now() - clock.t
        real_elapsed = time.monotonic() - real0
    finally:
        simulator.stop()
    assert not simulator.running
    assert simulator.thread is None
    assert sim_elapsed >= 30.0 * 0.2 - 1e-6
    assert sim_elapsed <= 30.0 * real_elapsed + 1e-6
    stopped_at = hospital.now()
    time.sleep(0.05)
    assert 0 < hospital.now() - stopped_at < 1.0


def test_simulator_walks_patient_to_appointment(hospital, clock):
    keep_only(hospital, ["PG-11"])
    patient = hospital.patient_by_pager("PG-11")
    first = dispatch.guidance(hospital, "PG-11")
    clock.t = first["depart_at"]
    simulator = sim.Simulator(hospital, tick_s=TICK_S)
    simulator.last = clock.t
    tag = hospital.tags[patient.pager_tag_id]
    start = (tag.x, tag.y)
    clock.advance(TICK_S)
    simulator.tick()
    assert math.dist(start, (tag.x, tag.y)) > 0.5
    while clock.t < patient.appointment.start and dispatch.guidance(hospital, "PG-11")["status"] != "arrived":
        clock.advance(TICK_S)
        simulator.tick()
    assert tag.space_id == patient.appointment.space_id
    assert clock.t <= patient.appointment.start - dispatch.BUFFER_S + 3 * TICK_S


def test_simulator_lift_patients_board_one_after_another(hospital, clock):
    keep_only(hospital, LIFT_PAGERS)
    for pager_id in LIFT_PAGERS:
        patient = hospital.patient_by_pager(pager_id)
        patient.step_free = True
        patient.appointment.start = clock.t + 900
    plans = {pager_id: dispatch.guidance(hospital, pager_id) for pager_id in LIFT_PAGERS}
    clock.t = min(p["depart_at"] for p in plans.values()) - TICK_S
    simulator = sim.Simulator(hospital, tick_s=TICK_S)
    simulator.last = clock.t

    slot_at_go: dict[str, dict] = {}
    left_floor: dict[str, float] = {}
    deadline = clock.t + 900
    while len(left_floor) < len(LIFT_PAGERS) and clock.t < deadline:
        clock.advance(TICK_S)
        simulator.tick()
        assert_no_overlap(hospital.lifts.slots())
        for pager_id in LIFT_PAGERS:
            patient = hospital.patient_by_pager(pager_id)
            if pager_id not in slot_at_go and patient.plan and patient.plan["status"] == "go":
                slot_at_go[pager_id] = patient.plan["lift_slots"][0]
            if pager_id not in left_floor and hospital.tags[patient.pager_tag_id].floor_id != F2:
                left_floor[pager_id] = clock.t

    assert set(left_floor) == set(LIFT_PAGERS)
    assert_no_overlap(list(slot_at_go.values()))
    for pager_id, slot in slot_at_go.items():
        assert left_floor[pager_id] >= slot["end"] - 1e-6, pager_id
    for connector_id in {slot["connector_id"] for slot in slot_at_go.values()}:
        riders = sorted((s["start"], left_floor[p]) for p, s in slot_at_go.items() if s["connector_id"] == connector_id)
        assert [arrived for _, arrived in riders] == sorted(arrived for _, arrived in riders)
    for pager_id in LIFT_PAGERS:
        patient = hospital.patient_by_pager(pager_id)
        assert left_floor[pager_id] < patient.appointment.start


def tag_positions(hospital) -> dict:
    return {p.id: (hospital.tags[p.tag_id].floor_id, hospital.tags[p.tag_id].x, hospital.tags[p.tag_id].y)
            for p in hospital.patients.values() if p.kind == "patient"}


def moved_since(hospital, before: dict) -> bool:
    return any(floor_id != before[key][0] or math.dist(before[key][1:], (x, y)) > 0.5
               for key, (floor_id, x, y) in tag_positions(hospital).items())


def run_real_seconds(hospital, clock, simulator, seconds: float, until=None) -> float:
    for tick in range(1, int(seconds / simulator.tick_s) + 1):
        clock.advance(simulator.factor * simulator.tick_s)
        simulator.tick()
        if until and until():
            return tick * simulator.tick_s
    return seconds


def test_factor_30_patients_move_within_10_real_seconds(hospital, clock):
    simulator = sim.Simulator(hospital, factor=30.0, tick_s=1.0)
    simulator.last = clock.t
    before = tag_positions(hospital)
    run_real_seconds(hospital, clock, simulator, 10.0, lambda: moved_since(hospital, before))
    assert moved_since(hospital, before), "no patient moved within 10 real seconds at factor 30"
    assert hospital.history.query(kind="patient", since=clock.t - 300.0)


def test_factor_30_lift_queue_within_60_real_seconds(hospital, clock):
    simulator = sim.Simulator(hospital, factor=30.0, tick_s=1.0)
    simulator.last = clock.t
    lift_patients = {hospital.patient_by_pager(pager_id).id for pager_id in LIFT_PAGERS}

    def queued() -> bool:
        by_lift: dict[str, set] = {}
        for slot in hospital.lifts.slots():
            if slot["holder"] in lift_patients:
                by_lift.setdefault(slot["connector_id"], set()).add(slot["holder"])
        return any(len(holders) >= 2 for holders in by_lift.values())

    run_real_seconds(hospital, clock, simulator, 60.0, queued)
    assert queued()


def test_sim_api_moves_patients_with_fast_clock(client, hospital, clock):
    before = tag_positions(hospital)
    started = client.post("/api/hospital/sim/start", json={"factor": 600})
    assert started.status_code == 200 and started.json()["factor"] == 600
    deadline = time.monotonic() + 5.0
    moved = False
    while not moved and time.monotonic() < deadline:
        time.sleep(0.25)
        rows = {row["tag_id"]: row for row in client.get("/api/hospital/tracking", params={"kind": "patient"}).json()}
        moved = any(math.dist(before[p.id][1:], (rows[p.tag_id]["x"], rows[p.tag_id]["y"])) > 0.5
                    for p in hospital.patients.values() if p.kind == "patient")
    client.post("/api/hospital/sim/stop")
    assert moved
    assert hospital.now() - clock.t > 60.0
