"""Time-based trips for pager holders (patients and staff): which lift or stairs, when to leave, what to do next.

WalkRouter answers in metres. This module turns a route into a timeline (walking, stopovers, lift queue, ride),
picks the lift or stairs that arrives first, and books the lift slots, so patients reach the lift when it is
theirs instead of queueing in the corridor. `guidance` is what a pager or the voice agent asks every few seconds.
The goal is, by priority: a searched object (start_search), a named place (go_to), the appointment, the patient's
own room; staff without any of these are idle."""

import bisect
import itertools
import math
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from core.building_map import LIFT, STAIRS_M_PER_RISE
from core.walk import Place, WalkRoute
from hospital import finder, rules
from hospital import stops as stopovers
from hospital.state import WALKING_SPEED_M_S, Hospital, Patient, route_json

BUFFER_S = 120.0
WALK_STOP_S = 3.0  # the demo walk stands this long at each stop
NEXT_TURNS = 3
TURN_DONE_M = 1.0
SAY_DISTANCE_M = 5.0
ARRIVED_M = 2.0
SEARCH_ROOM_M = 5.0
CONNECTOR_MANEUVERS = {"lift_up", "lift_down", "stairs_up", "stairs_down"}


# ---------- routes and timelines ----------

@contextmanager
def closing(h: Hospital, extra: set[str]) -> Iterator[None]:
    """Temporarily close connectors; h.bmap.closed is shared state, so always restore it."""
    saved = set(h.bmap.closed)
    h.bmap.closed |= extra
    try:
        yield
    finally:
        h.bmap.closed.clear()
        h.bmap.closed |= saved


def connectors_of(walk: WalkRoute) -> tuple[str, ...]:
    return tuple(step["connector"] for step in walk.instructions if "connector" in step)


def leg_variants(h: Hospital, start: Place, goal: Place, step_free: bool) -> list[WalkRoute]:
    """The shortest route, plus one route per lift (and the stairs unless step_free) forced as the only way up/down."""
    default = h.router.route(start, goal, step_free)
    if not connectors_of(default):
        return [default]
    routes = {connectors_of(default): default}
    usable = sorted(cid for cid, c in h.bmap.connectors.items() if c["step_free"] or not step_free)
    for only in usable:
        with closing(h, set(h.bmap.connectors) - {only}):
            try:
                walk = h.router.route(start, goal, step_free)
            except ValueError:
                continue
        routes.setdefault(connectors_of(walk), walk)
    return list(routes.values())


def cumulative(points: list[dict]) -> list[float]:
    """Walked metres up to each point; floor changes (lift, stairs) add no walking."""
    total, result = 0.0, [0.0]
    for a, b in itertools.pairwise(points):
        if a["floor_id"] == b["floor_id"]:
            total += math.dist((a["x"], a["y"]), (b["x"], b["y"]))
        result.append(total)
    return result


def timeline(h: Hospital, walk: WalkRoute, ready: float, holder: str) -> tuple[float, list[dict]]:
    """Walk the route starting at `ready`; return the arrival time and the lift slots it needs (not booked)."""
    along = cumulative(walk.points)
    t, done, slots = ready, 0, []
    for step in walk.instructions:
        if "connector" not in step:
            continue
        index, cid = step["index"], step["connector"]
        t += (along[index] - along[done]) / WALKING_SPEED_M_S
        from_floor, to_floor = walk.points[index]["floor_id"], step["floor_id"]
        if h.bmap.connectors[cid]["kind"] == LIFT:
            duration = h.lifts.trip_seconds(cid, from_floor, to_floor)
            start = h.lifts.earliest(cid, t, duration)
            slots.append({"connector_id": cid, "holder": holder, "start": start, "end": start + duration,
                          "from_floor": from_floor, "to_floor": to_floor, "index": index, "ready_at": t})
            t = start + duration
        else:
            rise = abs(h.bmap.floors[to_floor]["elevation_m"] - h.bmap.floors[from_floor]["elevation_m"])
            t += STAIRS_M_PER_RISE * rise / WALKING_SPEED_M_S
        done = index
    return t + (along[-1] - along[done]) / WALKING_SPEED_M_S, slots


