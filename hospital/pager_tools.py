"""What a staff pager can do, as plain functions over the deterministic core (BuildingMap, WalkRouter, dispatch).

The voice agent (hospital/agent.py) exposes each function as a Claude tool; the offline phrase rules (hospital/voice.py)
call the same functions. Every function takes the shared Hospital (callers hold h.lock) and returns a dict with
`say` (one or two short sentences for the pager to show and speak) and `view` (what the pager map should highlight).
Errors that the user should hear raise PagerError with a message to say instead."""

from core.building_map import MapError
from hospital import dispatch, finder, nomenclature, rules, state, voice
from hospital import stops as stopovers
from hospital.state import Hospital

MAX_LISTED = 5
FREE_WORDS = ("a", "an", "any", "another", "free", "spare", "available", "empty", "unused", "nearest", "closest",
              "next")


class PagerError(ValueError):
    pass


def empty_view() -> dict:
    return {"spaces": [], "assets": []}


def building_of(h: Hospital, space_id: str) -> str | None:
    """'Building B' for a campus space, None for single buildings and links between buildings."""
    code = h.bmap.spaces[space_id].get("building")
    names = {b["id"]: b["name"] for b in h.bmap.data.get("buildings", [])}
    return names.get(code) if code else None


def where_text(h: Hospital, space_id: str) -> str:
    """'floor 1, Building B' / 'the ground floor'."""
    floor = finder.floor_ref(h.bmap.floor_name(h.bmap.spaces[space_id]["floor_id"]))
    building = building_of(h, space_id)
    return f"{floor}, {building}" if building else floor


def persist(h: Hospital) -> bool:
    """Imported buildings keep conversational edits (labels, doors, moves); the demo buildings stay as generated."""
    if h.root.parent != state.IMPORTED:
        return False
    h.bmap.save(h.root / "building.json")
    return True


def replan_everyone(h: Hospital) -> None:
    """A closure or a new door changes routes: every pager plans again on its next update."""
    for holder in h.patients.values():
        h.lifts.release(holder.id)
        holder.plan = None


# ---------- resolving what people say ----------

def resolve_place(h: Hospital, pager_id: str, text: str, towards: str | None = None) -> tuple[str, str | None]:
    """A room, department or item as said ('the CT', 'Theatre 1', 'B0.10', 'the MRI scanner') -> (space id, asset id).
    Several matches (WC, Lift A on every floor) -> the nearest by walking."""
    text = text.strip()
    if text in h.bmap.spaces:
        return text, None
    if text in h.bmap.assets:
        return h.bmap.assets[text]["space_id"], text
    lowered = text.lower().removeprefix("the ")
    exact = [s["id"] for s in h.bmap.spaces.values()
             if lowered in (s["name"].lower(), (s.get("ref") or "").lower())]
    if exact:
        return voice.nearest_space(h, pager_id, exact) or exact[0], None
    named_assets = [a for a in h.bmap.assets.values() if a["name"].lower() == lowered]
    if len(named_assets) == 1:
        return named_assets[0]["space_id"], named_assets[0]["id"]
    free = free_asset(h, pager_id, lowered, towards)
    if free:
        return free
    spaces = voice.place_mention(h, text)
    if spaces:
        nearest = voice.nearest_space(h, pager_id, spaces)
        if nearest is None:
            raise PagerError(f"There is no route to {h.bmap.spaces[spaces[0]]['name']} from here right now.")
        return nearest, None
    found = h.bmap.search(lowered)
    rooms = [r["id"] for r in found if r["kind"] == "space"]
    if rooms:
        return voice.nearest_space(h, pager_id, rooms) or rooms[0], None
    items = [r["id"] for r in found if r["kind"] == "asset"]
    if items:
        by_space = {h.bmap.assets[a]["space_id"]: a for a in items}
        space_id = voice.nearest_space(h, pager_id, list(by_space)) or next(iter(by_space))
        return space_id, by_space[space_id]
    raise PagerError(f"I can't find '{text}' on the map.")


