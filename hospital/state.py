"""Shared in-memory hospital state: building, router, tags, beacons, patients, lift bookings and the clock.

One instance per server. Callers hold `h.lock` around everything that reads or changes it."""

import itertools
import json
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon

from core.building_map import OUTSIDE, BuildingMap
from core.walk import Place, WalkRoute, WalkRouter
from hospital.history import History, Sample, load_file
from hospital.lifts import LiftScheduler

ROOT = Path(__file__).resolve().parent.parent
HOSPITALS = ROOT / "hospitals"
IMPORTED = ROOT / "imported"
HISTORY_FILE = "history.json.gz"
WALKING_SPEED_M_S = 1.2
OFF_ROUTE_M = 5.0
BEACON_SPACING_M = 10.0
NO_BEACON_TYPES = ("lift", "stair")
WALKWAY_TYPES = ("corridor", "hall")
WALKWAY_ROOM_TYPES = ("corridor", "ward", "lobby", "walkway")


@dataclass
class Tag:
    tag_id: str
    kind: str
    label: str
    ref: str | None
    floor_id: str | None = None
    x: float | None = None
    y: float | None = None
    space_id: str | None = None
    seen_at: float | None = None
    role: str = ""


@dataclass
class Appointment:
    space_id: str
    start: float
    label: str = ""


@dataclass
class Patient:
    id: str
    number: str
    name: str
    room_id: str
    ward: str
    pager_id: str
    tag_id: str
    pager_tag_id: str
    step_free: bool
    visit_code: str
    appointment: Appointment | None = None
    stops: list[dict] = field(default_factory=list)
    stops_ordered: bool = False
    plan: dict | None = None
    kind: str = "patient"
    role: str = ""
    search: dict | None = None
    destination: dict | None = None
    avoid: set[str] = field(default_factory=set)  # lifts, stairs, walkways this holder's routes keep away from
    manual: bool = False  # a test pager: the simulator never walks it, only the L key does


def route_json(walk: WalkRoute) -> dict:
    seconds = walk.cost_m / WALKING_SPEED_M_S
    return {"points": walk.points, "instructions": walk.instructions, "walk_m": walk.walk_m, "cost_m": walk.cost_m,
            "seconds": round(seconds, 1), "minutes": round(seconds / 60, 1), "spaces": walk.spaces}


def distance_to_route(points: list[dict], floor_id: str, x: float, y: float) -> float:
    here = Point(x, y)
    on_floor = [here.distance(Point(p["x"], p["y"])) for p in points if p["floor_id"] == floor_id]
    segments = [here.distance(LineString([(a["x"], a["y"]), (b["x"], b["y"])]))
                for a, b in itertools.pairwise(points)
                if a["floor_id"] == b["floor_id"] == floor_id and (a["x"], a["y"]) != (b["x"], b["y"])]
    return min(on_floor + segments, default=math.inf)