def join(legs: list[WalkRoute]) -> WalkRoute:
    """Concatenate per-leg routes the way WalkRouter.describe joins a route with via stops."""
    if len(legs) == 1:
        return legs[0]
    points: list[dict] = []
    spaces: list[str] = []
    instructions: list[dict] = []
    for number, leg in enumerate(legs):
        steps = [step | {"index": step["index"] + len(points)} for step in leg.instructions]
        if number > 0:
            steps = steps[1:]
        if number < len(legs) - 1:
            steps[-1] |= {"maneuver": "waypoint", "stop": number + 1,
                          "text": steps[-1]["text"].replace("Arrive at the ", "Arrive at ", 1)
                          .replace("Arrive at", f"Stop {number + 1}:", 1)}
        instructions += steps
        points += leg.points
        spaces += leg.spaces[1:] if spaces and leg.spaces and leg.spaces[0] == spaces[-1] else leg.spaces
    texts = [f"{i['text']} ({i['detail']})" if i["detail"] else i["text"] for i in instructions]
    return WalkRoute(round(sum(leg.walk_m for leg in legs), 1), round(sum(leg.cost_m for leg in legs), 1),
                     points, texts, spaces, instructions)


def choice_of(h: Hospital, walk: WalkRoute) -> str:
    names = []
    for cid in connectors_of(walk):
        connector = h.bmap.connectors[cid]
        names.append(f"lift {connector['name']}" if connector["kind"] == LIFT else "stairs")
    return ", ".join(names) or "walk"


def plan_trip(h: Hospital, holder: str, start: Place, goal: Place, step_free: bool, depart_at: float,
              via: Sequence[Place] = (), dwell_s: Sequence[float] = (), reserve: bool = True,
              variants: dict | None = None) -> dict:
    """Fastest trip in time, leg by leg; depart_at moves later so the patient meets the first lift slot exactly.

    `variants` caches leg routes across calls that only differ in time (they do not depend on the queue)."""
    if reserve:
        h.lifts.release(holder)
    variants = {} if variants is None else variants
    places = [start, *via, goal]
    t = depart_at
    legs: list[WalkRoute] = []
    slots: list[dict] = []
    stop_points: list[int] = []
    for number, (a, b) in enumerate(itertools.pairwise(places)):
        key = (a.space_id, a.point, b.space_id, b.point, step_free, frozenset(h.bmap.closed))
        if key not in variants:
            variants[key] = leg_variants(h, a, b, step_free)
        best = None
        for walk in variants[key]:
            arrive, needed = timeline(h, walk, t, holder)
            if best is None or arrive < best[0] - 1e-6:
                best = (arrive, walk, needed)
        arrive, walk, needed = best
        offset = sum(len(leg.points) for leg in legs)
        for slot in needed:
            slot["index"] += offset
            if reserve:
                h.lifts.reserve(slot["connector_id"], holder, slot["start"], slot["end"] - slot["start"],
                                slot["from_floor"], slot["to_floor"])
        slots += needed
        legs.append(walk)
        t = arrive
        if number < len(via):
            stop_points.append(offset + len(walk.points) - 1)
            t += dwell_s[number] if number < len(dwell_s) else 0.0
    if slots:
        depart_at += slots[0]["start"] - slots[0]["ready_at"]
        slots[0]["ready_at"] = slots[0]["start"]
    for slot in slots:
        slot["wait_s"] = round(slot["start"] - slot.pop("ready_at"), 1)
    walk = join(legs)
    seconds = t - depart_at
    route = route_json(walk) | {"seconds": round(seconds, 1), "minutes": round(seconds / 60, 1), "lift_slots": slots}
    return {"route": route, "depart_at": depart_at, "arrive_at": t, "seconds": round(seconds, 1),
            "lift_slots": slots, "choice": choice_of(h, walk), "stop_points": stop_points}


# ---------- guidance ----------

def mode_of(patient: Patient) -> str:
    if patient.search:
        return "search"
    if patient.destination:
        return "destination"
    if patient.appointment:
        return "appointment"
    return "idle" if patient.kind == "staff" and not patient.stops else "return"