def free_asset(h: Hospital, pager_id: str, text: str, towards: str | None = None) -> tuple[str, str] | None:
    """'a free wheelchair', 'the nearest defibrillator' -> (space id, asset id) of the nearest free one, or of the
    one that costs the least detour on the way to the space `towards`."""
    words = text.split()
    if not words or words[0] not in FREE_WORDS:
        return None
    while words and words[0] in FREE_WORDS:
        words = words[1:]
    asset_type = asset_type_for(h, " ".join(words)) if words else None
    if asset_type is None:
        return None
    holder = h.patient_by_pager(pager_id)
    asset = None
    for avoid in dict.fromkeys([frozenset(holder.avoid), frozenset()]):  # a crowded lift may be the only way
        try:
            with dispatch.closing(h, set(avoid)):
                if towards is None:
                    asset = finder.nearest_asset(h, asset_type, h.place_of(holder), holder.step_free)["asset"]
                else:
                    asset = on_the_way(h, asset_type, h.place_of(holder), towards, holder.step_free)
            break
        except LookupError as error:
            failure = error
    if asset is None:
        raise PagerError(str(failure)) from failure
    return asset["space_id"], asset["id"]


def on_the_way(h: Hospital, asset_type: str, here, goal_space: str, step_free: bool) -> dict:
    """The free item of this type with the shortest walk here -> item -> goal."""
    goal = h.router.place_for_space(goal_space)
    free = [a for a in h.bmap.assets.values() if a["type"] == asset_type and a["id"] not in h.in_use]
    if not free:
        raise LookupError(f"No free {finder.noun(asset_type)} found.")
    places = {a["id"]: h.router.place_for_asset(a["id"]) for a in free}
    free.sort(key=lambda a: stopovers.straight_line_m(h, here, places[a["id"]])
              + stopovers.straight_line_m(h, places[a["id"]], goal))
    best = None
    for asset in free[:stopovers.MAX_CANDIDATES]:
        try:
            cost = (h.router.route(here, places[asset["id"]], step_free).cost_m
                    + h.router.route(places[asset["id"]], goal, step_free).cost_m)
        except ValueError:
            continue
        if best is None or cost < best[0]:
            best = (cost, asset)
    if best is None:
        raise LookupError(f"No free {finder.noun(asset_type)} can be reached on the way.")
    return best[1]


def free_asset_wording(text: str) -> bool:
    words = text.strip().lower().split()
    return bool(words) and words[0] in FREE_WORDS


def resolve_element(h: Hospital, text: str) -> str:
    """A lift, staircase, walkway, corridor, door or room by name -> its id."""
    if text in h.bmap.connectors or text in h.bmap.spaces:
        return text
    lowered = text.strip().lower().removeprefix("the ")
    connectors = [c["id"] for c in h.bmap.connectors.values() if c["name"].lower() == lowered]
    if connectors:
        return connectors[0]
    try:
        return h.bmap.find_element(text)
    except MapError:
        return resolve_room(h, text)


def resolve_room(h: Hospital, text: str) -> str:
    """A room by number or name; an unlabelled imported room ('Room 0.07') also answers to '0.07'."""
    for reference in (text.strip(), f"Room {text.strip()}"):
        try:
            return h.bmap.resolve_space(reference)
        except MapError:
            continue
    spaces = voice.place_mention(h, text)
    if len(spaces) == 1:
        return spaces[0]
    raise PagerError(f"Which room do you mean by '{text}'? Say its number, e.g. A0.12.")


def asset_type_for(h: Hospital, word: str) -> str | None:
    word = word.strip().lower()
    known = finder.findable_types(h)
    if word.replace(" ", "_") in known:
        return word.replace(" ", "_")
    typed = voice.mention(voice.spoken_form(word), voice.FIND_WORDS)
    if typed in known:
        return typed
    return next((t for t in known if rules.label(t).lower() in word or word in rules.label(t).lower()), None)


# ---------- level 1: locate ----------

