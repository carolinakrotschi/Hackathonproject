"""What a voice agent (e.g. ElevenLabs server tools) needs to drive a pager: tool descriptions and "where am I".

Every pager endpoint answers with an English `say`; the agent reads it back instead of phrasing its own."""

import copy
import re

from shapely.geometry import Point

from hospital import dispatch, finder, rules, utterance
from hospital import stops as stopovers
from hospital.state import WALKWAY_ROOM_TYPES, WALKWAY_TYPES, Hospital
from hospital.utterance import FIND_WORDS, PLACE_WORDS, STOP_WORDS, Mention, Trip, spoken_form

PAGER_PATH = "/api/hospital/pagers/{pager_id}"
NEARBY = 3
NEARBY_M = 25.0
LANDMARK_BONUS_M = 10.0
LANDMARK_ROOM_TYPES = {
    "lift", "stairwell", "reception", "lobby", "cafe", "kiosk", "nurse_station", "toilet", "pharmacy", "radiology",
    "emergency", "triage", "lounge", "lab", "therapy",
}

READ_BACK = "Afterwards, read the `say` field of the response to the user exactly, without adding words or numbers."
PAGER_ID = {"pager_id": {"type": "string",
                         "description": "ID of this pager, e.g. PG-07. Comes from the session "
                                        "(dynamic variable); never ask the user for it."}}
NO_PARAMS = {"type": "object", "properties": {}, "required": []}


def type_help(types: list[str]) -> str:
    return ", ".join(f"{t} = {rules.label(t)}" for t in types)


def category_help(categories: list[str]) -> str:
    return ", ".join(f"{c} = {stopovers.STOP_CATEGORIES[c]['label']}" for c in categories)


def find_params(types: list[str]) -> dict:
    return {"type": "object",
            "properties": {"type": {"type": "string", "enum": types,
                                    "description": f"Object type. Mapping: {type_help(types)}."}},
            "required": ["type"]}


def stop_params(categories: list[str]) -> dict:
    return {"type": "object",
            "properties": {
                "stops": {"type": "array", "minItems": 1, "maxItems": stopovers.MAX_STOPS,
                          "items": {"type": "object",
                                    "properties": {"category": {"type": "string", "enum": categories,
                                                                "description": f"Kind of stop: "
                                                                               f"{category_help(categories)}."}},
                                    "required": ["category"]},
                          "description": "All requested stopovers; replaces any stops planned before."},
                "ordered": {"type": "boolean", "default": False,
                            "description": "true only if the user explicitly gives an order."},
                "dry_run": {"type": "boolean", "default": False,
                            "description": "true if the user only asks whether a stop still fits "
                                           "(\"Do I have time for a coffee?\"); false if they want it planned."},
            },
            "required": ["stops"]}


