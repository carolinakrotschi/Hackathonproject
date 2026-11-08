import pytest
from core.queries import area
from hospital import rules

from klinikum import API, CAFE, ENTRANCE, ER, IDS, LIFT_PAGERS, TOILET_F2

def spaces_of(bmap, room_type: str) -> list[str]:
    found = [space_id for space_id, space in bmap.spaces.items() if space["room_type"] == room_type]
    assert found, room_type
    return found


def count(bmap, space_id: str, asset_type: str) -> int:
    return sum(1 for a in bmap.assets.values() if a["space_id"] == space_id and a["type"] == asset_type)


def test_toilets_at_most_6_m2(hospital):
    for space_id in spaces_of(hospital.bmap, "toilet"):
        assert area(hospital.bmap, space_id) <= 6.0 + 1e-6, space_id


def test_patient_rooms_18_to_30_m2_with_1_or_2_beds(hospital):
    for space_id in spaces_of(hospital.bmap, "patient_room"):
        assert 18.0 <= area(hospital.bmap, space_id) <= 30.0, space_id
        assert 1 <= count(hospital.bmap, space_id, "bed") <= 2, space_id


def test_er_treatment_bays_8_to_14_m2(hospital):
    for space_id in spaces_of(hospital.bmap, "treatment_bay"):
        assert 8.0 <= area(hospital.bmap, space_id) <= 14.0, space_id


def test_v1_cases_still_hold_on_new_plan(hospital, scenario):
    coffee = scenario["demo_cases"]["coffee"]
    assert coffee["nearest_to_start"] in hospital.bmap.assets
    assert coffee["best_detour"] in hospital.bmap.spaces
    assert coffee["start"] in hospital.bmap.spaces and coffee["goal"] in hospital.bmap.spaces
    assert tuple(scenario["demo_cases"]["lift"]["pager_ids"]) == LIFT_PAGERS
    assert hospital.entrance_id == ENTRANCE


@pytest.mark.parametrize("asset_type", ["bed", "stretcher"])
@pytest.mark.parametrize("room_type", ["toilet", "corridor", "ward", "stairwell", "lift", "cafe"])
def test_optimize_rejects_lying_in_wrong_room(client, hospital, room_type, asset_type):
    space_id = spaces_of(hospital.bmap, room_type)[0]
    response = client.post(f"{API}/rooms/{space_id}/optimize", json={"add": {"type": asset_type, "count": 1}})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert detail.startswith(f"No {rules.noun(asset_type, 2)} are planned")
    assert count(hospital.bmap, space_id, asset_type) == 0


def test_scenario_rooms_reject_beds(client):
    for space_id in (TOILET_F2, CAFE):
        response = client.post(f"{API}/rooms/{space_id}/optimize", json={"add": {"type": "bed", "count": 2}})
        assert response.status_code == 422
        assert "No beds" in response.json()["detail"]


def test_patient_room_capped_at_3_beds(client, hospital):
    assert rules.rule_for("patient_room")["allowed"]["bed"] == 3
    rooms = spaces_of(hospital.bmap, "patient_room")
    single = next(space_id for space_id in rooms if count(hospital.bmap, space_id, "bed") == 1)
    response = client.post(f"{API}/rooms/{single}/optimize", json={"add": {"type": "bed", "count": 5}})
    assert response.status_code == 200
    proposal = response.json()
    assert proposal["requested"] == 5
    assert proposal["feasible"] is False
    assert 1 <= proposal["max_count"] < 5
    assert len(proposal["added"]) == proposal["max_count"]
    assert 1 + proposal["max_count"] <= 3
    assert proposal["limited_by"] in ("rule", "space")
    assert isinstance(proposal["reason"], str) and "bed" in proposal["reason"].lower()

    full = next(space_id for space_id in rooms if count(hospital.bmap, space_id, "bed") == 2)
    response = client.post(f"{API}/rooms/{full}/optimize", json={"add": {"type": "bed", "count": 1}})
    if response.status_code == 200:
        assert 2 + response.json()["max_count"] <= 3
    else:
        assert response.status_code == 422
        assert "bed" in response.json()["detail"].lower()


def test_treatment_bay_holds_one_stretcher(client, hospital):
    bay = spaces_of(hospital.bmap, "treatment_bay")[0]
    if count(hospital.bmap, bay, "stretcher"):
        full = client.post(f"{API}/rooms/{bay}/optimize", json={"add": {"type": "stretcher", "count": 1}})
        assert full.status_code == 422
        assert "1 stretcher" in full.json()["detail"]
        for asset_id in [a["id"] for a in hospital.bmap.assets.values()
                         if a["space_id"] == bay and a["type"] == "stretcher"]:
            del hospital.bmap.assets[asset_id]
    proposal = client.post(f"{API}/rooms/{bay}/optimize", json={"add": {"type": "stretcher", "count": 3}}).json()
    assert proposal["requested"] == 3
    assert proposal["max_count"] == 1
    assert len(proposal["added"]) == 1
    assert proposal["limited_by"] == "rule"
    assert "1 stretcher" in proposal["reason"]


def test_er_allows_many_stretchers(client):
    proposal = client.post(f"{API}/rooms/{ER}/optimize", json={"add": {"type": "stretcher", "count": 2}}).json()
    assert proposal["feasible"] is True
    assert proposal["limited_by"] is None


def test_room_options_lists_allowed_items(client, hospital):
    room = spaces_of(hospital.bmap, "patient_room")[0]
    response = client.get(f"{API}/rooms/{room}/options")
    assert response.status_code == 200
    body = response.json()
    assert body["space_id"] == room
    assert body["room_type"] == "patient_room"
    assert body["room_label"] == "Patient room"
    assert body["area_m2"] == pytest.approx(area(hospital.bmap, room), abs=0.1)
    items = {item["type"]: item for item in body["items"]}
    for item in body["items"]:
        assert {"type", "label", "allowed", "existing", "max_count", "can_add", "reason"} <= item.keys()
    bed = items["bed"]
    assert bed["allowed"] is True
    assert bed["label"] == "Bed"
    assert bed["existing"] == count(hospital.bmap, room, "bed")
    assert bed["max_count"] <= 3
    assert bed["can_add"] == bed["max_count"] - bed["existing"]
    assert items["stretcher"]["allowed"] is False
    assert items["stretcher"]["reason"]


def test_room_options_toilet_and_unknown(client):
    body = client.get(f"{API}/rooms/{IDS['toilet_f2']}/options").json()
    items = {item["type"]: item for item in body["items"]}
    assert items["bed"]["allowed"] is False and items["bed"]["can_add"] == 0
    assert "toilet" in items["bed"]["reason"]
    assert client.get(f"{API}/rooms/nope/options").status_code == 404


@pytest.mark.parametrize("requested", [31, 50, 200])
def test_optimize_accepts_large_counts_uncapped(client, requested):
    response = client.post(f"{API}/rooms/{ER}/optimize", json={"add": {"type": "stretcher", "count": requested}})
    assert response.status_code == 200
    proposal = response.json()
    assert proposal["requested"] == requested
    assert proposal["feasible"] is False
    assert 0 < proposal["max_count"] < requested
    assert len(proposal["added"]) == proposal["max_count"]
    assert proposal["limited_by"] in ("rule", "space")
    assert proposal["reason"]
