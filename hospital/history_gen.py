"""Demo week of movement history: staff walk their daily routines, patients go to appointments, visitors come to
the wards. Every walk follows WalkRouter.route, sampled every ~2 s of walking and every 60 s while standing.

    uv run python -m hospital.history_gen [--root hospitals/demo-klinikum] [--scenario ...] [--out ...]

Writes the `history.load_file` format; the same seed gives the same file."""

import argparse
import gzip
import itertools
import json
import math
import random
from pathlib import Path

from core.building_map import BuildingMap
from core.walk import Place, WalkRouter
from hospital.history import FILE_VERSION
from hospital.state import HISTORY_FILE, HOSPITALS, WALKING_SPEED_M_S

DAY_S = 86_400.0
WALK_SAMPLE_S = 2.0
STEP_M = WALKING_SPEED_M_S * WALK_SAMPLE_S
DWELL_SAMPLE_S = 60.0
FLOOR_CHANGE_S = 30.0
DEPART_JITTER_S = 240.0
DWELL_JITTER_M = 0.3
SHIFT_TAIL_S = (1800.0, 3600.0)
DEFAULT_SHIFT = ("07:30", "15:30")
DRINKS = ("vending_machine", "coffee_machine", "water_dispenser")
APPOINTMENT_ROOMS = ("radiology", "therapy", "lab", "treatment_bay")
APPOINTMENT_CHANCE = 0.45
VISITORS_PER_DAY = (18, 30)
VISITING = ("10:00", "19:00")
VISITOR_SNACK_ROOMS = ("cafe", "kiosk")
VISITOR_SNACK_CHANCE = 0.3


def seconds(clock: str) -> float:
    hours, minutes = clock.split(":")
    return 3600.0 * int(hours) + 60.0 * int(minutes)