class Hospital:
    def __init__(self, root: Path, bmap: BuildingMap, scenario: dict, clock: Callable[[], float] = time.time) -> None:
        self.root = root
        self.bmap = bmap
        self.scenario = scenario
        self.clock = clock
        self.lock = threading.RLock()
        self.router = WalkRouter(bmap)
        self.lifts = LiftScheduler(bmap)
        self.visiting_hours: str = scenario.get("visiting_hours", "")
        self.entrance_id: str = scenario["main_entrance"]
        self.in_use: set[str] = set(scenario.get("in_use", []))
        self.shapes = {space_id: Polygon(space["polygon"]) for space_id, space in bmap.spaces.items()}
        self.beacons: dict[str, dict] = {}
        for beacon in scenario.get("beacons", []):
            space_id = beacon.get("space_id") or self.place_at(beacon["floor_id"], beacon["x"], beacon["y"]).space_id
            self.beacons[beacon["id"]] = {**beacon, "space_id": space_id}
        self.sightings: dict[str, list[dict]] = {}
        self.history = History()
        self.tags: dict[str, Tag] = {}
        self.patients: dict[str, Patient] = {}
        now = self.now()
        for item in scenario.get("patients", []):
            booked = item.get("appointment")
            appointment = Appointment(booked["space_id"], now + 60.0 * booked["start_offset_min"],
                                      booked.get("label", "")) if booked else None
            patient = Patient(item["id"], item["number"], item["name"], item["room_id"], item["ward"],
                              item["pager_id"], item["tag_id"], item["pager_tag_id"], item["step_free"],
                              item["visit_code"], appointment)
            self.patients[patient.id] = patient
            x, y = bmap.anchor(patient.room_id)
            self.add_tag(Tag(patient.tag_id, "patient", patient.name, patient.id), patient.room_id, x, y)
            self.add_tag(Tag(patient.pager_tag_id, "pager", f"Pager {patient.pager_id}", patient.id), patient.room_id, x, y)
        for item in scenario.get("asset_tags", []):
            asset = bmap.assets[item["asset_id"]]
            x, y = asset["position"]
            self.add_tag(Tag(item["tag_id"], "asset", asset["name"], asset["id"]), asset["space_id"], x, y)
        for item in scenario.get("staff", []):
            x, y = bmap.anchor(item["space_id"])
            role = item.get("role", "")
            if item.get("pager_id"):
                staff = Patient(item["id"], "", item["label"], item.get("home_id") or item["space_id"], role,
                                item["pager_id"], item["tag_id"], item["pager_tag_id"], False, "", kind="staff",
                                role=role, manual=item.get("manual", False))
                self.patients[staff.id] = staff
                self.add_tag(Tag(staff.pager_tag_id, "pager", f"Pager {staff.pager_id}", staff.id, role=role),
                             item["space_id"], x, y)
            self.add_tag(Tag(item["tag_id"], "staff", item["label"], item.get("id"), role=role), item["space_id"], x, y)
        # Where each pager started, so a demo walk can be replayed (dispatch.walk_reset).
        self.home = {tag.tag_id: (tag.floor_id, tag.x, tag.y) for tag in self.tags.values() if tag.kind == "pager"}

    @classmethod
    def load(cls, root: Path = HOSPITALS / "demo-klinikum", clock: Callable[[], float] = time.time) -> "Hospital":
        bmap = BuildingMap.load(root)
        scenario_file = root / "scenario.json"
        if scenario_file.exists():
            scenario = json.loads(scenario_file.read_text(encoding="utf-8"))
        else:
            scenario = auto_scenario(bmap)
        h = cls(root, bmap, scenario, clock)
        if (root / HISTORY_FILE).exists():
            h.history.extend(load_file(root / HISTORY_FILE, h.now(), h.tags, bmap))
        return h

    def now(self) -> float:
        return self.clock()

    def add_tag(self, tag: Tag, space_id: str, x: float, y: float) -> None:
        tag.floor_id, tag.x, tag.y, tag.space_id = self.bmap.spaces[space_id]["floor_id"], x, y, space_id
        tag.seen_at = self.now()
        self.tags[tag.tag_id] = tag
        self.history.labels[tag.tag_id] = tag.label

    def patient_by_pager(self, pager_id: str) -> Patient:
        for patient in self.patients.values():
            if patient.pager_id == pager_id:
                return patient
        raise KeyError(pager_id)

    def find_patients(self, query: str) -> list[Patient]:
        query = query.strip()
        if not query:
            return []
        patients = [p for p in self.patients.values() if p.kind == "patient"]
        exact = [p for p in patients if p.number == query]
        if exact:
            return exact
        lowered = query.lower()
        return [p for p in patients if lowered in p.name.lower()]

    def place_of(self, patient: Patient) -> Place:
        for tag_id in (patient.pager_tag_id, patient.tag_id):
            tag = self.tags.get(tag_id)
            if tag and tag.space_id and tag.x is not None and tag.y is not None:
                return Place(tag.space_id, (tag.x, tag.y))
        return self.router.place_for_space(patient.room_id)

    def place_at(self, floor_id: str, x: float, y: float) -> Place:
        point = Point(x, y)
        candidates = [space_id for space_id, space in self.bmap.spaces.items() if space["floor_id"] == floor_id]
        if not candidates:
            raise KeyError(floor_id)
        space_id = min(candidates, key=lambda candidate: self.shapes[candidate].distance(point))
        return Place(space_id, (x, y))

    def set_position(self, tag_id: str, floor_id: str, x: float, y: float) -> Tag:
        tag = self.tags[tag_id]
        space_id = self.place_at(floor_id, x, y).space_id
        moved = [tag]
        if tag.kind in ("patient", "pager", "staff") and tag.ref in self.patients:
            patient = self.patients[tag.ref]
            moved = [self.tags[patient.tag_id], self.tags[patient.pager_tag_id]]
            points = (patient.plan or {}).get("route", {}).get("points", [])
            if patient.plan is not None and distance_to_route(points, floor_id, x, y) > OFF_ROUTE_M:
                patient.plan = None
        elif tag.kind == "asset":
            asset = self.bmap.assets[tag.ref]
            old_space = asset["space_id"]
            asset.update(position=[x, y], space_id=space_id, floor_id=floor_id)
            self.router.invalidate([old_space, space_id])
        now = self.now()
        for item in moved:
            item.floor_id, item.x, item.y, item.space_id, item.seen_at = floor_id, x, y, space_id, now
        # One sample per person: a patient's (or staff member's) pager moves with them and is not counted twice.
        self.history.add(Sample(now, moved[0].tag_id, moved[0].kind, moved[0].role, floor_id, x, y, space_id))
        return tag


