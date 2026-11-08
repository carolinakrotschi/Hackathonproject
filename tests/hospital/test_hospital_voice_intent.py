import pytest

from klinikum import API, IDS

STAFF_PAGER = IDS["staff_pagers"][0]
PATIENT_PAGER = "PG-01"


def say(client, pager_id, text):
    response = client.post(f"{API}/pagers/{pager_id}/voice", json={"text": text})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("find, cancel", [
    ("Finde das nächste freie Bett", "Suche abbrechen"),
    ("Find the nearest free bed", "Cancel the search"),
    ("Find the nearest free bed", "stop"),
])
def test_find_then_cancel_search(client, find, cancel):
    body = say(client, STAFF_PAGER, find)
    assert body["intent"] == "find" and body["ok"]
    assert body["say"].startswith("Nearest free bed")
    assert client.get(f"{API}/pagers/{STAFF_PAGER}/guidance").json()["mode"] == "search"
    body = say(client, STAFF_PAGER, cancel)
    assert body["intent"] == "cancel_search" and body["ok"]
    assert client.get(f"{API}/pagers/{STAFF_PAGER}/guidance").json()["status"] == "idle"


@pytest.mark.parametrize("text", ["Wo bin ich?", "Where am I?"])
def test_where(client, text):
    body = say(client, STAFF_PAGER, text)
    assert body["intent"] == "where" and body["say"].startswith("You are in")


@pytest.mark.parametrize("add, clear", [
    ("Ich will noch einen Kaffee auf dem Weg", "Zwischenstopps löschen"),
    ("Coffee on the way", "Clear stops"),
])
def test_add_stop_and_clear(client, add, clear):
    body = say(client, PATIENT_PAGER, add)
    assert body["intent"] == "add_stop" and body["ok"]
    body = say(client, PATIENT_PAGER, clear)
    assert body["intent"] == "clear_stops" and body["ok"]


@pytest.mark.parametrize("text", ["Wie geht es weiter?", "What's next?"])
def test_next(client, text):
    body = say(client, PATIENT_PAGER, text)
    assert body["intent"] == "next" and body["say"]


@pytest.mark.parametrize("text", ["Banane", "banana"])
def test_unknown(client, text):
    body = say(client, PATIENT_PAGER, text)
    assert body["intent"] == "unknown" and not body["ok"]
    assert body["say"].startswith("Sorry")
    assert say(client, PATIENT_PAGER, "  ")["intent"] == "unknown"


def test_unknown_pager(client):
    response = client.post(f"{API}/pagers/PG-99/voice", json={"text": "Wo bin ich?"})
    assert response.status_code == 404
