"""What a spoken staff request means for a trip, without an LLM: the destination, stops in order, lifts or links to
avoid or prefer, closures, step-free and urgency.

"Take me to theatre 1 with a bed, stop by the pharmacy and don't use lift A, there are a lot of people" ->
destination Theatre 1, stop Pharmacy, avoid Lift A, step-free. The text is split into clauses (commas, "and",
"then", ...); every room, department, item or lift it names is a mention, and the trigger words before it in its
clause decide its role: "to"/"bring" -> destination, "via"/"stop by"/"pick up"/"from" -> stop, "avoid"/"don't use"
-> avoid. The last destination mention is the goal; every other place is a stop, in the order said. Staff decide
where patients go: words about the patient's condition only mark the request urgent, they never pick a room."""

import re
from dataclasses import dataclass, field

from hospital.state import WALKWAY_ROOM_TYPES, Hospital

FIND_WORDS = {
    "bed": "bed", "beds": "bed", "stretcher": "stretcher", "stretchers": "stretcher", "gurney": "stretcher",
    "gurneys": "stretcher", "trolley": "stretcher", "trolleys": "stretcher", "wheelchair": "wheelchair",
    "wheelchairs": "wheelchair", "wheel chair": "wheelchair", "infusion pump": "infusion_pump",
    "infusion pumps": "infusion_pump", "pump": "infusion_pump", "pumps": "infusion_pump", "drip": "infusion_pump",
    "walker": "walker", "walkers": "walker", "rollator": "walker", "defibrillator": "defibrillator",
    "defib": "defibrillator", "aed": "defibrillator", "ultrasound": "ultrasound", "ultrasound machine": "ultrasound",
    "monitor": "monitor", "monitors": "monitor", "patient monitor": "monitor", "ventilator": "ventilator",
    "operating table": "operating_table", "surgery table": "operating_table", "surgical table": "operating_table",
    "op table": "operating_table", "operating tables": "operating_table",
    "anaesthesia machine": "anaesthesia_machine", "anaesthetic machine": "anaesthesia_machine",
    "bett": "bed", "betten": "bed", "liege": "stretcher", "trage": "stretcher", "rollstuhl": "wheelchair",
    "infusionspumpe": "infusion_pump", "pumpe": "infusion_pump", "defi": "defibrillator",
    "ultraschall": "ultrasound", "beatmungsgerät": "ventilator", "op tisch": "operating_table",
}
STOP_WORDS = {
    "coffee": "coffee", "cafe": "coffee", "café": "coffee", "cafeteria": "coffee", "toilet": "toilet",
    "toilets": "toilet", "restroom": "toilet", "bathroom": "toilet", "washroom": "toilet", "wc": "toilet",
    "loo": "toilet", "kiosk": "kiosk", "shop": "kiosk", "pharmacy": "pharmacy", "chemist": "pharmacy",
    "water": "water",
    "kaffee": "coffee", "toilette": "toilet", "klo": "toilet", "apotheke": "pharmacy", "wasser": "water",
}
# Department words -> room type (the nearest room of that type).
PLACE_WORDS = {
    "emergency room": "emergency", "emergency department": "emergency", "emergency unit": "emergency",
    "emergency ward": "emergency", "er": "emergency", "ed": "emergency", "casualty": "emergency",
    "notaufnahme": "emergency", "x ray": "radiology", "xray": "radiology", "imaging": "radiology",
    "radiologie": "radiology", "canteen": "cafe", "nurses station": "nurse_station", "nurse station": "nurse_station",
    "nursing station": "nurse_station", "reception": "reception", "rezeption": "reception",
    "physiotherapy": "therapy", "physio": "therapy", "rehab": "therapy", "apotheke": "pharmacy",
    "resuscitation": "resuscitation", "intensive care": "resuscitation", "theatre": "operating_theatre",
    "theatres": "operating_theatre", "operating theatre": "operating_theatre", "operating room": "operating_theatre",
    "surgery": "operating_theatre", "op": "operating_theatre", "operationssaal": "operating_theatre",
    "toilet": "toilet", "toilets": "toilet", "restroom": "toilet", "loo": "toilet", "washroom": "toilet",
    "consulting room": "consulting", "consultation room": "consulting", "clinic room": "consulting",
}
# Other names people use for one room (or rooms whose name starts with the target).
NAME_WORDS = {
    "resus": "ED Resus", "resus room": "ED Resus", "resus bay": "ED Resus", "shock room": "ED Resus",
    "trauma room": "ED Resus", "trauma bay": "ED Resus", "schockraum": "ED Resus", "triage": "ED Triage",
    "recovery room": "Recovery", "pacu": "Recovery", "anaesthetics": "Anaesthetic room",
    "anaesthesia room": "Anaesthetic room", "scrub": "Scrub room", "scrubs": "Scrub room",
    "sterile supplies": "Sterile store", "sterile supply": "Sterile store", "changing room": "Theatre staff changing",
    "blood test": "Phlebotomy", "blood tests": "Phlebotomy", "blood draw": "Phlebotomy",
    "blood sampling": "Phlebotomy", "path lab": "Pathology lab", "pathology": "Pathology lab",
    "lab": "Pathology lab", "laboratory": "Pathology lab", "labor": "Pathology lab", "radiology": "Radiology reception",
    "x ray department": "Radiology reception", "ct scan": "CT", "ct scanner": "CT", "cat scan": "CT",
    "computed tomography": "CT", "mri scan": "MRI", "mri scanner": "MRI", "outpatients": "Outpatients reception",
    "outpatient clinic": "Outpatients reception", "outpatient department": "Outpatients reception",
    "opd": "Outpatients reception", "waiting area": "Outpatients waiting area", "waiting room": "Outpatients waiting area",
    "front door": "Main entrance", "entrance": "Main entrance", "main door": "Main entrance", "lobby": "Main entrance",
    "eingang": "Main entrance", "building a": "Main entrance", "building b": "Outpatients reception",
    "canteen": "Cafeteria", "cafe": "Cafeteria", "coffee": "Cafeteria", "coffee shop": "Cafeteria", "restaurant": "Cafeteria",
    "kantine": "Cafeteria", "kiosk": "Shop", "newsagent": "Shop", "chemist": "Pharmacy", "dispensary": "Pharmacy",
    "ward 1a": "Nurses' station 1A", "ward 1": "Nurses' station 1A", "ward 2a": "Nurses' station 2A",
    "ward 2": "Nurses' station 2A", "doctors room": "Doctors' office", "sluice": "Dirty utility",
    "break room": "Staff room", "staff lounge": "Staff room", "admin": "Admin office", "administration": "Admin office",
    "bathroom": "Assisted bathroom", "control room": "MRI control room",
}
# Lifts, stairs and links by other names; "*lift" / "*stairs" stand for all of them.
ELEMENT_WORDS = {
    "bridge": "Bridge A–B", "skybridge": "Bridge A–B", "link bridge": "Bridge A–B", "first floor bridge": "Bridge A–B",
    "walkway": "Covered walkway A–B", "covered walkway": "Covered walkway A–B", "outside walkway": "Covered walkway A–B",
    "ground floor walkway": "Covered walkway A–B", "outside": "Covered walkway A–B",
    "lift": "*lift", "lifts": "*lift", "stairs": "*stairs", "staircase": "*stairs",
}
# Spoken variants -> the words the map uses.
SPELLING = {"theater": "theatre", "theaters": "theatre", "anesthetic": "anaesthetic", "anesthesia": "anaesthesia",
            "anesthetics": "anaesthetics", "elevator": "lift", "elevators": "lifts", "stairway": "stairs",
            "stairwell": "stairs", "staircases": "stairs", "stair": "stairs", "aufzug": "lift", "fahrstuhl": "lift",
            "treppe": "stairs", "xray": "x ray", "ambulatory": "outpatients"}