TOOLS: list[dict] = [
    {"name": "next_instruction",
     "description": "Gets the current directions for this pager: wait, go, the next turns, the lift, or "
                    "destination reached. Call it when the user asks where to go, what comes next, when to leave "
                    "or whether they are there yet, and after every move when they ask \"what now?\". `status` is "
                    "wait, go, arrived or idle; `mode` is search (looking for an object), appointment, return "
                    "(back to the patient's room) or idle (no destination). " + READ_BACK,
     "method": "GET", "path": f"{PAGER_PATH}/guidance", "path_params": PAGER_ID, "params": NO_PARAMS,
     "example_say": "In 20 metres, turn left into Corridor 1, then take Lift A down to the ground floor."},
    {"name": "find_object",
     "description": "Finds the nearest free object of a type (by walking distance from the current position) and "
                    "navigates there from now on; this takes priority over the appointment and the way back. Call "
                    "it for requests like \"Where is the nearest free bed?\", \"I need a wheelchair\", \"Find a "
                    "stretcher\". Map the user's word to the object type. On error 422 the type does not exist in "
                    "this building, on 409 none is free: then read out the error message. " + READ_BACK,
     "method": "POST", "path": f"{PAGER_PATH}/find", "path_params": PAGER_ID,
     "params": find_params(list(rules.ASSET_LABELS)),
     "example_say": "Nearest free bed: Room 1.12, floor 1, about 1 minute. In 20 metres, turn left into "
                    "Corridor 1."},
    {"name": "cancel_search",
     "description": "Ends the current object search; afterwards the appointment, the way back to the room or (for "
                    "staff) no destination applies again. Call it for \"stop searching\", \"I don't need it "
                    "anymore\", \"I already have one\". " + READ_BACK,
     "method": "DELETE", "path": f"{PAGER_PATH}/find", "path_params": PAGER_ID, "params": NO_PARAMS,
     "example_say": "Bed search cancelled. No active destination."},
    {"name": "add_stop",
     "description": "Plans stopovers on the way to the current destination (coffee, toilet, kiosk, pharmacy, "
                    "water) and says whether there is still time before the appointment. The best place for each "
                    "stop and the order are chosen automatically. Replaces stops planned before, so send all "
                    "requested stops at once. For a plain question (\"Do I have time for a coffee?\") use "
                    "dry_run=true; if the user then agrees, call again with dry_run=false. " + READ_BACK,
     "method": "POST", "path": f"{PAGER_PATH}/stops", "path_params": PAGER_ID,
     "params": stop_params(list(stopovers.STOP_CATEGORIES)),
     "example_say": "Coffee fits, you'll arrive 6 min before your appointment."},
    {"name": "clear_stops",
     "description": "Removes all planned stopovers; the route then goes straight to the destination. Call it for "
                    "\"no coffee after all\", \"clear the stops\", \"go straight there\". Afterwards call "
                    "next_instruction and read out its `say`.",
     "method": "DELETE", "path": f"{PAGER_PATH}/stops", "path_params": PAGER_ID, "params": NO_PARAMS,
     "example_say": "All stopovers cleared."},
    {"name": "go_to",
     "description": "Navigates to a named room or department (\"Take me to the emergency room\", \"Go to Room "
                    "1.12\", \"Bring the patient to radiology\") from now on, instead of the appointment or the way "
                    "back; an object search still comes first. Pass the place as the user said it; on error 422 the "
                    "place is unknown, then read out the error message. " + READ_BACK,
     "method": "POST", "path": f"{PAGER_PATH}/destination", "path_params": PAGER_ID,
     "params": {"type": "object",
                "properties": {"place": {"type": "string", "description": "Room name, number or department."}},
                "required": ["place"]},
     "example_say": "Time to go. In 20 metres, turn left into Corridor G, then take Lift A down to the ground floor."},
    {"name": "cancel_go_to",
     "description": "Ends navigation to a named place; the appointment, the way back or (for staff) no destination "
                    "applies again. Call it for \"cancel the route\", \"I don't need to go there anymore\". "
                    + READ_BACK,
     "method": "DELETE", "path": f"{PAGER_PATH}/destination", "path_params": PAGER_ID, "params": NO_PARAMS,
     "example_say": "Route to Emergency cancelled. No active destination."},
    {"name": "where_am_i",
     "description": "Says which room and floor the pager is in right now, with up to three landmarks nearby "
                    "(lift, stairs, nurses' station ...). Call it for \"Where am I?\", \"Where are we?\", "
                    "\"I'm lost\". " + READ_BACK,
     "method": "GET", "path": f"{PAGER_PATH}/where", "path_params": PAGER_ID, "params": NO_PARAMS,
     "example_say": "You are in Corridor 1 on floor 1. Nearby: Lift A, Nurses' station and Room 1.05."},
]


def tools_for(h: Hospital) -> list[dict]:
    """TOOLS with the object types and stop categories this building actually has."""
    tools = copy.deepcopy(TOOLS)
    categories = [c for c in stopovers.STOP_CATEGORIES if stopovers.candidates(h, c, 0.0)]
    for tool in tools:
        if tool["name"] == "find_object":
            tool["params"] = find_params(finder.findable_types(h))
        elif tool["name"] == "add_stop":
            tool["params"] = stop_params(categories)
    return tools


def where(h: Hospital, pager_id: str) -> dict:
    patient = h.patient_by_pager(pager_id)
    here = h.place_of(patient)
    space = h.bmap.spaces[here.space_id]
    floor_id = space["floor_id"]
    floor_name = h.bmap.floor_name(floor_id)
    point = Point(here.point)
    ranked = []
    for space_id, other in h.bmap.spaces.items():
        if (space_id == here.space_id or other["floor_id"] != floor_id or other["type"] in WALKWAY_TYPES
                or other["room_type"] in WALKWAY_ROOM_TYPES):
            continue
        distance = h.shapes[space_id].distance(point)
        if distance <= NEARBY_M:
            bonus = LANDMARK_BONUS_M if other["room_type"] in LANDMARK_ROOM_TYPES else 0.0
            ranked.append((distance - bonus, other["name"]))
    nearby = [name for _, name in sorted(ranked)[:NEARBY]]
    say = f"You are in {space['name']} on {finder.floor_ref(floor_name)}."
    if nearby:
        listed = nearby[0] if len(nearby) == 1 else f"{', '.join(nearby[:-1])} and {nearby[-1]}"
        say += f" Nearby: {listed}."
    return {"space_id": here.space_id, "name": space["name"], "floor_id": floor_id, "floor_name": floor_name,
            "nearby": nearby, "say": say}


