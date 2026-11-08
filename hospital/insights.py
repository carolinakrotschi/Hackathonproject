"""Insights from the movement history: recurring daily routes per person, and better places for objects people walk to.

A person's day is their samples up to a gap of DAY_GAP_S. A stay is a run of samples in one space lasting at least
MIN_STAY_S (lifts and stairwells never count). A routine is a round trip from the person's home space (where they
spend the most time) through other stays and back, seen on at least min_days days. A use of an object is a run of
samples in its space within NEAR_ASSET_M of it lasting at least MIN_USE_S; its trip goes from the stay before to the
stay after."""

import itertools
import math
from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np
from shapely.geometry import Polygon

from core.walk import CLEARANCE_M, Place, footprint
from hospital import optimizer, rules
from hospital.state import WALKING_SPEED_M_S, Hospital, route_json

DAY_S = 86_400.0
DAY_GAP_S = 4 * 3600.0
MIN_STAY_S = 150.0
PASS_THROUGH = ("lift", "stair", "stairwell")
NEAR_ASSET_M = 3.0
MIN_USE_S = 45.0
MIN_SAVING_MIN = 0.5
USE_TYPES = ("vending_machine", "coffee_machine", "water_dispenser")
SPOT_TRIES = 5


@dataclass
class Stay:
    space_id: str
    start: float
    end: float
    point: tuple[float, float]


@dataclass
class Trip:
    tag_id: str
    prev: str
    next: str


# ---------- days and stays ----------

def days_of(h: Hospital, **filters) -> dict[str, list[np.ndarray]]:
    """History rows per tag in time order, split into days at gaps longer than DAY_GAP_S."""
    history = h.history
    order = history.ordered(history.mask(**filters))
    if not len(order):
        return {}
    columns = history.columns()
    tag, t = columns["tag"][order], columns["t"][order]
    cuts = np.flatnonzero((tag[1:] != tag[:-1]) | (np.diff(t) > DAY_GAP_S)) + 1
    result: dict[str, list[np.ndarray]] = defaultdict(list)
    for rows in np.split(order, cuts):
        result[history.tag_codes.values[columns["tag"][rows[0]]]].append(rows)
    return result


def stays(h: Hospital, rows: np.ndarray) -> list[Stay]:
    columns = h.history.columns()
    t, space, x, y = (columns[name][rows] for name in ("t", "space", "x", "y"))
    starts = np.flatnonzero(np.r_[True, space[1:] != space[:-1]])
    result: list[Stay] = []
    for a, b in zip(starts, np.r_[starts[1:], len(rows)], strict=True):
        space_id = h.history.space_codes.values[space[a]]
        found = h.bmap.spaces.get(space_id)
        if found is None or found["type"] in PASS_THROUGH or found["room_type"] in PASS_THROUGH:
            continue
        end = float(t[b] if b < len(rows) else t[b - 1])
        if end - t[a] < MIN_STAY_S:
            continue
        if result and result[-1].space_id == space_id:
            result[-1].end = end
            continue
        middle = (a + b - 1) // 2
        result.append(Stay(space_id, float(t[a]), end, (float(x[middle]), float(y[middle]))))
    return result


def observed_days(h: Hospital) -> int:
    t = h.history.columns()["t"]
    return max(1, round(float(t.max() - t.min()) / DAY_S)) if len(t) else 1


def who(h: Hospital, tag_id: str, rows: np.ndarray | None = None) -> tuple[str, str]:
    tag = h.tags.get(tag_id)
    if tag:
        return tag.label, tag.role
    role = h.history.role_codes.values[h.history.role[int(rows[0])]] if rows is not None and len(rows) else ""
    return h.history.labels.get(tag_id, tag_id), role or ""


def number(value: float) -> str:
    return f"{value:g}"


def times_a_day(value: float) -> str:
    return {1: "once", 2: "twice"}.get(value, f"{number(value)} times")


def space_ref(h: Hospital, space_id: str) -> dict:
    return {"space_id": space_id, "name": h.bmap.spaces[space_id]["name"]}


# ---------- routines ----------

def routines(h: Hospital, *, tag_id=None, role=None, min_days=3) -> list[dict]:
    found = []
    for tag, days in sorted(days_of(h, tag_id=tag_id, role=role).items()):
        if len(days) < min_days:
            continue
        per_day = [stays(h, rows) for rows in days]
        time_in: Counter = Counter()
        for day in per_day:
            for stay in day:
                time_in[stay.space_id] += stay.end - stay.start
        if not time_in:
            continue
        home = max(sorted(time_in), key=time_in.__getitem__)
        loops: dict[tuple, list[tuple[int, list[Stay]]]] = defaultdict(list)
        for number, day in enumerate(per_day):
            at_home = [i for i, stay in enumerate(day) if stay.space_id == home]
            for i, j in itertools.pairwise(at_home):
                loops[tuple(stay.space_id for stay in day[i:j + 1])].append((number, day[i:j + 1]))
        label, tag_role = who(h, tag, days[0])
        for sequence, seen in loops.items():
            days_seen = len({number for number, _ in seen})
            if days_seen < min_days:
                continue
            places = [Place(stay.space_id, stay.point) for stay in seen[0][1]]
            try:
                route = route_json(h.router.route(places[0], places[-1], via=places[1:-1]))
            except ValueError:
                route = None
            per = round(len(seen) / days_seen, 1)
            found.append({"tag_id": tag, "label": label, "role": tag_role, "days": days_seen, "per_day": per,
                          "times": len(seen), "sequence": [space_ref(h, space_id) for space_id in sequence],
                          "route": route,
                          "minutes_per_day": round(route["minutes"] * len(seen) / days_seen, 1) if route else None})
    return sorted(found, key=lambda r: (-(r["minutes_per_day"] or 0), r["tag_id"]))