NUMBERS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
           "nine": "9", "ten": "10", "eins": "1", "zwei": "2", "drei": "3"}

# Trigger words: the last one before a mention in its clause decides the mention's role.
GO = ("to", "into", "towards", "toward", "take me", "bring me", "bring", "go", "head", "navigate", "route", "find",
      "need", "needs", "send", "transfer", "transport", "move", "back to", "before", "where", "get me", "reach",
      "meet", "meet me", "zur", "zum", "nach", "in to")
FETCH = ("get", "grab", "fetch", "pick", "collect", "bring", "need", "needs", "find", "locate", "borrow", "hole",
         "holen", "brauche")
STOP = ("via", "stop by", "stop at", "stop off at", "stopping at", "stopping by", "drop by", "drop off", "pop by",
        "pop into", "swing by", "pass by", "passing", "past", "pick up", "picking up", "collect", "get", "grab",
        "fetch", "from", "after", "first at")
AVOID = ("avoid", "avoiding", "don t use", "dont use", "do not use", "don t take", "dont take", "do not take",
         "not using", "without using", "without taking", "without", "not via", "not through", "not over",
         "not the", "skip", "skipping", "bypass", "stay off", "stay away from", "keep away from", "keep off",
         "instead of", "rather than", "no", "not", "never", "don t go through", "do not go through",
         "don t go via", "nicht", "ohne", "kein", "keinen")
