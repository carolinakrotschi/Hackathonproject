"""Conversational guide for one building, as used by the demo server. Claude talks; BuildingMap and core.queries
supply every fact. Each tool also records what the map should show (highlights, the route) in `view`."""

import json
import os

import anthropic
from anthropic import beta_tool
from anthropic.lib.tools import ToolError

from core import queries
from core.building_map import OUTSIDE, BuildingMap, MapError
from core.walk import Place, WalkRoute, WalkRouter

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5")
EFFORT = os.environ.get("AGENT_EFFORT", "medium")
MAX_TOKENS = 16000
MAX_TOOL_ITERATIONS = 12
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_RESULTS = 12
MAX_CANDIDATES = 15

SYSTEM_PROMPT = """You are the voice of a factory building, inside a map app. People ask you where machines and rooms \
are and how to get there, and tell you about changes (a lift is closed, a door was added, a machine moved). \
Whatever your tools find or route is highlighted on the app's map automatically.

Facts
- Every room, machine, route, distance and area comes from your tools. Never guess, and don't add colour you \
didn't look up (what's inside a room, where a coffee machine stands): use room_info if it matters. If something \
isn't on the map, say so plainly, don't speculate where it might be, and offer the closest useful thing you do have.
- The message tells you where the person's blue dot is: that is their start unless they say otherwise. Remember \
any place they tell you.

Asking
- Don't ask what you can decide sensibly. "A kitchen", "the toilets", "a meeting room on floor 2", "the CNC \
lathe" when there are several: take the nearest by walking distance (find_nearest, with floor_level when a floor \
is named) and say there are others, e.g. "The nearest of six CNC lathes is H-A3-C." Ask one short question only \
when the choice really matters and nearest isn't a sensible default.
- Constraints carry over: once someone mentions a wheelchair, crutches, a pram, a trolley, pallet, cart or \
forklift, or "no stairs", every later route is step_free=true until they say otherwise. For a wide load set \
min_width_m to its width. Say "step-free" in each such answer.
- If they change their mind, drop the old goal and say so briefly ("No problem, a kitchen on floor 1 instead").

Answers (they may be read aloud)
- Start with one short line of what you understood, naming the start: "From Warehouse zone H-C3, where you \
are, to the nearest kitchen:" plus any constraint (step-free) or change (lift closed). If you picked one of \
several, say so and offer the others in a few words.
- Then the directions as 3-5 short numbered steps a person can follow while walking: turns, doors, the lift or \
stairs and the floor, and landmarks (machines or rooms you pass). Merge trivial steps. End with the total \
distance and how long it takes, using the tool's "takes" wording as given.
- Name rooms and machines the way the signs do, kind first: "the kitchen (room 0.25)", "Production zone H-A3", \
"the CNC lathe marked H-A3-C". Never show internal ids. For a wheelchair user say "about a minute", not "walking".
- If the person is busy (hands full, on a speaker, "short please"): two or three very short spoken steps and \
the total distance, no mention of the map.
- Before changing the building (closing something, adding a door, moving a machine), say in one line what you \
understood, do it, then say what changes for people (for example: no step-free way upstairs while the only lift \
is closed).
- Off-topic requests (passwords, people, opening hours): say you only know the building's layout, rooms, \
machines and routes, and suggest who could help.

Building overview:
{overview}"""


WALKING_SPEED_M_S = 1.2


def duration(cost_m: float) -> str:
    """How long a route takes, as said aloud. Use this wording as given; don't recompute it."""
    seconds = cost_m / WALKING_SPEED_M_S
    if seconds < 45:
        return "under a minute"
    minutes = round(seconds / 60)
    return "about a minute" if minutes <= 1 else f"about {minutes} minutes"


