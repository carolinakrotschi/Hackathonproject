import math

from hospital import rules
from shapely.geometry import Polygon

from klinikum import API, DEMO, F0, F1, IDS, NURSE_F1, RADIOLOGY

RADIOLOGIST = "T-S01"
NEVER_COFFEE = ("toilet", "patient_room", "emergency", "radiology", "resuscitation", "lift", "stairwell")


def total(grid: dict) -> int:
    return sum(map(sum, grid["counts"]))


def in_space(grid: dict, bmap, space_id: str) -> int:
    x0, y0, x1, y1 = Polygon(bmap.spaces[space_id]["polygon"]).bounds
    cell, (ox, oy) = grid["cell_m"], grid["origin"]
    return sum(grid["counts"][row][col]
               for row in range(max(0, math.floor((y0 - oy) / cell)), min(grid["rows"], math.ceil((y1 - oy) / cell)))
               for col in range(max(0, math.floor((x0 - ox) / cell)), min(grid["cols"], math.ceil((x1 - ox) / cell))))


def heatmap(client, **params) -> dict:
    response = client.get(f"{API}/heatmap", params=params)
    assert response.status_code == 200
    return response.json()


def people(client) -> dict:
    return {row["tag_id"]: row for row in client.get(f"{API}/history/people").json()["people"]}


def test_demo_week_history_file_exists(hospital):
    assert (DEMO / "history.json.gz").exists()
    samples = hospital.history.query(tag_id=RADIOLOGIST)
    assert samples
    assert samples[-1].t - samples[0].t >= 5 * 86_400


def test_heatmap_grid_on_radiology_floor(client, hospital):
    assert hospital.bmap.spaces[RADIOLOGY]["floor_id"] == F0
    grid = heatmap(client, floor_id=F0)
    assert {"floor_id", "cell_m", "origin", "cols", "rows", "counts", "max"} <= grid.keys()
    assert grid["floor_id"] == F0
    assert grid["cell_m"] <= 1.0
    assert len(grid["counts"]) == grid["rows"]
    assert all(len(row) == grid["cols"] for row in grid["counts"])
    assert grid["max"] > 0
    assert in_space(grid, hospital.bmap, RADIOLOGY) > 0
    coarse = heatmap(client, floor_id=F0, cell_m=1.0)
    assert coarse["cell_m"] == 1.0 and coarse["cols"] < grid["cols"]


def test_heatmap_errors(client):
    assert client.get(f"{API}/heatmap", params={"floor_id": "nope"}).status_code == 404
    assert client.get(f"{API}/heatmap", params={"floor_id": F0, "cell_m": 20}).status_code == 422


def test_heatmap_filters_change_result(client, hospital):
    everyone = heatmap(client, floor_id=F0)
    one = heatmap(client, floor_id=F0, tag=RADIOLOGIST)
    staff = heatmap(client, floor_id=F0, kind="staff")
    patients = heatmap(client, floor_id=F0, kind="patient")
    radiology = heatmap(client, floor_id=F0, role="Radiology")
    nobody = heatmap(client, floor_id=F0, role="Gibt es nicht")
    assert 0 < total(one) < total(everyone)
    assert 0 < total(staff) < total(everyone)
    assert 0 < total(patients) < total(everyone)
    assert staff["counts"] != patients["counts"]
    assert 0 < total(radiology) <= total(staff)
    assert total(nobody) == 0

    samples = hospital.history.query(tag_id=RADIOLOGIST)
    middle = (samples[0].t + samples[-1].t) / 2
    early = heatmap(client, floor_id=F0, tag=RADIOLOGIST, until=middle)
    late = heatmap(client, floor_id=F0, tag=RADIOLOGIST, since=middle)
    assert 0 < total(early) < total(one)
    assert 0 < total(late) < total(one)
    assert total(heatmap(client, floor_id=F0, since=hospital.now() + 3600)) == 0