PREFER = ("use", "using", "take", "taking", "ride", "catch", "via", "through", "over", "across", "by", "with the",
          "go via", "go over", "go through", "nimm", "mit dem")
HERE = ("i m in", "i am in", "i m at", "i am at", "we re in", "we are in", "we re at", "we are at", "i m standing in",
        "currently in", "currently at")
LATER = ("first", "on the way", "on our way", "on my way", "along the way", "en route", "too", "as well", "beforehand",
         "auf dem weg", "unterwegs", "vorher", "zuerst")
CROWDED = ("crowded", "busy", "full", "packed", "jammed", "congested", "lot of people", "lots of people",
           "many people", "queue", "queues", "long wait", "slow", "rush", "crowd", "traffic", "overcrowded",
           "voll", "überfüllt")
CLOSED = ("closed", "out of order", "out of service", "broken", "blocked", "shut", "not working",
          "under maintenance", "defect", "defekt", "kaputt", "gesperrt")
URGENT = ("urgent", "urgently", "critical", "critically", "stat", "asap", "hurry", "quickly", "fast",
          "immediately", "code blue", "code red", "cardiac arrest", "arrest", "crash", "crashing", "priority",
          "life threatening", "collapsed", "unconscious", "bleeding", "not breathing", "stroke", "sepsis",
          "deteriorating", "emergency", "trauma", "incoming", "anaphylaxis", "anaphylactic", "seizure",
          "haemorrhage", "hemorrhage", "overdose", "choking", "rapid response", "mayday", "dringend", "notfall",
          "schnell", "sofort")
PATIENT = ("patient", "patients", "person", "man", "woman", "child", "baby", "casualty", "victim", "him", "her",
           "them")
MODES = ("with", "on", "in", "by", "using", "mit", "im", "auf")  # "with a bed": how, not what to fetch
FREE = ("free", "spare", "available", "empty", "unused", "nearest", "closest", "next", "another", "any", "freie",
        "freien", "freies")
STEP_FREE = ("step free", "stepfree", "no stairs", "without stairs", "avoid stairs", "avoid the stairs",
             "lift only", "lifts only", "bed", "beds", "trolley", "stretcher", "gurney", "wheelchair", "barrierefrei",
             "ohne treppe", "bett", "rollstuhl", "liege", "trage", "incubator", "hoist")
NOT_DESTINATIONS = (*WALKWAY_ROOM_TYPES, "lift", "stairwell")
SPLIT = re.compile(r"[,;:!?]|\.(?!\d)|\b(and then|then|and|but|because|cause|since|while|so|also|plus|und|dann|aber)\b")
TRAILING_CODE = re.compile(r"\s+(?:[A-Z]?\d+[A-Z]?)$")
FLOOR_WORDS = {0: ("ground floor", "floor 0", "level 0", "erdgeschoss"),
               1: ("first floor", "floor 1", "level 1", "1st floor", "erster stock"),
               2: ("second floor", "floor 2", "level 2", "2nd floor", "zweiter stock")}
