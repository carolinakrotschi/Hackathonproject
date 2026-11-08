"""The staff pager's conversation layer: Claude understands what was said and calls hospital/pager_tools, which
supply every fact (rooms, routes, distances, what is free or closed). One conversation per pager.

Without Anthropic credentials, or when the API fails, the pager falls back to the phrase rules in hospital/voice.py,
so the demo never goes silent."""

import json
import os
from collections.abc import Callable

import anthropic
from anthropic import beta_tool
from anthropic.lib.tools import ToolError

from hospital import pager_tools as tools
from hospital.pager_tools import PagerError
from hospital.state import Hospital

MODEL = os.environ.get("PAGER_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("PAGER_EFFORT", "low")  # spoken answers on a pager: fast beats thorough
MAX_TOKENS = 4000
MAX_TOOL_ITERATIONS = 8
MAX_TURNS = 20  # then the conversation starts fresh (history is never edited, only dropped whole)
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """You are the voice of a staff pager in a hospital. Doctors, nurses, porters and technicians talk to \
you while walking, often with their hands full. You find rooms and equipment, guide them there, and record changes \
to the building. Every fact comes from your tools; never guess a room, route, distance or what is free.

How to act
- Act, don't ask, when a sensible default exists: "the WC", "a wheelchair", "the CT" means the nearest one.
- Navigation: call go_to (or find_free for equipment). It starts the pager's turn-by-turn guidance. "From X to Y" passes start=X: the pager is moved to X first.
- Constraints: a bed, trolley, stretcher, wheelchair, "no stairs" or bringing a patient means step_free=true for that trip. Errands \
with stops ("coffee, then the pharmacy, then theatre 1") are go_to with via in the order given. "Don't use lift A, \
it's crowded" is avoid=["Lift A"] for this trip; "take lift C" avoids the other lifts and stairs. "The next theatre \
with a free operating table" is place="free operating table".
- Changes people report ("lift A is out of order", "the bridge is closed", "add a door between A0.12 and A0.13", \
"A0.05 is the plaster room", "the ultrasound is now in ED Bay 2"): call the tool, then say what changes.
- Ask one short question only when a wrong guess would send someone the wrong way.
- You do not triage, diagnose or choose destinations for patients: staff decide where patients go and how urgent \
it is; you only find the way.

How to answer (it is read aloud and shown on a small screen)
- One or two short sentences. First what you understood ("To CT, step-free:"), then the result.
- Use the tool's `say` text when it fits; don't add numbers or places the tools didn't give you.
- Room names as on the signs ("ED Resus", "Theatre 1", "Room A2.07"), never internal ids.

Hospital overview:
{overview}"""


MAPS_PROMPT = """You are the assistant on a building map website, like Google Maps for the inside of this hospital. \
Facility managers, staff and visitors type or say what they need; the map next to you shows your results. You find \
rooms and equipment, give directions between any two places, and keep the building data up to date by conversation. \
Every fact comes from your tools; never guess a room, route, distance or what is free.

How to act
- Locate: "where is X" -> find; it is highlighted on the map.
- Directions: "from A to B", "how do I get to B" -> directions. Without a start, use what the user selected on the \
map or ask where they are (the only question worth asking). A bed, trolley, wheelchair, pallet, "no stairs" or \
bringing a patient means step_free=true. Errands with stops are via, in the order given.
- Equipment: "a free wheelchair near the main entrance" -> find_free with near.
- Changes: closures ("lift A is out of order", "reopen stairs B"), new doors, room names after a plan upload \
("0.05 is the CT"), moved equipment: call the tool, then say what changes. A closure changes every route.
- Floors and buildings, lifts and stairs, what is closed: building_overview.

How to answer (shown next to the map)
- Short: first what you understood, then the result. Directions: duration, distance, lift or stairs; the map and \
the step list show the rest, so don't repeat every turn.
- Room names as on the signs, never internal ids. Don't add numbers or places the tools didn't give you.

Hospital overview:
{overview}"""


def available() -> bool:
    """Anthropic credentials in the environment (an `ant auth login` profile also works if set via env)."""
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


class PagerAgent:
    """One pager's conversation. `ask` runs Claude with tools that act on the shared hospital under its lock."""

    def __init__(self, h: Hospital, pager_id: str, client: anthropic.Anthropic) -> None:
        self.h = h
        self.pager_id = pager_id
        self.client = client
        self.messages: list[dict] = []
        self.turns = 0
        self.view: dict = tools.empty_view()
        self.tools = self.make_tools()
        overview = tools.overview(h)
        overview.pop("view")
        self.system = [{"type": "text", "text": SYSTEM_PROMPT.format(overview=json.dumps(overview, ensure_ascii=False)),
                        "cache_control": {"type": "ephemeral"}}]

    def context(self) -> str:
        """Who is asking and where they are, sent with each message (not in the cached system prompt)."""
        with self.h.lock:
            holder = self.h.patient_by_pager(self.pager_id)
            here = self.h.place_of(holder)
            where = f"{self.h.bmap.spaces[here.space_id]['name']}, {tools.where_text(self.h, here.space_id)}"
            goal = (holder.search or {}).get("name") or (holder.destination or {}).get("name")
        role = f", {holder.role}" if holder.role else ""
        return f"[Pager {self.pager_id}: {holder.name}{role}. Now in {where}." + (f" Heading to {goal}." if goal else "") + "]"

    def ask(self, text: str) -> tuple[str, dict]:
        if self.turns >= MAX_TURNS:
            self.messages, self.turns = [], 0
        self.turns += 1
        self.view = tools.empty_view()
        self.navigated = False
        self.acted = False  # a tool ran: the request was understood (a reply without one asks back or declines)
        self.messages.append({"role": "user", "content": f"{self.context()}\n{text}"})
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
            raise RuntimeError("no response")
        if final.stop_reason == "refusal":
            return "Sorry, I can only help with finding your way and the building.", self.view
        reply = " ".join(block.text for block in final.content if block.type == "text").strip()
        # A new route is what the map should follow; don't fly to whatever else the tools touched (a closed lift).
        return reply or "Done.", tools.empty_view() if self.navigated else self.view

    # ---------- tools ----------

    def run(self, action: Callable[[], dict]) -> str:
        """Run a pager tool under the hospital lock; remember what to show; errors become what Claude reads."""
        try:
            with self.h.lock:
                result = action()
        except PagerError as error:
            raise ToolError(str(error)) from error
        self.acted = True
        for key in ("spaces", "assets"):
            self.view[key] += [i for i in result.get("view", {}).get(key, []) if i not in self.view[key]]
        return json.dumps({k: v for k, v in result.items() if k != "view"}, ensure_ascii=False)

    def make_tools(self) -> list:
        h, pager, run = self.h, self.pager_id, self.run

        @beta_tool
        def find(query: str, floor_level: int | None = None) -> str:
            """Where a room, department or item is (nearest first), shown on the pager map. Does not navigate.

            Args:
                query: What to look for as said, e.g. "CT", "theatre 1", "A0.12", "defibrillator", "MRI scanner".
                floor_level: Only this floor (0 = ground floor), e.g. "a WC on floor 2".
            """
            return run(lambda: tools.find(h, pager, query, floor_level))

        @beta_tool
        def go_to(place: str, step_free: bool = False, via: list[str] | None = None, avoid: list[str] | None = None,
                  urgent: bool = False, start: str | None = None) -> str:
            """Start turn-by-turn guidance to a room, department, room number or named item, across floors and buildings.

            Args:
                place: The goal as said, e.g. "Theatre 1", "the nearest WC", "ED Resus", "B0.10", or a free item
                    ("free operating table") when the goal is wherever one is free.
                step_free: True for a bed, trolley, stretcher, wheelchair or "no stairs": lifts only.
                via: Stops to make on the way, in order, e.g. ["Cafeteria", "Pharmacy", "free wheelchair", "coffee"].
                    A free item is the one that costs the least detour.
                avoid: Lifts, stairs, links or rooms to keep away from on this trip only, e.g. ["Lift A"] for "lift A
                    is crowded", ["Bridge A–B"]. Use set_closed instead when something is broken or closed for all.
                urgent: The staff member said it is urgent (critical patient, code blue ...).
                start: Only when the user names where the trip starts ("from the main entrance to CT"): the pager is
                    moved there and guides from it. Omit to start where the pager is.
            """
            self.navigated = True
            return run(lambda: tools.go_to(h, pager, place, step_free, via, avoid, urgent, start))

        @beta_tool
        def avoid_on_route(elements: list[str]) -> str:
            """Keep the current trip away from these lifts, stairs or links from now on ("lift B is busy").

            Args:
                elements: Names as on the signs, e.g. ["Lift B"].
            """
            return run(lambda: tools.avoid_on_route(h, pager, elements))

        @beta_tool
        def find_free(what: str) -> str:
            """Guide to the nearest FREE item of a kind by walking distance (wheelchair, bed, pump, defibrillator ...).

            Args:
                what: The kind of item, e.g. "wheelchair".
            """
            return run(lambda: tools.find_free(h, pager, what))

        @beta_tool
        def cancel() -> str:
            """Stop the current guidance, search and stops."""
            return run(lambda: tools.cancel(h, pager))

        @beta_tool
        def where_am_i() -> str:
            """The room, floor and building the pager is in, with landmarks nearby."""
            return run(lambda: tools.where_am_i(h, pager))

        @beta_tool
        def set_closed(element: str, closed: bool) -> str:
            """Close or reopen a lift, staircase, room, corridor or the walkway/bridge; routes avoid closed parts.

            Args:
                element: Name as said, e.g. "Lift A", "Stairs B", "Bridge A–B", "ED Resus".
                closed: True to close, False to reopen.
            """
            return run(lambda: tools.set_closed(h, pager, element, closed))

        @beta_tool
        def add_door(first_room: str, second_room: str) -> str:
            """Add a door in the wall two neighbouring rooms on one floor share.

            Args:
                first_room: Room name or number.
                second_room: Room name or number.
            """
            return run(lambda: tools.add_door(h, first_room, second_room))

        @beta_tool
        def label_room(room: str, name: str, room_type: str | None = None) -> str:
            """Name or rename a room as its sign reads, e.g. after a floor plan was uploaded: "0.05 is the CT".

            Args:
                room: The room's current name or number.
                name: The sign name, e.g. "CT", "Dirty utility 2A", "ED Bay 3".
                room_type: Optional kind (radiology, toilet, office, storage ...); otherwise taken from the name.
            """
            return run(lambda: tools.label_room(h, room, name, room_type))

        @beta_tool
        def move_equipment(item: str, to_room: str) -> str:
            """Record that a movable item is now in another room.

            Args:
                item: The item's name, e.g. "Ultrasound unit", "Wheelchair 03".
                to_room: Room name or number it is now in.
            """
            return run(lambda: tools.move_equipment(h, item, to_room))

        @beta_tool
        def building_overview() -> str:
            """Buildings, floors, lifts and stairs, links between buildings, and what is closed."""
            return run(lambda: tools.overview(h))

        return [find, go_to, avoid_on_route, find_free, cancel, where_am_i, set_closed, add_door, label_room,
                move_equipment, building_overview]


class MapsAgent(PagerAgent):
    """The maps page's conversation: levels 1-4 without a pager. Directions are drawn on the page, not guided;
    building edits tell the page to reload the plan."""

    def __init__(self, h: Hospital, client: anthropic.Anthropic) -> None:
        super().__init__(h, "", client)
        overview = tools.overview(h)
        overview.pop("view")
        self.system = [{"type": "text", "text": MAPS_PROMPT.format(overview=json.dumps(overview, ensure_ascii=False)),
                        "cache_control": {"type": "ephemeral"}}]
        self.page = ""

    def context(self) -> str:
        return f"[Maps page. {self.page}]" if self.page else "[Maps page.]"

    def ask(self, text: str) -> tuple[str, dict]:
        self.route, self.changed = None, False
        return super().ask(text)

    def run(self, action: Callable[[], dict], edits: bool = False) -> str:
        """Like the pager's, but a route goes to the page (Claude reads only the steps) and edits mark the plan changed."""
        try:
            with self.h.lock:
                result = action()
        except PagerError as error:
            raise ToolError(str(error)) from error
        self.acted = True
        self.changed |= edits
        for key in ("spaces", "assets"):
            self.view[key] += [i for i in result.get("view", {}).get(key, []) if i not in self.view[key]]
        shown = {k: v for k, v in result.items() if k not in ("view", "route")}
        if result.get("route"):
            self.route = result
            shown["steps"] = [step["text"] for step in result["route"]["instructions"]]
        return json.dumps(shown, ensure_ascii=False)

    def make_tools(self) -> list:
        h, run = self.h, self.run

        @beta_tool
        def find(query: str, floor_level: int | None = None) -> str:
            """Where a room, department or item is, highlighted on the map.

            Args:
                query: What to look for as said, e.g. "CT", "Room 2.14", "defibrillator", "pharmacy".
                floor_level: Only this floor (0 = ground floor), e.g. "a WC on floor 2".
            """
            return run(lambda: tools.find(h, None, query, floor_level))

        @beta_tool
        def directions(origin: str, destination: str, step_free: bool = False, via: list[str] | None = None) -> str:
            """The shortest walk from A to B (across floors and buildings), drawn on the map with a step list.

            Args:
                origin: Where it starts, as said: a room, department, room number or named item.
                destination: Where it ends.
                step_free: True for a bed, trolley, wheelchair, pallet or "no stairs": lifts only.
                via: Stops on the way, in order.
            """
            return run(lambda: tools.directions(h, origin, destination, step_free, via))

        @beta_tool
        def find_free(what: str, near: str, step_free: bool = False) -> str:
            """The nearest FREE item of a kind from a place, by walking distance, with the way there.

            Args:
                what: The kind of item, e.g. "wheelchair", "bed", "infusion pump".
                near: The place to measure from, e.g. "Emergency", "Main entrance".
                step_free: True if the way there must avoid stairs.
            """
            return run(lambda: tools.free_near(h, what, near, step_free))

        @beta_tool
        def set_closed(element: str, closed: bool) -> str:
            """Close or reopen a lift, staircase, room, corridor or walkway; all routes avoid closed parts.

            Args:
                element: Name as said, e.g. "Lift A", "Stairs B", "ED Resus".
                closed: True to close, False to reopen.
            """
            return run(lambda: tools.set_closed(h, None, element, closed), edits=True)

        @beta_tool
        def add_door(first_room: str, second_room: str) -> str:
            """Add a door in the wall two neighbouring rooms on one floor share.

            Args:
                first_room: Room name or number.
                second_room: Room name or number.
            """
            return run(lambda: tools.add_door(h, first_room, second_room), edits=True)

        @beta_tool
        def label_room(room: str, name: str, room_type: str | None = None) -> str:
            """Name or rename a room as its sign reads, e.g. after a floor plan was uploaded: "0.05 is the CT".

            Args:
                room: The room's current name or number.
                name: The sign name.
                room_type: Optional kind (radiology, toilet, office, storage ...); otherwise taken from the name.
            """
            return run(lambda: tools.label_room(h, room, name, room_type), edits=True)

        @beta_tool
        def move_equipment(item: str, to_room: str) -> str:
            """Record that a movable item is now in another room.

            Args:
                item: The item's name, e.g. "Ultrasound unit", "Wheelchair 03".
                to_room: Room name or number it is now in.
            """
            return run(lambda: tools.move_equipment(h, item, to_room), edits=True)

        @beta_tool
        def building_overview() -> str:
            """Buildings, floors, lifts and stairs, links between buildings, and what is closed."""
            return run(lambda: tools.overview(h))

        return [find, directions, find_free, set_closed, add_door, label_room, move_equipment, building_overview]
