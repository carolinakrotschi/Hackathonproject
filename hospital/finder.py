"""Nearest free asset, in-use flags and visitor lookup for the hospital."""

import hmac
import math

from core.building_map import MapError
from core.walk import Place
from hospital import rules
from hospital.state import Hospital, route_json

MAX_CANDIDATES = 15
FLOOR_PENALTY_M = 50.0


def resolve_start(h: Hospital, from_ref: str) -> str:
    try:
        return h.bmap.resolve_space(from_ref)
    except MapError as error:
        raise KeyError(from_ref) from error


def lowercase(text: str) -> str:
    """'Bed' → 'bed', 'Ground floor' → 'ground floor'; acronyms like 'CT scanner' stay."""
    return text if text[1:2].isupper() else text[:1].lower() + text[1:]


def floor_ref(floor_name: str) -> str:
    """How a floor reads after 'to' or 'on': 'the ground floor', 'floor 1'."""
    name = lowercase(floor_name)
    return f"the {name}" if name.endswith("floor") else name


def noun(asset_type: str) -> str:
    return lowercase(rules.label(asset_type))


def nearest_phrase(asset_type: str) -> str:
    return f"Nearest free {noun(asset_type)}"


def at_phrase(asset_type: str) -> str:
    return f"at the {noun(asset_type)}"


def findable_types(h: Hospital) -> list[str]:
    present = {a["type"] for a in h.bmap.assets.values()}
    return [t for t in rules.ASSET_LABELS if t in present]


def straight_line_m(h: Hospital, start: Place, asset: dict) -> float:
    space_floor = h.bmap.spaces[start.space_id]["floor_id"]
    ax, ay = asset["position"]
    levels = abs(h.bmap.floors[space_floor]["level"] - h.bmap.floors[asset["floor_id"]]["level"])
    return math.hypot(start.point[0] - ax, start.point[1] - ay) + FLOOR_PENALTY_M * levels


def nearest_asset(h: Hospital, asset_type: str, from_ref: str | Place, step_free: bool = False) -> dict:
    """Nearest free asset of a type by walking cost, from a space reference or an exact Place (a pager's position)."""
    begin = from_ref if isinstance(from_ref, Place) else h.router.place_for_space(resolve_start(h, from_ref))
    name = rules.label(asset_type)
    free = [a for a in h.bmap.assets.values() if a["type"] == asset_type and a["id"] not in h.in_use]
    if not free:
        raise LookupError(f"No free {noun(asset_type)} found.")
    free.sort(key=lambda asset: straight_line_m(h, begin, asset))
    best = None
    for asset in free[:MAX_CANDIDATES]:
        try:
            walk = h.router.route(begin, h.router.place_for_asset(asset["id"]), step_free=step_free)
        except ValueError:
            continue
        if best is None or walk.cost_m < best[1].cost_m:
            best = (asset, walk)
    if best is None:
        raise LookupError(f"No free {noun(asset_type)} can be reached from here.")
    asset, walk = best
    route = route_json(walk)
    space = h.bmap.spaces[asset["space_id"]]
    floor = h.bmap.floor_name(asset["floor_id"])
    say = f"{nearest_phrase(asset_type)}: {space['name']}, {lowercase(floor)}, about {minutes_text(route['seconds'])}."
    return {
        "asset": {"id": asset["id"], "name": asset["name"], "type": asset["type"], "label": name,
                  "floor_id": asset["floor_id"], "space_id": asset["space_id"], "position": asset["position"],
                  "in_use": asset["id"] in h.in_use},
        "walk_m": walk.walk_m,
        "minutes": route["minutes"],
        "route": route,
        "say": say,
    }


def minutes_text(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} {'minute' if minutes == 1 else 'minutes'}"


def set_in_use(h: Hospital, asset_id: str, in_use: bool) -> dict:
    if asset_id not in h.bmap.assets:
        raise KeyError(asset_id)
    if in_use:
        h.in_use.add(asset_id)
    else:
        h.in_use.discard(asset_id)
    return {"id": asset_id, "in_use": in_use}


def visit(h: Hospital, patient_query: str, code: str) -> dict | None:
    matches = h.find_patients(patient_query)
    if len(matches) != 1:
        return None
    patient = matches[0]
    if not hmac.compare_digest(code.strip().encode(), patient.visit_code.encode()):
        return None
    room = h.bmap.spaces[patient.room_id]
    floor = {"id": room["floor_id"], "name": h.bmap.floor_name(room["floor_id"])}
    walk = h.router.route(h.router.place_for_space(h.entrance_id), h.router.place_for_space(patient.room_id),
                          step_free=True)
    say = (f"{patient.name} is in {room['name']}, {patient.ward}, {floor_ref(floor['name'])}. "
           f"Visiting hours: {h.visiting_hours}.")
    return {
        "patient": {"name": patient.name, "number": patient.number,
                    "room": {"id": patient.room_id, "name": room["name"], "ref": room.get("ref")},
                    "ward": patient.ward, "floor": floor},
        "route": route_json(walk),
        "visiting_hours": h.visiting_hours,
        "say": say,
    }