QUALIFIER_GLUE = {"in", "on", "at", "of", "the", "floor", "level", "building", "ward", "near", "next", "im", "auf"}
LEVEL_BONUS = (32, 24, 16, 8, 0)  # a room's own name beats a department word that overlaps it


def spoken_form(text: str) -> str:
    """Lowercase words and numbers, space-padded, the way speech recognition writes them: 'Room A1.05' ->
    ' room a 105 ', 'Theater one' -> ' theatre 1 '."""
    text = a_and_e(text)
    words = [SPELLING.get(w, NUMBERS.get(w, w)) for w in re.findall(r"[a-zäöüßé]+|[0-9]+", text)]
    return re.sub(r"(?<=\d) (?=\d)", "", f" {' '.join(words)} ")


def a_and_e(text: str) -> str:
    return re.sub(r"\ba\s*(?:and|&|n)\s*e\b", "emergency department", text.lower())


@dataclass
class Mention:
    start: int
    end: int
    kind: str  # place | asset | category | element
    ids: list[str] = field(default_factory=list)  # places: candidate spaces; elements: connector/space ids
    value: str = ""  # asset type, stop category, floor id, or "*lift" / "*stairs"
    role: str = ""  # go | stop | avoid | prefer | close | here | mode | ""
    said: str = ""
    level: int = 4  # places: how specific the matched word is (0 = the room's own name)
    context: bool = False  # "in recovery" with no verb: where something happens, not an errand

    def score(self) -> int:
        bonus = {"place": LEVEL_BONUS[self.level], "element": 16, "asset": 16}.get(self.kind, 0)
        return self.end - self.start + bonus


@dataclass
class Trip:
    goal: Mention | None = None
    stops: list[Mention] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)  # element ids
    close: list[str] = field(default_factory=list)
    reopen: list[str] = field(default_factory=list)
    step_free: bool = False
    urgent: bool = False
    mentions: list[Mention] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # items asked for that this building doesn't have


def spaces_named(h: Hospital, target: str) -> list[str]:
    """'Theatre 1' -> that room; 'Doctors' office' -> every room whose name starts with it."""
    target = target.lower()
    exact = [s["id"] for s in h.bmap.spaces.values() if s["name"].lower() == target]
    return exact or [s["id"] for s in h.bmap.spaces.values() if s["name"].lower().startswith(target)]


def place_index(h: Hospital, names_only: bool = False) -> dict[str, tuple[list[str], int]]:
    """Spoken key -> candidate spaces, most specific first: names, numbers, names without the ward code, other
    names, department words, room types."""
    levels: list[dict[str, list[str]]] = [{}, {}, {}, {}, {}]
    for space in h.bmap.spaces.values():
        if space["room_type"] in NOT_DESTINATIONS:
            continue
        keys = [space["name"], space.get("ref") or ""]
        if space["room_type"] == "patient_room" and space.get("ref"):
            keys.append("room " + re.sub(r"^[A-Za-z]+", "", space["ref"]))
        for key in filter(None, map(str.strip, keys)):
            levels[0].setdefault(spoken_form(key), []).append(space["id"])
        base = TRAILING_CODE.sub("", space["name"])
        if base != space["name"] and base.lower() != "room":
            levels[1].setdefault(spoken_form(base), []).append(space["id"])
    if not names_only:
        for word, target in NAME_WORDS.items():
            if ids := spaces_named(h, target):
                levels[2].setdefault(spoken_form(word), ids)
        for word, room_type in PLACE_WORDS.items():
            if ids := [s["id"] for s in h.bmap.spaces.values() if s["room_type"] == room_type]:
                levels[3].setdefault(spoken_form(word), ids)
        for room_type in {s["room_type"] for s in h.bmap.spaces.values()} - {*NOT_DESTINATIONS, *URGENT}:
            ids = [s["id"] for s in h.bmap.spaces.values() if s["room_type"] == room_type]
            levels[4].setdefault(spoken_form(room_type.replace("_", " ")), ids)
    index: dict[str, tuple[list[str], int]] = {}
    for number, level in enumerate(levels):
        for key, ids in level.items():
            if key.strip():
                index.setdefault(key, (ids, number))
    return index


