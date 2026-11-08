"""Deterministic answers to the benchmark question kinds. Each function takes a BuildingMap plus the
question's parameters (resolved ids) and returns a dict with the result; `reply` turns it into text that
ends with the scored 'Answer:' line. Definitions follow section 4 of the kit README."""

import copy
import math

import networkx as nx
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.ops import linemerge

from core.building_map import DOOR_PREFIX, OUTSIDE, BuildingMap, MapError

WALL_TOLERANCE_M = 0.35
MIN_SHARED_WALL_M = 1.0
NOT_ROOMS = {"corridor", "stair", "lift", "outdoor"}
# Delivery and reachability counts also skip halls (lobbies, production and logistics zones).
NOT_DELIVERY_ROOMS = NOT_ROOMS | {"hall"}
NOT_ROOM_TYPES = {"corridor", "stairwell", "lift"}
SHORTER_EGRESS_MARGIN_M = 1.0


def polygon(bmap: BuildingMap, space_id: str) -> Polygon:
    shape = Polygon(bmap.spaces[space_id]["polygon"])
    return shape if shape.is_valid else shape.buffer(0)


def area(bmap: BuildingMap, space_id: str) -> float:
    return polygon(bmap, space_id).area


def is_room(space: dict) -> bool:
    return space["type"] not in NOT_ROOMS and space["room_type"] not in NOT_ROOM_TYPES


def spaces_on(bmap: BuildingMap, floor_id: str, room_type: str | None = None) -> list[str]:
    return [
        space["id"]
        for space in bmap.spaces.values()
        if space["floor_id"] == floor_id and (room_type is None or space["room_type"] == room_type)
    ]


def rooms_on(bmap: BuildingMap, floor_id: str) -> list[str]:
    return [space_id for space_id in spaces_on(bmap, floor_id) if is_room(bmap.spaces[space_id])]


def distances_from(graph: nx.MultiGraph, source: str) -> dict[str, float]:
    if source not in graph:
        return {}
    return nx.single_source_dijkstra_path_length(graph, source, weight="weight")


# ---------- room geometry ----------


def largest_room(bmap: BuildingMap, floor_id: str, room_type: str) -> dict:
    candidates = spaces_on(bmap, floor_id, room_type)
    best = max(candidates, key=lambda space_id: area(bmap, space_id))
    space = bmap.spaces[best]
    return {"space_id": best, "name": space["name"], "ref": space.get("ref"), "area_m2": round(area(bmap, best), 2)}


def floor_area_total(bmap: BuildingMap, floor_id: str, room_type: str) -> dict:
    candidates = spaces_on(bmap, floor_id, room_type)
    return {"count": len(candidates), "value": round(sum(area(bmap, space_id) for space_id in candidates), 2)}


def shared_wall_length(first: Polygon, second: Polygon) -> float:
    one_way = first.exterior.intersection(second.exterior.buffer(WALL_TOLERANCE_M)).length
    other_way = second.exterior.intersection(first.exterior.buffer(WALL_TOLERANCE_M)).length
    return max(one_way, other_way)


def shared_walls_count(bmap: BuildingMap, start: str) -> dict:
    shape = polygon(bmap, start)
    neighbours = [
        space_id
        for space_id in rooms_on(bmap, bmap.spaces[start]["floor_id"])
        if space_id != start and shared_wall_length(shape, polygon(bmap, space_id)) >= MIN_SHARED_WALL_M
    ]
    return {"value": len(neighbours), "rooms": [bmap.spaces[space_id]["name"] for space_id in neighbours]}


def room_stacked(bmap: BuildingMap, start: str, direction: str) -> dict:
    floor = bmap.floors[bmap.spaces[start]["floor_id"]]
    others = [f for f in bmap.floors.values() if (f["level"] > floor["level"]) == (direction == "above")]
    others = [f for f in others if f["level"] != floor["level"]]
    if not others:
        raise ValueError(f"No floor {direction} {floor['name']}")
    target = min(others, key=lambda f: abs(f["level"] - floor["level"]))
    shape = polygon(bmap, start)
    best = max(spaces_on(bmap, target["id"]), key=lambda space_id: polygon(bmap, space_id).intersection(shape).area)
    return {"space_id": best, "name": bmap.spaces[best]["name"], "ref": bmap.spaces[best].get("ref")}