def find(h: Hospital, pager_id: str, query: str, floor_level: int | None = None) -> dict:
    """Rooms and items matching the query, nearest first; highlighted on the map, nothing navigated."""
    view = empty_view()
    try:
        space_id, asset_id = resolve_place(h, pager_id, query)
        matches = [(space_id, asset_id)]
    except PagerError:
        matches = []
    if floor_level is not None:
        found = h.bmap.search(query, floor_level)
        matches = [(r["id"], None) if r["kind"] == "space" else (h.bmap.assets[r["id"]]["space_id"], r["id"])
                   for r in found]
    if not matches:
        raise PagerError(f"I can't find '{query}' on the map.")
    lines = []
    for space_id, asset_id in matches[:MAX_LISTED]:
        view["spaces"].append(space_id)
        if asset_id:
            view["assets"].append(asset_id)
        what = h.bmap.assets[asset_id]["name"] if asset_id else h.bmap.spaces[space_id]["name"]
        inside = f" in {h.bmap.spaces[space_id]['name']}" if asset_id else ""
        lines.append(f"{what}{inside}, {where_text(h, space_id)}")
    say = lines[0][:1].upper() + lines[0][1:] + "." if len(lines) == 1 else \
        f"{len(matches)} found. Nearest: {lines[0]}."
    return {"say": say, "view": view, "results": lines}


def where_am_i(h: Hospital, pager_id: str) -> dict:
    result = voice.where(h, pager_id)
    building = building_of(h, result["space_id"])
    say = result["say"] if not building else result["say"].replace(" on ", f" ({building}) on ", 1)
    return {"say": say, "view": {"spaces": [result["space_id"]], "assets": []}}


def overview(h: Hospital) -> dict:
    floors = sorted(h.bmap.floors.values(), key=lambda f: f["level"])
    buildings = [b["name"] for b in h.bmap.data.get("buildings", [])]
    links = [s["name"] for s in h.bmap.spaces.values() if s["room_type"] == "walkway"]
    connectors = [f"{c['name']} ({'lift' if c['kind'] == 'lift' else 'stairs'})" for c in h.bmap.connectors.values()]
    closed = [describe_element(h, e) for e in sorted(h.bmap.closed)]
    say = (f"{h.bmap.data['building'].get('name', 'This hospital')}: {len(floors)} floors"
           + (f", {len(buildings)} buildings" if len(buildings) > 1 else "") + ".")
    return {"say": say, "view": empty_view(), "buildings": buildings, "floors": [f["name"] for f in floors],
            "links_between_buildings": links, "lifts_and_stairs": connectors, "closed": closed}


# ---------- level 2/3: route ----------

def stop_for(h: Hospital, pager_id: str, text: str, towards: str | None = None) -> dict:
    """A stop as said -> a stop entry: 'coffee' or 'the toilet' picks the one best on the way; rooms and free items
    are fixed now."""
    category = voice.mention(voice.spoken_form(text), voice.STOP_WORDS)
    if category and not voice.place_mention(h, text, names_only=True):
        return {"category": category, "dwell_s": stopovers.STOP_CATEGORIES[category]["dwell_s"]}
    space_id, asset_id = resolve_place(h, pager_id, text, towards)
    return {"id": asset_id or space_id, "category": None, "dwell_s": 0.0}