def auto_scenario(bmap: BuildingMap) -> dict:
    """Scenario for an imported plan: beacons in every room plus every ~10 m along walkways, nobody on site."""
    beacons = []
    for floor in sorted(bmap.floors.values(), key=lambda f: f["level"]):
        count = 0
        for space_id, space in bmap.spaces.items():
            if space["floor_id"] != floor["id"] or space["type"] in NO_BEACON_TYPES:
                continue
            for x, y in beacon_points(bmap, space):
                count += 1
                beacons.append({"id": f"B-F{floor['level']}-{count:02d}", "floor_id": floor["id"],
                                "x": round(x, 2), "y": round(y, 2), "space_id": space_id})
    entrance = main_entrance(bmap)
    # One staff pager at the entrance, so a freshly imported plan can be walked with the pager right away.
    staff = [{"id": "S01", "tag_id": "T-S01", "label": "Staff", "role": "", "space_id": entrance, "home_id": entrance,
              "pager_id": "PG-S01", "pager_tag_id": "T-PG-S01", "routine": []}]
    return {"visiting_hours": "", "main_entrance": entrance, "beacons": beacons, "patients": [],
            "staff": staff, "asset_tags": [], "in_use": [], "demo_cases": {}}


def beacon_points(bmap: BuildingMap, space: dict) -> list[tuple[float, float]]:
    points = [bmap.anchor(space["id"])]
    if space["type"] not in WALKWAY_TYPES and space["room_type"] not in WALKWAY_ROOM_TYPES:
        return points
    shape = Polygon(space["polygon"])
    x0, y0, x1, y1 = shape.bounds
    step = max(1.0, math.sqrt(shape.area / 20_000))
    xs, ys = np.meshgrid(np.arange(x0 + step / 2, x1, step), np.arange(y0 + step / 2, y1, step))
    xs, ys = xs.ravel(), ys.ravel()
    inside = shapely.contains_xy(shape, xs, ys)
    xs, ys = xs[inside], ys[inside]
    # Most central candidates first, so beacons sit on the middle line of a corridor.
    depth = shapely.distance(shape.exterior, shapely.points(xs, ys))
    for i in np.argsort(-depth, kind="stable"):
        if min(math.hypot(xs[i] - px, ys[i] - py) for px, py in points) >= BEACON_SPACING_M:
            points.append((float(xs[i]), float(ys[i])))
    return points


def main_entrance(bmap: BuildingMap) -> str:
    level = {floor_id: abs(floor["level"]) for floor_id, floor in bmap.floors.items()}
    outside = [space_id for opening in bmap.openings.values() if OUTSIDE in opening["space_ids"]
               for space_id in opening["space_ids"] if space_id != OUTSIDE and space_id in bmap.spaces]
    if outside:
        return min(outside, key=lambda space_id: level[bmap.spaces[space_id]["floor_id"]])
    walkways = [space_id for space_id, space in bmap.spaces.items() if space["type"] in WALKWAY_TYPES]
    return min(walkways or list(bmap.spaces), key=lambda space_id: level[bmap.spaces[space_id]["floor_id"]])


def available_buildings() -> list[dict]:
    found = [(path.parent, "demo") for path in sorted(HOSPITALS.glob("*/scenario.json"))
             if (path.parent / "building.json").exists()]
    found += [(path.parent, "imported") for path in sorted(IMPORTED.glob("*/building.json"))]
    result = []
    for directory, source in found:
        data = json.loads((directory / "building.json").read_text(encoding="utf-8"))
        result.append({"id": directory.name, "name": data["building"].get("name") or directory.name, "source": source,
                       "floors": len(data["floors"])})
    return result


def building_dir(building_id: str) -> Path:
    for base in (HOSPITALS, IMPORTED):
        directory = base / building_id
        if directory.parent == base and building_id not in ("", ".", "..") and (directory / "building.json").exists():
            return directory
    raise KeyError(building_id)
