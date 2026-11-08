import json

from hospital.state import HOSPITALS

DEMO = HOSPITALS / "demo-klinikum"
SCENARIO = json.loads((DEMO / "scenario.json").read_text(encoding="utf-8"))
IDS = SCENARIO["ids"]
ENTRANCE = IDS["entrance"]
ER = IDS["emergency"]
RADIOLOGY = IDS["radiology"]
LIFTS = tuple(IDS["lifts"])
STAIRS = IDS["stairs"][0]
F0, F1, F2 = IDS["floors"]
CAFE = IDS["cafe"]
PHARMACY = IDS["pharmacy"]
TOILET_F2 = IDS["toilet_f2"]
FAR_ROOM_F2 = IDS["far_room_f2"]
NURSE_F1 = IDS["nurse_stations"][0]
COFFEE_START = SCENARIO["demo_cases"]["coffee"]["start"]
LIFT_PAGERS = tuple(SCENARIO["demo_cases"]["lift"]["pager_ids"])
T0 = 1_800_000_000.0
API = "/api/hospital"


class FakeClock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


def assert_no_overlap(slots: list[dict]) -> None:
    by_lift: dict[str, list[dict]] = {}
    for slot in slots:
        by_lift.setdefault(slot["connector_id"], []).append(slot)
    for connector_id, booked in by_lift.items():
        booked.sort(key=lambda s: s["start"])
        for a, b in zip(booked, booked[1:]):
            assert a["end"] <= b["start"] + 1e-6, f"{connector_id}: {a['holder']} overlaps {b['holder']}"


def assert_turns(turns: list[dict]) -> None:
    assert 1 <= len(turns) <= 3
    for turn in turns:
        assert turn["maneuver"]
        assert isinstance(turn["text"], str) and turn["text"]
        assert turn["distance_m"] >= 0
        assert turn["floor_id"]


def first_connector(route: dict) -> dict | None:
    return next((step for step in route["instructions"] if "connector" in step), None)
