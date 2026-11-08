"""Deterministic demo hospital: `python -m hospital.generate` writes hospitals/demo-klinikum/building.json
(same schema as factories/*/building.json) and hospitals/demo-klinikum/scenario.json.

Every floor is a corridor spine (y 9–12) with rooms of realistic size on both sides (patient rooms 3.8 × 6 m,
WCs 2 × 2.5 m, ER treatment bays 3.5 × 3.5 m), a central core (lift A, stairs, store room) and lift B at the east
end of the corridor; shafts sit at the same x, y on every floor. Rooms added in v2 are created after the v1 rooms
of their floor, so the v1 space ids still name the same rooms."""

import json
from pathlib import Path

BID = "demo-klinikum"
OUT = Path(__file__).resolve().parent.parent / "hospitals" / BID
LENGTH_M = 72.0
CORRIDOR = (9.0, 12.0)
ROOM_DEPTH_M = 6.0
PATIENT_ROOM_WIDTH_M = 3.8
WC = (2.0, 2.5)
WC_ACCESSIBLE = (2.3, 2.5)
BAY_M = 3.5
BEACON_SPACING_M = 10.0
FLOOR_NAMES = {0: "Ground floor", 1: "Floor 1", 2: "Floor 2"}
OUTSIDE = "outside"
SOUTH, NORTH = "south", "north"
FLOOR_TAGS = {0: "G", 1: "1", 2: "2"}