def element_index(h: Hospital) -> dict[str, list[str]]:
    """Spoken key -> lift, stairs, walkway or corridor ids."""
    index: dict[str, list[str]] = {}
    for connector in h.bmap.connectors.values():
        index.setdefault(spoken_form(connector["name"]), []).append(connector["id"])
    for space in h.bmap.spaces.values():
        if space["room_type"] in WALKWAY_ROOM_TYPES and space["room_type"] != "lobby":
            index.setdefault(spoken_form(space["name"]), []).append(space["id"])
    for word, target in ELEMENT_WORDS.items():
        if target.startswith("*"):
            kind = target[1:]
            ids = [c["id"] for c in h.bmap.connectors.values() if c["kind"] == kind]
        else:
            ids = spaces_named(h, target)
        if ids:
            index.setdefault(spoken_form(word), ids)
    return index


def place_mention(h: Hospital, text: str, names_only: bool = False) -> list[str]:
    """Spaces the text names (the longest name or department word it contains); several can share it (WC, X-ray),
    the caller picks the nearest."""
    said = spoken_form(text)
    index = place_index(h, names_only)
    found = [key for key in index if key in said]
    return index[max(found, key=lambda key: len(key) + LEVEL_BONUS[index[key][1]])][0] if found else []


def occurrences(padded: str, key: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(re.escape(key), padded)]


def mentions_in(h: Hospital, said: str) -> list[Mention]:
    """Every place, item, stop category and lift/link in the text, longest first where they overlap."""
    found: list[Mention] = []
    for key, (ids, level) in place_index(h).items():
        found += [Mention(a, b, "place", ids, said=key.strip(), level=level) for a, b in occurrences(said, key)]
    for level, words in FLOOR_WORDS.items():
        floors = [f["id"] for f in h.bmap.floors.values() if f["level"] == level]
        for word in words:
            key = spoken_form(word)
            found += [Mention(a, b, "floor", floors, said=key.strip()) for a, b in occurrences(said, key)]
    for key, ids in element_index(h).items():
        value = ELEMENT_WORDS.get(key.strip(), "")
        value = value if value.startswith("*") else ""
        found += [Mention(a, b, "element", ids, value, said=key.strip()) for a, b in occurrences(said, key)]
    present = {a["type"] for a in h.bmap.assets.values()}
    for word, asset_type in FIND_WORDS.items():
        if asset_type in present:
            key = spoken_form(word)
            found += [Mention(a, b, "asset", value=asset_type, said=key.strip()) for a, b in occurrences(said, key)]
    for word, category in STOP_WORDS.items():
        key = spoken_form(word)
        found += [Mention(a, b, "category", value=category, said=key.strip()) for a, b in occurrences(said, key)]
    kept: list[Mention] = []
    rank = {"place": 0, "element": 1, "asset": 2, "floor": 3, "category": 4}
    for m in sorted(found, key=lambda m: (-m.score(), rank[m.kind], m.start)):
        if all(m.end <= k.start + 1 or m.start >= k.end - 1 for k in kept):  # keys share their padding spaces
            kept.append(m)
    return sorted(kept, key=lambda m: m.start)


def last_trigger(text: str, triggers) -> int:
    """Where the last of these phrases ends in text (padded), -1 if none."""
    ends = [m.end() for t in triggers for m in re.finditer(f" {re.escape(t)} ", text)]
    return max(ends, default=-1)


def has(text: str, phrases) -> bool:
    return any(f" {p} " in text for p in phrases)