STOPOVER_WORDS = ("stops", "stopover", "stopovers", "stop over", "detour", "detours", "stopps")
CANCEL_PHRASES = ("cancel", "end", "abort", "quit", "never mind", "nevermind", "forget", "delete", "clear", "remove",
                  "skip", "no more", "don t need")
EXPLICIT_CANCEL = ("cancel", "abort", "never mind", "nevermind", "forget it", "forget that",
                   "forget the route", "call it off")
CANCEL_PREFIXES = ("abbrech", "stopp", "beende", "storn", "lösch", "vergiss")
WHERE_PHRASES = ("where am i", "where are we", "i m lost", "im lost", "lost", "my location", "wo bin ich",
                 "wo sind wir", "verlaufen")
NEXT_PHRASES = ("next", "continue", "where to", "where now", "where do i go", "which way", "how long", "how far",
                "route", "way", "directions", "direction", "what now", "now what", "repeat", "say again",
                "instructions", "go",
                "weiter", "wohin", "nächste", "anweisung", "jetzt", "lange", "weg", "ziel")
GO_PHRASES = ("take me to", "bring me to", "take us to", "bring us to", "go to", "get to", "navigate to", "route to",
              "way to", "directions to", "head to", "walk to", "guide me to", "lead me to", "take the patient to",
              "bring the patient to", "transport to", "bring mich", "zur", "zum", "nach")
NOT_UNDERSTOOD = ("Sorry, I didn't get that. Try: take me to the emergency room, find the nearest free bed, "
                  "coffee on the way, or where am I?")
SHOW_PHRASES = ("where is", "where s", "where are", "show me", "show", "locate", "find", "look for", "wo ist")
CLOSE_RE = re.compile(r"\b(closed|out of (order|service)|broken|blocked|shut|not working|under maintenance)\b"
                      r"|^\s*(please\s+)?(close|block|shut)\b", re.IGNORECASE)
REOPEN_RE = re.compile(r"\b(re-?open(ed)?|open again|is open|back (in service|in order|open)|working again|fixed)\b",
                       re.IGNORECASE)
LABEL_RE = re.compile(r"^\s*(?:please\s+)?(?:label|name|rename|call|mark)\s+(?:room\s+)?(?P<room>.+?)\s+(?:as|to)\s+"
                      r"(?:the\s+|an?\s+)?(?P<name>.+?)\s*[.!]?$", re.IGNORECASE)
IS_RE = re.compile(r"^\s*(?:room\s+)?(?P<room>[a-z]{0,2}\s?\d{1,2}[.,]\s?\d{1,3}[a-z]?)\s+is\s+(?:now\s+)?"
                   r"(?:the\s+|an?\s+)?(?P<name>[^.!?]+?)\s*[.!]?$", re.IGNORECASE)
DOOR_RE = re.compile(r"add (?:a )?(?:new )?door between (?P<a>.+?) and (?P<b>.+?)\s*[.!]?$", re.IGNORECASE)
MOVE_RE = re.compile(r"^\s*(?:the\s+)?(?P<item>.+?)\s+(?:is now|was moved|has been moved|now stands|got moved)\s+"
                     r"(?:in|to|into|at)\s+(?:the\s+)?(?P<room>.+?)\s*[.!]?$", re.IGNORECASE)


def place_mention(h: Hospital, text: str, names_only: bool = False) -> list[str]:
    return utterance.place_mention(h, text, names_only)


def nearest_space(h: Hospital, pager_id: str | None, space_ids: list[str]) -> str | None:
    if pager_id is None:  # no one to be near (the maps page): the first match
        return space_ids[0] if space_ids else None
    patient = h.patient_by_pager(pager_id)
    here = h.place_of(patient)
    best = None
    for space_id in space_ids:
        try:
            cost = h.router.route(here, h.router.place_for_space(space_id), patient.step_free).cost_m
        except ValueError:
            continue
        if best is None or cost < best[0]:
            best = (cost, space_id)
    return best[1] if best else None