class Week:
    def __init__(self, bmap: BuildingMap, scenario: dict, seed: int) -> None:
        self.bmap, self.scenario = bmap, scenario
        self.router = WalkRouter(bmap)
        self.random = random.Random(seed)
        self.rows: list[tuple[float, str, str, float, float]] = []
        self.routes: dict[tuple, list[dict] | None] = {}
        self.entrance = scenario["main_entrance"]
        self.coffee_far = scenario.get("ids", {}).get("coffee_far")

    def place(self, space_id: str, asset_id: str | None) -> Place:
        return self.router.place_for_asset(asset_id) if asset_id else self.router.place_for_space(space_id)

    def route(self, start: tuple[str, str | None], goal: tuple[str, str | None], step_free: bool) -> list[dict] | None:
        key = (start, goal, step_free)
        if key not in self.routes:
            try:
                self.routes[key] = self.router.route(self.place(*start), self.place(*goal), step_free).points
            except ValueError:
                self.routes[key] = None
        return self.routes[key]

    def dwell(self, tag_id: str, target: tuple[str, str | None], start: float, until: float) -> None:
        place = self.place(*target)
        floor_id = self.bmap.spaces[place.space_id]["floor_id"]
        t = start
        while t < until:
            x = place.point[0] + self.random.uniform(-DWELL_JITTER_M, DWELL_JITTER_M)
            y = place.point[1] + self.random.uniform(-DWELL_JITTER_M, DWELL_JITTER_M)
            self.rows.append((t, tag_id, floor_id, x, y))
            t += DWELL_SAMPLE_S

    def walk(self, tag_id: str, start: tuple[str, str | None], goal: tuple[str, str | None], t: float,
             step_free: bool = False) -> float:
        """Walks start → goal from time t; returns the arrival time (t itself when there is no route)."""
        points = self.route(start, goal, step_free)
        if not points:
            return t
        first = points[0]
        self.rows.append((t, tag_id, first["floor_id"], first["x"], first["y"]))
        ahead = STEP_M
        for a, b in itertools.pairwise(points):
            if a["floor_id"] != b["floor_id"]:
                t += FLOOR_CHANGE_S
                self.rows.append((t, tag_id, b["floor_id"], b["x"], b["y"]))
                ahead = STEP_M
                continue
            length = math.dist((a["x"], a["y"]), (b["x"], b["y"]))
            while ahead <= length:
                f = ahead / length
                self.rows.append((t + ahead / WALKING_SPEED_M_S, tag_id, a["floor_id"],
                                  a["x"] + f * (b["x"] - a["x"]), a["y"] + f * (b["y"] - a["y"])))
                ahead += STEP_M
            ahead -= length
            t += length / WALKING_SPEED_M_S
        last = points[-1]
        self.rows.append((t, tag_id, last["floor_id"], last["x"], last["y"]))
        return t

    def jitter(self, spread: float) -> float:
        return self.random.uniform(-spread, spread)

    def drink_in(self, space_id: str) -> str | None:
        far = self.bmap.assets.get(self.coffee_far or "")
        if far and far["space_id"] == space_id:
            return far["id"]
        found = sorted(a["id"] for a in self.bmap.assets.values() if a["space_id"] == space_id and a["type"] in DRINKS)
        return found[0] if found else None

    def staff_day(self, item: dict, day: float) -> None:
        home = item.get("home_id") or item["space_id"]
        routine = sorted(item.get("routine") or [{"at": DEFAULT_SHIFT[0], "space_id": home}], key=lambda e: e["at"])
        stops = []
        for entry in routine:
            space_id = entry["space_id"]
            asset_id = entry.get("asset_id") or (self.drink_in(space_id) if space_id != home else None)
            stops.append(((space_id, asset_id), day + seconds(entry["at"]) + self.jitter(DEPART_JITTER_S)))
        end = (stops[-1][1] + self.random.uniform(*SHIFT_TAIL_S) if item.get("routine")
               else day + seconds(DEFAULT_SHIFT[1]) + self.jitter(DEPART_JITTER_S))
        here, t = (self.entrance, None), stops[0][1]
        for number, (target, depart) in enumerate(stops):
            t = self.walk(item["tag_id"], here, target, max(t, depart))
            leave = stops[number + 1][1] if number + 1 < len(stops) else end
            leave = max(leave, t + DWELL_SAMPLE_S)
            self.dwell(item["tag_id"], target, t + DWELL_SAMPLE_S, leave)
            here, t = target, leave
        self.walk(item["tag_id"], here, (self.entrance, None), t)

    def patient_day(self, item: dict, day: float, rooms: list[str]) -> None:
        if self.random.random() > APPOINTMENT_CHANCE:
            return
        booked = (item.get("appointment") or {}).get("space_id")
        target = booked if booked and self.random.random() < 0.5 else self.random.choice(rooms)
        home, goal = (item["room_id"], None), (target, None)
        t = day + seconds("08:00") + self.random.uniform(0, 8 * 3600)
        self.dwell(item["tag_id"], home, t - 300, t)
        t = self.walk(item["tag_id"], home, goal, t, item.get("step_free", False))
        leave = t + self.random.uniform(900, 2400)
        self.dwell(item["tag_id"], goal, t + DWELL_SAMPLE_S, leave)
        t = self.walk(item["tag_id"], goal, home, leave, item.get("step_free", False))
        self.dwell(item["tag_id"], home, t + DWELL_SAMPLE_S, t + 300)

    def visitor_day(self, number: int, day: float, rooms: list[str], snacks: list[str]) -> None:
        tag_id = f"V-D{int(day // DAY_S) + 1}-{number:02d}"
        entrance, ward = (self.entrance, None), (self.random.choice(rooms), None)
        t = day + self.random.uniform(seconds(VISITING[0]), seconds(VISITING[1]))
        t = self.walk(tag_id, entrance, ward, t)
        leave = t + self.random.uniform(1200, 3600)
        self.dwell(tag_id, ward, t + DWELL_SAMPLE_S, leave)
        here = ward
        if snacks and self.random.random() < VISITOR_SNACK_CHANCE:
            snack = (self.random.choice(snacks), None)
            t = self.walk(tag_id, ward, snack, leave)
            leave = t + self.random.uniform(600, 1200)
            self.dwell(tag_id, snack, t + DWELL_SAMPLE_S, leave)
            here = snack
        self.walk(tag_id, here, entrance, leave)

    def generate(self, days: int) -> None:
        spaces = sorted(self.bmap.spaces.values(), key=lambda s: s["id"])
        appointment_rooms = [s["id"] for s in spaces if s["room_type"] in APPOINTMENT_ROOMS]
        patient_rooms = sorted({p["room_id"] for p in self.scenario.get("patients", [])}
                               | {s["id"] for s in spaces if s["room_type"] == "patient_room"})
        snacks = [s["id"] for s in spaces if s["room_type"] in VISITOR_SNACK_ROOMS]
        for number in range(days):
            day = number * DAY_S
            for item in self.scenario.get("staff", []):
                self.staff_day(item, day)
            if appointment_rooms:
                for item in self.scenario.get("patients", []):
                    self.patient_day(item, day, appointment_rooms)
            if patient_rooms:
                for visitor in range(self.random.randint(*VISITORS_PER_DAY)):
                    self.visitor_day(visitor + 1, day, patient_rooms, snacks)

    def save(self, path: Path, days: int) -> None:
        end = days * DAY_S
        rows = [[round(t - end, 1), tag_id, floor_id, round(x, 2), round(y, 2)]
                for t, tag_id, floor_id, x, y in sorted(self.rows, key=lambda row: (row[0], row[1]))
                if t <= end]
        # No name and mtime in the gzip header, so the same seed gives a byte-identical file.
        with open(path, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as packed:
            packed.write(json.dumps({"version": FILE_VERSION, "samples": rows}, separators=(",", ":")).encode())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=HOSPITALS / "demo-klinikum")
    parser.add_argument("--scenario", type=Path, help="default: <root>/scenario.json")
    parser.add_argument("--out", type=Path, help=f"default: <root>/{HISTORY_FILE}")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    scenario = json.loads((args.scenario or args.root / "scenario.json").read_text(encoding="utf-8"))
    week = Week(BuildingMap.load(args.root), scenario, args.seed)
    week.generate(args.days)
    out = args.out or args.root / HISTORY_FILE
    week.save(out, args.days)
    print(f"{len(week.rows)} samples → {out} ({out.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