def clauses(text: str) -> list[tuple[str, bool]]:
    """The text split into clauses, each spoken-form padded, with whether it started with 'then'."""
    result, then = [], False
    for part in SPLIT.split(a_and_e(text)):
        if part is None:
            continue
        if part.strip() in ("then", "and then", "dann"):
            then = True
            continue
        if SPLIT.fullmatch(part):
            continue
        if part.strip():
            result.append((spoken_form(part), then))
            then = False
    return result


def parse(h: Hospital, text: str) -> Trip:
    trip = Trip()
    whole = spoken_form(text)
    trip.urgent = has(re.sub(r" emergency (room|department|unit|ward) ", " ", whole), URGENT)
    ordered: list[Mention] = []
    previous_role = ""
    for clause, after_then in clauses(text):
        crowded, closed = has(clause, CROWDED), has(clause, CLOSED)
        reopened = has(clause, ("reopened", "re opened", "open again", "is open", "back in service", "working again",
                                "fixed", "back open"))
        later = [m.start() for t in LATER for m in re.finditer(f" {re.escape(t)} ", clause)]
        mentions = mentions_in(h, clause)
        clause_roles = []
        for number, m in enumerate(mentions):
            before = clause[:m.start + 1]
            roles = {"go": last_trigger(before, GO), "stop": last_trigger(before, STOP),
                     "avoid": last_trigger(before, AVOID), "here": last_trigger(before, HERE)}
            if m.kind == "element":
                roles["prefer"] = last_trigger(before, PREFER)
                if reopened:
                    m.role = "reopen"
                elif closed:
                    m.role = "close"
                elif crowded or roles["avoid"] >= 0 and roles["avoid"] >= roles["prefer"]:
                    m.role = "avoid"
                elif roles["prefer"] >= 0 or roles["go"] >= 0 or roles["stop"] >= 0:
                    m.role = "prefer"
                clause_roles.append(m.role)
                continue
            if m.kind in ("place", "floor") and qualifies(h, mentions[:number], clause, m):
                continue  # "the doctors' office on ward 2": narrows the room before it, not a stop
            if m.kind == "floor":
                continue
            if closed or reopened:
                m.role = "reopen" if reopened else "close"
                clause_roles.append(m.role)
                continue
            words = before.split()[-3:]
            right_before = [w for w in words if w not in ("a", "an", "the", "his", "her", "their")][-1:]
            if m.kind == "asset" and not set(words) & set(FREE) and (
                    set(right_before) & set(MODES) or not set(before.split()[-4:]) & set(FETCH)):
                m.role = "mode"  # "with a bed", "bed transport": how the patient travels, not an errand
                continue
            best = max(roles, key=roles.get)
            m.role = best if roles[best] >= 0 else ""
            if m.role in ("", "go") and any(p >= m.end - 1 for p in later):
                m.role = "stop"
            if not m.role:
                m.role = "go" if after_then else previous_role if previous_role in ("stop", "go") and number == 0 else ""
            m.context = not m.role and bool(words) and words[-1] in ("in", "on", "at", "near")  # "code blue on ward 1A"
            if m.kind == "asset" and "with" in words and ordered and ordered[-1].kind == "place" \
                    and ordered[-1] in mentions[:number]:
                m.role = ordered.pop().role or "go"  # "the next theatre with a free operating table"
            clause_roles.append(m.role)
            ordered.append(m)
        roles_here = [r for r in clause_roles if r]
        previous_role = roles_here[-1] if roles_here else previous_role
        trip.mentions += mentions
    for m, after in zip(trip.mentions, trip.mentions[1:]):
        # "lift A and lift B are packed": the first lift is in a clause of its own
        if m.kind == after.kind == "element" and not m.role and after.role in ("avoid", "close", "reopen"):
            m.role = after.role
    present = {a["type"] for a in h.bmap.assets.values()}
    trip.missing = sorted({t for word, t in FIND_WORDS.items() if t not in present and has(whole, (word,))})
    for m in trip.mentions:
        if m.kind == "element" or (m.role in ("avoid", "close", "reopen") and m.kind == "place"):
            target = {"avoid": trip.avoid, "close": trip.close, "reopen": trip.reopen}.get(m.role)
            if m.value == "*stairs":
                if m.role == "avoid":
                    trip.step_free = True
                elif m.role == "prefer":
                    trip.avoid += [c for c in h.bmap.connectors if h.bmap.connectors[c]["kind"] == "lift"]
                continue
            if m.value == "*lift":
                if m.role == "avoid":
                    trip.avoid += m.ids
                elif m.role == "prefer":
                    trip.step_free = True
                continue
            if target is not None:
                target += m.ids[:1] if m.kind == "place" else m.ids
            elif m.role == "prefer":
                trip.avoid += siblings(h, m.ids[0])
    places = [m for m in ordered if m.role not in ("here", "avoid", "close", "reopen", "mode")]
    goals = [m for m in places if m.role == "go"]
    unmarked = [m for m in places if not m.role]
    trip.goal = goals[-1] if goals else unmarked[-1] if unmarked else None
    if trip.goal is not None and trip.goal.kind == "asset":
        # "Bleeding in ED Bay 2, bring an infusion pump": the item goes to the place said without a verb
        there = [m for m in unmarked if m.kind == "place" and m in places[:places.index(trip.goal)]]
        if there:
            places.remove(trip.goal)
            places.insert(places.index(there[-1]), trip.goal)
            trip.goal = there[-1]
    trip.stops = []
    for m in places:  # a room said twice in a row ("go to phlebotomy for a blood test") is one stop
        following = places[places.index(m) + 1:] + ([trip.goal] if trip.goal else [])
        if m is trip.goal or m.context or (following and following[0].ids == m.ids and following[0] is not m):
            continue
        trip.stops.append(m)
    rest = whole
    for m in trip.mentions:
        if m.kind == "element" and m.value != "*stairs":
            rest = rest.replace(f" {m.said} ", " ")
    trip.step_free = (trip.step_free or has(rest, STEP_FREE) or any(m.role == "mode" for m in trip.mentions)
                      or trip.urgent and has(whole, PATIENT))  # an urgent patient travels lying down
    trip.avoid = [e for e in dict.fromkeys(trip.avoid) if e not in trip.close]
    return trip


