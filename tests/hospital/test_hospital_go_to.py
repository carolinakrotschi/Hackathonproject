from klinikum import API, IDS

STAFF_PAGER = IDS["staff_pagers"][0]
PATIENT_PAGER = "PG-01"


def say(client, pager_id, text):
    response = client.post(f"{API}/pagers/{pager_id}/voice", json={"text": text})
    assert response.status_code == 200
    return response.json()


def guidance(client, pager_id):
    return client.get(f"{API}/pagers/{pager_id}/guidance").json()


def test_take_me_to_the_emergency_room(client):
    body = say(client, STAFF_PAGER, "Take me to the emergency room")
    assert body["intent"] == "go_to" and body["ok"]
    g = guidance(client, STAFF_PAGER)
    assert g["mode"] == "destination" and g["goal"]["name"] == "Emergency" and g["route"]


def test_room_name_without_go_phrase_and_room_number(client):
    assert say(client, STAFF_PAGER, "emergency")["intent"] == "go_to"
    assert guidance(client, STAFF_PAGER)["goal"]["name"] == "Emergency"
    body = say(client, STAFF_PAGER, "go to room 1.12")
    assert body["intent"] == "go_to" and guidance(client, STAFF_PAGER)["goal"]["name"] == "Room 1.12"


def test_destination_overrides_appointment_then_cancel(client):
    appointment = guidance(client, PATIENT_PAGER)["goal"]["name"]
    say(client, PATIENT_PAGER, "Bring the patient to radiology")
    g = guidance(client, PATIENT_PAGER)
    assert g["mode"] == "destination" and g["goal"]["name"] == "Radiology"
    body = say(client, PATIENT_PAGER, "Cancel the route")
    assert body["intent"] == "cancel_go_to"
    g = guidance(client, PATIENT_PAGER)
    assert g["mode"] == "appointment" and g["goal"]["name"] == appointment


def test_object_search_and_stops_still_win(client):
    assert say(client, STAFF_PAGER, "Find the nearest free wheelchair")["intent"] == "find"
    assert say(client, PATIENT_PAGER, "I want a coffee on the way")["intent"] == "add_stop"
    assert say(client, STAFF_PAGER, "Can I take the stairs")["intent"] != "go_to"


def test_simulated_walk_reaches_destination(client):
    say(client, STAFF_PAGER, "Take me to the emergency room")
    for _ in range(200):
        body = client.post(f"{API}/pagers/{STAFF_PAGER}/walk", json={"metres": 5}).json()
        if body["status"] == "arrived":
            break
    assert body["status"] == "arrived" and body["say"] == "You have arrived at Emergency."
    assert body["position"]["space_id"] == body["goal"]["space_id"]


def test_walk_crosses_floors_for_a_waiting_patient(client):
    start = client.post(f"{API}/pagers/{PATIENT_PAGER}/walk", json={"metres": 0}).json()
    for _ in range(300):
        body = client.post(f"{API}/pagers/{PATIENT_PAGER}/walk", json={"metres": 5}).json()
        if body["status"] == "arrived":
            break
    assert body["status"] == "arrived"
    assert body["position"]["floor_id"] != start["position"]["floor_id"]
    assert body["position"]["space_id"] == body["goal"]["space_id"]


def test_destination_endpoint(client):
    response = client.post(f"{API}/pagers/{STAFF_PAGER}/destination", json={"place": "Pharmacy"})
    assert response.status_code == 200 and response.json()["goal"]["name"] == "Pharmacy"
    assert client.post(f"{API}/pagers/{STAFF_PAGER}/destination", json={"place": "Moon base"}).status_code == 422
    response = client.delete(f"{API}/pagers/{STAFF_PAGER}/destination")
    assert response.status_code == 200 and response.json()["say"].startswith("Route to Pharmacy cancelled")


def test_reset_walk_is_a_clean_slate(client):
    say(client, STAFF_PAGER, "Take me to the emergency room")
    start = client.post(f"{API}/pagers/{STAFF_PAGER}/walk", json={"metres": 0}).json()["position"]
    for _ in range(200):
        if client.post(f"{API}/pagers/{STAFF_PAGER}/walk", json={"metres": 5}).json()["status"] == "arrived":
            break
    body = client.delete(f"{API}/pagers/{STAFF_PAGER}/walk").json()
    assert body["position"] == start
    assert body["mode"] == "idle" and body["route"] is None