def go_to(h: Hospital, pager_id: str, place: str, step_free: bool = False, via: list[str] | None = None,
          avoid: list[str] | None = None, urgent: bool = False, start: str | None = None) -> dict:
    """Navigate this pager to a place, optionally step-free (bed, trolley, wheelchair), via stops in order, and
    keeping away from lifts, stairs or walkways ('lift A is crowded') for this trip only."""
    holder = h.patient_by_pager(pager_id)
    if start:  # "from the main entrance to CT": the pager is put there first
        start_space, start_asset = resolve_place(h, pager_id, start)
        here = h.router.place_for_asset(start_asset) if start_asset else h.router.place_for_space(start_space)
        h.set_position(holder.pager_tag_id, h.bmap.spaces[here.space_id]["floor_id"], *here.point)
        h.lifts.release(holder.id)
        holder.plan = None
    before = (holder.step_free, holder.avoid)
    holder.step_free = step_free
    holder.avoid = {resolve_element(h, element) for element in avoid or []}
    notes = []
    try:
        space_id, asset_id = resolve_place(h, pager_id, place)
        stops = []
        for stop in via or []:
            try:
                stops.append(stop_for(h, pager_id, stop, space_id))
            except PagerError as error:  # "a free wheelchair" when none is free: go anyway, say so
                if not free_asset_wording(stop):
                    raise
                notes.append(f"{error} Skipping that stop.")
    except PagerError:
        holder.step_free, holder.avoid = before
        raise
    holder.search = None
    holder.stops = stops
    holder.stops_ordered = True
    try:
        result = dispatch.go_to(h, pager_id, space_id)
    except ValueError as error:  # no route with these constraints and closures
        if not holder.avoid:
            holder.destination, holder.stops = None, []
            raise PagerError(str(error)) from error
        # Avoiding a crowded lift must not strand a bed: take it anyway rather than no route at all.
        avoided = " and ".join(describe_element(h, e) for e in sorted(holder.avoid))
        holder.avoid = set()
        try:
            result = dispatch.go_to(h, pager_id, space_id)
        except ValueError as again:
            holder.destination, holder.stops = None, []
            raise PagerError(str(again)) from again
        steps = (result.get("route") or {}).get("instructions", [])
        taken = list(dict.fromkeys(h.bmap.connectors[i["connector"]]["name"] for i in steps if "connector" in i))
        notes.append(f"No {'step-free ' if step_free else ''}way around {avoided}, so taking "
                     f"{' and '.join(taken) or 'the shortest way'} anyway.")
    if asset_id:
        holder.destination["name"] = h.bmap.assets[asset_id]["name"]
    planned = result.get("stops") or []
    stop_ids = [stop["id"] for stop in planned]
    view = {"spaces": [space_id, *(h.bmap.assets[i]["space_id"] if i in h.bmap.assets else i for i in stop_ids)],
            "assets": [a for a in [asset_id, *stop_ids] if a in h.bmap.assets]}
    route = result.get("route") or {}
    understood = f"To {holder.destination['name']}, {where_text(h, space_id)}"
    if planned:
        understood += " via " + ", ".join(stop["name"] for stop in planned)
    if step_free:
        understood += ", step-free"
    if holder.avoid:
        understood += ", avoiding " + " and ".join(describe_element(h, e) for e in sorted(holder.avoid))
    minutes = max(1, round(route.get("minutes") or 1))
    say = f"{understood}: about {minutes} min. {result['say'].removeprefix('Time to go. ')}"
    if notes:
        say = f"{' '.join(notes)} {say}"
    if urgent:
        say = f"Urgent. {say}"
    return {"say": say, "view": view, "choice": result.get("choice"), "walk_m": route.get("walk_m"),
            "stops": [stop["name"] for stop in planned]}


def avoid_on_route(h: Hospital, pager_id: str, elements: list[str]) -> dict:
    """Keep the current trip away from these lifts, stairs or walkways from now on."""
    holder = h.patient_by_pager(pager_id)
    if not (holder.destination or holder.search):
        raise PagerError("No route to change. Say where to go.")
    added = {resolve_element(h, element) for element in elements}
    before = set(holder.avoid)
    holder.avoid |= added
    h.lifts.release(holder.id)
    holder.plan = None
    try:
        g = dispatch.guidance(h, pager_id)
    except ValueError as error:
        holder.avoid = before
        holder.plan = None
        raise PagerError(f"{error} Keeping the current route.") from error
    names = " and ".join(describe_element(h, e) for e in sorted(added))
    say = f"Avoiding {names}." + (f" Your route: {g['choice']}." if g.get("choice") else "")
    goal = (g.get("goal") or {}).get("space_id")
    return {"say": say, "view": {"spaces": [goal] if goal else [], "assets": []}}


def place_ref(h: Hospital, ref: str) -> tuple[str, str | None]:
    """A room or item for directions without a pager: an id from the map, else the first room or item by name."""
    ref = ref.strip()
    if ref in h.bmap.spaces:
        return ref, None
    if ref in h.bmap.assets:
        return h.bmap.assets[ref]["space_id"], ref
    lowered = ref.lower().removeprefix("the ")
    exact = [s["id"] for s in h.bmap.spaces.values() if lowered in (s["name"].lower(), (s.get("ref") or "").lower())]
    spaces = exact or voice.place_mention(h, ref)
    if spaces:
        return spaces[0], None
    found = h.bmap.search(lowered)
    if found:
        hit = found[0]
        return (hit["id"], None) if hit["kind"] == "space" else (h.bmap.assets[hit["id"]]["space_id"], hit["id"])
    raise PagerError(f"I can't find '{ref}' on the map.")


