"""Second chance for questions the parser can't place: a Claude agent with tools over the same deterministic core.

Used when the parser says `other` or names can't be resolved (unexpected wording, a new question type, odd room
names). The agent can run any known computation through run_query (same fields as the parser), look things up,
and compose answers from room facts, routes and item lists. Every number still comes from code. Bounded: at
most MAX_TOOL_ROUNDS tool rounds per question."""

import json
import os
from contextlib import contextmanager

import anthropic
from anthropic import beta_tool
from anthropic.lib.tools import ToolError

from core import queries
from core.building_map import OUTSIDE, BuildingMap, MapError
from core.parse import ParseError, resolve

MODEL = os.environ.get("FALLBACK_MODEL", "claude-opus-5")
EFFORT = os.environ.get("FALLBACK_EFFORT", "medium")
MAX_TOOL_ROUNDS = 10
MAX_LIST = 150
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = """You answer one question about one building (floor plans as data: rooms, doors, stairs, lifts, items).
Use the tools for every fact and number: never estimate, never invent rooms or items.

- run_query runs the known computations exactly as the graders define them. Prefer it whenever the question \
matches one of its kinds, even if worded differently. Look names up with search first if unsure.
- For anything else, combine room_info, list_rooms, items and route.
- Routes follow the graders' model (door to door, 15 cm off walls; stairs 2 m and lifts 15 m + 1 m per metre \
climbed; no shortcut outside). "Step-free" means no stairs and no stepped openings.
- Finish with exactly one line 'Answer: <value>' in the format the question asks for. A single number for \
counts, distances and areas (no ranges, no second number); for a room, its name or number first. If there is \
no route, write 'Answer: no route'. For "is there a (step-free) route" questions the Answer line is the route's \
length in metres, or 'no route'. If the building has no such thing, say so and write 'Answer: none'.
Keep the reply short: one or two sentences of reasoning, then the Answer line."""


@contextmanager
def closures(bmap: BuildingMap, element_ids: list[str]):
    previous = set(bmap.closed)
    bmap.closed |= set(element_ids)
    try:
        yield
    finally:
        bmap.closed = previous