class Builder:
    def __init__(self) -> None:
        self.floors: list[dict] = []
        self.spaces: list[dict] = []
        self.openings: list[dict] = []
        self.connectors: list[dict] = []
        self.assets: list[dict] = []
        self.beacons: list[dict] = []
        self.ids: dict[str, str] = {OUTSIDE: OUTSIDE}
        self.asset_ids: dict[str, str] = {}
        self.boxes: dict[str, tuple[str, float, float, float, float]] = {}

    @staticmethod
    def floor_id(level: int) -> str:
        return f"{BID}-F{level}"

    def floor(self, level: int) -> None:
        self.floors.append({"id": self.floor_id(level), "level": level, "name": FLOOR_NAMES[level],
                            "elevation_m": 4.0 * level, "height_m": 4.0})

    def space(self, level: int, ref: str, name: str, kind: str, room_type: str,
              x0: float, y0: float, x1: float, y1: float, side: str = NORTH) -> str:
        count = sum(1 for s in self.spaces if s["floor_id"] == self.floor_id(level)) + 1
        space_id = f"{BID}-F{level}-S{count:03d}"
        x0, y0, x1, y1 = (round(v, 2) for v in (x0, y0, x1, y1))
        self.spaces.append({"id": space_id, "floor_id": self.floor_id(level), "type": kind, "room_type": room_type,
                            "name": name, "ref": ref, "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                            "synthetic": False})
        self.ids[ref] = space_id
        self.boxes[ref] = (side, x0, y0, x1, y1)
        return space_id

    def opening(self, level: int, a: str, b: str, x: float, y: float, width: float = 1.2,
                kind: str = "door") -> None:
        count = sum(1 for o in self.openings if o["floor_id"] == self.floor_id(level)) + 1
        self.openings.append({"id": f"{BID}-F{level}-O{count:04d}", "floor_id": self.floor_id(level),
                              "space_ids": [self.ids[a], self.ids[b]], "kind": kind,
                              "position": [round(x, 2), round(y, 2)], "width_m": width, "step_free": True,
                              "synthetic": False})

    def at(self, ref: str, dx: float, d: float) -> tuple[float, float]:
        """Point dx from the room's west wall and d from its outer wall (the one away from the corridor)."""
        side, x0, y0, _, y1 = self.boxes[ref]
        return round(x0 + dx, 2), round(y0 + d if side == SOUTH else y1 - d, 2)

    def asset(self, ref: str, key: str, name: str, category: str, kind: str, x: float, y: float,
              width: float, depth: float, rotation: float = 0.0, movable: bool = True) -> str:
        space = next(s for s in self.spaces if s["id"] == self.ids[ref])
        asset_id = f"{BID}-A{len(self.assets) + 1:04d}"
        self.assets.append({"id": asset_id, "name": name, "category": category, "type": kind,
                            "space_id": space["id"], "floor_id": space["floor_id"], "position": [x, y],
                            "footprint": {"width_m": width, "depth_m": depth, "rotation_deg": rotation},
                            "movable": movable, "source": "generated"})
        self.asset_ids[key] = asset_id
        return asset_id

    def connector(self, kind: str, name: str, refs: list[str], step_free: bool) -> str:
        connector_id = f"{BID}-V{len(self.connectors) + 1:02d}"
        self.connectors.append({"id": connector_id, "kind": kind, "name": name,
                                "floor_ids": [self.floor_id(level) for level in range(len(refs))],
                                "space_ids": [self.ids[ref] for ref in refs], "step_free": step_free,
                                "synthetic": False})
        return connector_id

    def place_beacons(self) -> None:
        for level in range(len(self.floors)):
            number = 0
            for space in (s for s in self.spaces if s["floor_id"] == self.floor_id(level)):
                (x0, y0), _, (x1, y1), _ = space["polygon"]
                along_x = x1 - x0 >= y1 - y0
                span = max(x1 - x0, y1 - y0)
                count = max(1, round(span / BEACON_SPACING_M))
                for i in range(count):
                    offset = span * (i + 0.5) / count
                    x, y = (x0 + offset, (y0 + y1) / 2) if along_x else ((x0 + x1) / 2, y0 + offset)
                    number += 1
                    self.beacons.append({"id": f"B-F{level}-{number:02d}", "floor_id": self.floor_id(level),
                                         "x": round(x, 2), "y": round(y, 2), "space_id": space["id"]})


def rooms(b: Builder, level: int, side: str, x0: float, specs: list[tuple]) -> float:
    """A row of rooms along the corridor, each with a door to it: specs = (ref, name, kind, room_type, width[, depth]).
    Returns the x where the row ends."""
    corridor_ref = corridor_of(level)
    for ref, name, kind, room_type, width, *rest in specs:
        depth = rest[0] if rest else ROOM_DEPTH_M
        if side == SOUTH:
            b.space(level, ref, name, kind, room_type, x0, CORRIDOR[0] - depth, x0 + width, CORRIDOR[0], side)
        else:
            b.space(level, ref, name, kind, room_type, x0, CORRIDOR[1], x0 + width, CORRIDOR[1] + depth, side)
        door_width = 0.9 if width < 2.2 else 1.0 if width < 3.0 else 1.4 if width > 5.0 else 1.2
        b.opening(level, ref, corridor_ref, x0 + width / 2, CORRIDOR[0] if side == SOUTH else CORRIDOR[1],
                  door_width)
        x0 += width
    return x0


def corridor_of(level: int) -> str:
    return f"Corridor {FLOOR_TAGS[level]}"


def core(b: Builder, level: int) -> None:
    tag = FLOOR_TAGS[level]
    corridor = corridor_of(level)
    b.space(level, f"LIFT-A-{tag}", "Lift A", "lift", "lift", 32.0, 12.0, 35.0, 15.0)
    b.space(level, f"STORE-{tag}", "Store", "room", "storage", 32.0, 15.0, 35.0, 18.0)
    b.space(level, f"STAIRS-{tag}", "Stairs", "stair", "stairwell", 35.0, 12.0, 40.0, 21.0)
    b.space(level, f"LIFT-B-{tag}", "Lift B", "lift", "lift", 72.0, 9.0, 75.0, 12.0)
    b.opening(level, f"LIFT-A-{tag}", corridor, 33.5, 12.0)
    b.opening(level, f"STAIRS-{tag}", corridor, 37.5, 12.0)
    b.opening(level, f"STORE-{tag}", f"STAIRS-{tag}", 35.0, 16.5, 1.0)
    b.opening(level, f"LIFT-B-{tag}", corridor, 72.0, 10.5, 1.4)
    b.asset(f"STORE-{tag}", f"shelf-{tag}", f"Store shelf {tag}", "furniture", "shelf", 32.3, 16.5, 0.5, 2.6,
            movable=False)


def wc(b: Builder, level: int, side: str, x0: float, ref: str, name: str, size: tuple[float, float] = WC) -> float:
    return rooms(b, level, side, x0, [(ref, name, "room", "toilet", size[0], size[1])])


# Single rooms have one bed (by number on every ward), the others two.
SINGLE_ROOMS = {5, 10, 15, 20, 25}
# Free beds ("<room>-<bed>"); every other bed is occupied.
FREE_BEDS = {"1.02-2", "1.07-1", "1.12-2", "1.16-2", "1.21-1", "1.24-2", "1.27-1",
             "2.01-2", "2.06-1", "2.13-2", "2.19-2", "2.23-1", "2.26-2"}


def patient_room(b: Builder, level: int, number: int, x0: float, side: str) -> float:
    ref = f"{level}.{number:02d}"
    end = rooms(b, level, side, x0, [(ref, f"Room {ref}", "room", "patient_room", PATIENT_ROOM_WIDTH_M)])
    spots = [1.9] if number in SINGLE_ROOMS else [0.9, 2.9]
    for bed, dx in enumerate(spots, start=1):
        b.asset(ref, f"bed-{ref}-{bed}", f"Bed {ref}-{bed}", "furniture", "bed", *b.at(ref, dx, 1.3), 1.0, 2.2)
    b.asset(ref, f"cabinet-{ref}", f"Cabinet {ref}", "furniture", "cabinet", *b.at(ref, 0.35, 4.6), 0.6, 1.0,
            movable=False)
    b.asset(ref, f"chair-{ref}", f"Chair {ref}", "furniture", "chair", *b.at(ref, 3.4, 3.2), 0.5, 0.5)
    return end


def treatment_bay(b: Builder, bay: int) -> None:
    x0 = 40.0 + BAY_M * (bay - 1)
    ref = f"Bay {bay}"
    b.space(0, ref, f"Bay {bay}", "room", "treatment_bay", x0, -BAY_M, x0 + BAY_M, 0.0, SOUTH)
    b.opening(0, ref, "ER", x0 + BAY_M / 2, 0.0, 2.4, "opening")
    b.asset(ref, f"stretcher-bay-{bay}", f"Stretcher bay {bay}", "equipment", "stretcher",
            *b.at(ref, BAY_M / 2, 1.3), 0.7, 2.0, 90.0)
    b.asset(ref, f"monitor-bay-{bay}", f"Monitor bay {bay}", "equipment", "monitor",
            *b.at(ref, BAY_M - 0.35, 0.3), 0.5, 0.4, movable=False)


def ground_floor(b: Builder) -> None:
    b.floor(0)
    b.space(0, "Corridor G", "Corridor G", "corridor", "corridor", 0.0, CORRIDOR[0], LENGTH_M, CORRIDOR[1])
    b.space(0, "Entrance", "Main entrance", "hall", "lobby", 0.0, 0.0, 14.0, 9.0, SOUTH)
    b.opening(0, "Entrance", OUTSIDE, 7.0, 0.0, 3.0, "entrance")
    b.opening(0, "Entrance", "Corridor G", 7.0, 9.0, 6.0, "opening")
    rooms(b, 0, SOUTH, 14.0, [
        ("0.02", "Kiosk", "room", "kiosk", 4.0, 5.0),
        ("0.03", "Admissions", "room", "reception", 8.0),
        ("0.04", "Cafeteria", "room", "cafe", 12.0, 9.0),
    ])
    b.space(0, "ER", "Emergency", "hall", "emergency", 40.0, 0.0, 58.0, 9.0, SOUTH)
    b.opening(0, "ER", "Corridor G", 49.0, 9.0, 1.8)
    for bay in range(1, 5):
        treatment_bay(b, bay)
    b.space(0, "Triage", "Triage", "room", "triage", 58.0, 3.0, 64.0, 9.0, SOUTH)
    b.opening(0, "Triage", "Corridor G", 61.0, 9.0, 1.4)
    b.opening(0, "Triage", "ER", 58.0, 6.0, 1.6)
    b.opening(0, "Triage", OUTSIDE, 61.0, 3.0, 2.5, "entrance")
    b.space(0, "Resus", "Resus", "room", "resuscitation", 64.0, 3.0, 70.0, 9.0, SOUTH)
    b.opening(0, "Resus", "Corridor G", 67.0, 9.0, 1.6)
    b.opening(0, "Resus", "Triage", 64.0, 6.0, 1.6)
    wc(b, 0, NORTH, 0.0, "0.10", "WC")
    rooms(b, 0, NORTH, 4.3, [
        ("0.11", "Pharmacy", "room", "pharmacy", 6.0),
        ("0.12", "Lab", "room", "lab", 8.0),
        ("0.13", "Physio", "room", "therapy", 8.0),
    ])
    core(b, 0)
    rooms(b, 0, NORTH, 40.0, [
        ("RAD", "Radiology", "room", "radiology", 8.0),
        ("RAD-1", "X-ray 1", "room", "radiology", 6.0),
        ("RAD-2", "X-ray 2", "room", "radiology", 6.0),
        ("RAD-CT", "CT", "room", "radiology", 8.0),
    ])
    treatment_bay(b, 5)
    wc(b, 0, NORTH, 2.0, "0.14", "Accessible WC", WC_ACCESSIBLE)
    rooms(b, 0, NORTH, 26.3, [("0.15", "Admin", "room", "office", 5.7)])
    wc(b, 0, SOUTH, 38.0, "0.16", "Cafeteria WC")
    rooms(b, 0, NORTH, 68.0, [("RAD-R", "Reporting room", "room", "office", 4.0)])
    wc(b, 0, SOUTH, 70.0, "0.17", "Emergency WC")

    b.asset("Entrance", "info", "Info desk", "fixture", "counter", 10.0, 3.5, 3.0, 1.0, movable=False)
    b.asset("Entrance", "bench-lobby", "Lobby bench", "furniture", "bench", 3.5, 6.5, 2.5, 0.6)
    b.asset("Entrance", "wheelchair-1", "Wheelchair 01", "equipment", "wheelchair", 0.8, 1.5, 0.7, 1.1)
    b.asset("Entrance", "wheelchair-2", "Wheelchair 02", "equipment", "wheelchair", 1.6, 1.5, 0.7, 1.1)
    b.asset("Entrance", "water-0", "Water dispenser G", "fixture", "water_dispenser",
            13.6, 7.5, 0.4, 0.4, movable=False)
    b.asset("0.02", "kiosk-shelf", "Magazine rack", "furniture", "shelf", 17.6, 6.5, 0.5, 3.0, movable=False)
    b.asset("0.03", "reception-desk", "Admissions desk", "fixture", "counter", 22.0, 5.0, 4.0, 0.8, movable=False)
    b.asset("0.04", "coffee-bar", "Coffee bar", "fixture", "counter", 32.0, 0.8, 4.0, 0.8, movable=False)
    for column, x in enumerate((28.0, 30.0, 34.0, 36.0)):
        for row, y in enumerate((3.0, 5.2, 7.2)):
            b.asset("0.04", f"cafe-table-{column}{row}", f"Cafeteria table {column * 3 + row + 1}", "furniture",
                    "table", x, y, 0.8, 0.8)
    b.asset("0.11", "pharmacy-counter", "Pharmacy counter", "fixture", "counter", 7.3, 16.6, 3.0, 0.8, movable=False)
    b.asset("0.12", "lab-chair", "Blood draw chair", "furniture", "chair", 12.0, 16.4, 0.8, 1.2)
    b.asset("0.12", "lab-bench", "Lab bench", "furniture", "table", 16.0, 17.4, 2.0, 0.8, movable=False)
    b.asset("0.13", "physio-table", "Therapy table", "furniture", "therapy_table", 22.3, 16.6, 0.8, 2.0, 90.0)
    b.asset("0.15", "admin-desk", "Admin desk", "furniture", "desk", 29.15, 17.4, 2.0, 0.8,
            movable=False)
    b.asset("RAD", "rad-desk", "Radiology desk", "fixture", "counter", 40.6, 15.5, 0.8, 2.0, movable=False)
    b.asset("RAD", "rad-bench", "Radiology bench", "furniture", "bench", 44.5, 17.6, 3.0, 0.6)
    b.asset("RAD", "rad-stretcher", "Radiology stretcher", "equipment", "stretcher", 47.1, 14.5, 0.7, 2.0)
    b.asset("RAD-1", "xray-1", "X-ray unit 1", "machine", "xray", 51.0, 15.8, 2.0, 3.0, movable=False)
    b.asset("RAD-2", "xray-2", "X-ray unit 2", "machine", "xray", 57.0, 15.8, 2.0, 3.0, movable=False)
    b.asset("RAD-CT", "ct", "CT scanner", "machine", "ct_scanner", 64.0, 16.0, 2.2, 5.0, 90.0, movable=False)
    b.asset("RAD-R", "rad-report-desk", "Reporting desk", "furniture", "desk", 70.0, 17.4, 2.0, 0.8,
            movable=False)

    b.asset("ER", "zna-counter", "ER nurses' desk", "fixture", "counter", 40.9, 6.0, 1.2, 3.0,
            movable=False)
    b.asset("ER", "zna-cabinet", "ER drug cabinet", "furniture", "cabinet", 57.6, 7.8, 0.6, 1.2,
            movable=False)
    b.asset("ER", "zna-stretcher-1", "ER stretcher 1", "equipment", "stretcher", 45.0, 5.5, 0.7, 2.0, 30.0)
    b.asset("ER", "zna-stretcher-2", "ER stretcher 2", "equipment", "stretcher", 52.0, 6.0, 0.7, 2.0)
    b.asset("ER", "wheelchair-3", "Wheelchair 03", "equipment", "wheelchair", 48.5, 4.5, 0.7, 1.1, 45.0)
    b.asset("ER", "wheelchair-4", "Wheelchair 04", "equipment", "wheelchair", 55.0, 5.5, 0.7, 1.1, 20.0)
    b.asset("ER", "zna-ultrasound", "Ultrasound unit", "equipment", "ultrasound", 53.5, 3.0, 0.6, 0.8)
    b.asset("ER", "zna-monitor", "Monitor cart", "equipment", "monitor", 56.5, 3.5, 0.6, 0.6)
    b.asset("Triage", "triage-desk", "Triage desk", "furniture", "desk", 59.5, 8.4, 1.2, 0.6, movable=False)
    b.asset("Triage", "triage-stretcher", "Triage stretcher", "equipment", "stretcher", 63.3, 5.0, 0.7, 2.0)
    b.asset("Resus", "shock-stretcher", "Resus stretcher", "equipment", "stretcher", 67.0, 5.5, 0.7, 2.0)
    b.asset("Resus", "ventilator", "Ventilator", "equipment", "ventilator", 69.4, 4.0, 0.6, 0.6)


def ward_floor(b: Builder, level: int) -> None:
    b.floor(level)
    station = f"{level}A"
    b.space(level, corridor_of(level), corridor_of(level), "corridor", "ward", 0.0, CORRIDOR[0], LENGTH_M, CORRIDOR[1])
    x = rooms(b, level, SOUTH, 0.0, [(f"{level}.00", f"Lounge {station}", "room", "lounge", 6.0)])
    for number in range(1, 7):
        x = patient_room(b, level, number, x, SOUTH)
    x = rooms(b, level, SOUTH, x, [
        (f"{level}.NS", f"Nurses {station}", "room", "nurse_station", 6.0),
        (f"{level}.UT", f"Utility {station}", "room", "utility", 3.0),
    ])
    for number in range(7, 15):
        x = patient_room(b, level, number, x, SOUTH)
    south_end = x
    wc(b, level, NORTH, 0.0, f"{level}.WC", f"WC {station}")
    x = rooms(b, level, NORTH, 4.3, [(f"{level}.BATH", f"Bathroom {station}", "room", "bathroom", 4.9, 3.0)])
    for number in range(15, 21):
        x = patient_room(b, level, number, x, NORTH)
    core(b, level)
    x = rooms(b, level, NORTH, 40.0, [
        (f"{level}.DR", f"Doctors {station}", "room", "office", 4.0),
        (f"{level}.EQ", f"Equipment {station}", "room", "storage", 3.0),
    ])
    for number in range(21, 27):
        x = patient_room(b, level, number, x, NORTH)
    patient_room(b, level, 27, south_end, SOUTH)
    wc(b, level, NORTH, 2.0, f"{level}.WCA", f"Accessible WC {station}", WC_ACCESSIBLE)
    wc(b, level, NORTH, x, f"{level}.WCS", f"Staff WC {station}", (LENGTH_M - x, WC[1]))

    lounge = f"{level}.00"
    b.asset(lounge, f"lounge-table-{level}", f"Lounge table {station}", "furniture", "table",
            *b.at(lounge, 4.2, 2.0), 1.2, 0.8)
    b.asset(lounge, f"water-{level}", f"Water dispenser {station}", "fixture", "water_dispenser",
            *b.at(lounge, 5.7, 0.4), 0.4, 0.4, movable=False)
    key, name = ("vending-2", f"Vending machine {station}") if level == 2 else \
        ("vending-far", f"Staff coffee machine {station}")
    b.asset(lounge, key, name, "machine", "vending_machine", *b.at(lounge, 0.45, 2.5), 0.9, 0.8, 90.0,
            movable=key == "vending-far")
    b.asset(f"{level}.NS", f"nurse-desk-{level}", f"Nurses' desk {station}", "furniture", "desk",
            *b.at(f"{level}.NS", 3.0, 0.5), 2.0, 0.8, movable=False)
    b.asset(f"{level}.NS", f"med-cabinet-{level}", f"Drug cabinet {station}", "furniture", "cabinet",
            *b.at(f"{level}.NS", 5.6, 2.5), 0.6, 1.2, movable=False)
    b.asset(f"{level}.UT", f"utility-shelf-{level}", f"Utility shelf {station}", "furniture", "shelf",
            *b.at(f"{level}.UT", 2.7, 2.5), 0.5, 3.0, movable=False)
    b.asset(f"{level}.DR", f"doctor-desk-{level}", f"Doctors' desk {station}", "furniture", "desk",
            *b.at(f"{level}.DR", 2.0, 0.5), 2.0, 0.8, movable=False)
    b.asset(f"{level}.EQ", f"wheelchair-{level + 4}", f"Wheelchair {level + 4:02d}", "equipment", "wheelchair",
            *b.at(f"{level}.EQ", 0.5, 0.7), 0.7, 1.1)
    b.asset(f"{level}.EQ", f"stretcher-{level}", f"Stretcher {station}", "equipment", "stretcher",
            *b.at(f"{level}.EQ", 2.2, 1.2), 0.7, 2.0)
    b.asset(f"{level}.BATH", f"shower-chair-{level}", f"Shower chair {station}", "furniture", "chair",
            *b.at(f"{level}.BATH", 4.3, 0.5), 0.5, 0.5)


PUMPS = [  # (space ref, dx from west wall, distance from outer wall, in use)
    ("ER", 4.0, 2.0, True), ("ER", 12.0, 7.5, False), ("Resus", 5.6, 0.4, True), ("Bay 3", 0.4, 0.6, True),
    ("Bay 5", 0.4, 0.6, False), ("1.EQ", 0.4, 2.4, False), ("1.EQ", 0.9, 2.4, False), ("1.05", 0.8, 0.6, True),
    ("1.11", 1.9, 0.6, True), ("1.NS", 5.6, 4.5, False), ("2.EQ", 0.4, 2.4, False), ("2.NS", 5.6, 4.5, False),
    ("2.10", 0.8, 0.6, True), ("2.20", 0.8, 0.6, True), ("2.04", 1.9, 0.6, True),
]
IN_USE_STRETCHERS = ("stretcher-bay-1", "stretcher-bay-3", "zna-stretcher-1", "triage-stretcher")

PATIENTS = [  # (number, name, room ref, ward, step_free, visit code, appointment ref, offset min, label)
    ("100231", "Anna Becker", "2.04", "Ward 2A", False, "4711", "RAD", 30, "Chest X-ray"),
    ("100245", "Klaus Hoffmann", "2.08", "Ward 2A", True, "2580", "RAD-1", 15, "Knee X-ray"),
    ("100262", "Ingrid Schulz", "2.12", "Ward 2A", True, "9134", "RAD-2", 15, "Hip X-ray"),
    ("100278", "Mehmet Yilmaz", "2.16", "Ward 2A", False, "3307", "RAD-CT", 16, "CT abdomen"),
    ("100293", "Sabine Wagner", "2.22", "Ward 2A", True, "6652", "0.12", 16, "Blood test"),
    ("100301", "Josef Huber", "2.25", "Ward 2A", False, "8820", "0.13", 16, "Physiotherapy"),
    ("100318", "Gertrud Maier", "1.03", "Ward 1A", True, "1945", "0.12", 6, "Blood test"),
    ("100324", "Lukas Fischer", "1.11", "Ward 1A", False, "5038", "RAD-1", 45, "Shoulder X-ray"),
    ("100337", "Elif Demir", "1.18", "Ward 1A", False, "7261", None, None, ""),
    ("100349", "Franz Bauer", "1.22", "Ward 1A", True, "4419", "0.13", 60, "Physiotherapy"),
    ("100356", "Monika Schneider", "Entrance", "Outpatients", False, "3826", "RAD-CT", 10, "CT head"),
    ("100362", "Theo Wolf", "Bay 2", "Emergency", False, "6190", None, None, ""),
]

# Dr. Weber's coffee: the staff coffee machine on floor 1, although the ground-floor corridor right outside
# radiology may hold one too. That detour is what the placement optimiser should find.
COFFEE_FAR_ROOM = "1.00"
COFFEE = (COFFEE_FAR_ROOM, "vending-far")
# Routine stops are walked with a few minutes of jitter, so they stay >= 20 min apart.
STAFF = [  # (id, label, role, home ref, has pager, routine [(HH:MM, ref[, asset key])])
    ("S01", "Dr. Weber", "Radiology", "RAD", True, [
        ("07:30", "RAD"), ("08:00", "RAD-CT"), ("08:30", "RAD"), ("09:00", *COFFEE), ("09:20", "RAD"),
        ("10:00", "RAD-1"), ("10:30", "RAD"), ("12:00", "0.04"), ("12:40", "RAD"), ("14:00", *COFFEE),
        ("14:20", "RAD"), ("15:00", "RAD-2"), ("15:30", "RAD"), ("16:20", *COFFEE), ("16:40", "RAD"),
    ]),
    ("S02", "Jonas Keller", "Nursing", "2.NS", True, [
        ("06:00", "2.NS"), ("06:30", "2.04"), ("06:50", "2.08"), ("07:10", "2.12"), ("07:30", "2.UT"),
        ("07:50", "2.NS"), ("09:00", "2.16"), ("09:20", "2.22"), ("09:40", "2.25"), ("10:00", "2.NS"),
        ("12:00", "2.00"), ("12:30", "2.NS"), ("14:00", "2.EQ"), ("14:20", "2.NS"),
    ]),
    ("S03", "Lena Vogt", "Nursing", "1.NS", False, [
        ("06:00", "1.NS"), ("06:30", "1.03"), ("06:50", "1.11"), ("07:10", "1.18"), ("07:30", "1.UT"),
        ("07:50", "1.NS"), ("09:00", "1.22"), ("09:20", "1.EQ"), ("09:40", "1.NS"), ("13:00", "1.05"),
        ("13:20", "1.NS"),
    ]),
    ("S04", "Ali Kaya", "Transport", "Entrance", True, [
        ("07:00", "Entrance"), ("08:00", "2.04"), ("08:20", "RAD"), ("09:00", "1.11"), ("09:20", "RAD-1"),
        ("10:00", "Entrance"), ("11:00", "2.16"), ("11:20", "RAD-CT"), ("12:00", "Entrance"),
        ("13:30", "ER"), ("13:50", "1.EQ"), ("14:30", "Entrance"),
    ]),
    ("S05", "Maria Brandt", "Lab", "0.12", False, [
        ("07:00", "0.12"), ("07:30", "1.NS"), ("07:50", "2.NS"), ("08:15", "0.12"), ("10:00", "ER"),
        ("10:20", "0.12"), ("13:00", "1.NS"), ("13:20", "2.NS"), ("13:45", "0.12"), ("15:00", "ER"),
        ("15:20", "0.12"),
    ]),
]

COFFEE_PATIENT = "P001"
LIFT_CASE = ["P002", "P003", "P004", "P005", "P006"]


def staff_entries(b: Builder) -> list[dict]:
    entries, pagers = [], 0
    for staff_id, label, role, home, has_pager, routine in STAFF:
        entry = {"id": staff_id, "tag_id": f"T-{staff_id}", "label": label, "role": role, "space_id": b.ids[home],
                 "home_id": b.ids[home]}
        if has_pager:
            pagers += 1
            entry |= {"pager_id": f"PG-S{pagers:02d}", "pager_tag_id": f"T-PG-S{pagers:02d}"}
        entry["routine"] = [{"at": at, "space_id": b.ids[ref]} | ({"asset_id": b.asset_ids[key[0]]} if key else {})
                            for at, ref, *key in routine]
        entries.append(entry)
    return entries


def build() -> tuple[dict, dict]:
    b = Builder()
    ground_floor(b)
    ward_floor(b, 1)
    ward_floor(b, 2)
    lifts = [b.connector("lift", "Lift A", ["LIFT-A-G", "LIFT-A-1", "LIFT-A-2"], True),
             b.connector("lift", "Lift B", ["LIFT-B-G", "LIFT-B-1", "LIFT-B-2"], True)]
    stairs = b.connector("stairs", "Stairs", ["STAIRS-G", "STAIRS-1", "STAIRS-2"], False)
    for number, (ref, dx, d, _) in enumerate(PUMPS, start=1):
        b.asset(ref, f"pump-{number}", f"Infusion pump {number:02d}", "equipment", "infusion_pump",
                *b.at(ref, dx, d), 0.3, 0.25)
    b.place_beacons()

    building = {
        "building": {"id": BID, "name": "Demo Hospital Munich", "source": "procedural",
                     "source_ref": {"generator": "hospital.generate", "seed": "2", "parameters": "fixed"},
                     "license": "CC0-1.0", "attribution": "Procedurally generated for the Munich hackathon (CC0)",
                     "split": "demo", "units": "m"},
        "floors": b.floors, "spaces": b.spaces, "openings": b.openings,
        "vertical_connectors": b.connectors, "assets": b.assets,
    }
    patients = []
    for index, (number, name, room, ward, step_free, code, goal, offset, label) in enumerate(PATIENTS, start=1):
        patients.append({
            "id": f"P{index:03d}", "number": number, "name": name, "room_id": b.ids[room], "ward": ward,
            "pager_id": f"PG-{index:02d}", "tag_id": f"T-P{index:03d}", "pager_tag_id": f"T-PG-{index:02d}",
            "step_free": step_free, "visit_code": code,
            "appointment": {"space_id": b.ids[goal], "start_offset_min": offset, "label": label} if goal else None,
        })
    by_id = {p["id"]: p for p in patients}
    staff = staff_entries(b)
    asset_tags = [{"tag_id": f"T-A-PUMP-{n:02d}", "asset_id": b.asset_ids[f"pump-{n}"]} for n in range(1, len(PUMPS) + 1)]
    asset_tags += [{"tag_id": f"T-A-RS-{n:02d}", "asset_id": b.asset_ids[f"wheelchair-{n}"]} for n in range(1, 7)]
    in_use = [b.asset_ids[f"pump-{n}"] for n, (*_, used) in enumerate(PUMPS, start=1) if used]
    in_use += [asset_id for key, asset_id in b.asset_ids.items()
               if key.startswith("bed-") and key.removeprefix("bed-") not in FREE_BEDS]
    in_use += [b.asset_ids[key] for key in IN_USE_STRETCHERS]
    scenario = {
        "visiting_hours": "10:00–20:00",
        "main_entrance": b.ids["Entrance"],
        "ids": {
            "entrance": b.ids["Entrance"], "emergency": b.ids["ER"], "radiology": b.ids["RAD"],
            "floors": [f["id"] for f in b.floors], "lifts": lifts, "stairs": [stairs],
            "coffee_far": b.asset_ids["vending-far"], "coffee_far_room": b.ids[COFFEE_FAR_ROOM],
            "cafe": b.ids["0.04"], "pharmacy": b.ids["0.11"], "lab": b.ids["0.12"], "triage": b.ids["Triage"],
            "resuscitation": b.ids["Resus"], "toilet_f2": b.ids["2.WC"],
            "nurse_stations": [b.ids["1.NS"], b.ids["2.NS"]], "far_room_f2": b.ids["2.25"],
            "radiologist": "S01", "staff_pagers": [s["pager_id"] for s in staff if "pager_id" in s],
        },
        "beacons": b.beacons,
        "patients": patients,
        "staff": staff,
        "asset_tags": asset_tags,
        "in_use": in_use,
        "demo_cases": {
            "coffee": {"pager_id": by_id[COFFEE_PATIENT]["pager_id"], "patient_id": COFFEE_PATIENT,
                       "start": by_id[COFFEE_PATIENT]["room_id"], "goal": by_id[COFFEE_PATIENT]["appointment"]["space_id"],
                       "nearest_to_start": b.asset_ids["vending-2"], "best_detour": b.ids["0.04"]},
            "lift": {"floor_id": Builder.floor_id(2), "pager_ids": [by_id[p]["pager_id"] for p in LIFT_CASE],
                     "patient_ids": LIFT_CASE},
        },
    }
    return building, scenario


def main() -> None:
    building, scenario = build()
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in (("building.json", building), ("scenario.json", scenario)):
        (OUT / name).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{OUT}: {len(building['spaces'])} spaces, {len(building['assets'])} assets, "
          f"{len(scenario['beacons'])} beacons, {len(scenario['patients'])} patients, {len(scenario['staff'])} staff")


if __name__ == "__main__":
    main()
