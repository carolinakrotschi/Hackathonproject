"""Question → (kind, params with resolved ids). Claude only extracts the phrases as written (one structured-output
call, cached on disk); resolving them to ids against the building is deterministic code."""

import hashlib
import json
import os
import re
import threading
from pathlib import Path

import anthropic

from core.building_map import BuildingMap
from core.queries import QUERIES

MODEL = os.environ.get("PARSE_MODEL", "claude-opus-5")
EFFORT = os.environ.get("PARSE_EFFORT", "low")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CACHE_FILE = Path(__file__).resolve().parent.parent / ".cache" / "parse.jsonl"
_CACHE_LOCK = threading.Lock()

SYSTEM = """You read one question about an indoor building plan and extract its parameters as JSON.
Copy names exactly as they appear in the question. Do not answer the question.

Kinds and the fields each one uses:
- largest_room: room_type, floor ("which is the largest <room type> on <floor>")
- floor_area_total: room_type, floor (total area of every <room type> on <floor>)
- shared_walls_count: start_room, start_floor (how many rooms share a wall with a room)
- room_stacked: start_room, start_floor, direction ("above" or "below")
- straight_line_3d: start_room, start_floor, second_room, second_floor
- wide_delivery_count: start_room, start_floor (room the entrance leads into), floor (target floor), min_width_m
- step_free_reachable_count: closed (the out-of-service lift/stairs/door name), start_room, start_floor, floor
- errand: start_room, start_floor, asset_type, room_type, floor (target: largest <room type> on <floor>)
- what_if_door: start_room, start_floor, second_room, second_floor (the two spaces of the new door), floor (the floor whose rooms are counted)
- route: start_room, start_floor, second_room, second_floor ("How do I get from <start> to <goal>?")
- route_step_free: as route, when the question asks for a step-free / wheelchair / no-stairs route
- route_with_closure: as route, plus closed (the closed lift, stairs, door or room named in the question)
- tour: stops = every place in visiting order, starting point first ("Starting at A, visit B then C")
- locate: asset_type ("Where is the <thing>?")
- count: asset_type, floor ("How many <thing> items are on <floor>?")
- other: anything that fits none of the above (all fields null)
- step_free (any route kind, tour): true when the question rules out stairs or steps (step-free, wheelchair,
  "without stairs", trolley); combine it with closed when both are asked. Otherwise false.

Rules:
- A room written as "Office 0.29 (Ground floor)" gives start_room "Office 0.29" and start_floor "Ground floor".
  Keep any other brackets that are part of the name, e.g. "Bedroom (apartment 8)".
- Floors: copy the floor phrase without "the", e.g. "ground floor", "floor 2", "Basement 1".
- room_type and asset_type: pick the closest value from the lists given with the question.
- Fields a kind does not use are null."""

FIELDS = ["start_room", "start_floor", "second_room", "second_floor", "floor", "room_type", "asset_type", "closed", "direction"]
SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": sorted([*QUERIES, "other"])},
        **{field: {"type": ["string", "null"]} for field in FIELDS},
        "min_width_m": {"type": ["number", "null"]},
        "step_free": {"type": "boolean"},
        "stops": {"type": "array", "items": {
            "type": "object", "properties": {"room": {"type": "string"}, "floor": {"type": ["string", "null"]}},
            "required": ["room", "floor"], "additionalProperties": False}},
    },
    "required": ["kind", *FIELDS, "min_width_m", "step_free", "stops"],
    "additionalProperties": False,
}


class ParseError(ValueError):
    pass


# ---------- Claude extraction (cached) ----------


def _cache() -> dict[str, dict]:
    if not CACHE_FILE.exists():
        return {}
    entries = (json.loads(line) for line in CACHE_FILE.read_text(encoding="utf-8").splitlines() if line)
    return {entry["key"]: entry["fields"] for entry in entries}


def extract(question: str, room_types: list[str], asset_types: list[str], client: anthropic.Anthropic) -> dict:
    prompt = (
        f"Room types in this building: {', '.join(room_types)}\n"
        f"Asset types in this building: {', '.join(asset_types)}\n\nQuestion: {question}"
    )
    key = hashlib.sha256(f"{MODEL}|{SYSTEM}|{prompt}".encode()).hexdigest()
    cached = _cache().get(key)
    if cached is not None:
        return cached
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise ParseError(f"parser stopped: {response.stop_reason}")
    fields = json.loads(next(block.text for block in response.content if block.type == "text"))
    CACHE_FILE.parent.mkdir(exist_ok=True)
    with _CACHE_LOCK, CACHE_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"key": key, "fields": fields}, ensure_ascii=False) + "\n")
    return fields


# ---------- deterministic resolution ----------


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower().removeprefix("the "))