def directions(h: Hospital, origin: str, destination: str, step_free: bool = False,
               via: list[str] | None = None) -> dict:
    """A route from A to B for anyone (the maps page): the pager's planner, with lifts, stairs and closures, but no
    pager moved and no lift slot booked."""
    ends = [place_ref(h, ref) for ref in (origin, *(via or []), destination)]
    places = [h.router.place_for_asset(asset) if asset else h.router.place_for_space(space) for space, asset in ends]
    named = [h.bmap.assets[asset]["name"] if asset else h.bmap.spaces[space]["name"] for space, asset in ends]
    try:
        plan = dispatch.plan_trip(h, "directions", places[0], places[-1], step_free, h.now(), places[1:-1],
                                  reserve=False)
    except ValueError as error:  # closed, or no step-free way
        raise PagerError(f"No {'step-free ' if step_free else ''}route from {named[0]} to {named[-1]} right now.") from error
    route = plan["route"]
    ends_json = [{"space_id": space, "asset_id": asset, "name": name, "floor_id": h.bmap.spaces[space]["floor_id"]}
                 for (space, asset), name in zip(ends, named, strict=True)]
    minutes = max(1, round(route["minutes"]))
    say = f"{named[0]} to {named[-1]}: about {minutes} min, {round(route['walk_m'])} m" + (
        f", {plan['choice']}" if plan["choice"] != "walk" else "") + (", step-free" if step_free else "") + "."
    return {"say": say, "from": ends_json[0], "to": ends_json[-1], "via": ends_json[1:-1], "step_free": step_free,
            "choice": plan["choice"], "walk_m": route["walk_m"], "minutes": route["minutes"], "route": route}


def free_near(h: Hospital, what: str, near: str, step_free: bool = False) -> dict:
    """The nearest free item of a kind from a named place, with the way there (the maps page has no pager)."""
    asset_type = asset_type_for(h, what)
    if asset_type is None:
        known = ", ".join(finder.noun(t) for t in finder.findable_types(h))
        raise PagerError(f"There is no {what} here. I can find: {known}.")
    space_id, _ = place_ref(h, near)
    try:
        found = finder.nearest_asset(h, asset_type, space_id, step_free)
    except LookupError as error:
        raise PagerError(str(error)) from error
    asset = found["asset"]
    ends = [{"space_id": space_id, "asset_id": None, "name": h.bmap.spaces[space_id]["name"],
             "floor_id": h.bmap.spaces[space_id]["floor_id"]},
            {"space_id": asset["space_id"], "asset_id": asset["id"], "name": asset["name"], "floor_id": asset["floor_id"]}]
    return {"say": f"{asset['name']} is free. {found['say']}", "from": ends[0], "to": ends[1], "via": [],
            "step_free": step_free, "choice": None, "walk_m": found["walk_m"], "minutes": found["minutes"],
            "route": found["route"], "view": {"spaces": [asset["space_id"]], "assets": [asset["id"]]}}


def find_free(h: Hospital, pager_id: str, what: str) -> dict:
    """Navigate to the nearest free item of a kind (wheelchair, bed, infusion pump, defibrillator ...)."""
    asset_type = asset_type_for(h, what)
    if asset_type is None:
        known = ", ".join(finder.noun(t) for t in finder.findable_types(h))
        raise PagerError(f"There is no {what} here. I can find: {known}.")
    holder = h.patient_by_pager(pager_id)
    holder.destination = None
    try:
        result = dispatch.start_search(h, pager_id, asset_type)
    except (KeyError, LookupError) as error:
        raise PagerError(str(error.args[0] if error.args else error)) from error
    search = holder.search
    return {"say": result["say"], "view": {"spaces": [search["space_id"]], "assets": [search["asset_id"]]}}


def cancel(h: Hospital, pager_id: str) -> dict:
    holder = h.patient_by_pager(pager_id)
    if holder.search:
        say = dispatch.stop_search(h, pager_id)["say"]
    elif holder.destination:
        say = dispatch.stop_go_to(h, pager_id)["say"]
    else:
        say = "Nothing to cancel."
    holder.stops, holder.stops_ordered, holder.step_free = [], False, False
    holder.avoid = set()
    holder.plan = None
    return {"say": say.removesuffix(" No active destination."), "view": empty_view()}


# ---------- changing the building (level 2 closures, level 4 edits) ----------