def straight_line_3d(bmap: BuildingMap, first: str, second: str) -> dict:
    points = []
    for space_id in (first, second):
        centre = polygon(bmap, space_id).centroid
        points.append((centre.x, centre.y, bmap.floors[bmap.spaces[space_id]["floor_id"]]["elevation_m"]))
    return {"value": round(math.dist(*points), 2)}


# ---------- reachability and routes ----------


def reachable_rooms_count(
    bmap: BuildingMap,
    start: str,
    floor_id: str,
    step_free: bool = True,
    min_width: float | None = None,
    closed: str | list[str] | None = None,
) -> dict:
    closed_ids = {closed} if isinstance(closed, str) else set(closed or [])
    graph = bmap.graph(step_free, min_width=min_width, closed=closed_ids)
    reached = distances_from(graph, start)
    rooms = [
        space_id
        for space_id in rooms_on(bmap, floor_id)
        if space_id in reached and bmap.spaces[space_id]["type"] not in NOT_DELIVERY_ROOMS
    ]
    return {"value": len(rooms)}


def errand(bmap: BuildingMap, start: str, asset_type: str, floor_id: str, room_type: str) -> dict:
    graph = bmap.graph(step_free=False)
    from_start = distances_from(graph, start)
    holders = {asset["space_id"] for asset in bmap.assets.values() if asset["type"] == asset_type}
    holders = [space_id for space_id in holders if space_id in from_start]
    via = min(holders, key=lambda space_id: from_start[space_id])
    target = largest_room(bmap, floor_id, room_type)["space_id"]
    second_leg = nx.dijkstra_path_length(graph, via, target, weight="weight")
    return {"value": round(from_start[via] + second_leg, 2), "via": via, "target": target}


def wall_midpoint(first: Polygon, second: Polygon) -> Point:
    shared = first.exterior.intersection(second.exterior.buffer(WALL_TOLERANCE_M))
    if isinstance(shared, MultiLineString):
        shared = linemerge(shared)
    if not isinstance(shared, LineString):
        shared = max(getattr(shared, "geoms", [shared]), key=lambda part: part.length)
    middle = shared.interpolate(0.5, normalized=True)
    # Put the door halfway between the two outlines (walls have thickness).
    return Point((middle.x + second.exterior.interpolate(second.exterior.project(middle)).x) / 2,
                 (middle.y + second.exterior.interpolate(second.exterior.project(middle)).y) / 2)


def egress_distances(bmap: BuildingMap) -> dict[str, float]:
    """Route-model distance from every node to its nearest entrance. Entrances lead out of the building only
    (kit 1.1.0), so 'walking out' means reaching an entrance door."""
    graph = bmap.graph(step_free=False)
    exits = [DOOR_PREFIX + o["id"] for o in bmap.openings.values() if OUTSIDE in o["space_ids"]]
    exits = [node for node in exits if node in graph]
    return nx.multi_source_dijkstra_path_length(graph, exits, weight="weight") if exits else {}


def what_if_door(bmap: BuildingMap, first: str, second: str, door_floor_id: str, floor_id: str) -> dict:
    position = wall_midpoint(polygon(bmap, first), polygon(bmap, second))
    changed = BuildingMap(copy.deepcopy(bmap.data))
    changed.walks = {k: v for k, v in bmap.walks.items() if k not in (first, second)}  # reuse unaffected walks
    changed.add_door(first, second, step_free=True, position=(position.x, position.y))
    before, after = egress_distances(bmap), egress_distances(changed)
    improved = [
        space_id
        for space_id in rooms_on(bmap, floor_id)
        if space_id in after and after[space_id] < before.get(space_id, math.inf) - SHORTER_EGRESS_MARGIN_M
    ]
    return {"value": len(improved)}


# ---------- routes, tours, locate, count (the warm-up kinds; the Sunday building is about routes) ----------


def route(bmap: BuildingMap, start: str, goal: str, step_free: bool = False, closed: str | None = None,
          via: list[str] | None = None) -> dict:
    """Shortest route under the judges' model (kit 1.1.0), or reachable False. A tour is a route with stops."""
    previous = set(bmap.closed)
    if closed:
        bmap.closed.add(closed)
    try:
        result = bmap.route(start, goal, step_free, via)
    except MapError:
        return {"reachable": False, "start": bmap.spaces[start]["name"], "goal": bmap.spaces[goal]["name"],
                "step_free": step_free, "closed": closed and closed_name(bmap, closed)}
    finally:
        bmap.closed = previous
    return {"reachable": True, "value": result.length_m, "path": [bmap.spaces[s]["name"] for s in result.space_path],
            "step_free": step_free, "closed": closed and closed_name(bmap, closed), "steps": result.steps}