def resolve_floor(bmap: BuildingMap, phrase: str | None) -> str:
    if not phrase:
        raise ParseError("missing floor")
    wanted = normalise(phrase)
    for floor in bmap.floors.values():
        if normalise(floor["name"]) == wanted:
            return floor["id"]
    raise ParseError(f"unknown floor '{phrase}'")


def resolve_space(bmap: BuildingMap, name: str | None, floor_phrase: str | None) -> str:
    if not name:
        raise ParseError("missing room")
    floor_id = resolve_floor(bmap, floor_phrase) if floor_phrase else None
    pool = [s for s in bmap.spaces.values() if floor_id is None or s["floor_id"] == floor_id]
    wanted = normalise(name)
    for test in (
        lambda s: normalise(s["name"]) == wanted,
        lambda s: normalise(s.get("ref") or "") == wanted,
        lambda s: wanted in normalise(f"{s['name']} {s.get('ref') or ''}"),
    ):
        matches = [s["id"] for s in pool if test(s)]
        if len(matches) == 1:
            return matches[0]
        if matches and len({bmap.spaces[m]["name"] for m in matches}) == 1 and test is not None and normalise(bmap.spaces[matches[0]]["name"]) == wanted:
            return matches[0]  # identical names on one floor (an OSM quirk): no text can tell them apart
        if matches:
            raise ParseError(f"'{name}' is ambiguous: {matches[:5]}")
    raise ParseError(f"no room '{name}' on {floor_phrase}")


def resolve_element(bmap: BuildingMap, name: str | None) -> str:
    if not name:
        raise ParseError("missing closed element")
    wanted = normalise(name)
    for elements in (bmap.connectors.values(), bmap.openings.values(), bmap.spaces.values()):
        matches = [e["id"] for e in elements if normalise(e.get("name") or "") == wanted or e["id"] == name]
        if len(matches) == 1:
            return matches[0]
    raise ParseError(f"unknown closed element '{name}'")


def resolve(bmap: BuildingMap, f: dict) -> tuple[str, dict]:
    kind = f["kind"]
    start = lambda: resolve_space(bmap, f["start_room"], f["start_floor"])  # noqa: E731
    second = lambda: resolve_space(bmap, f["second_room"], f["second_floor"])  # noqa: E731
    floor = lambda: resolve_floor(bmap, f["floor"])  # noqa: E731
    if kind in ("largest_room", "floor_area_total"):
        return kind, {"floor_id": floor(), "room_type": f["room_type"]}
    if kind == "shared_walls_count":
        return kind, {"start": start()}
    if kind == "room_stacked":
        return kind, {"start": start(), "direction": f["direction"]}
    if kind == "straight_line_3d":
        return kind, {"first": start(), "second": second()}
    if kind == "wide_delivery_count":
        return kind, {"start": start(), "floor_id": floor(), "min_width": f["min_width_m"]}
    if kind == "step_free_reachable_count":
        return kind, {"closed": resolve_element(bmap, f["closed"]), "start": start(), "floor_id": floor()}
    if kind == "errand":
        return kind, {"start": start(), "asset_type": f["asset_type"], "floor_id": floor(), "room_type": f["room_type"]}
    step_free = bool(f.get("step_free"))
    if kind in ("route", "route_step_free"):
        return ("route_step_free" if step_free else kind), {"start": start(), "goal": second()}
    if kind == "route_with_closure":
        return kind, {"start": start(), "goal": second(), "closed": resolve_element(bmap, f["closed"]), "step_free": step_free}
    if kind == "tour":
        stops = [resolve_space(bmap, stop["room"], stop["floor"]) for stop in f.get("stops") or []]
        if len(stops) < 2:
            raise ParseError("a tour needs at least two places")
        return kind, {"stops": stops, "step_free": step_free}
    if kind in ("locate", "count"):
        types = {a["type"] for a in bmap.assets.values()}
        asset_type = (f["asset_type"] or "").strip().lower().replace(" ", "_")
        if asset_type not in types:
            raise ParseError(f"no item of type '{f['asset_type']}' here")
        return kind, {"asset_type": asset_type} | ({"floor_id": floor()} if kind == "count" else {})
    if kind == "what_if_door":
        first_id = start()
        return kind, {
            "first": first_id,
            "second": second(),
            "door_floor_id": bmap.spaces[first_id]["floor_id"],
            "floor_id": floor(),
        }
    raise ParseError(f"unknown kind {kind}")


def parse(bmap: BuildingMap, question: str, client: anthropic.Anthropic) -> tuple[str, dict]:
    room_types = sorted({s["room_type"] for s in bmap.spaces.values()})
    asset_types = sorted({a["type"] for a in bmap.assets.values()})
    return resolve(bmap, extract(question, room_types, asset_types, client))