def as_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def route_payload(bmap: BuildingMap, result: WalkRoute, begin: Place, goal: Place, mode: str = "walk",
                  via: list[Place] | None = None) -> dict:
    """Everything the app needs to preview and navigate a route."""
    def name(place: Place) -> str:
        return bmap.assets[place.asset_id]["name"] if place.asset_id else bmap.spaces[place.space_id]["name"]

    seconds = result.cost_m / WALKING_SPEED_M_S
    return {
        "points": result.points,
        "instructions": result.instructions,
        "walk_m": result.walk_m,
        "seconds": round(seconds),
        "minutes": max(1, round(seconds / 60)),
        "from": {"id": begin.asset_id or begin.space_id, "name": name(begin)},
        "to": {"id": goal.asset_id or goal.space_id, "name": name(goal)},
        "stops": [{"id": p.asset_id or p.space_id, "name": name(p)} for p in via or []],
        "mode": mode,
        "length_m": result.walk_m,
    }


class GuideSession:
    """One conversation about one building. The building copy is per session, so edits stay local."""

    def __init__(self, building_map: BuildingMap, client: anthropic.Anthropic) -> None:
        self.map = building_map
        self.save_path = None  # set for imported buildings: corrections are written back
        self.router = WalkRouter(building_map)
        self.client = client
        self.messages: list[dict] = []
        self.view: dict = {}
        self.tools = self.make_tools()
        self.system = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT.format(overview=as_json(building_map.overview())),
                "cache_control": {"type": "ephemeral"},
            }
        ]

    # ---------- conversation ----------

    def ask(self, user_text: str) -> tuple[str, dict]:
        self.view = {"highlight_spaces": [], "highlight_assets": [], "highlight_openings": [], "route": None}
        self.messages.append({"role": "user", "content": user_text})
        runner = self.client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            max_iterations=MAX_TOOL_ITERATIONS,
            system=self.system,
            tools=self.tools,
            messages=list(self.messages),
            output_config={"effort": EFFORT},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        final = None
        for message in runner:
            final = message
            self.messages.append({"role": "assistant", "content": message.content})
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                self.messages.append(tool_response)
        if final is None:
            return "(no response)", self.view
        if final.stop_reason == "refusal":
            return "Sorry, I can't help with that request.", self.view
        text = "\n".join(block.text for block in final.content if block.type == "text").strip()
        return text, self.view

    # ---------- helpers ----------

    def resolve_asset(self, reference: str) -> str:
        if reference in self.map.assets:
            return reference
        lowered = reference.strip().lower()
        exact = [a["id"] for a in self.map.assets.values() if a["name"].lower() == lowered]
        if len(exact) == 1:
            return exact[0]
        found = [r for r in self.map.search(reference) if r["kind"] == "asset"]
        if len(found) == 1:
            return found[0]["id"]
        if not found:
            raise ToolError(f"No machine or item matches '{reference}'.")
        raise ToolError("Ambiguous, use one of: " + "; ".join(f"{r['name']} ({r['in']})" for r in found[:MAX_RESULTS]))

    def place(self, reference: str) -> Place:
        """A machine or item ("the CNC mill H-A1-B") becomes a point next to it; a room becomes its centre."""
        lowered = reference.strip().lower()
        asset_ids = [reference] if reference in self.map.assets else [
            a["id"] for a in self.map.assets.values() if a["name"].lower() == lowered
        ]
        if len(asset_ids) == 1:
            return self.router.place_for_asset(asset_ids[0])
        try:
            return self.router.place_for_space(self.map.resolve_space(reference))
        except MapError as error:
            found = [r for r in self.map.search(reference) if r["kind"] == "asset"]
            if len(found) == 1:
                return self.router.place_for_asset(found[0]["id"])
            raise ToolError(str(error)) from error

    def persist(self) -> None:
        if self.save_path is not None:
            self.map.save(self.save_path)

    # ---------- tools ----------

    def make_tools(self) -> list:
        bmap, session = self.map, self

        @beta_tool
        def search_building(query: str, floor_level: int | None = None) -> str:
            """Find rooms, halls and things (machines, equipment, furniture) by name, room number or type.
            Matches are highlighted on the map.

            Args:
                query: Words to look for, e.g. "cnc mill", "press", "loading dock", "0.21", "toilet".
                floor_level: Only this floor (0 = ground floor). Omit to search all floors.
            """
            results = bmap.search(query, floor_level)
            if not results:
                raise ToolError(f"Nothing matches '{query}'. Try a shorter or more general query.")
            shown = results[:MAX_RESULTS]
            session.view["highlight_spaces"] += [r["id"] for r in shown if r["kind"] == "space"]
            session.view["highlight_assets"] += [r["id"] for r in shown if r["kind"] == "asset"]
            return as_json({"matches": len(results), "results": shown})

        @beta_tool
        def find_route(
            start: str,
            destination: str,
            step_free: bool = False,
            via: list[str] | None = None,
            min_width_m: float | None = None,
        ) -> str:
            """Walking route around walls and machines, with turn-by-turn directions, landmarks and metres. Drawn on
            the map. Pass a machine's exact name to route to (or from) the machine itself rather than its room.

            Args:
                start: Exact machine/item name, room number/name, or id of where the person is.
                destination: Exact machine/item name, room number/name, or id of the goal.
                step_free: True to avoid stairs and steps (wheelchair, trolley, forklift, pallet).
                via: Places to visit in order on the way (multi-stop tour).
                min_width_m: Only use doors at least this wide (wide loads).
            """
            begin, goal = session.place(start), session.place(destination)
            stops = [session.place(stop) for stop in via or []]
            try:
                result = session.router.route(begin, goal, step_free, min_width_m, stops)
            except ValueError as error:
                raise ToolError(str(error)) from error
            mode = "cart" if min_width_m else ("accessible" if step_free else "walk")
            session.view["route"] = route_payload(bmap, result, begin, goal, mode, stops)
            session.view["highlight_spaces"] += [begin.space_id, goal.space_id]
            session.view["highlight_assets"] += [p.asset_id for p in (begin, goal, *stops) if p.asset_id]
            return as_json({"walking_m": result.walk_m, "takes": duration(result.cost_m),
                            "step_free": step_free, "steps": result.steps, "closed": sorted(bmap.closed)})

        @beta_tool
        def find_nearest(start: str, what: str, step_free: bool = False, floor_level: int | None = None,
                         min_width_m: float | None = None) -> str:
            """Closest machine, item or room matching `what` by walking distance (e.g. "cnc mill", "toilet",
            "kitchen", "meeting room"), with the route to it and its directions. Use it whenever any one of
            several will do.

            Args:
                start: Exact machine/item name, room number/name, or id of where the person is.
                what: Search words for the kind of thing wanted.
                step_free: True to avoid stairs and steps.
                floor_level: Only consider this floor (0 = ground floor), e.g. "a meeting room on floor 2".
                min_width_m: Only doors at least this wide (wide loads).
            """
            begin = session.place(start)
            matches = bmap.search(what, floor_level)
            candidates = matches[:MAX_CANDIDATES]
            if not candidates:
                raise ToolError(f"Nothing matches '{what}'.")
            best = None
            for candidate in candidates:
                goal = (session.router.place_for_asset(candidate["id"]) if candidate["kind"] == "asset"
                        else session.router.place_for_space(candidate["id"]))
                try:
                    result = session.router.route(begin, goal, step_free, min_width_m)
                except ValueError:
                    continue
                if best is None or result.cost_m < best[1].cost_m:
                    best = (candidate, result, goal)
            if best is None:
                raise ToolError(f"No reachable '{what}' from there with these constraints.")
            candidate, result, goal = best
            session.view["route"] = route_payload(bmap, result, begin, goal, "accessible" if step_free else "walk")
            session.view["highlight_spaces"] += [begin.space_id, goal.space_id]
            session.view["highlight_assets"] += [p.asset_id for p in (begin, goal) if p.asset_id]
            return as_json({"nearest": candidate["name"], "matches_in_building": len(matches),
                            "walking_m": result.walk_m, "takes": duration(result.cost_m),
                            "step_free": step_free, "steps": result.steps})

        @beta_tool
        def room_info(room: str) -> str:
            """Facts about one room or hall: type, floor, area in m², door widths, neighbours and what is inside.

            Args:
                room: Space id or exact room number/name.
            """
            try:
                space_id = bmap.resolve_space(room)
            except MapError as error:
                raise ToolError(str(error)) from error
            space = bmap.spaces[space_id]
            inside: dict[str, int] = {}
            for asset in bmap.assets.values():
                if asset["space_id"] == space_id:
                    inside[asset["type"]] = inside.get(asset["type"], 0) + 1
            doors = [
                {
                    "to": "outside" if (other := bmap.other_side(o, space_id)) == OUTSIDE else bmap.spaces[other]["name"],
                    "width_m": o["width_m"],
                    "step_free": o["step_free"],
                    "closed": o["id"] in bmap.closed,
                }
                for o in bmap.openings.values()
                if space_id in o["space_ids"]
            ]
            session.view["highlight_spaces"].append(space_id)
            return as_json(
                {
                    "name": space["name"],
                    "type": space["room_type"],
                    "floor": bmap.floor_name(space["floor_id"]),
                    "area_m2": round(queries.area(bmap, space_id), 1),
                    "doors": doors,
                    "contents": inside,
                    "shares_walls_with": queries.shared_walls_count(bmap, space_id)["rooms"],
                }
            )

        @beta_tool
        def set_closed(element: str, closed: bool) -> str:
            """Close or reopen a lift, staircase, door or room (maintenance, blocked aisle). Affects later routes.

            Args:
                element: Name or id of a lift/staircase ("Office lift"), a door id, or a room/hall.
                closed: True to close, False to reopen.
            """
            try:
                element_id = bmap.set_closed(element, closed)
            except MapError as error:
                raise ToolError(str(error)) from error
            if element_id in bmap.spaces:
                session.view["highlight_spaces"].append(element_id)
            return as_json({"element_id": element_id, "closed": closed, "all_closed": sorted(bmap.closed)})

        @beta_tool
        def add_door(first_space: str, second_space: str, step_free: bool = True) -> str:
            """Add a door in the middle of the wall two neighbouring rooms on the same floor share.

            Args:
                first_space: Space id or exact room number/name.
                second_space: Space id or exact room number/name.
                step_free: Whether the new door has no step.
            """
            try:
                opening = bmap.add_door(first_space, second_space, step_free)
            except MapError as error:
                raise ToolError(str(error)) from error
            session.router.invalidate(opening["space_ids"])
            session.persist()
            session.view["highlight_openings"].append(opening["id"])
            return as_json({"added_door_between": [bmap.spaces[s]["name"] for s in opening["space_ids"]]})

        @beta_tool
        def move_asset(asset: str, to_room: str) -> str:
            """Record that a machine or item now stands in another room or hall ("the press is now in hall 0.12").

            Args:
                asset: Asset id or exact name, e.g. "Hydraulic press 0.14-A".
                to_room: Space id or exact room number/name it was moved to.
            """
            asset_id = session.resolve_asset(asset)
            try:
                space_id = bmap.resolve_space(to_room)
            except MapError as error:
                raise ToolError(str(error)) from error
            x, y = bmap.anchor(space_id)
            item = bmap.assets[asset_id]
            was = bmap.describe_space(item["space_id"])
            session.router.invalidate([item["space_id"], space_id])
            item.update(space_id=space_id, floor_id=bmap.spaces[space_id]["floor_id"], position=[x, y])
            session.view["highlight_assets"].append(asset_id)
            return as_json({"moved": item["name"], "was_in": was, "now_in": bmap.describe_space(space_id)})

        @beta_tool
        def rename_space(room: str, name: str, room_type: str | None = None) -> str:
            """Correct a room's name or kind, e.g. after importing plans: "0.17 is the kitchen". Keeps its number.

            Args:
                room: Space id or exact room number/name.
                name: New display name, e.g. "Kitchen 0.17" (include the room number if it has one).
                room_type: Optional kind, e.g. kitchen, office, toilet, meeting_room, production_zone, warehouse_zone.
            """
            try:
                space_id = bmap.resolve_space(room)
            except MapError as error:
                raise ToolError(str(error)) from error
            space = bmap.spaces[space_id]
            old = space["name"]
            space["name"] = name
            if room_type:
                space["room_type"] = room_type.strip().lower().replace(" ", "_")
            session.persist()
            session.view["highlight_spaces"].append(space_id)
            return as_json({"renamed": old, "to": name, "room_type": space["room_type"], "saved": session.save_path is not None})

        return [search_building, find_route, find_nearest, room_info, set_closed, add_door, move_asset, rename_space]
