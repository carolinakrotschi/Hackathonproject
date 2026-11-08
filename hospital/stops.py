"""Stopovers on the way to an appointment: coffee, toilet, kiosk, pharmacy, water.

Each category maps to room types and asset types. One candidate per stop and the visiting order are chosen so the
whole walk start → stops → goal is shortest (not the stop nearest to the start); dispatch then says what that does
to the arrival time, lift queues included."""

import itertools
import math

from core.walk import Place
from hospital import dispatch
from hospital.state import Hospital

STOP_CATEGORIES = {
    "coffee": {"room_types": ("cafe",), "asset_types": ("coffee_machine", "vending_machine"), "dwell_s": 180,
               "label": "Coffee"},
    "toilet": {"room_types": ("toilet",), "asset_types": (), "dwell_s": 180, "label": "Toilet"},
    "kiosk": {"room_types": ("kiosk",), "asset_types": (), "dwell_s": 240, "label": "Kiosk"},
    "pharmacy": {"room_types": ("pharmacy",), "asset_types": (), "dwell_s": 300, "label": "Pharmacy"},
    "water": {"room_types": (), "asset_types": ("water_dispenser",), "dwell_s": 60, "label": "Water"},
}
MAX_STOPS = 6
MAX_CANDIDATES = 6
FLOOR_PENALTY_M = 30.0


def stop_at(h: Hospital, stop_id: str, category: str | None, dwell_s: float) -> dict:
    if stop_id in h.bmap.assets:
        asset = h.bmap.assets[stop_id]
        return {"id": stop_id, "name": asset["name"], "category": category,
                "place": h.router.place_for_asset(stop_id), "dwell_s": dwell_s}
    space_id = h.bmap.resolve_space(stop_id)
    return {"id": space_id, "name": h.bmap.spaces[space_id]["name"], "category": category,
            "place": h.router.place_for_space(space_id), "dwell_s": dwell_s}


def candidates(h: Hospital, category: str, dwell_s: float) -> list[dict]:
    spec = STOP_CATEGORIES[category]
    closed = h.bmap.closed
    found = [stop_at(h, s["id"], category, dwell_s) for s in h.bmap.spaces.values()
             if s.get("room_type") in spec["room_types"] and s["id"] not in closed]
    found += [stop_at(h, a["id"], category, dwell_s) for a in h.bmap.assets.values()
              if a["type"] in spec["asset_types"] and a["id"] not in h.in_use and a["space_id"] not in closed]
    return found


def straight_line_m(h: Hospital, a: Place, b: Place) -> float:
    floors = [h.lifts.levels[h.bmap.spaces[p.space_id]["floor_id"]] for p in (a, b)]
    return math.dist(a.point, b.point) + FLOOR_PENALTY_M * abs(floors[0] - floors[1])


def resolve_stops(h: Hospital, start: Place, goal: Place, stops: list[dict], ordered: bool,
                  step_free: bool) -> list[dict]:
    """[{category} | {id}] → [{id, name, category, place, dwell_s}] in visiting order, minimising the walk
    start → … → goal. Exact over all orders (unless ordered) and all candidates (best MAX_CANDIDATES per stop)."""
    if len(stops) > MAX_STOPS:
        raise ValueError(f"At most {MAX_STOPS} stops.")
    groups = []
    for stop in stops:
        category = stop.get("category")
        if category is not None and category not in STOP_CATEGORIES:
            raise ValueError(f"Unknown stop category '{category}'. Known: {', '.join(STOP_CATEGORIES)}.")
        dwell_s = float(stop.get("dwell_s", STOP_CATEGORIES[category]["dwell_s"] if category else 0.0))
        if stop.get("id"):
            groups.append([stop_at(h, stop["id"], category, dwell_s)])
            continue
        if category is None:
            raise ValueError("Each stop needs a category or an id.")
        found = candidates(h, category, dwell_s)
        if not found:
            raise ValueError(f"No reachable {category} stop in this building.")
        found.sort(key=lambda c: straight_line_m(h, start, c["place"]) + straight_line_m(h, c["place"], goal))
        groups.append(found[:MAX_CANDIDATES])
    if ordered and all(len(group) == 1 for group in groups):
        return [group[0] for group in groups]

    costs: dict[tuple, float] = {}

    def cost(a: Place, b: Place) -> float:
        key = (a.space_id, a.point, b.space_id, b.point)
        if key not in costs:
            try:
                costs[key] = h.router.route(a, b, step_free).cost_m
            except ValueError:
                costs[key] = math.inf
        return costs[key]

    best: tuple[float, list[dict]] = (math.inf, [])
    for order in [range(len(groups))] if ordered else itertools.permutations(range(len(groups))):
        # Layered shortest path over the candidates of each stop in this order.
        layer = [(0.0, [], start)]
        for g in order:
            layer = [min(((total + cost(place, c["place"]), [*path, c], c["place"]) for total, path, place in layer),
                         key=lambda item: item[0]) for c in groups[g]]
        total, path, _ = min(((total + cost(place, goal), path, place) for total, path, place in layer),
                             key=lambda item: item[0])
        if total < best[0]:
            best = (total, path)
    if stops and math.isinf(best[0]):
        raise ValueError("No route through these stops with the current closures.")
    return best[1]


