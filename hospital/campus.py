"""Demo hospital campus for the staff pager: `python -m hospital.campus` writes hospitals/demo-campus.

Building A is the demo hospital from hospital.generate (emergency department, radiology, two wards; 3 floors). Building
B (outpatients and MRI on the ground floor, theatres on floor 1) stands 16 m west of it. They are joined by a covered
walkway on the ground floor and a glass bridge on floor 1, so routes can change building on either level; B has its own
lift and stairs. Both buildings share one XY frame and one set of floors (levels 0-2), as the kit format expects, and
every space says which building it belongs to (`building`). Rooms are named the way hospital signs name them: a
department or room name plus a room number of building, floor and room ("A0.12", "B1.01").

Staff only: the scenario has staff pagers and no patients."""

import json
import re
from contextlib import contextmanager
from pathlib import Path

from hospital import generate
from hospital.generate import NORTH, OUTSIDE, SOUTH, Builder

CID = "demo-campus"
OUT = Path(__file__).resolve().parent.parent / "hospitals" / CID
B_WEST, B_EAST = -46.0, -16.0
CORRIDOR = generate.CORRIDOR
BUILDINGS = [{"id": "A", "name": "Building A", "description": "Main building: emergency department, radiology, wards"},
             {"id": "B", "name": "Building B", "description": "Outpatients, MRI and theatres"}]

# Building A, by the generator's refs: (sign name, room number). Patient rooms keep their number with an A in front.
A_NAMES = {
    "Corridor G": ("Main corridor", "A0.C"), "Entrance": ("Main entrance", "A0.01"), "0.02": ("Shop", "A0.02"),
    "0.03": ("Admissions", "A0.03"), "0.04": ("Cafeteria", "A0.04"), "ER": ("Emergency Department (ED)", "A0.20"),
    "Bay 1": ("ED Bay 1", "A0.21"), "Bay 2": ("ED Bay 2", "A0.22"), "Bay 3": ("ED Bay 3", "A0.23"),
    "Bay 4": ("ED Bay 4", "A0.24"), "Bay 5": ("ED Bay 5", "A0.25"), "Triage": ("ED Triage", "A0.26"),
    "Resus": ("ED Resus", "A0.27"), "0.10": ("WC", "A0.10"), "0.11": ("Pharmacy", "A0.11"),
    "0.12": ("Pathology lab", "A0.12"), "0.13": ("Physiotherapy", "A0.13"), "RAD": ("Radiology reception", "A0.30"),
    "RAD-1": ("X-ray 1", "A0.31"), "RAD-2": ("X-ray 2", "A0.32"), "RAD-CT": ("CT", "A0.33"),
    "RAD-R": ("Radiology reporting", "A0.34"), "0.14": ("Accessible WC", "A0.14"), "0.15": ("Admin office", "A0.15"),
    "0.16": ("Cafeteria WC", "A0.16"), "0.17": ("ED WC", "A0.17"),
}
A_WARD_NAMES = {  # per ward floor; {s} = station (1A, 2A), {l} = level
    "Corridor {t}": ("Ward {s} corridor", "A{l}.C"), "{l}.00": ("Day room {s}", "A{l}.00"),
    "{l}.NS": ("Nurses' station {s}", "A{l}.40"), "{l}.UT": ("Dirty utility {s}", "A{l}.41"),
    "{l}.WC": ("WC {s}", "A{l}.42"), "{l}.BATH": ("Assisted bathroom {s}", "A{l}.43"),
    "{l}.DR": ("Doctors' office {s}", "A{l}.44"), "{l}.EQ": ("Equipment store {s}", "A{l}.45"),
    "{l}.WCA": ("Accessible WC {s}", "A{l}.46"), "{l}.WCS": ("Staff WC {s}", "A{l}.47"),
}
CORE_NAMES = {"LIFT-A": "Lift A", "STORE": "Store", "STAIRS": "Stairs A", "LIFT-B": "Lift B"}


@contextmanager
def campus_ids():
    """generate.Builder names ids after generate.BID; build the campus under its own id."""
    saved = generate.BID
    generate.BID = CID
    try:
        yield
    finally:
        generate.BID = saved


def b_rooms(b: Builder, level: int, side: str, x0: float, specs: list[tuple]) -> float:
    """generate.rooms for building B: the doors open onto B's own corridor."""
    saved = generate.corridor_of
    generate.corridor_of = lambda _level: f"B{level}.C"
    try:
        return generate.rooms(b, level, side, x0, specs)
    finally:
        generate.corridor_of = saved