def test_live_position_lands_in_history(client, hospital, clock):
    patient = hospital.patient_by_pager("PG-01")
    before = people(client).get(patient.tag_id, {"samples": 0})["samples"]
    clock.advance(5)
    x, y = hospital.bmap.anchor(NURSE_F1)
    response = client.post(f"{API}/pagers/PG-01/position", json={"floor_id": F1, "x": x, "y": y})
    assert response.status_code == 200
    after = people(client)[patient.tag_id]
    assert after["samples"] == before + 1
    assert after["last"] == clock.t
    assert after["kind"] == "patient"
    live = heatmap(client, floor_id=F1, tag=patient.tag_id, since=clock.t)
    assert total(live) >= 1


def test_live_sightings_land_in_history(client, hospital, clock):
    before = people(client).get("T-S04", {"samples": 0})["samples"]
    clock.advance(5)
    target = hospital.bmap.anchor(NURSE_F1)
    near = sorted((b for b in hospital.beacons.values() if b["floor_id"] == F1),
                  key=lambda b: math.dist(target, (b["x"], b["y"])))[:3]
    payload = [{"tag_id": "T-S04", "beacon_id": b["id"], "rssi": r} for b, r in zip(near, (-58, -63, -70), strict=True)]
    assert client.post(f"{API}/sightings", json=payload).status_code == 200
    after = people(client)["T-S04"]
    assert after["samples"] > before
    assert after["last"] == clock.t


def test_routines_find_radiologist_coffee_round(client):
    response = client.get(f"{API}/insights/routines", params={"role": "Radiology"})
    assert response.status_code == 200
    routines = response.json()["routines"]
    assert routines
    weber = next(r for r in routines if r["label"] == "Dr. Weber")
    assert weber["role"] == "Radiology"
    assert weber["tag_id"] == RADIOLOGIST
    assert weber["days"] >= 5
    assert weber["per_day"] >= 1
    sequence = [step["space_id"] for step in weber["sequence"]]
    assert IDS["coffee_far_room"] in sequence
    assert sequence[0] == RADIOLOGY and sequence[-1] == RADIOLOGY
    assert weber["route"]["points"]
    assert all(r["role"] == "Radiology" for r in routines)


def test_routines_by_tag(client):
    routines = client.get(f"{API}/insights/routines", params={"tag": RADIOLOGIST}).json()["routines"]
    assert routines and {r["tag_id"] for r in routines} == {RADIOLOGIST}


def test_placement_proposes_allowed_closer_spot_and_applies(client, hospital):
    asset_id = IDS["coffee_far"]
    asset = hospital.bmap.assets[asset_id]
    current = asset["space_id"]
    assert current == IDS["coffee_far_room"]
    response = client.post(f"{API}/insights/placement", json={"asset_id": asset_id})
    assert response.status_code == 200
    result = response.json()
    assert result["asset"]["id"] == asset_id
    assert result["saving_min_per_day"] > 0
    assert result["minutes_per_day_after"] < result["minutes_per_day_before"]
    assert result["trips_per_day"] > 0
    assert result["reason"]
    assert result["proposal_id"]
    proposal = result["proposal"]
    target = proposal["space_id"]
    assert target != current
    assert rules.may_stand(hospital.bmap, target, asset["type"], asset_id=asset_id)[0]
    assert hospital.bmap.spaces[target]["room_type"] not in NEVER_COFFEE

    applied = client.post(f"{API}/insights/placement/apply", json={"proposal_id": result["proposal_id"]})
    assert applied.status_code == 200
    assert asset_id in applied.json()["changed"]
    moved = next(a for a in client.get(f"{API}/map").json()["assets"] if a["id"] == asset_id)
    assert moved["space_id"] == target
    assert hospital.bmap.assets[asset_id]["space_id"] == target
    again = client.post(f"{API}/insights/placement/apply", json={"proposal_id": result["proposal_id"]})
    assert again.status_code == 409


def test_placement_errors(client):
    assert client.post(f"{API}/insights/placement", json={"asset_id": "nope"}).status_code == 404
    assert client.post(f"{API}/insights/placement/apply", json={"proposal_id": "nope"}).status_code == 409