def qualifies(h: Hospital, earlier: list[Mention], clause: str, m: Mention) -> bool:
    """'on ward 2', 'in building B', 'on the first floor' right after a place: keep only that place's rooms there."""
    places = [e for e in earlier if e.kind == "place" and e.role not in ("avoid", "close", "reopen")]
    if not places:
        return False
    target = places[-1]
    if set(clause[target.end:m.start + 1].split()) - QUALIFIER_GLUE:
        return False
    if m.kind == "floor":
        floors = set(m.ids)
        narrowed = [i for i in target.ids if h.bmap.spaces[i]["floor_id"] in floors]
    else:
        spots = {(h.bmap.spaces[i]["floor_id"], h.bmap.spaces[i].get("building")) for i in m.ids}
        buildings = {h.bmap.spaces[i].get("building") for i in m.ids}
        narrowed = [i for i in target.ids
                    if (h.bmap.spaces[i]["floor_id"], h.bmap.spaces[i].get("building")) in spots]
        if not narrowed and m.said.startswith("building"):
            narrowed = [i for i in target.ids if h.bmap.spaces[i].get("building") in buildings]
    if narrowed:
        target.ids = narrowed
    m.role = "qualifier"
    return True


def siblings(h: Hospital, element_id: str) -> list[str]:
    """'Take lift B' avoids the other lifts and stairs; 'over the bridge' avoids the other links."""
    if element_id in h.bmap.connectors:
        return [c for c in h.bmap.connectors if c != element_id]
    room_type = h.bmap.spaces[element_id]["room_type"]
    if room_type != "walkway":
        return []
    return [s["id"] for s in h.bmap.spaces.values() if s["room_type"] == "walkway" and s["id"] != element_id]