def building_b_ground(b: Builder) -> None:
    corridor = "B0.C"
    b.space(0, corridor, "Outpatients corridor", "corridor", "corridor", B_WEST, CORRIDOR[0], B_EAST, CORRIDOR[1])
    b.space(0, "WALK-G", "Covered walkway A–B", "outdoor", "walkway", B_EAST, CORRIDOR[0], 0.0, CORRIDOR[1])
    b.opening(0, "WALK-G", corridor, B_EAST, 10.5, 2.4)
    b.opening(0, "WALK-G", "Corridor G", 0.0, 10.5, 2.4)
    b_rooms(b, 0, SOUTH, B_WEST, [
        ("B0.01", "Outpatients reception", "hall", "reception", 10.0),
        ("B0.02", "Outpatients waiting area", "room", "waiting", 8.0),
        ("B0.03", "Consulting room B0.03", "room", "consulting", 4.0),
        ("B0.04", "Consulting room B0.04", "room", "consulting", 4.0),
        ("B0.05", "Phlebotomy", "room", "lab", 4.0),
    ])
    b.opening(0, "B0.01", OUTSIDE, B_WEST + 5.0, CORRIDOR[0] - generate.ROOM_DEPTH_M, 3.0, "entrance")
    b.space(0, "LIFT-C-G", "Lift C", "lift", "lift", B_WEST, CORRIDOR[1], B_WEST + 3.0, CORRIDOR[1] + 3.0, NORTH)
    b.space(0, "STAIRS-B-G", "Stairs B", "stair", "stairwell", B_WEST + 3.0, CORRIDOR[1], B_WEST + 8.0,
            CORRIDOR[1] + 6.0, NORTH)
    b.opening(0, "LIFT-C-G", corridor, B_WEST + 1.5, CORRIDOR[1], 1.4)
    b.opening(0, "STAIRS-B-G", corridor, B_WEST + 5.5, CORRIDOR[1], 1.2)
    b_rooms(b, 0, NORTH, B_WEST + 8.0, [
        ("B0.10", "MRI", "room", "radiology", 8.0),
        ("B0.11", "MRI control room", "room", "office", 4.0),
        ("B0.12", "Equipment store B0", "room", "storage", 4.0),
        ("B0.13", "WC", "room", "toilet", 2.3, 2.5),
        ("B0.14", "Staff room", "room", "lounge", 3.7),
    ])
    b.asset("B0.01", "b-reception-desk", "Outpatients desk", "fixture", "counter", B_WEST + 6.0, 6.0, 3.0, 0.8,
            movable=False)
    b.asset("B0.02", "b-waiting-bench", "Waiting bench", "furniture", "bench", B_WEST + 14.0, 4.0, 3.0, 0.6)
    b.asset("B0.10", "mri", "MRI scanner", "machine", "mri_scanner", B_WEST + 12.0, 16.0, 2.2, 5.0, 90.0,
            movable=False)
    b.asset("B0.12", "b-wheelchair-1", "Wheelchair 07", "equipment", "wheelchair", *b.at("B0.12", 0.6, 0.7),
            0.7, 1.1)
    b.asset("B0.12", "b-wheelchair-2", "Wheelchair 08", "equipment", "wheelchair", *b.at("B0.12", 1.6, 0.7),
            0.7, 1.1)
    b.asset("B0.12", "b-defib-0", "Defibrillator B0", "equipment", "defibrillator", *b.at("B0.12", 3.3, 0.4),
            0.4, 0.3)
    b.asset("B0.14", "b-coffee", "Staff coffee machine B0", "machine", "coffee_machine",
            *b.at("B0.14", 0.5, 2.5), 0.6, 0.5, movable=False)


