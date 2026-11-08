import pytest

from klinikum import API, IDS, assert_turns

STAFF_PAGER = IDS["staff_pagers"][0]
VOICE_TOOLS = {"next_instruction", "find_object", "add_stop", "clear_stops", "cancel_search", "where_am_i"}


def test_staff_pager_without_appointment_is_idle(client, hospital):
    staff = hospital.patient_by_pager(STAFF_PAGER)
    assert staff.kind == "staff" and staff.appointment is None
    assert staff.id == IDS["radiologist"]
    body = client.get(f"{API}/pagers/{STAFF_PAGER}/guidance").json()
    assert body["status"] == "idle"
    assert body["mode"] == "idle"
    assert body["say"] == "No active destination."


def test_find_bed_from_staff_pager(client, hospital):
    response = client.post(f"{API}/pagers/{STAFF_PAGER}/find", json={"type": "bed"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "search"
    assert body["status"] in ("go", "wait")
    assert body["say"].startswith("Nearest free bed")
    assert_turns(body["next_turns"])
    search = body["search"]
    assert search["type"] == "bed"
    target = hospital.bmap.assets[search["asset_id"]]
    assert target["type"] == "bed"
    assert search["asset_id"] not in hospital.in_use
    assert body["goal"]["asset_id"] == search["asset_id"]
    assert body["route"]["points"]
    last = body["route"]["points"][-1]
    assert last["floor_id"] == target["floor_id"]
    assert hospital.patient_by_pager(STAFF_PAGER).search["asset_id"] == search["asset_id"]

    again = client.get(f"{API}/pagers/{STAFF_PAGER}/guidance").json()
    assert again["mode"] == "search" and again["search"]["asset_id"] == search["asset_id"]


def test_find_skips_beds_in_use(client, hospital):
    first = client.post(f"{API}/pagers/{STAFF_PAGER}/find", json={"type": "bed"}).json()["search"]["asset_id"]
    assert client.patch(f"{API}/assets/{first}", json={"in_use": True}).status_code == 200
    second = client.post(f"{API}/pagers/{STAFF_PAGER}/find", json={"type": "bed"}).json()
    assert second["search"]["asset_id"] != first
    assert second["search"]["asset_id"] not in hospital.in_use


def test_find_from_patient_pager_overrides_appointment(client):
    body = client.post(f"{API}/pagers/PG-01/find", json={"type": "wheelchair"}).json()
    assert body["mode"] == "search"
    assert body["search"]["type"] == "wheelchair"
    stopped = client.delete(f"{API}/pagers/PG-01/find").json()
    assert stopped["mode"] == "appointment"
    assert stopped["search"] is None


def test_cancel_search_returns_staff_to_idle(client, hospital):
    client.post(f"{API}/pagers/{STAFF_PAGER}/find", json={"type": "bed"})
    response = client.delete(f"{API}/pagers/{STAFF_PAGER}/find")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "idle"
    assert body["search"] is None
    assert body["say"].startswith("Bed search cancelled")
    assert hospital.patient_by_pager(STAFF_PAGER).search is None
    assert client.get(f"{API}/pagers/{STAFF_PAGER}/guidance").json()["status"] == "idle"


def test_find_errors(client):
    assert client.post(f"{API}/pagers/PG-99/find", json={"type": "bed"}).status_code == 404
    assert client.delete(f"{API}/pagers/PG-99/find").status_code == 404
    assert client.get(f"{API}/pagers/PG-99/where").status_code == 404
    unknown_type = client.post(f"{API}/pagers/{STAFF_PAGER}/find", json={"type": "piano"})
    assert 400 <= unknown_type.status_code < 500
    assert client.post(f"{API}/pagers/{STAFF_PAGER}/find", json={}).status_code == 422


def test_voice_tools_describe_all_pager_actions(client):
    response = client.get(f"{API}/voice/tools")
    assert response.status_code == 200
    tools = {tool["name"]: tool for tool in response.json()["tools"]}
    assert set(tools) >= VOICE_TOOLS
    for name in VOICE_TOOLS:
        tool = tools[name]
        assert {"name", "description", "method", "path", "params", "example_say"} <= tool.keys(), name
        assert tool["description"] and tool["example_say"], name
        assert tool["method"] in ("GET", "POST", "DELETE"), name
        assert tool["path"].startswith(f"{API}/pagers/"), name
        assert isinstance(tool["params"], dict), name
    assert (tools["find_object"]["method"], tools["find_object"]["path"].rsplit("/", 1)[1]) == ("POST", "find")
    assert (tools["cancel_search"]["method"], tools["cancel_search"]["path"].rsplit("/", 1)[1]) == ("DELETE", "find")
    assert tools["next_instruction"]["path"].endswith("/guidance")
    assert tools["where_am_i"]["path"].endswith("/where")
    assert (tools["add_stop"]["method"], tools["clear_stops"]["method"]) == ("POST", "DELETE")
    assert "bed" in str(tools["find_object"]["params"])


@pytest.mark.parametrize("pager_id", ["PG-01", STAFF_PAGER])
def test_where_am_i(client, hospital, pager_id):
    response = client.get(f"{API}/pagers/{pager_id}/where")
    assert response.status_code == 200
    body = response.json()
    tag = hospital.tags[hospital.patient_by_pager(pager_id).pager_tag_id]
    assert body["space_id"] == tag.space_id
    assert body["floor_id"] == tag.floor_id
    assert body["say"].startswith("You are in ")
    assert body["name"] in body["say"]
    assert isinstance(body["nearby"], list)