def goal_space(patient: Patient) -> str:
    if patient.search:
        return patient.search["space_id"]
    if patient.destination:
        return patient.destination["space_id"]
    return patient.appointment.space_id if patient.appointment else patient.room_id


def goal_place(h: Hospital, patient: Patient) -> Place:
    if patient.search:
        return h.router.place_for_asset(patient.search["asset_id"])
    return h.router.place_for_space(goal_space(patient))


def at_goal(h: Hospital, patient: Patient, here: Place, goal: Place) -> bool:
    if not patient.search:
        return here.space_id == goal.space_id
    asset = h.bmap.assets[patient.search["asset_id"]]
    if h.bmap.spaces[here.space_id]["floor_id"] != asset["floor_id"]:
        return False
    return (math.dist(here.point, goal.point) <= ARRIVED_M
            or (here.space_id == asset["space_id"] and math.dist(here.point, asset["position"]) <= SEARCH_ROOM_M))


def search_for(h: Hospital, patient: Patient, asset_type: str, here: Place) -> dict:
    with closing(h, patient.avoid):
        found = finder.nearest_asset(h, asset_type, here, patient.step_free)["asset"]
    space = h.bmap.spaces[found["space_id"]]
    return {"type": asset_type, "asset_id": found["id"], "name": found["name"], "label": found["label"],
            "space_id": found["space_id"], "space_name": space["name"], "floor_id": found["floor_id"],
            "floor_name": h.bmap.floor_name(found["floor_id"])}


def start_search(h: Hospital, pager_id: str, asset_type: str) -> dict:
    """Navigate to the nearest free object of this type (by walking cost from where the holder is now).
    Unknown type: KeyError; none free or reachable: LookupError; both carry a message for the user."""
    patient = h.patient_by_pager(pager_id)
    if not any(a["type"] == asset_type for a in h.bmap.assets.values()):
        known = ", ".join(rules.label(t) for t in finder.findable_types(h))
        raise KeyError(f"There are no objects of type '{asset_type}' here. You can search for: {known}.")
    patient.search = search_for(h, patient, asset_type, h.place_of(patient))
    h.lifts.release(patient.id)
    patient.plan = None
    return guidance(h, pager_id)


def stop_search(h: Hospital, pager_id: str) -> dict:
    patient = h.patient_by_pager(pager_id)
    had = patient.search
    patient.search = None
    h.lifts.release(patient.id)
    patient.plan = None
    result = guidance(h, pager_id)
    if had:
        result["say"] = f"{capital(had['label'])} search cancelled. {result['say']}"
    return result


def go_to(h: Hospital, pager_id: str, space_ref: str) -> dict:
    """Navigate to a named room from now on (after any object search). Unknown room: MapError."""
    patient = h.patient_by_pager(pager_id)
    space_id = h.bmap.resolve_space(space_ref)
    patient.destination = {"space_id": space_id, "name": h.bmap.spaces[space_id]["name"]}
    h.lifts.release(patient.id)
    patient.plan = None
    return guidance(h, pager_id)


def stop_go_to(h: Hospital, pager_id: str) -> dict:
    patient = h.patient_by_pager(pager_id)
    had = patient.destination
    patient.destination = None
    h.lifts.release(patient.id)
    patient.plan = None
    result = guidance(h, pager_id)
    if had:
        result["say"] = f"Route to {had['name']} cancelled. {result['say']}"
    return result


def walk_reset(h: Hospital, pager_id: str) -> dict:
    """Demo: a clean slate for the next request. The pager goes back where it started with no destination, search,
    stops or avoided lifts, and everything closed by voice opens again."""
    patient = h.patient_by_pager(pager_id)
    h.lifts.release(patient.id)
    patient.plan = patient.search = patient.destination = None
    patient.stops, patient.stops_ordered, patient.step_free = [], False, False
    patient.avoid = set()
    if h.bmap.closed:
        h.bmap.closed.clear()
        for holder in h.patients.values():
            h.lifts.release(holder.id)
            holder.plan = None
    h.set_position(patient.pager_tag_id, *h.home[patient.pager_tag_id])
    return guidance(h, pager_id)