def building_b_theatres(b: Builder) -> None:
    corridor = "B1.C"
    b.space(1, corridor, "Theatres corridor", "corridor", "corridor", B_WEST, CORRIDOR[0], B_EAST, CORRIDOR[1])
    b.space(1, "BRIDGE-1", "Bridge A–B", "corridor", "walkway", B_EAST, CORRIDOR[0], 0.0, CORRIDOR[1])
    b.opening(1, "BRIDGE-1", corridor, B_EAST, 10.5, 2.4)
    b.opening(1, "BRIDGE-1", "Corridor 1", 0.0, 10.5, 2.4)
    b_rooms(b, 1, SOUTH, B_WEST, [
        ("B1.01", "Theatre 1", "room", "operating_theatre", 8.0),
        ("B1.02", "Anaesthetic room", "room", "treatment_bay", 4.0),
        ("B1.03", "Theatre 2", "room", "operating_theatre", 8.0),
        ("B1.04", "Scrub room", "room", "utility", 4.0),
        ("B1.05", "Sterile store", "room", "storage", 6.0),
    ])
    b.space(1, "LIFT-C-1", "Lift C", "lift", "lift", B_WEST, CORRIDOR[1], B_WEST + 3.0, CORRIDOR[1] + 3.0, NORTH)
    b.space(1, "STAIRS-B-1", "Stairs B", "stair", "stairwell", B_WEST + 3.0, CORRIDOR[1], B_WEST + 8.0,
            CORRIDOR[1] + 6.0, NORTH)
    b.opening(1, "LIFT-C-1", corridor, B_WEST + 1.5, CORRIDOR[1], 1.4)
    b.opening(1, "STAIRS-B-1", corridor, B_WEST + 5.5, CORRIDOR[1], 1.2)
    b_rooms(b, 1, NORTH, B_WEST + 8.0, [
        ("B1.10", "Recovery", "room", "recovery", 10.0),
        ("B1.11", "Theatre staff changing", "room", "office", 6.0),
        ("B1.12", "Dirty utility B1", "room", "utility", 3.0),
        ("B1.13", "Staff WC B1", "room", "toilet", 3.0, 2.5),
    ])
    for ref, key, x in (("B1.01", "or-table-1", B_WEST + 4.0), ("B1.03", "or-table-2", B_WEST + 16.0)):
        b.asset(ref, key, f"Operating table {ref[-1]}", "machine", "operating_table", x, 6.0, 0.6, 2.0, 90.0,
                movable=False)
    b.asset("B1.02", "b-anaesthesia", "Anaesthesia machine", "equipment", "anaesthesia_machine",
            *b.at("B1.02", 3.3, 0.6), 0.7, 0.7)
    b.asset("B1.05", "b-pump-1", "Infusion pump 16", "equipment", "infusion_pump", *b.at("B1.05", 0.6, 0.4),
            0.3, 0.25)
    b.asset("B1.05", "b-pump-2", "Infusion pump 17", "equipment", "infusion_pump", *b.at("B1.05", 1.2, 0.4),
            0.3, 0.25)
    b.asset("B1.10", "b-recovery-monitor", "Recovery monitor", "equipment", "monitor", *b.at("B1.10", 9.4, 0.4),
            0.5, 0.4)
    for bed in (1, 2, 3):
        b.asset("B1.10", f"b-trolley-{bed}", f"Recovery trolley {bed}", "equipment", "stretcher",
                *b.at("B1.10", 1.5 + 3.0 * (bed - 1), 1.3), 0.7, 2.0)
    b.asset("B1.10", "b-defib-1", "Defibrillator B1", "equipment", "defibrillator", *b.at("B1.10", 9.4, 5.6),
            0.4, 0.3)


def rename(building: dict, b: Builder) -> None:
    """Sign names, room numbers and the building of every space."""
    names = dict(A_NAMES)
    for level, station, tag in ((1, "1A", "1"), (2, "2A", "2")):
        for key, (name, number) in A_WARD_NAMES.items():
            names[key.format(l=level, t=tag)] = (name.format(s=station), number.format(l=level))
        for number in range(1, 28):
            ref = f"{level}.{number:02d}"
            names[ref] = (f"Room A{ref}", f"A{ref}")
    by_id = {space_id: ref for ref, space_id in b.ids.items() if ref != OUTSIDE}
    for space in building["spaces"]:
        ref = by_id[space["id"]]
        core = re.fullmatch(r"(LIFT-A|STORE|STAIRS|LIFT-B)-(G|1|2)", ref)
        if core:
            level = {"G": 0}.get(core.group(2), core.group(2))
            space["name"], space["ref"] = CORE_NAMES[core.group(1)], None
            if core.group(1) == "STORE":
                space["name"], space["ref"] = f"Store A{level}", f"A{level}.50"
        elif ref in names:
            space["name"], space["ref"] = names[ref]
        elif not ref.startswith("B") and ref not in ("WALK-G", "BRIDGE-1") and not ref.startswith(("LIFT-C", "STAIRS-B")):
            raise KeyError(f"no campus name for {ref}")
        space["building"] = "B" if space["polygon"][0][0] < 0 else "A"
        if ref in ("WALK-G", "BRIDGE-1") or ref.startswith(("LIFT-C", "STAIRS-B")) or                 space["room_type"] in ("corridor", "ward"):
            space["ref"] = None  # signed by name only
        if ref in ("WALK-G", "BRIDGE-1"):
            space["building"] = None  # between the buildings


