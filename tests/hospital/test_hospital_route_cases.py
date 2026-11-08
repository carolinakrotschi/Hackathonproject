"""The 100 spoken route requests in hospital/route_cases.py, simulated on pager PG-10 (docs/route-tests.md)."""

import pytest

from app.hospital_api import default_hospital
from hospital import dispatch, route_cases, voice


@pytest.fixture(scope="module")
def campus():
    return default_hospital()


@pytest.mark.parametrize("case", route_cases.CASES, ids=lambda case: str(case["say"])[:60])
def test_route_case(campus, case):
    problems, happened = route_cases.run_case(campus, case)
    assert not problems, f"{problems}: {happened['say']}"


def test_reset_clears_everything(campus):
    voice.handle(campus, route_cases.PAGER, "Lift A is out of order. Take me to ward 2A with a bed, avoid lift B")
    assert campus.bmap.closed
    g = dispatch.walk_reset(campus, route_cases.PAGER)
    holder = campus.patient_by_pager(route_cases.PAGER)
    assert g["status"] == "idle" and g["route"] is None
    assert not (campus.bmap.closed or holder.avoid or holder.stops or holder.step_free or holder.destination)


def test_test_pager_is_not_simulated(campus):
    assert campus.patient_by_pager(route_cases.PAGER).manual


# The demo server runs the klinikum (HOSPITAL_BUILDING=demo-klinikum); PG-01 starts in Emergency.
KLINIKUM_ROUTES = [
    ("Pick up a free wheelchair, stop by the pharmacy, then collect the patient from cardiology and bring them to "
     "physio, avoid lift B, there are too many people", "Physio", ["Wheelchair", "Pharmacy", "Cardiology"], "Lift A"),
    ("Code blue in room 1.20, fastest way please, I'm pushing a bed, no stairs, lift A has too many people",
     "Room 1.20", [], "Lift B"),
    ("Collect the patient in bed from room 2.08, grab a free infusion pump on the way and bring them to the CT, "
     "lift B is crowded", "CT", ["Room 2.08", "Infusion pump"], "Lift A"),
    ("Critical patient on a stretcher, pick them up from bay 3, go via the lab and take them to cardiology, don't use "
     "lift A, too many people", "Cardiology", ["Bay 3", "Lab"], "Lift B"),
    ("Get a free stretcher, pick up the patient from room 1.12, then X-ray 2, then back to room 1.12, lift A and lift "
     "B are packed", "Room 1.12", ["stretcher", "Room 1.12", "X-ray 2"], "Lift"),
]


@pytest.fixture(scope="module")
def klinikum(monkeypatch_module):
    monkeypatch_module.setenv("HOSPITAL_BUILDING", "demo-klinikum")
    return default_hospital()


@pytest.fixture(scope="module")
def monkeypatch_module():
    with pytest.MonkeyPatch.context() as patch:
        yield patch


@pytest.mark.parametrize("pager", ["PG-01", "PG-05"])
@pytest.mark.parametrize("text, goal, stops, lift", KLINIKUM_ROUTES, ids=[r[0][:40] for r in KLINIKUM_ROUTES])
def test_klinikum_demo_routes(klinikum, pager, text, goal, stops, lift):
    dispatch.walk_reset(klinikum, pager)
    reply = voice.handle(klinikum, pager, text)
    g = dispatch.guidance(klinikum, pager)
    assert reply["ok"] and g["route"] and g["step_free"], reply["say"]
    assert g["goal"]["name"] == goal
    assert len(g["stops"]) == len(stops) and all(w.lower() in s["name"].lower() for w, s in zip(stops, g["stops"])), g["stops"]
    lifts = {i["connector"] for i in g["route"]["instructions"] if "connector" in i}
    assert lifts and all(klinikum.bmap.connectors[c]["name"].startswith(lift) for c in lifts)
