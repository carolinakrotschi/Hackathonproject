"""Demo simulator on an accelerated clock: patients who may go walk their planned route, wait for their lift slot and
ride, staff walk their daily routine, and pagers emit beacon sightings. Everything moves in sim time (`h.now()`)."""

import logging
import math
import random
import threading
import time
from datetime import datetime
from dataclasses import dataclass

from hospital import dispatch, tracking
from hospital.state import WALKING_SPEED_M_S, Hospital

SIGHTED_BEACONS = 3
RSSI_AT_1M = -59.0


STAFF_LIFT_PAUSE_S = 20.0


class FastClock:
    def __init__(self, factor: float, base: float | None = None) -> None:
        self.factor, self.base, self.real0 = factor, time.time() if base is None else base, time.monotonic()

    def __call__(self) -> float:
        return self.base + (time.monotonic() - self.real0) * self.factor


@dataclass
class Cursor:
    plan: dict
    index: int = 0
    metres: float = 0.0
    hold_until: float | None = None
    held: int = -1


class Simulator:
    def __init__(self, h: Hospital, factor: float = 30.0, tick_s: float = 0.25) -> None:
        self.h, self.factor, self.tick_s = h, factor, tick_s
        self.cursors: dict[str, Cursor] = {}
        self.visited: set[tuple[str, str, str]] = set()
        self.random = random.Random(7)
        self.halt = threading.Event()
        self.thread: threading.Thread | None = None
        self.last = 0.0

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self.halt.clear()
        with self.h.lock:
            self.h.clock = FastClock(self.factor, self.h.now())
            self.last = self.h.now()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.halt.set()
        if self.thread:
            self.thread.join(timeout=5)
        self.thread = None
        with self.h.lock:
            self.h.clock = FastClock(1.0, self.h.now())

    def loop(self) -> None:
        while not self.halt.wait(self.tick_s):
            with self.h.lock:
                try:
                    self.tick()
                except Exception:
                    logging.exception("simulator tick failed")

    def tick(self) -> None:
        h = self.h
        now = h.now()
        budget, self.last = WALKING_SPEED_M_S * (now - self.last), now
        for patient in h.patients.values():
            if patient.manual:
                continue
            if patient.kind == "staff" and not (patient.search or patient.destination):
                continue
            if patient.kind != "staff" and not (patient.appointment or patient.stops):
                continue
            if dispatch.guidance(h, patient.pager_id)["status"] != "go":
                self.cursors.pop(patient.id, None)
                continue
            cursor = self.cursors.get(patient.id)
            if cursor is None or cursor.plan is not patient.plan:
                cursor = self.cursors[patient.id] = Cursor(patient.plan)
            floor_id, x, y = self.advance(cursor, budget, now)
            plan = patient.plan
            tracking.ingest(h, self.sightings(patient.pager_tag_id, floor_id, x, y))
            patient.plan = plan  # the beacon estimate may stray > OFF_ROUTE_M; the true point below is on route
            h.set_position(patient.pager_tag_id, floor_id, x, y)
        for item in h.scenario.get("staff", []):
            holder = h.patients.get(item.get("id"))
            if not (holder and (holder.search or holder.destination or holder.manual)):
                self.routine(item, budget, now)

    def routine(self, item: dict, budget: float, now: float) -> None:
        h = self.h
        key = item.get("id") or item["tag_id"]
        cursor = self.cursors.get(key)
        if cursor is None:
            clock = datetime.fromtimestamp(now)
            day = clock.strftime("%Y-%m-%d")
            due = [e for e in item.get("routine", []) if e["at"] <= clock.strftime("%H:%M")]
            for entry in due[:-1]:
                self.visited.add((key, day, entry["at"]))
            entry = next((e for e in due[-1:] if (key, day, e["at"]) not in self.visited), None)
            if entry is None:
                return
            self.visited.add((key, day, entry["at"]))
            tag = h.tags[item["tag_id"]]
            if tag.floor_id is None or tag.space_id == entry["space_id"]:
                return
            here = h.place_at(tag.floor_id, tag.x, tag.y)
            points = h.router.route(here, h.router.place_for_space(entry["space_id"]), False).points
            slots = [{"index": i, "start": 0.0, "end": STAFF_LIFT_PAUSE_S}
                     for i in range(len(points) - 1) if points[i]["floor_id"] != points[i + 1]["floor_id"]]
            plan = {"route": {"points": points}, "lift_slots": slots, "stop_points": [], "stops": []}
            cursor = self.cursors[key] = Cursor(plan)
        floor_id, x, y = self.advance(cursor, budget, now)
        h.set_position(item.get("pager_tag_id") or item["tag_id"], floor_id, x, y)
        if cursor.index >= len(cursor.plan["route"]["points"]) - 1:
            del self.cursors[key]

    def advance(self, cursor: Cursor, budget: float, now: float) -> tuple[str, float, float]:
        plan = cursor.plan
        points = plan["route"]["points"]
        lifts = {slot["index"]: slot for slot in plan["lift_slots"]}
        dwell = dict(zip(plan["stop_points"], (stop["dwell_s"] for stop in plan["stops"]), strict=False))
        while cursor.index < len(points) - 1:
            i = cursor.index
            if cursor.metres == 0 and cursor.held != i and (i in lifts or i in dwell):
                if cursor.hold_until is None:
                    if i in lifts and now < lifts[i]["start"]:
                        break
                    seconds = lifts[i]["end"] - lifts[i]["start"] if i in lifts else dwell[i]
                    cursor.hold_until = now + seconds
                if now < cursor.hold_until:
                    break
                cursor.hold_until, cursor.held = None, i
                if i in lifts:
                    cursor.index += 1
                continue
            a, b = points[i], points[i + 1]
            length = math.dist((a["x"], a["y"]), (b["x"], b["y"])) if a["floor_id"] == b["floor_id"] else 0.0
            if cursor.metres + budget < length:
                cursor.metres += budget
                break
            budget -= length - cursor.metres
            cursor.index, cursor.metres = i + 1, 0.0
        a = points[cursor.index]
        if cursor.metres == 0 or cursor.index + 1 >= len(points):
            return a["floor_id"], a["x"], a["y"]
        b = points[cursor.index + 1]
        f = cursor.metres / math.dist((a["x"], a["y"]), (b["x"], b["y"]))
        return a["floor_id"], a["x"] + f * (b["x"] - a["x"]), a["y"] + f * (b["y"] - a["y"])

    def sightings(self, tag_id: str, floor_id: str, x: float, y: float) -> list[dict]:
        beacons = sorted((math.dist((x, y), (b["x"], b["y"])), b["id"])
                         for b in self.h.beacons.values() if b["floor_id"] == floor_id)
        return [{"tag_id": tag_id, "beacon_id": beacon_id,
                 "rssi": round(RSSI_AT_1M - 20 * math.log10(max(d, 0.5)) + self.random.uniform(-1.5, 1.5), 1)}
                for d, beacon_id in beacons[:SIGHTED_BEACONS]]
