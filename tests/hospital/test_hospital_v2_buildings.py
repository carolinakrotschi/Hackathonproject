import json

import pytest
from hospital import state

from klinikum import API

hospital_api = pytest.importorskip("app.hospital_api")

FACTORY = state.ROOT / "factories" / "proc-factory-dev-003"
IMPORTED_ID = "plan-upload-test"


@pytest.fixture
def imported(tmp_path, monkeypatch):
    root = tmp_path / "imported"
    directory = root / IMPORTED_ID
    directory.mkdir(parents=True)
    data = json.loads((FACTORY / "building.json").read_text(encoding="utf-8"))
    data["building"]["name"] = "Hochgeladener Plan"
    (directory / "building.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(state, "IMPORTED", root)
    monkeypatch.setattr(hospital_api, "IMPORTED", root)
    return directory


def spaces_of(body: dict, room_type: str) -> list[str]:
    return [space["id"] for space in body["spaces"] if space.get("room_type") == room_type]


def test_buildings_lists_demo_and_imported(client, imported):
    response = client.get(f"{API}/buildings")
    assert response.status_code == 200
    body = response.json()
    assert body["current"] == "demo-klinikum"
    by_id = {b["id"]: b for b in body["buildings"]}
    assert by_id["demo-klinikum"]["source"] == "demo"
    assert by_id[IMPORTED_ID]["source"] == "imported"
    assert by_id[IMPORTED_ID]["name"] == "Hochgeladener Plan"
    assert by_id[IMPORTED_ID]["floors"] >= 1


def test_select_imported_building_runs_map_heatmap_optimizer(client, imported):
    response = client.post(f"{API}/buildings/select", json={"building_id": IMPORTED_ID})
    assert response.status_code == 200
    assert response.json()["current"] == IMPORTED_ID
    assert response.json()["source"] == "imported"
    h = hospital_api.hospital()
    assert h.root == imported
    assert client.get(f"{API}/buildings").json()["current"] == IMPORTED_ID

    body = client.get(f"{API}/map").json()
    assert body["building_source"] == "imported"
    assert body["patients"] == []
    assert [s["pager_id"] for s in body["staff"]] == ["PG-S01"]  # one staff pager to walk the new plan with
    assert body["beacons"]
    beacon_floors = {b["floor_id"] for b in body["beacons"]}
    assert beacon_floors == set(h.bmap.floors)
    rooms = [s for s in h.bmap.spaces.values() if s["type"] == "room"]
    beaconed = {b["space_id"] for b in body["beacons"]}
    assert all(room["id"] in beaconed for room in rooms)
    assert h.entrance_id in h.bmap.spaces

    for floor_id in h.bmap.floors:
        grid = client.get(f"{API}/heatmap", params={"floor_id": floor_id}).json()
        assert grid["cell_m"] <= 1.0
        assert grid["rows"] > 0 and grid["cols"] > 0
        assert len(grid["counts"]) == grid["rows"]

    office = spaces_of(body, "office")[0]
    proposal = client.post(f"{API}/rooms/{office}/optimize", json={"add": {"type": "chair", "count": 2}})
    assert proposal.status_code == 200
    assert proposal.json()["requested"] == 2
    assert proposal.json()["added"]
    applied = client.post(f"{API}/rooms/{office}/apply", json={"proposal_id": proposal.json()["proposal_id"]})
    assert applied.status_code == 200
    options = client.get(f"{API}/rooms/{office}/options")
    assert options.status_code == 200 and options.json()["items"]

    toilet = spaces_of(body, "toilet")[0]
    rejected = client.post(f"{API}/rooms/{toilet}/optimize", json={"add": {"type": "bed", "count": 1}})
    assert rejected.status_code == 422
    assert "toilet" in rejected.json()["detail"]


def test_select_unknown_building_404(client, imported):
    for building_id in ("nope", "..", "../demo-klinikum"):
        assert client.post(f"{API}/buildings/select", json={"building_id": building_id}).status_code == 404
    assert hospital_api.hospital().root.name == "demo-klinikum"


def test_select_back_to_demo(client, imported):
    client.post(f"{API}/buildings/select", json={"building_id": IMPORTED_ID})
    response = client.post(f"{API}/buildings/select", json={"building_id": "demo-klinikum"})
    assert response.status_code == 200
    body = client.get(f"{API}/map").json()
    assert body["building_source"] == "demo"
    assert body["patients"]


def test_imported_building_analysis_without_history(client, imported):
    """An imported plan has no movement history yet; the Analyse tab must still load (empty lists, not 500)."""
    assert client.post(f"{API}/buildings/select", json={"building_id": IMPORTED_ID}).status_code == 200
    routines = client.get(f"{API}/insights/routines")
    assert routines.status_code == 200
    assert routines.json()["routines"] == []
    assets = client.get(f"{API}/insights/assets")
    assert assets.status_code == 200
    assert assets.json()["assets"] == []


def test_plan_upload_without_api_key_imports_and_is_selectable(client, tmp_path, monkeypatch):
    """Plan upload must work on a server without ANTHROPIC_API_KEY (labels are optional), then be selectable."""
    server = pytest.importorskip("app.server")
    root = tmp_path / "imported"
    for module in (server, state, hospital_api):
        monkeypatch.setattr(module, "IMPORTED", root)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    response = client.post("/api/import", data={"sample": "plans/proc-factory-dev-003", "name": "Upload ohne Key"})
    assert response.status_code == 200, response.text
    building_id = response.json()["id"]
    listed = {b["id"]: b for b in client.get(f"{API}/buildings").json()["buildings"]}
    assert listed[building_id]["source"] == "imported"
    assert client.post(f"{API}/buildings/select", json={"building_id": building_id}).status_code == 200
    body = client.get(f"{API}/map").json()
    assert body["beacons"] and body["patients"] == []
    assert client.get(f"{API}/insights/routines").status_code == 200