def walk(h: Hospital, pager_id: str, metres: float) -> dict:
    """Demo stand-in for really walking: move the pager `metres` further along its planned route. Lifts and stairs
    are passed instantly and a planned departure is not waited for."""
    patient = h.patient_by_pager(pager_id)
    result = guidance(h, pager_id)
    plan = patient.plan
    if plan is None or result["status"] in ("arrived", "idle"):
        return result
    if plan.get("paused_until", 0) > time.time():  # standing at a stop for a moment
        return result
    plan["status"] = "go"
    points = plan["route"]["points"]
    along = cumulative(points)
    here_m = metres_at(along, plan["progress"])
    target = min(here_m + metres, along[-1])
    stops_m = [along[index] for index in plan["stop_points"]]
    if (stop_m := next((m for m in stops_m if here_m < m <= target), None)) is not None:
        target = stop_m
        plan["paused_until"] = time.time() + WALK_STOP_S
    # bisect_right lands after a floor change (same metres on both floors), so the walker is through the lift
    i = min(bisect.bisect_right(along, target) - 1, len(points) - 1)
    a = points[i]
    x, y = a["x"], a["y"]
    if i + 1 < len(points) and points[i + 1]["floor_id"] == a["floor_id"] and along[i + 1] > along[i]:
        b, f = points[i + 1], (target - along[i]) / (along[i + 1] - along[i])
        x, y = a["x"] + f * (b["x"] - a["x"]), a["y"] + f * (b["y"] - a["y"])
        i += f
    # The walk knows where it is on the route; matching the point again would jump back where a route goes into a
    # room and out the same way (a stop).
    plan["progress"] = max(plan["progress"], i)
    h.set_position(patient.pager_tag_id, a["floor_id"], x, y)
    return guidance(h, pager_id)


def retarget(h: Hospital, patient: Patient, here: Place) -> str:
    """The searched object was taken (or removed) on the way: search again from here; return what to tell the user."""
    old = patient.search
    note = f"The {finder.noun(old['type'])} in {old['space_name']} is no longer free."
    h.lifts.release(patient.id)
    patient.plan = None
    try:
        patient.search = search_for(h, patient, old["type"], here)
    except LookupError as error:
        patient.search = None
        return f"{note} {error} Search cancelled."
    return note


def schedule(h: Hospital, patient: Patient, here: Place, now: float) -> dict:
    """The trip starts now (give or take the first lift slot), with this patient's lift slots booked."""
    with closing(h, patient.avoid):
        return schedule_open(h, patient, here, now)


def schedule_open(h: Hospital, patient: Patient, here: Place, now: float) -> dict:
    goal_id = goal_space(patient)
    goal = goal_place(h, patient)
    chosen = stopovers.resolve_stops(h, here, goal, patient.stops, patient.stops_ordered, patient.step_free)
    via, dwell = [c["place"] for c in chosen], [c["dwell_s"] for c in chosen]
    variants: dict = {}
    h.lifts.release(patient.id)

    def trip(depart: float, reserve: bool = False) -> dict:
        return plan_trip(h, patient.id, here, goal, patient.step_free, depart, via, dwell, reserve, variants)

    plan = trip(now, reserve=True)
    plan["status"] = "go"
    plan["goal"] = {"space_id": goal_id, "name": h.bmap.spaces[goal_id]["name"], "asset_id": goal.asset_id}
    if patient.search:
        plan["goal"] |= {"name": patient.search["name"], "type": patient.search["type"]}
    plan["stops"] = [{key: c[key] for key in ("id", "name", "category", "dwell_s")} for c in chosen]
    plan["progress"] = 0.0
    return plan


def locate(points: list[dict], floor_id: str, point: tuple[float, float], after: float) -> float:
    """Position along the route (point index + fraction) nearest to `point`, never behind `after`."""
    best, position = math.inf, after
    px, py = point
    for i in range(int(after), len(points)):
        a = points[i]
        if a["floor_id"] != floor_id:
            continue
        b = points[i + 1] if i + 1 < len(points) and points[i + 1]["floor_id"] == floor_id else a
        dx, dy = b["x"] - a["x"], b["y"] - a["y"]
        span = dx * dx + dy * dy
        t = 0.0 if span == 0 else min(1.0, max(0.0, ((px - a["x"]) * dx + (py - a["y"]) * dy) / span))
        distance = math.dist(point, (a["x"] + t * dx, a["y"] + t * dy))
        if distance < best - 1e-9:
            best, position = distance, max(after, i + t)
    return position