# ---------- objects people walk to ----------

def trips(h: Hospital, asset: dict, days: dict[str, list[np.ndarray]]) -> list[Trip]:
    history = h.history
    code = history.space_codes.index.get(asset["space_id"])
    if code is None:
        return []
    columns = history.columns()
    ax, ay = asset["position"]
    near = (columns["space"] == code) & (np.hypot(columns["x"] - ax, columns["y"] - ay) <= NEAR_ASSET_M)
    if not near.any():
        return []
    found = []
    for tag_id, tag_days in days.items():
        for rows in tag_days:
            close = near[rows]
            if not close.any():
                continue
            t = columns["t"][rows]
            edges = np.diff(np.r_[0, close.astype(np.int8), 0])
            around = None
            for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1), strict=True):
                if (t[b] if b < len(rows) else t[b - 1]) - t[a] < MIN_USE_S:
                    continue
                if around is None:
                    around = [s for s in stays(h, rows) if s.space_id != asset["space_id"]]
                before = next((s.space_id for s in reversed(around) if s.start < t[a]), None)
                after = next((s.space_id for s in around if s.start > t[a]), None)
                if before or after:
                    found.append(Trip(tag_id, before or after, after or before))
    return found


def movable_assets(h: Hospital) -> list[dict]:
    days, span = days_of(h), observed_days(h)
    result = []
    for asset in sorted(h.bmap.assets.values(), key=lambda a: a["id"]):
        # `movable` is about the room optimiser pushing furniture around; relocating a machine is a decision.
        if asset["type"] not in USE_TYPES:
            continue
        count = len(trips(h, asset, days))
        if count:
            result.append({"asset_id": asset["id"], "name": asset["name"], "type": asset["type"],
                           "space_id": asset["space_id"], "trips_per_day": round(count / span, 1)})
    return sorted(result, key=lambda a: (-a["trips_per_day"], a["asset_id"]))


class WalkTimes:
    """Walking seconds between places, cached per pair of keys (routes are symmetric)."""

    def __init__(self, h: Hospital) -> None:
        self.h, self.cache = h, {}

    def place(self, key: str) -> Place:
        return self.h.router.place_for_space(key)

    def seconds(self, a: str, b: str, place_b: Place | None = None) -> float:
        key = (a, b) if a <= b else (b, a)
        if key not in self.cache:
            try:
                walk = self.h.router.route(self.place(a), place_b or self.place(b))
                self.cache[key] = walk.cost_m / WALKING_SPEED_M_S
            except ValueError:
                self.cache[key] = math.inf
        return self.cache[key]

    def total(self, legs: Counter, target: str, place: Place | None = None) -> float:
        return sum(n * (self.seconds(prev, target, place) + self.seconds(after, target, place))
                   for (prev, after), n in legs.items())


def spot(h: Hospital, space_id: str, asset: dict) -> tuple[float, float, float] | None:
    """A wall-side pose for the asset in the space, reachable from a door and nearest to the space's walking point."""
    polygon = Polygon(h.bmap.spaces[space_id]["polygon"])
    polygon = polygon if polygon.is_valid else polygon.buffer(0)
    own = footprint(asset)
    others = [footprint(a) for a in h.bmap.assets.values() if a["space_id"] == space_id and a["id"] != asset["id"]]
    outside = [s for s in h.router.obstacles(space_id) if not s.equals(own) and not any(s.equals(o) for o in others)]
    layout = optimizer.Layout(polygon, optimizer.doors_of(h.bmap, space_id, polygon), outside + others)
    layout.rebuild()
    options = layout.candidates(*optimizer.size_of(asset))
    ok = (options["wall"] <= optimizer.WALL_GAP_M + 0.01) & layout.clear_of(options["box"], layout.shapes.values())
    tx, ty = h.router.place_for_space(space_id).point
    distance = np.hypot(options["x"] - tx, options["y"] - ty)
    for i in np.lexsort((options["rot"], distance)):
        if ok[i] and layout.try_add("#moved", options["box"][i], True):
            return round(float(options["x"][i]), 3), round(float(options["y"][i]), 3), float(options["rot"][i])
    return None