def labels(chosen: list[dict]) -> str:
    names = list(dict.fromkeys(STOP_CATEGORIES[c["category"]]["label"] if c["category"] in STOP_CATEGORIES
                               else c["name"] for c in chosen))
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def set_stops(h: Hospital, pager_id: str, stops: list[dict], ordered: bool = False, dry_run: bool = False) -> dict:
    """What the stops cost when leaving now; unless dry_run, guidance uses them from now on."""
    patient = h.patient_by_pager(pager_id)
    here = h.place_of(patient)
    goal = dispatch.goal_place(h, patient)
    chosen = resolve_stops(h, here, goal, stops, ordered, patient.step_free)
    holder, now, variants = patient.id, h.now(), {}
    booked = [s for s in h.lifts.slots() if s["holder"] == holder]
    h.lifts.release(holder)  # the patient's own bookings must not queue behind themselves
    try:
        base = dispatch.plan_trip(h, holder, here, goal, patient.step_free, now, reserve=False, variants=variants)
        trip = dispatch.plan_trip(h, holder, here, goal, patient.step_free, now, [c["place"] for c in chosen],
                                  [c["dwell_s"] for c in chosen], reserve=False, variants=variants)
    finally:
        for slot in booked:
            h.lifts.reserve(slot["connector_id"], holder, slot["start"], slot["end"] - slot["start"],
                            slot["from_floor"], slot["to_floor"])
    detour_min = round((trip["arrive_at"] - base["arrive_at"]) / 60, 1)
    timed = dispatch.mode_of(patient) == "appointment"
    margin = (patient.appointment.start - trip["arrive_at"]) / 60 if timed else None
    fits = margin is None or margin >= 0
    what = labels(chosen) if chosen else "No stopover"
    if margin is None:
        say = f"{what} costs {max(1, round(detour_min))} min."
    elif fits:
        say = f"{what} fits, you'll arrive {math.floor(margin)} min before your appointment."
    else:
        say = f"{what} costs {max(1, round(detour_min))} min, you'll be {math.ceil(-margin)} min late."
    if not dry_run:
        patient.stops = [{"id": c["id"], "category": c["category"], "dwell_s": c["dwell_s"]} for c in chosen]
        patient.stops_ordered = True
        patient.plan = None
    return {"stops": [{"id": c["id"], "name": c["name"], "category": c["category"], "dwell_s": c["dwell_s"]}
                      for c in chosen],
            "detour_min": detour_min,
            "arrival_margin_min": round(margin, 1) if margin is not None else None,
            "fits": fits,
            "route": trip["route"],
            "say": say}


def clear_stops(h: Hospital, pager_id: str) -> dict:
    patient = h.patient_by_pager(pager_id)
    had = bool(patient.stops)
    patient.stops = []
    patient.stops_ordered = False
    patient.plan = None
    say = "All stopovers cleared." if had else "No stopovers were planned."
    return {"stops": [], "say": say}