def metres_at(along: list[float], position: float) -> float:
    i = int(position)
    if i + 1 >= len(along):
        return along[-1]
    return along[i] + (position - i) * (along[i + 1] - along[i])


def capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def short_floor(h: Hospital, floor_id: str) -> str:
    level = h.bmap.floors[floor_id]["level"]
    return "G" if level == 0 else f"floor {level}"


def phrase(h: Hospital, plan: dict, step: dict) -> tuple[str, str]:
    """(pager text, phrase that reads after 'In 30 metres,' or 'then') for one router instruction."""
    maneuver = step["maneuver"]
    if maneuver in CONNECTOR_MANEUVERS:
        way = h.bmap.connectors[step["connector"]]["name"] if maneuver.startswith("lift") else "the stairs"
        direction = "up" if maneuver.endswith("_up") else "down"
        floor_id = step["floor_id"]
        return (f"Take {way} {direction} to {short_floor(h, floor_id)}",
                f"take {way} {direction} to {finder.floor_ref(h.bmap.floor_name(floor_id))}")
    if maneuver == "waypoint":
        stop = plan["stops"][step["stop"] - 1]
        label = stopovers.STOP_CATEGORIES.get(stop["category"] or "", {}).get("label")
        text = f"{label} stop: {stop['name']}" if label else f"Stop: {stop['name']}"
        return text, f"you reach your stop, {stop['name']}"
    if maneuver == "arrive":
        name = plan["goal"]["name"]
        return f"Arrive at {name}", f"you reach {name}"
    _, into, target = step["text"].partition(" into ")
    if into:
        verb = {"left": "Turn left into", "right": "Turn right into"}.get(maneuver)
        if verb:
            return f"{verb} {target}", f"{verb.lower()} {target}"
        return f"Straight to {target}", f"go straight to {target}"
    name = step["text"].removeprefix("Walk to the ")
    connector = next((c for c in h.bmap.connectors.values() if c["name"] == name), None)
    place = "the stairs" if connector and connector["kind"] != LIFT else name
    return f"Go to {place}", f"go to {place}"


def upcoming(h: Hospital, plan: dict, along: list[float]) -> list[tuple[dict, str]]:
    """Up to NEXT_TURNS instructions ahead of plan['progress'], each with its spoken phrase."""
    steps = plan["route"]["instructions"]
    here_m = metres_at(along, plan["progress"])
    previous, result = here_m, []
    for number, step in enumerate(steps):
        if step["maneuver"] == "depart":
            continue
        if step["maneuver"] in CONNECTOR_MANEUVERS:
            end = step["index"]
            if end < plan["progress"]:
                continue
        else:
            end = steps[number + 1]["index"] if number + 1 < len(steps) else len(along) - 1
            if along[end] - here_m <= TURN_DONE_M:
                continue
        text, spoken = phrase(h, plan, step)
        result.append(({"maneuver": step["maneuver"], "text": text, "distance_m": round(max(0.0, along[end] - previous), 1),
                        "floor_id": step["floor_id"]}, spoken))
        previous = along[end]
        if len(result) == NEXT_TURNS:
            break
    return result