def closed_name(bmap: BuildingMap, element_id: str) -> str:
    for group in (bmap.connectors, bmap.spaces):
        if element_id in group:
            return group[element_id]["name"]
    return element_id


def tour(bmap: BuildingMap, stops: list[str], step_free: bool = False) -> dict:
    return route(bmap, stops[0], stops[-1], step_free, via=stops[1:-1])


def locate(bmap: BuildingMap, asset_type: str, floor_id: str | None = None) -> dict:
    """Where an item is: its room and floor. Several of the type: all of them, the first named in the answer."""
    found = [a for a in bmap.assets.values() if a["type"] == asset_type and (floor_id is None or a["floor_id"] == floor_id)]
    if not found:
        return {"found": False, "asset_type": asset_type}
    rooms = [(bmap.spaces[a["space_id"]]["name"], bmap.floor_name(a["floor_id"]), a["name"]) for a in found]
    return {"found": True, "space_id": found[0]["space_id"], "asset_id": found[0]["id"], "rooms": rooms}


def count(bmap: BuildingMap, asset_type: str, floor_id: str) -> dict:
    return {"value": sum(a["type"] == asset_type and a["floor_id"] == floor_id for a in bmap.assets.values())}


# ---------- dispatch and replies ----------

QUERIES = {
    "largest_room": largest_room,
    "floor_area_total": floor_area_total,
    "shared_walls_count": shared_walls_count,
    "room_stacked": room_stacked,
    "straight_line_3d": straight_line_3d,
    "wide_delivery_count": lambda bmap, **kw: reachable_rooms_count(bmap, step_free=True, **kw),
    "step_free_reachable_count": lambda bmap, **kw: reachable_rooms_count(bmap, step_free=True, **kw),
    "errand": errand,
    "what_if_door": what_if_door,
    "route": route,
    "route_step_free": lambda bmap, **kw: route(bmap, step_free=True, **kw),
    "route_with_closure": route,
    "tour": tour,
    "locate": locate,
    "count": count,
}
ROUTE_KINDS = ("route", "route_step_free", "route_with_closure", "tour")


def run(bmap: BuildingMap, kind: str, params: dict) -> dict:
    return QUERIES[kind](bmap, **params)


def route_reply(result: dict) -> str:
    constraint = " step-free" if result.get("step_free") else ""
    closure = f" with {result['closed']} closed" if result.get("closed") else ""
    if not result["reachable"]:
        return (f"There is no{constraint} route from {result['start']} to {result['goal']}{closure}.\n"
                "Answer: no route")
    steps = "\n".join(f"{i}. {step}" for i, step in enumerate(result["steps"], 1))
    return (f"The shortest{constraint} route{closure} is {result['value']} m, through "
            f"{' → '.join(result['path'])}.\n{steps}\nAnswer: {result['value']} m")


def reply(kind: str, result: dict) -> str:
    if kind in ROUTE_KINDS:
        return route_reply(result)
    if kind == "locate":
        if not result["found"]:
            return f"There is no {result['asset_type'].replace('_', ' ')} in this building.\nAnswer: not found"
        room, floor, name = result["rooms"][0]
        others = f" There are {len(result['rooms'])} in total; the others are in " + ", ".join(
            f"{r} ({f})" for r, f, _ in result["rooms"][1:6]) + "." if len(result["rooms"]) > 1 else ""
        return f"The {name} is in {room} on the {floor}.{others}\nAnswer: {room} ({floor})"
    # Names repeat (e.g. several "Büro"), refs don't: lead with the ref.
    label = f"{result['ref']} ({result['name']})" if result.get("ref") else result.get("name")
    if kind == "largest_room":
        return f"The largest room is {label} at {result['area_m2']} m².\nAnswer: {label}, {result['area_m2']} m²"
    if kind == "room_stacked":
        return f"That is {label}.\nAnswer: {label}"
    return f"Answer: {result['value']}"