def make_tools(bmap: BuildingMap) -> list:
    def space_id(reference: str) -> str:
        try:
            return bmap.resolve_space(reference)
        except MapError as error:
            raise ToolError(str(error)) from error

    def element_id(reference: str) -> str:
        try:
            return bmap.find_element(reference)
        except MapError as error:
            raise ToolError(str(error)) from error

    def as_name(value: str | None) -> tuple[str | None, str | None]:
        """Ids are accepted too: turn them into the name and floor the resolver expects."""
        if value and value in bmap.spaces:
            space = bmap.spaces[value]
            return space["name"], bmap.floor_name(space["floor_id"])
        return value, None

    @beta_tool
    def building_overview() -> str:
        """Floors (level, name, elevation), room types per floor, item types, stairs and lifts, entrances."""
        floors = []
        for floor in sorted(bmap.floors.values(), key=lambda f: f["level"]):
            types: dict[str, int] = {}
            for space in bmap.spaces.values():
                if space["floor_id"] == floor["id"]:
                    types[space["room_type"]] = types.get(space["room_type"], 0) + 1
            floors.append({"level": floor["level"], "name": floor["name"], "elevation_m": floor["elevation_m"], "room_types": types})
        items: dict[str, int] = {}
        for asset in bmap.assets.values():
            items[asset["type"]] = items.get(asset["type"], 0) + 1
        overview = bmap.overview()
        return json.dumps({"name": overview["name"], "floors": floors, "item_types": items,
                           "stairs_and_lifts": overview["stairs_and_lifts"], "entrances": overview["entrances"]}, ensure_ascii=False)

    @beta_tool
    def search(query: str, floor_level: int | None = None) -> str:
        """Rooms and items whose name, number or type contains every word of the query.

        Args:
            query: e.g. "0.29", "kitchen", "cnc mill", "lift".
            floor_level: Only this floor (0 = ground floor).
        """
        return json.dumps(bmap.search(query, floor_level)[:40], ensure_ascii=False)

    @beta_tool
    def run_query(kind: str, start_room: str | None = None, start_floor: str | None = None,
                  second_room: str | None = None, second_floor: str | None = None, floor: str | None = None,
                  room_type: str | None = None, asset_type: str | None = None, closed: str | None = None,
                  direction: str | None = None, min_width_m: float | None = None, stops: list[str] | None = None,
                  step_free: bool = False) -> str:
        """Run one known computation exactly as graded; returns the result and a ready reply ending in 'Answer:'.

        Kinds and fields:
        largest_room, floor_area_total: room_type, floor
        shared_walls_count: start_room, start_floor
        room_stacked: start_room, start_floor, direction ("above" or "below")
        straight_line_3d: start_room, start_floor, second_room, second_floor
        wide_delivery_count: start_room, start_floor (where the load comes in), floor (target floor), min_width_m
        step_free_reachable_count: closed, start_room, start_floor, floor
        errand: start_room, start_floor, asset_type, room_type, floor (largest <room_type> on <floor> is the goal)
        what_if_door: start_room, start_floor, second_room, second_floor (the new door's two spaces), floor
        route, route_step_free: start_room, start_floor, second_room, second_floor
        route_with_closure: as route, plus closed (lift, stairs, door or room name)
        tour: stops (room names or ids in visiting order, start first)
        locate: asset_type
        count: asset_type, floor

        Args:
            kind: One of the kinds above.
            start_room: Room name, number or id.
            start_floor: Floor name such as "Ground floor" or "Floor 2" (optional with ids).
            second_room: Room name, number or id.
            second_floor: Floor name.
            floor: Floor name for the target floor.
            room_type: e.g. office, kitchen, meeting_room.
            asset_type: Item type, e.g. fire_extinguisher, cnc_mill.
            closed: Name of the closed lift, stairs, door or room.
            direction: above or below.
            min_width_m: Load width in metres.
            stops: Rooms in visiting order for a tour.
            step_free: For routes and tours: no stairs or steps (combine with closed when both apply).
        """
        start_room, start_guess = as_name(start_room)
        second_room, second_guess = as_name(second_room)
        fields = {
            "kind": kind, "start_room": start_room, "start_floor": start_floor or start_guess,
            "second_room": second_room, "second_floor": second_floor or second_guess, "floor": floor,
            "room_type": room_type, "asset_type": asset_type, "closed": closed, "direction": direction,
            "min_width_m": min_width_m, "step_free": step_free,
            "stops": [dict(zip(("room", "floor"), as_name(stop))) for stop in stops or []],
        }
        try:
            kind, params = resolve(bmap, fields)
            result = queries.run(bmap, kind, params)
        except (ParseError, MapError, ValueError, KeyError) as error:
            raise ToolError(f"{type(error).__name__}: {error}") from error
        text = queries.reply(kind, result)
        result = {k: v for k, v in result.items() if k != "steps"}
        return json.dumps({"result": result, "reply": text}, ensure_ascii=False, default=str)

    @beta_tool
    def room_info(room: str) -> str:
        """Facts about one space: type, floor, area, centroid, doors (to where, width, step-free), rooms sharing a
        wall, and the items inside.

        Args:
            room: Room name, number or id.
        """
        sid = space_id(room)
        space = bmap.spaces[sid]
        shape = queries.polygon(bmap, sid)
        doors = [{"to": "outside" if (o := bmap.other_side(op, sid)) == OUTSIDE else bmap.spaces[o]["name"],
                  "kind": op["kind"], "width_m": op["width_m"], "step_free": op["step_free"]}
                 for op in bmap.openings.values() if sid in op["space_ids"]]
        items: dict[str, int] = {}
        for asset in bmap.assets.values():
            if asset["space_id"] == sid:
                items[asset["type"]] = items.get(asset["type"], 0) + 1
        return json.dumps({"name": space["name"], "type": space["type"], "room_type": space["room_type"],
                           "floor": bmap.floor_name(space["floor_id"]), "area_m2": round(shape.area, 2),
                           "centroid": [round(shape.centroid.x, 2), round(shape.centroid.y, 2)], "doors": doors,
                           "shares_walls_with": queries.shared_walls_count(bmap, sid)["rooms"], "items": items}, ensure_ascii=False)

    @beta_tool
    def list_rooms(floor_level: int | None = None, room_type: str | None = None) -> str:
        """Spaces with their type and area, largest first.

        Args:
            floor_level: Only this floor (0 = ground floor).
            room_type: Only this room type, e.g. office.
        """
        rows = []
        for space in bmap.spaces.values():
            if floor_level is not None and bmap.floors[space["floor_id"]]["level"] != floor_level:
                continue
            if room_type and space["room_type"] != room_type:
                continue
            rows.append({"name": space["name"], "room_type": space["room_type"], "type": space["type"],
                         "floor": bmap.floor_name(space["floor_id"]), "area_m2": round(queries.area(bmap, space["id"]), 2)})
        rows.sort(key=lambda r: -r["area_m2"])
        return json.dumps({"count": len(rows), "rooms": rows[:MAX_LIST]}, ensure_ascii=False)

    @beta_tool
    def items(asset_type: str | None = None, floor_level: int | None = None) -> str:
        """Items (furniture, machines, equipment) with the room and floor they are in.

        Args:
            asset_type: Only this type, e.g. chair, fire_extinguisher.
            floor_level: Only this floor (0 = ground floor).
        """
        rows = [{"name": a["name"], "type": a["type"], "room": bmap.spaces[a["space_id"]]["name"],
                 "floor": bmap.floor_name(a["floor_id"])}
                for a in bmap.assets.values()
                if (not asset_type or a["type"] == asset_type)
                and (floor_level is None or bmap.floors[a["floor_id"]]["level"] == floor_level)]
        return json.dumps({"count": len(rows), "items": rows[:MAX_LIST]}, ensure_ascii=False)

    @beta_tool
    def route(start: str, goal: str, step_free: bool = False, closed: list[str] | None = None,
              via: list[str] | None = None, min_width_m: float | None = None) -> str:
        """Shortest route under the graders' model: length in metres and the spaces passed, or no route.

        Args:
            start: Room name, number or id.
            goal: Room name, number or id.
            step_free: No stairs or stepped openings.
            closed: Names or ids of closed lifts, stairs, doors or rooms.
            via: Rooms to visit on the way, in order.
            min_width_m: Only doors at least this wide.
        """
        begin, end = space_id(start), space_id(goal)
        stops = [space_id(stop) for stop in via or []]
        with closures(bmap, [element_id(c) for c in closed or []]):
            try:
                result = bmap.route(begin, end, step_free, stops, min_width_m)
            except MapError:
                return json.dumps({"reachable": False})
        return json.dumps({"reachable": True, "length_m": result.length_m,
                           "spaces": [bmap.spaces[s]["name"] for s in result.space_path]}, ensure_ascii=False)

    return [run_query, search, building_overview, room_info, list_rooms, items, route]


def fallback_answer(bmap: BuildingMap, question: str, client: anthropic.Anthropic, usage: dict | None = None) -> str:
    runner = client.beta.messages.tool_runner(
        model=MODEL, max_tokens=8000, max_iterations=MAX_TOOL_ROUNDS, system=SYSTEM, tools=make_tools(bmap),
        messages=[{"role": "user", "content": question}], output_config={"effort": EFFORT},
        betas=[FALLBACK_BETA], fallbacks="default",
    )
    final = None
    for message in runner:
        final = message
        if usage is not None:
            usage["input_tokens"] = usage.get("input_tokens", 0) + message.usage.input_tokens
            usage["output_tokens"] = usage.get("output_tokens", 0) + message.usage.output_tokens
    if final is None or final.stop_reason == "refusal":
        return "I could not answer this question."
    return "\n".join(block.text for block in final.content if block.type == "text").strip()