def clock_time(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def guidance(h: Hospital, pager_id: str) -> dict:
    """wait / go / arrived / idle for one pager. A waiting holder is re-planned on every call so depart_at follows the
    lift queue; once the holder may go, the plan freezes until they leave it (set_position drops it)."""
    patient = h.patient_by_pager(pager_id)
    now = h.now()
    here = h.place_of(patient)
    notes = []
    search = patient.search
    if search and (search["asset_id"] not in h.bmap.assets
                   or (search["asset_id"] in h.in_use and not at_goal(h, patient, here, goal_place(h, patient)))):
        notes.append(retarget(h, patient, here))
    mode = mode_of(patient)
    plan = patient.plan
    turns: list[tuple[dict, str]] = []
    goal = None
    if mode == "idle":
        h.lifts.release(patient.id)
        plan = patient.plan = None
        status = "idle"
    else:
        goal = goal_place(h, patient)
        if plan is not None and (plan["goal"]["space_id"], plan["goal"]["asset_id"]) != (goal.space_id, goal.asset_id):
            h.lifts.release(patient.id)
            plan = patient.plan = None
        if (plan is None or plan["status"] == "wait") and not patient.stops and at_goal(h, patient, here, goal):
            h.lifts.release(patient.id)
            plan = patient.plan = None
            status = "arrived"
        else:
            if plan is not None and plan["status"] == "wait" and now >= plan["depart_at"]:
                # Re-planning after depart_at would miss the booked lift slot and push everyone behind back.
                plan["status"] = "go"
            elif plan is None or plan["status"] == "wait":
                plan = patient.plan = schedule(h, patient, here, now)
            points = plan["route"]["points"]
            along = cumulative(points)
            plan["progress"] = locate(points, h.bmap.spaces[here.space_id]["floor_id"], here.point, plan["progress"])
            here_m = metres_at(along, plan["progress"])
            visited = sum(1 for index in plan["stop_points"] if along[index] - here_m <= TURN_DONE_M)
            if visited and plan["status"] == "go":
                patient.stops = [{"id": s["id"], "category": s["category"], "dwell_s": s["dwell_s"]}
                                 for s in plan["stops"][visited:]]
                patient.stops_ordered = True
            arrived = at_goal(h, patient, here, goal) and visited == len(plan["stop_points"])
            status = "arrived" if arrived else plan["status"]
            if status != "arrived":
                turns = upcoming(h, plan, along)
    search = patient.search
    goal_id = goal.space_id if goal else None
    goal_name = (plan["goal"]["name"] if plan else search["name"] if search
                 else h.bmap.spaces[goal_id]["name"] if goal_id else None)
    intro = "Time to go."
    if search and plan:
        intro = (f"{finder.nearest_phrase(search['type'])}: {search['space_name']}, "
                 f"{finder.lowercase(search['floor_name'])}, about {finder.minutes_text(plan['arrive_at'] - now)}.")
    if status == "idle":
        say = "No active destination."
    elif status == "arrived" and search:
        say = f"You're {finder.at_phrase(search['type'])}: {search['name']} in {search['space_name']}."
    elif status == "arrived":
        say = f"You have arrived at {goal_name}."
    elif status == "wait":
        say = f"Please wait. You can leave at {clock_time(plan['depart_at'])}."
        if search and plan["progress"] == 0:
            say = f"{intro} {say}"
    elif turns:
        first, spoken = turns[0]
        say = (f"In {round(first['distance_m'])} metres, {spoken}" if first["distance_m"] >= SAY_DISTANCE_M
               else capital(spoken))
        say += f", then {turns[1][1]}." if len(turns) > 1 else "."
        if plan["progress"] == 0:
            say = f"{intro} {say}"
    else:
        say = f"{goal_name} is just ahead."
    appointment = patient.appointment
    return {"patient": {"id": patient.id, "number": patient.number, "name": patient.name, "kind": patient.kind,
                        "role": patient.role},
            "mode": mode,
            "status": status,
            "depart_at": plan["depart_at"] if plan else None,
            "eta": plan["arrive_at"] if plan else None,
            "appointment": {"space_id": appointment.space_id, "name": h.bmap.spaces[appointment.space_id]["name"],
                            "start": appointment.start, "label": appointment.label} if appointment else None,
            "search": dict(search) if search else None,
            "destination": dict(patient.destination) if patient.destination else None,
            "step_free": patient.step_free,
            "goal": {"space_id": goal_id, "name": goal_name, "asset_id": goal.asset_id} if goal else None,
            "next_turns": [turn for turn, _ in turns],
            "route": plan["route"] if plan else None,
            "progress": plan["progress"] if plan else 0.0,
            "lift_slots": plan["lift_slots"] if plan else [],
            "stops": plan["stops"] if plan else [],
            "choice": plan["choice"] if plan else None,
            "say": " ".join([*notes, say])}