def build() -> tuple[dict, dict]:
    with campus_ids():
        b = Builder()
        generate.ground_floor(b)
        generate.ward_floor(b, 1)
        generate.ward_floor(b, 2)
        building_b_ground(b)
        building_b_theatres(b)
        lifts = [b.connector("lift", "Lift A", ["LIFT-A-G", "LIFT-A-1", "LIFT-A-2"], True),
                 b.connector("lift", "Lift B", ["LIFT-B-G", "LIFT-B-1", "LIFT-B-2"], True)]
        stairs = [b.connector("stairs", "Stairs A", ["STAIRS-G", "STAIRS-1", "STAIRS-2"], False)]
        lifts.append(b.connector("lift", "Lift C", ["LIFT-C-G", "LIFT-C-1"], True))
        stairs.append(b.connector("stairs", "Stairs B", ["STAIRS-B-G", "STAIRS-B-1"], False))
        for number, (ref, dx, d, _) in enumerate(generate.PUMPS, start=1):
            b.asset(ref, f"pump-{number}", f"Infusion pump {number:02d}", "equipment", "infusion_pump",
                    *b.at(ref, dx, d), 0.3, 0.25)
        b.asset("ER", "defib-ed", "Defibrillator ED", "equipment", "defibrillator", 57.6, 1.0, 0.4, 0.3)
        for level in (1, 2):
            b.asset(f"{level}.NS", f"defib-{level}", f"Defibrillator {level}A", "equipment", "defibrillator",
                    *b.at(f"{level}.NS", 0.4, 5.6), 0.4, 0.3)
        b.place_beacons()
        building = {
            "building": {"id": CID, "name": "Demo Hospital Campus Munich", "source": "procedural",
                         "source_ref": {"generator": "hospital.campus", "parameters": "fixed"},
                         "license": "CC0-1.0", "attribution": "Procedurally generated for the Munich hackathon (CC0)",
                         "split": "demo", "units": "m"},
            "buildings": BUILDINGS,
            "floors": b.floors, "spaces": b.spaces, "openings": b.openings,
            "vertical_connectors": b.connectors, "assets": b.assets,
        }
        rename(building, b)
        staff = generate.staff_entries(b)
        pager = sum("pager_id" in s for s in staff) + 1
        routine = [("07:30", "B1.01"), ("09:00", "B1.10"), ("11:00", "B1.05"), ("13:00", "0.04"), ("14:00", "B1.03")]
        staff.append({"id": "S06", "tag_id": "T-S06", "label": "Sam Okafor", "role": "Theatres",
                      "space_id": b.ids["B1.C"], "home_id": b.ids["B1.10"],
                      "pager_id": f"PG-S{pager:02d}", "pager_tag_id": f"T-PG-S{pager:02d}",
                      "routine": [{"at": at, "space_id": b.ids[ref]} for at, ref in routine]})
        # PG-10: the route test pager (docs/route-tests.md). It starts at the main entrance, the simulator leaves it
        # alone, and the R key resets it to a clean slate.
        staff.append({"id": "S10", "tag_id": "T-S10", "label": "Route tester", "role": "Test pager",
                      "space_id": b.ids["Entrance"], "home_id": b.ids["Entrance"], "pager_id": "PG-10",
                      "pager_tag_id": "T-PG-10", "manual": True, "routine": []})
        pumps = [key for key in b.asset_ids if key.startswith(("pump-", "b-pump-"))]
        asset_tags = [{"tag_id": f"T-A-{key.upper()}", "asset_id": b.asset_ids[key]} for key in pumps]
        asset_tags += [{"tag_id": f"T-A-{key.upper()}", "asset_id": b.asset_ids[key]}
                       for key in b.asset_ids if "wheelchair" in key or "defib" in key]
        in_use = [b.asset_ids[f"pump-{n}"] for n, (*_, used) in enumerate(generate.PUMPS, start=1) if used]
        in_use += [asset_id for key, asset_id in b.asset_ids.items()
                   if key.startswith("bed-") and key.removeprefix("bed-") not in generate.FREE_BEDS]
        in_use += [b.asset_ids[key] for key in generate.IN_USE_STRETCHERS]
        in_use.append(b.asset_ids["or-table-1"])  # surgery under way in Theatre 1
        scenario = {
            "visiting_hours": "10:00–20:00",
            "main_entrance": b.ids["Entrance"],
            "ids": {
                "entrance": b.ids["Entrance"], "emergency": b.ids["ER"], "radiology": b.ids["RAD"],
                "floors": [f["id"] for f in b.floors], "lifts": lifts, "stairs": stairs,
                "walkway": b.ids["WALK-G"], "bridge": b.ids["BRIDGE-1"], "theatre_1": b.ids["B1.01"],
                "mri": b.ids["B0.10"], "outpatients": b.ids["B0.01"], "recovery": b.ids["B1.10"],
                "staff_pagers": [s["pager_id"] for s in staff if "pager_id" in s],
            },
            "beacons": b.beacons,
            "patients": [],
            "staff": staff,
            "asset_tags": asset_tags,
            "in_use": in_use,
            "demo_cases": {},
        }
    return building, scenario


def main() -> None:
    building, scenario = build()
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in (("building.json", building), ("scenario.json", scenario)):
        (OUT / name).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{OUT}: {len(building['spaces'])} spaces, {len(building['assets'])} assets, "
          f"{len(scenario['beacons'])} beacons, {len(scenario['staff'])} staff")


if __name__ == "__main__":
    main()