def reference(h: Hospital, pager_id: str, m: Mention, stop: bool) -> str:
    """What pager_tools.go_to understands for a mention: a space id, 'free wheelchair', or a stop category."""
    if m.kind == "asset":
        return f"free {m.value}"
    if m.kind == "category" and stop:
        return m.value
    category = STOP_WORDS.get(m.said)
    if stop and category and len(m.ids) > 1:
        return category  # "a toilet on the way": the one that costs the least detour
    if m.kind == "category":
        found = stopovers.candidates(h, m.value, 0.0)
        ids = [c["id"] if c["id"] in h.bmap.spaces else h.bmap.assets[c["id"]]["space_id"] for c in found]
        return nearest_space(h, pager_id, ids) or m.value
    holder = h.patient_by_pager(pager_id)
    with dispatch.closing(h, holder.avoid):
        return nearest_space(h, pager_id, m.ids) or m.ids[0]


def navigate(h: Hospital, pager_id: str, text: str, trip: Trip, notes: list[str]) -> dict:
    """Start the trip the request describes: destination, stops in the order said, avoided lifts, step-free."""
    from hospital import pager_tools

    holder = h.patient_by_pager(pager_id)
    notes = [*(f"There is no {finder.noun(t)} in this building." for t in trip.missing), *notes]
    goal, stops = trip.goal, list(trip.stops)
    step_free = trip.step_free
    avoid = list(trip.avoid)
    if goal is None and dispatch.mode_of(holder) != "idle":
        # "stop by the pharmacy" on the way to where this pager already goes (a place, an item, the appointment)
        step_free = step_free or holder.step_free
        avoid += sorted(holder.avoid)
        if not holder.destination:
            entries = [pager_tools.stop_for(h, pager_id, reference(h, pager_id, m, True)) for m in stops]
            return with_notes(notes, run(text, "add_stop", lambda: stopovers.set_stops(h, pager_id, entries, True)))
        place = holder.destination["space_id"]
    else:
        goal = goal or stops.pop()
        holder.avoid = set(avoid)
        holder.step_free = step_free
        if goal.kind == "asset" and not stops:
            holder.stops = []
            if trip.urgent:
                notes = ["Urgent.", *notes]
            return with_notes(notes, run(text, "find", lambda: pager_tools.find_free(h, pager_id, goal.value)))
        place = reference(h, pager_id, goal, False)
    via = [reference(h, pager_id, m, True) for m in stops]
    return with_notes(notes, run(text, "go_to", lambda: pager_tools.go_to(h, pager_id, place, step_free, via, avoid,
                                                                         trip.urgent)))


def with_notes(notes: list[str], result: dict) -> dict:
    if notes:
        result["say"] = " ".join([*notes, result["say"]])
    return result


def changes(h: Hospital, pager_id: str, text: str, trip: Trip) -> tuple[list[str], dict | None]:
    """Closures said along with a request ("lift A is broken, take me to CT"): applied for everyone first."""
    from hospital import pager_tools

    notes = []
    for element_id, closed in [(e, True) for e in trip.close] + [(e, False) for e in trip.reopen]:
        result = run(text, "set_closed", lambda: pager_tools.set_closed(h, pager_id, element_id, closed))
        if not result["ok"]:
            return notes, result
        notes.append(result["say"].split(" Your route:")[0])
    return notes, None


def run(text: str, intent: str, action) -> dict:
    """A pager_tools action as a voice answer, with what the map should show."""
    from hospital.pager_tools import PagerError

    try:
        result = action()
    except PagerError as error:
        return answer(text, intent, False, str(error))
    return answer(text, intent, True, result["say"]) | {"view": result.get("view")}


def room_ref(text: str) -> str:
    """'a 0.05' / 'A0,05' as said -> 'A0.05'; anything else unchanged."""
    squeezed = re.sub(r"\s+", "", text).replace(",", ".")
    return squeezed.upper() if re.fullmatch(r"[A-Za-z]{0,2}\d{1,2}\.\d{1,3}[A-Za-z]?", squeezed) else text.strip()


def element_mention(h: Hospital, text: str) -> str | None:
    """A lift, staircase, walkway, corridor or room the text names (for closures)."""
    said = spoken_form(text)
    named = [(len(spoken_form(c["name"])), c["name"]) for c in h.bmap.connectors.values()
             if spoken_form(c["name"]) in said]
    named += [(len(spoken_form(s["name"])), s["name"]) for s in h.bmap.spaces.values()
              if s["type"] not in ("lift", "stair") and spoken_form(s["name"]) in said]
    if named:
        return max(named)[1]
    places = place_mention(h, text)
    return h.bmap.spaces[places[0]]["name"] if len(places) == 1 else None