def placement(h: Hospital, asset_id: str) -> dict:
    asset = h.bmap.assets[asset_id]
    here = asset["space_id"]
    days, span = days_of(h), observed_days(h)
    found = trips(h, asset, days)
    users = Counter(trip.tag_id for trip in found)
    result = {"asset": {"id": asset_id, "name": asset["name"], "type": asset["type"], "space_id": here,
                        "space_name": h.bmap.spaces[here]["name"]},
              "proposal": None, "trips_per_day": round(len(found) / span, 1), "minutes_per_day_before": 0.0,
              "minutes_per_day_after": 0.0, "saving_min_per_day": 0.0,
              "users": [{"tag_id": tag_id, "label": who(h, tag_id)[0], "role": who(h, tag_id)[1], "trips": n}
                        for tag_id, n in sorted(users.items(), key=lambda item: (-item[1], item[0]))]}
    if not found:
        return result | {"reason": f"The movement history has no trips to “{asset['name']}”."}

    legs = Counter((trip.prev, trip.next) for trip in found)
    times = WalkTimes(h)
    per_day = lambda seconds: seconds / 60 / span  # noqa: E731
    before = times.total(legs, f"asset:{asset_id}", h.router.place_for_asset(asset_id))
    result["minutes_per_day_before"] = result["minutes_per_day_after"] = round(per_day(before), 1)
    candidates = [space_id for space_id in sorted(h.bmap.spaces)
                  if space_id != here and space_id not in h.bmap.closed
                  and rules.may_stand(h.bmap, space_id, asset["type"], asset_id=asset_id)[0]]
    ranked = sorted((times.total(legs, space_id), space_id) for space_id in candidates)
    for estimate, space_id in ranked[:SPOT_TRIES]:
        if per_day(before - estimate) < MIN_SAVING_MIN:
            break
        pose = spot(h, space_id, asset)
        if pose is None:
            continue
        x, y, rotation = pose
        after = WalkTimes(h).total(legs, f"spot:{space_id}", Place(space_id, (x, y)))
        if per_day(before - after) < MIN_SAVING_MIN:
            continue
        space = h.bmap.spaces[space_id]
        saving = round(per_day(before - after), 1)
        reason = (f"“{asset['name']}” is used about {times_a_day(result['trips_per_day'])} a day. "
                  f"In {space['name']} ({h.bmap.floor_name(space['floor_id']).lower()}) "
                  f"the past trips would be {number(saving)} minutes shorter per day. "
                  f"{rules.may_stand(h.bmap, space_id, asset['type'], asset_id=asset_id)[1]}")
        return result | {"proposal": {"space_id": space_id, "name": space["name"], "position": [x, y],
                                      "rotation_deg": rotation, "floor_id": space["floor_id"]},
                         "minutes_per_day_after": round(per_day(after), 1), "saving_min_per_day": saving,
                         "reason": reason}
    return result | {"reason": f"The current location of “{asset['name']}” already suits the past trips; "
                               f"no permitted room saves at least {number(MIN_SAVING_MIN)} minutes per day."}


def apply_placement(h: Hospital, result: dict) -> list[str]:
    """Moves the asset of a placement() result to its proposal."""
    proposal = result.get("proposal")
    if not proposal:
        raise ValueError("There is no location proposal to apply.")
    asset_id = result["asset"]["id"]
    asset = h.bmap.assets.get(asset_id)
    if asset is None:
        raise ValueError(f"The item {asset_id} no longer exists.")
    if asset["space_id"] != result["asset"]["space_id"]:
        raise ValueError(f"“{asset['name']}” has been moved in the meantime; please recalculate.")
    target = proposal["space_id"]
    ok, reason = rules.may_stand(h.bmap, target, asset["type"], asset_id=asset_id)
    if not ok:
        raise ValueError(reason)
    old_space, old_floor, old_shape = asset["space_id"], asset["floor_id"], footprint(asset)
    floor_id = h.bmap.spaces[target]["floor_id"]
    x, y = proposal["position"]
    rotation = proposal.get("rotation_deg", (asset.get("footprint") or {}).get("rotation_deg") or 0.0)
    asset.update(space_id=target, floor_id=floor_id, position=[x, y],
                 footprint={**(asset.get("footprint") or {}), "rotation_deg": rotation})
    now = h.now()
    for tag in h.tags.values():
        if tag.kind == "asset" and tag.ref == asset_id:
            tag.floor_id, tag.x, tag.y, tag.space_id, tag.seen_at = floor_id, x, y, target, now
    touched = ((old_floor, old_shape), (floor_id, footprint(asset)))
    neighbours = [s["id"] for s in h.bmap.spaces.values()
                  if any(s["floor_id"] == floor and Polygon(s["polygon"]).buffer(CLEARANCE_M).intersects(shape)
                         for floor, shape in touched)]
    h.router.invalidate(sorted({old_space, target, *neighbours}))
    for patient in h.patients.values():
        patient.plan = None
    return [asset_id]
