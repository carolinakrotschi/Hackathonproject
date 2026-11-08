import math

import pytest

from klinikum import (CAFE, COFFEE_START, ENTRANCE, ER, F0, F1, LIFT_PAGERS, NURSE_F1, RADIOLOGY, TOILET_F2,
                      assert_no_overlap, assert_turns)

hospital_api = pytest.importorskip("app.hospital_api")

API = "/api/hospital"
FORBIDDEN = {"detail": "Not found or wrong code"}


@pytest.mark.parametrize("path", ["/hospital", "/hospital/pager/PG-01", "/hospital/visit"])
def test_pages(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_map(client, hospital):
    response = client.get(f"{API}/map")
    assert response.status_code == 200
    body = response.json()
    assert {"floors", "spaces", "assets", "beacons", "patients", "stop_categories"} <= body.keys()
    assert len(body["floors"]) >= 3
    assert len(body["beacons"]) == len(hospital.beacons)
    assert {"id", "number", "name", "pager_id", "room_id"} <= body["patients"][0].keys()
    assert {p["pager_id"] for p in body["patients"]} >= {"PG-01", *LIFT_PAGERS}
    assert "coffee" in body["stop_categories"]


def test_guidance_goes_now(client, clock, hospital):
    response = client.get(f"{API}/pagers/PG-01/guidance")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "go"
    assert {"eta", "next_turns", "route", "lift_slots", "say"} <= body.keys()
    assert {"points", "instructions"} <= body["route"].keys()
    assert_turns(body["next_turns"])
    assert body["say"]


def test_unknown_pager_404(client):
    assert client.get(f"{API}/pagers/PG-99/guidance").status_code == 404
    assert client.post(f"{API}/pagers/PG-99/position", json={"space_id": ENTRANCE}).status_code == 404
    assert client.post(f"{API}/pagers/PG-99/stops", json={"stops": [{"category": "coffee"}]}).status_code == 404
    assert client.delete(f"{API}/pagers/PG-99/stops").status_code == 404


def test_position_advances_next_turns(client, clock):
    clock.t = client.get(f"{API}/pagers/PG-01/guidance").json()["depart_at"]
    before = client.get(f"{API}/pagers/PG-01/guidance").json()
    assert before["status"] == "go" and len(before["next_turns"]) == 3
    stairs = next(step for step in before["route"]["instructions"] if "connector" in step)
    point = before["route"]["points"][stairs["index"]]

    response = client.post(f"{API}/pagers/PG-01/position",
                           json={"floor_id": point["floor_id"], "x": point["x"], "y": point["y"]})
    assert response.status_code == 200
    after = client.get(f"{API}/pagers/PG-01/guidance").json()
    assert after["next_turns"][0] == before["next_turns"][2]
    assert after["route"]["points"] == before["route"]["points"]


def test_position_by_space_replans_from_there(client, clock):
    clock.t = client.get(f"{API}/pagers/PG-01/guidance").json()["depart_at"]
    client.get(f"{API}/pagers/PG-01/guidance")
    response = client.post(f"{API}/pagers/PG-01/position", json={"space_id": ENTRANCE})
    assert response.status_code == 200
    after = client.get(f"{API}/pagers/PG-01/guidance").json()
    first = after["route"]["points"][0]
    assert first["floor_id"] == F0
    assert after["route"]["points"][-1]["floor_id"] == F0
    assert not any("connector" in step for step in after["route"]["instructions"])


def test_position_bad_body(client):
    assert client.post(f"{API}/pagers/PG-01/position", json={}).status_code == 422
    assert client.post(f"{API}/pagers/PG-01/position", json={"space_id": "nope"}).status_code == 404


def test_sightings_and_tracking(client, hospital):
    target = hospital.bmap.anchor(NURSE_F1)
    near = sorted((b for b in hospital.beacons.values() if b["floor_id"] == F1),
                  key=lambda b: math.dist(target, (b["x"], b["y"])))[:3]
    rssi = [-58.0, -63.0, -70.0]
    payload = [{"tag_id": "T-S04", "beacon_id": b["id"], "rssi": r} for b, r in zip(near, rssi, strict=True)]
    response = client.post(f"{API}/sightings", json=payload)
    assert response.status_code == 200

    weights = [10 ** (r / 20) for r in rssi]
    expected = (sum(b["x"] * w for b, w in zip(near, weights, strict=True)) / sum(weights),
                sum(b["y"] * w for b, w in zip(near, weights, strict=True)) / sum(weights))
    rows = client.get(f"{API}/tracking").json()
    assert {row["kind"] for row in rows} == {"patient", "pager", "asset", "staff"}
    for row in rows:
        assert {"tag_id", "kind", "floor_id", "x", "y", "space_id", "seen_at"} <= row.keys()
    staff = next(row for row in rows if row["tag_id"] == "T-S04")
    assert staff["floor_id"] == F1
    assert math.dist((staff["x"], staff["y"]), expected) <= 3.0

    only_assets = client.get(f"{API}/tracking", params={"kind": "asset"}).json()
    assert only_assets and {row["kind"] for row in only_assets} == {"asset"}


def test_sightings_unknown_tag_rejected(client, hospital):
    beacon_id = next(iter(hospital.beacons))
    response = client.post(f"{API}/sightings", json=[{"tag_id": "T-NOPE", "beacon_id": beacon_id, "rssi": -60}])
    assert 400 <= response.status_code < 500


def test_nearest_pump_and_in_use(client, hospital):
    params = {"type": "infusion_pump", "from": COFFEE_START}
    response = client.get(f"{API}/assets/nearest", params=params)
    assert response.status_code == 200
    first = response.json()
    assert {"asset", "route", "walk_m", "minutes", "say"} <= first.keys()
    assert first["asset"]["id"] not in hospital.in_use
    assert first["route"]["points"]

    patched = client.patch(f"{API}/assets/{first['asset']['id']}", json={"in_use": True})
    assert patched.status_code == 200
    second = client.get(f"{API}/assets/nearest", params=params).json()
    assert second["asset"]["id"] != first["asset"]["id"]
    assert second["route"]["cost_m"] >= first["route"]["cost_m"] - 1e-6


def test_nearest_errors(client):
    assert client.get(f"{API}/assets/nearest", params={"type": "infusion_pump", "from": "nope"}).status_code == 404
    assert client.patch(f"{API}/assets/nope", json={"in_use": True}).status_code == 404


def test_visit(client, hospital):
    patient = hospital.patients["P001"]
    response = client.get(f"{API}/visit", params={"patient": patient.number, "code": patient.visit_code})
    assert response.status_code == 200
    body = response.json()
    assert body["patient"]["room"]["id"] == patient.room_id
    assert body["patient"]["ward"] == patient.ward
    assert body["patient"]["floor"]["id"] == hospital.bmap.spaces[patient.room_id]["floor_id"]
    first = body["route"]["points"][0]
    assert hospital.place_at(first["floor_id"], first["x"], first["y"]).space_id == ENTRANCE


def test_visit_wrong_code_and_unknown_patient_identical_403(client, hospital):
    patient = hospital.patients["P001"]
    wrong = client.get(f"{API}/visit", params={"patient": patient.number, "code": "0000"})
    unknown = client.get(f"{API}/visit", params={"patient": "999999", "code": patient.visit_code})
    assert wrong.status_code == unknown.status_code == 403
    assert wrong.json() == unknown.json() == FORBIDDEN
    assert wrong.content == unknown.content


def test_optimize_apply_round_trip(client, hospital):
    response = client.post(f"{API}/rooms/{ER}/optimize", json={"add": {"type": "stretcher", "count": 4}})
    assert response.status_code == 200
    proposal = response.json()
    assert proposal["feasible"] is True
    assert proposal["max_count"] == 4
    assert {"proposal_id", "moves", "added", "before", "after"} <= proposal.keys()
    added = [a["id"] for a in proposal["added"]]
    assert not set(added) & set(hospital.bmap.assets)

    applied = client.post(f"{API}/rooms/{ER}/apply", json={"proposal_id": proposal["proposal_id"]})
    assert applied.status_code == 200
    assert set(added) <= set(hospital.bmap.assets)
    assert set(added) <= {a["id"] for a in client.get(f"{API}/map").json()["assets"]}
    again = client.post(f"{API}/rooms/{ER}/apply", json={"proposal_id": proposal["proposal_id"]})
    assert again.status_code == 409


def test_optimize_too_many_reports_max(client):
    proposal = client.post(f"{API}/rooms/{ER}/optimize", json={"add": {"type": "stretcher", "count": 200}}).json()
    assert proposal["feasible"] is False
    assert proposal["max_count"] < 200
    assert len(proposal["added"]) == proposal["max_count"]


def test_optimize_errors(client):
    assert client.post(f"{API}/rooms/nope/optimize", json={"add": {"type": "stretcher", "count": 1}}).status_code == 404
    assert client.post(f"{API}/rooms/{ER}/optimize", json={"add": {"type": "piano", "count": 1}}).status_code == 422
    assert client.post(f"{API}/rooms/{ER}/apply", json={"proposal_id": "nope"}).status_code == 409


def test_stops_coffee(client, hospital, scenario, clock):
    case = scenario["demo_cases"]["coffee"]
    client.get(f"{API}/pagers/PG-01/guidance")
    response = client.post(f"{API}/pagers/PG-01/stops", json={"stops": [{"category": "coffee"}]})
    assert response.status_code == 200
    body = response.json()
    assert {"stops", "detour_min", "arrival_margin_min", "fits", "route", "say"} <= body.keys()
    assert [s["id"] for s in body["stops"]] == [case["best_detour"]]
    assert body["fits"] is True and body["arrival_margin_min"] > 0
    assert "Coffee" in body["say"]

    after = client.get(f"{API}/pagers/PG-01/guidance").json()
    assert any(step["maneuver"] == "waypoint" for step in after["route"]["instructions"])

    cleared = client.delete(f"{API}/pagers/PG-01/stops")
    assert cleared.status_code == 200
    assert client.get(f"{API}/pagers/PG-01/guidance").json()["stops"] == []


def test_stops_dry_run_and_ordered(client, hospital):
    wanted = [{"id": CAFE}, {"id": TOILET_F2}]
    ordered = client.post(f"{API}/pagers/PG-01/stops", json={"stops": wanted, "ordered": True, "dry_run": True}).json()
    free = client.post(f"{API}/pagers/PG-01/stops", json={"stops": wanted, "dry_run": True}).json()
    assert [s["id"] for s in ordered["stops"]] == [w["id"] for w in wanted]
    assert free["route"]["cost_m"] <= ordered["route"]["cost_m"] + 1e-6
    assert hospital.patient_by_pager("PG-01").stops == []


def test_stops_bad_category_422(client):
    response = client.post(f"{API}/pagers/PG-01/stops", json={"stops": [{"category": "sauna"}]})
    assert response.status_code == 422


def test_lifts_after_lift_case(client, hospital):
    for pager_id in LIFT_PAGERS:
        assert client.get(f"{API}/pagers/{pager_id}/guidance").status_code == 200
    response = client.get(f"{API}/lifts")
    assert response.status_code == 200
    lifts = response.json()["lifts"]
    assert {lift["id"] for lift in lifts} == set(hospital.lifts.lifts())
    slots = [slot for lift in lifts for slot in lift["slots"]]
    assert slots
    for slot in slots:
        assert {"connector_id", "holder", "start", "end"} <= slot.keys()
    assert_no_overlap(slots)


def test_sim_start_stop(client):
    started = client.post(f"{API}/sim/start", json={"factor": 8})
    assert started.status_code == 200
    assert started.json() == {"running": True, "factor": 8}
    assert hospital_api.state["sim"].running
    stopped = client.post(f"{API}/sim/stop")
    assert stopped.status_code == 200
    assert stopped.json()["running"] is False
    assert hospital_api.state["sim"] is None
    assert client.post(f"{API}/sim/start", json={"factor": 0}).status_code == 422


def test_reset(client, hospital):
    hospital.in_use.clear()
    response = client.post(f"{API}/reset")
    assert response.status_code == 200
    assert response.json()["running"] is False
    assert hospital_api.hospital() is not hospital
    assert hospital_api.hospital().in_use
    assert RADIOLOGY in hospital_api.hospital().bmap.spaces