def describe_element(h: Hospital, element_id: str) -> str:
    if element_id in h.bmap.connectors:
        return h.bmap.connectors[element_id]["name"]
    if element_id in h.bmap.spaces:
        return h.bmap.spaces[element_id]["name"]
    opening = h.bmap.openings.get(element_id)
    if opening:
        sides = [h.bmap.spaces[s]["name"] for s in opening["space_ids"] if s in h.bmap.spaces]
        return "the door between " + " and ".join(sides)
    return element_id


def set_closed(h: Hospital, pager_id: str, element: str, closed: bool) -> dict:
    """Close or reopen a lift, staircase, room, corridor or walkway; every pager re-plans around it."""
    element_id = resolve_element(h, element)
    if closed:
        h.bmap.closed.add(element_id)
    else:
        h.bmap.closed.discard(element_id)
    replan_everyone(h)
    name = describe_element(h, element_id)
    say = f"{name} {'closed' if closed else 'open again'}. Routes now {'avoid' if closed else 'use'} it."
    holder = h.patient_by_pager(pager_id) if pager_id else None
    if holder and (holder.destination or holder.search):
        g = dispatch.guidance(h, pager_id)
        if g.get("choice"):
            say += f" Your route: {g['choice']}."
    view = {"spaces": [element_id] if element_id in h.bmap.spaces else
            [s for s in h.bmap.connectors.get(element_id, {}).get("space_ids", [])], "assets": []}
    return {"say": say, "view": view, "closed": [describe_element(h, e) for e in sorted(h.bmap.closed)]}


def add_door(h: Hospital, first: str, second: str) -> dict:
    """A new door in the wall two neighbouring rooms share."""
    a, b = resolve_room(h, first), resolve_room(h, second)
    try:
        opening = h.bmap.add_door(a, b)
    except MapError as error:
        raise PagerError(str(error)) from error
    h.router.invalidate(opening["space_ids"])
    replan_everyone(h)
    saved = persist(h)
    names = [h.bmap.spaces[s]["name"] for s in opening["space_ids"]]
    return {"say": f"Door added between {names[0]} and {names[1]}." + (" Saved." if saved else ""),
            "view": {"spaces": list(opening["space_ids"]), "assets": []}}


def label_room(h: Hospital, room: str, name: str, room_type: str | None = None) -> dict:
    """Name a room the way its sign does ('A0.05 is the CT'); the room type follows from the name if not given."""
    space_id = resolve_room(h, room)
    space = h.bmap.spaces[space_id]
    old = space["name"]
    space["name"] = nomenclature.sign_name(name)
    number = nomenclature.room_number(name)
    if number:
        space["ref"] = number
    kind = (room_type or "").strip().lower().replace(" ", "_") or nomenclature.room_type_for(name)
    if kind:
        space["room_type"] = kind
    saved = persist(h)
    label = rules.room_label(space["room_type"]).lower()
    return {"say": f"{old} is now {space['name']} ({label})." + (" Saved." if saved else ""),
            "view": {"spaces": [space_id], "assets": []}}


def move_equipment(h: Hospital, item: str, to_room: str) -> dict:
    """Record that a movable item now stands in another room ('the ultrasound is now in ED Bay 2')."""
    lowered = item.strip().lower().removeprefix("the ")
    found = [a for a in h.bmap.assets.values() if a["name"].lower() == lowered] or \
        [h.bmap.assets[r["id"]] for r in h.bmap.search(lowered) if r["kind"] == "asset"]
    if not found:
        raise PagerError(f"I can't find '{item}'.")
    if len({a["type"] for a in found}) == 1 and len(found) > 1:
        raise PagerError(f"Which one? {', '.join(a['name'] for a in found[:MAX_LISTED])}.")
    asset = found[0]
    if not asset.get("movable", True):
        raise PagerError(f"{asset['name']} is fixed and can't move.")
    space_id = resolve_room(h, to_room)
    old_space = asset["space_id"]
    x, y = h.bmap.anchor(space_id)
    asset.update(space_id=space_id, floor_id=h.bmap.spaces[space_id]["floor_id"], position=[x, y])
    h.router.invalidate([old_space, space_id])
    saved = persist(h)
    return {"say": f"{asset['name']} is now in {h.bmap.spaces[space_id]['name']}." + (" Saved." if saved else ""),
            "view": {"spaces": [space_id], "assets": [asset["id"]]}}