def edits(h: Hospital, pager_id: str, text: str, padded: str) -> dict | None:
    """Changes staff report by voice: closures, room labels, new doors, moved equipment."""
    from hospital import pager_tools

    if match := DOOR_RE.search(text):
        return run(text, "add_door", lambda: pager_tools.add_door(h, room_ref(match["a"]), room_ref(match["b"])))
    if match := (LABEL_RE.search(text) or IS_RE.search(text)):
        return run(text, "label_room", lambda: pager_tools.label_room(h, room_ref(match["room"]), match["name"]))
    if match := MOVE_RE.search(text):
        return run(text, "move_equipment", lambda: pager_tools.move_equipment(h, match["item"], room_ref(match["room"])))
    return None


def answer(text: str, intent: str, ok: bool, say: str) -> dict:
    return {"heard": text, "intent": intent, "ok": ok, "say": say}


def says(padded: str, phrases) -> bool:
    return any(f" {p} " in padded for p in phrases)


def mention(padded: str, table: dict[str, str]) -> str | None:
    """The table value for the earliest phrase in the text (longest phrase on a tie)."""
    hits = [(padded.find(f" {key} "), -len(key), value) for key, value in table.items() if f" {key} " in padded]
    return min(hits)[2] if hits else None


def answer(text: str, intent: str, ok: bool, say: str) -> dict:
    return {"heard": text, "intent": intent, "ok": ok, "say": say}


def says(padded: str, phrases) -> bool:
    return any(f" {p} " in padded for p in phrases)


def mention(padded: str, table: dict[str, str]) -> str | None:
    """The table value for the earliest phrase in the text (longest phrase on a tie)."""
    hits = [(padded.find(f" {key} "), -len(key), value) for key, value in table.items() if f" {key} " in padded]
    return min(hits)[2] if hits else None


def handle(h: Hospital, pager_id: str, text: str) -> dict:
    from hospital import pager_tools

    words = re.findall(r"[a-zäöüßé]+", text.lower())
    padded = f" {' '.join(words)} "
    edited = edits(h, pager_id, text, padded)
    if edited is not None:
        return edited
    trip = utterance.parse(h, text)
    wants = trip.goal is not None or bool(trip.stops)
    stop_category = mention(padded, STOP_WORDS)
    # "stop" alone cancels, but "stop for a coffee" asks for a stopover and "stop by the lab, then CT" is a trip.
    cancel = (says(padded, CANCEL_PHRASES) or any(w.startswith(c) for w in words for c in CANCEL_PREFIXES)
              or ("stop" in words and stop_category is None))
    if cancel and (says(padded, EXPLICIT_CANCEL) or not (wants or trip.avoid)):
        if stop_category or says(padded, STOPOVER_WORDS) or any(w.startswith("zwischenstopp") for w in words):
            stopovers.clear_stops(h, pager_id)
            return answer(text, "clear_stops", True, "Stopovers cleared.")
        holder = h.patient_by_pager(pager_id)
        holder.avoid = set()
        if holder.search:
            say = dispatch.stop_search(h, pager_id)["say"].removesuffix(" No active destination.")
            return answer(text, "cancel_search", True, say)
        if holder.destination:
            return answer(text, "cancel_go_to", True, dispatch.stop_go_to(h, pager_id)["say"])
        had = bool(h.patient_by_pager(pager_id).stops)
        stopovers.clear_stops(h, pager_id)
        return answer(text, "clear_stops", True, "Stopovers cleared." if had else "Nothing to cancel.")
    if says(padded, WHERE_PHRASES) and not wants:
        return answer(text, "where", True, where(h, pager_id)["say"])
    notes, failed = changes(h, pager_id, text, trip)
    if failed:
        return failed
    if not wants:
        if trip.avoid:
            return with_notes(notes, run(text, "avoid", lambda: pager_tools.avoid_on_route(h, pager_id, trip.avoid)))
        if notes:
            return answer(text, "set_closed", True, " ".join(notes))
        if CLOSE_RE.search(text) or REOPEN_RE.search(text):
            return answer(text, "set_closed", False, "Which lift, stairs, walkway or room?")
    goal = trip.goal
    if (goal is not None and goal.kind == "place" and not trip.stops and says(padded, SHOW_PHRASES)
            and not says(padded, GO_PHRASES)):  # "where is the CT?" shows it; "take me to the CT" navigates
        return run(text, "find", lambda: pager_tools.find(h, pager_id, reference(h, pager_id, goal, False)))
    if wants:
        return navigate(h, pager_id, text, trip, notes)
    if says(padded, NEXT_PHRASES):
        return answer(text, "next", True, dispatch.guidance(h, pager_id)["say"])
    return answer(text, "unknown", False, NOT_UNDERSTOOD)
