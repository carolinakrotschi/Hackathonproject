"""Walkable routes for people and the map: obstacle-aware paths inside rooms, ending at the machine itself.

The benchmark route model (building_map.py) joins room anchors and door points with straight lines, which is
what the expected answers use but cuts through walls and machines when drawn. This module keeps the same
topology (spaces, doors, stairs, lifts) and replaces each walk inside a space by the shortest path through
that space's free area: its polygon shrunk away from the walls, minus every machine or piece of furniture
(grown by a clearance). Paths come from a visibility graph over the free area's corners."""

import functools
import itertools
import math
import threading
from dataclasses import dataclass, field

import networkx as nx
from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import nearest_points, unary_union
from shapely.prepared import prep

from core.building_map import LIFT, LIFT_FIXED_M, LIFT_M_PER_RISE, OUTSIDE, STAIRS_M_PER_RISE, BuildingMap

WALL_MARGIN_M = 0.3
CLEARANCE_M = 0.4
# Crowded rooms (server racks, dining tables) leave no free area at full clearance: retry tighter.
FALLBACK_CLEARANCES_M = (0.2, 0.05)
MIN_OBSTACLE_AREA_M2 = 0.3
EPSILON = 1e-6
LANDMARK_DISTANCE_M = 3.0
LANDMARK_CATEGORIES = {"machine", "equipment"}
TURN_THRESHOLD_DEG = 35.0
MAX_LANDMARKS_PER_LEG = 2


@dataclass
class Place:
    """Where a route starts or ends: a space, a point in it, and optionally the asset the person wants."""

    space_id: str
    point: tuple[float, float]
    asset_id: str | None = None


@dataclass
class WalkRoute:
    walk_m: float
    cost_m: float
    points: list[dict]
    steps: list[str]
    spaces: list[str] = field(default_factory=list)
    instructions: list[dict] = field(default_factory=list)


def footprint(asset: dict) -> Polygon:
    size = asset.get("footprint") or {}
    width, depth = size.get("width_m") or 0.6, size.get("depth_m") or 0.6
    x, y = asset["position"]
    shape = box(x - width / 2, y - depth / 2, x + width / 2, y + depth / 2)
    return affinity.rotate(shape, size.get("rotation_deg") or 0.0, origin=(x, y))


def polygons(shape) -> list[Polygon]:
    if isinstance(shape, Polygon):
        return [shape]
    return [part for geom in getattr(shape, "geoms", []) for part in polygons(geom)]


def corners(shape) -> list[tuple[float, float]]:
    parts = polygons(shape)
    points = set()
    for part in parts:
        for ring in [part.exterior, *part.interiors]:
            points.update((round(x, 3), round(y, 3)) for x, y in ring.coords)
    return sorted(points)


class SpaceNav:
    """Visibility graph over one space's free area."""

    def __init__(self, polygon: Polygon, obstacles: list[Polygon], clearance: float) -> None:
        free = polygon.buffer(-WALL_MARGIN_M, join_style="mitre")
        if obstacles:
            free = free.difference(unary_union([o.buffer(clearance, join_style="mitre") for o in obstacles]))
        self.free = free = MultiPolygon(polygons(free))
        self.walkable = prep(free.buffer(EPSILON))
        self.graph = nx.Graph()
        vertices = corners(free)
        self.graph.add_nodes_from(vertices)
        for a, b in itertools.combinations(vertices, 2):
            if self.visible(a, b):
                self.graph.add_edge(a, b, weight=math.dist(a, b))

    def visible(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        return a == b or self.walkable.covers(LineString([a, b]))

    def inside(self, point: tuple[float, float]) -> tuple[float, float]:
        if self.walkable.covers(Point(point)):
            return point
        nearest = nearest_points(self.free, Point(point))[0]
        return (nearest.x, nearest.y)

    def path(self, a: tuple[float, float], b: tuple[float, float]) -> list[tuple[float, float]] | None:
        """Shortest path a → b; a and b may lie just outside the free area (doors, machines). None if the
        free area doesn't connect them."""
        if self.free.is_empty:
            return None
        inner_a, inner_b = self.inside(a), self.inside(b)
        if self.visible(inner_a, inner_b):
            middle = [inner_a, inner_b]
        else:
            start, end = "start", "end"
            vertices = list(self.graph.nodes)
            for node, point in ((start, inner_a), (end, inner_b)):
                self.graph.add_node(node)
                for vertex in vertices:
                    if self.visible(point, vertex):
                        self.graph.add_edge(node, vertex, weight=math.dist(point, vertex))
            try:
                nodes = nx.dijkstra_path(self.graph, start, end, weight="weight")
                middle = [inner_a, *nodes[1:-1], inner_b]
            except nx.NetworkXNoPath:
                return None
            finally:
                self.graph.remove_nodes_from([start, end])
        path = [a]
        for point in [*middle, b]:
            if math.dist(point, path[-1]) > 0.01:
                path.append(point)
        return path if len(path) > 1 else [a, b]


def locked(method):
    """The router caches per-call graph state and mutates visibility graphs, so one call at a time."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapper


def length(path: list[tuple[float, float]]) -> float:
    return sum(math.dist(p, q) for p, q in itertools.pairwise(path))


class WalkRouter:
    def __init__(self, bmap: BuildingMap) -> None:
        self.map = bmap
        self.lock = threading.RLock()
        self.navs: dict[tuple[str, float], SpaceNav] = {}
        self.pairs: dict[tuple, list[tuple[float, float]]] = {}

    # ---------- per-space geometry, cached ----------

    def obstacles(self, space_id: str) -> list[Polygon]:
        """Every footprint on the floor that reaches into this space, also from neighbouring zones."""
        space = self.map.spaces[space_id]
        outline = Polygon(space["polygon"]).buffer(CLEARANCE_M)
        shapes = (footprint(a) for a in self.map.assets.values() if a["floor_id"] == space["floor_id"])
        return [shape for shape in shapes if shape.area >= MIN_OBSTACLE_AREA_M2 and shape.intersects(outline)]

    def nav(self, space_id: str, clearance: float) -> SpaceNav:
        key = (space_id, clearance)
        if key not in self.navs:
            polygon = Polygon(self.map.spaces[space_id]["polygon"])
            polygon = polygon if polygon.is_valid else polygon.buffer(0)
            self.navs[key] = SpaceNav(polygon, self.obstacles(space_id), clearance)
        return self.navs[key]

    @locked
    def invalidate(self, space_ids: list[str]) -> None:
        """Call after moving an asset or adding a door in these spaces."""
        self.navs = {key: nav for key, nav in self.navs.items() if key[0] not in space_ids}
        self.pairs = {key: path for key, path in self.pairs.items() if key[0] not in space_ids}

    def walk_path(self, space_id: str, clearance: float, pa, pb) -> list[tuple[float, float]]:
        for tighter in (clearance, *(c for c in FALLBACK_CLEARANCES_M if c < clearance)):
            path = self.nav(space_id, tighter).path(pa, pb)
            if path:
                return path
        return [pa, pb]

    def free_nav(self, space_id: str, clearance: float) -> SpaceNav:
        for tighter in (clearance, *(c for c in FALLBACK_CLEARANCES_M if c < clearance)):
            nav = self.nav(space_id, tighter)
            if not nav.free.is_empty:
                return nav
        return nav

    def inside(self, space_id: str, clearance: float, point: tuple[float, float]) -> tuple[float, float]:
        nav = self.free_nav(space_id, clearance)
        return point if nav.free.is_empty else nav.inside(point)

    def inner_path(self, space_id: str, clearance: float, a: tuple, b: tuple, pa, pb) -> list[tuple[float, float]]:
        key = (space_id, clearance, a, b)
        if key not in self.pairs:
            self.pairs[key] = self.walk_path(space_id, clearance, pa, pb)
            self.pairs[(space_id, clearance, b, a)] = self.pairs[key][::-1]
        return self.pairs[key]

    def connector_point(self, space_id: str, clearance: float) -> tuple[float, float]:
        return self.inside(space_id, clearance, self.map.anchor(space_id))

    # ---------- places ----------

    @locked
    def place_for_asset(self, asset_id: str, clearance: float = CLEARANCE_M) -> Place:
        asset = self.map.assets[asset_id]
        nav = self.free_nav(asset["space_id"], clearance)
        if nav.free.is_empty:
            return Place(asset["space_id"], tuple(asset["position"]), asset_id)
        near = nearest_points(nav.free, footprint(asset))[0]
        return Place(asset["space_id"], (near.x, near.y), asset_id)

    @locked
    def place_for_space(self, space_id: str, clearance: float = CLEARANCE_M) -> Place:
        return Place(space_id, self.inside(space_id, clearance, self.map.anchor(space_id)))

    # ---------- routing ----------

    def graph(self, step_free: bool, min_width: float | None, clearance: float) -> nx.Graph:
        closed = self.map.closed
        graph = nx.Graph()
        doors_by_space: dict[str, list[dict]] = {}
        for opening in self.map.openings.values():
            if opening["id"] in closed or OUTSIDE in opening["space_ids"]:
                continue
            if (step_free and not opening["step_free"]) or (min_width and opening["width_m"] < min_width):
                continue
            for space_id in opening["space_ids"]:
                doors_by_space.setdefault(space_id, []).append(opening)
        connector_spaces: set[str] = set()
        levels = {floor_id: floor["level"] for floor_id, floor in self.map.floors.items()}
        elevations = {floor_id: floor["elevation_m"] for floor_id, floor in self.map.floors.items()}
        for connector in self.map.connectors.values():
            if connector["id"] in closed or (step_free and not connector["step_free"]):
                continue
            stops = sorted(zip(connector["floor_ids"], connector["space_ids"], strict=True), key=lambda s: levels[s[0]])
            # kit 1.1.0 costs: a lift ride pays its 15 m once, so every pair of stops is joined; stairs per flight
            is_lift = connector["kind"] == LIFT
            for (low_floor, low), (high_floor, high) in (itertools.combinations(stops, 2) if is_lift else itertools.pairwise(stops)):
                if low in closed or high in closed:
                    continue
                rise = abs(elevations[high_floor] - elevations[low_floor])
                cost = LIFT_FIXED_M + LIFT_M_PER_RISE * rise if is_lift else STAIRS_M_PER_RISE * rise
                a, b = ("conn", low), ("conn", high)
                if not graph.has_edge(a, b) or graph[a][b]["weight"] > cost:
                    graph.add_edge(a, b, weight=cost, walk=0.0, connector=connector["id"], space=None, path=None)
                connector_spaces.update((low, high))
        for space_id, doors in doors_by_space.items():
            if space_id in closed:
                continue
            nodes = [(("door", d["id"]), tuple(d["position"])) for d in doors]
            if space_id in connector_spaces:
                nodes.append((("conn", space_id), self.connector_point(space_id, clearance)))
            for (a, pa), (b, pb) in itertools.combinations(nodes, 2):
                self.add_walk_edge(graph, space_id, clearance, a, b, pa, pb)
        for space_id in connector_spaces - set(doors_by_space):
            graph.add_node(("conn", space_id))
        self.positions = {("conn", s): self.connector_point(s, clearance) for s in connector_spaces}
        self.positions |= {("door", o["id"]): tuple(o["position"]) for ds in doors_by_space.values() for o in ds}
        self.space_nodes = {
            s: [n for n in self.positions if (n[0] == "conn" and n[1] == s) or (n[0] == "door" and s in self.map.openings[n[1]]["space_ids"] and n in graph)]
            for s in self.map.spaces
        }
        return graph

    def add_walk_edge(self, graph: nx.Graph, space_id: str, clearance: float, a, b, pa, pb) -> None:
        path = self.inner_path(space_id, clearance, a, b, pa, pb)
        walk = length(path)
        if not graph.has_edge(a, b) or graph[a][b]["weight"] > walk:
            graph.add_edge(a, b, weight=walk, walk=walk, connector=None, space=space_id, path={a: path, b: path[::-1]})

    def attach(self, graph: nx.Graph, node, place: Place, clearance: float) -> None:
        if place.space_id in self.map.closed:
            raise ValueError(f"{self.map.spaces[place.space_id]['name']} is closed.")
        graph.add_node(node)
        for other in self.space_nodes.get(place.space_id, []):
            path = self.walk_path(place.space_id, clearance, place.point, self.positions[other])
            graph.add_edge(node, other, weight=length(path), walk=length(path), connector=None, space=place.space_id,
                           path={node: path, other: path[::-1]})

    @locked
    def route(self, start: Place, goal: Place, step_free: bool = False, min_width: float | None = None,
              via: list[Place] | None = None) -> WalkRoute:
        clearance = max(CLEARANCE_M, (min_width or 0) / 2)
        stops = [start, *(via or []), goal]
        legs = []
        for leg_start, leg_goal in itertools.pairwise(stops):
            graph = self.graph(step_free, min_width, clearance)
            self.attach(graph, "start", leg_start, clearance)
            self.attach(graph, "goal", leg_goal, clearance)
            if leg_start.space_id == leg_goal.space_id:
                path = self.walk_path(leg_start.space_id, clearance, leg_start.point, leg_goal.point)
                graph.add_edge("start", "goal", weight=length(path), walk=length(path), connector=None,
                               space=leg_start.space_id, path={"start": path, "goal": path[::-1]})
            try:
                nodes = nx.dijkstra_path(graph, "start", "goal", weight="weight")
            except nx.NetworkXNoPath as error:
                raise ValueError("No route with these constraints and the current closures.") from error
            legs.append([(u, v, graph[u][v]) for u, v in itertools.pairwise(nodes)])
        return self.describe(legs, stops)

    # ---------- output ----------

    def describe(self, legs: list[list[tuple]], stops: list[Place]) -> WalkRoute:
        points: list[dict] = []
        walked = cost = 0.0
        spaces: list[str] = []
        instructions: list[dict] = []
        for number, leg in enumerate(legs):
            pieces: list[dict] = []
            for u, v, edge in leg:
                cost += edge["weight"]
                walked += edge["walk"]
                if edge["connector"]:
                    pieces.append({"kind": "connector", "id": edge["connector"], "from": u[1], "to": v[1],
                                   "index": max(len(points) - 1, 0)})
                    continue
                path = edge["path"][u]
                floor_id = self.map.spaces[edge["space"]]["floor_id"]
                index = len(points)
                points += [{"floor_id": floor_id, "x": round(x, 3), "y": round(y, 3)} for x, y in path]
                last = pieces[-1] if pieces else None
                if last and last["kind"] == "walk" and last["space"] == edge["space"]:
                    last["path"] += path[1:]
                else:
                    pieces.append({"kind": "walk", "space": edge["space"], "path": list(path), "index": index,
                                   "floor_id": floor_id})
                if not spaces or spaces[-1] != edge["space"]:
                    spaces.append(edge["space"])
            leg_steps = self.instructions(pieces, stops[number], stops[number + 1])
            if number > 0:
                leg_steps = leg_steps[1:]  # the previous leg already arrived here
            if number < len(legs) - 1:
                leg_steps[-1] |= {"maneuver": "waypoint", "stop": number + 1,
                                  "text": leg_steps[-1]["text"].replace("Arrive at the ", "Arrive at ", 1)
                                  .replace("Arrive at", f"Stop {number + 1}:", 1)}
            instructions += leg_steps
        steps = [f"{i['text']} ({i['detail']})" if i["detail"] else i["text"] for i in instructions]
        return WalkRoute(round(walked, 1), round(cost, 1), points, steps, spaces, instructions)

    def instructions(self, pieces: list[dict], start: Place, goal: Place) -> list[dict]:
        """Google-style maneuvers: each one says what to do at the end of the stretch you are walking."""
        name = lambda space_id: self.map.spaces[space_id]["name"]  # noqa: E731
        floor_of = lambda space_id: self.map.spaces[space_id]["floor_id"]  # noqa: E731
        skip = {start.asset_id, goal.asset_id}
        origin = f"the {self.map.assets[start.asset_id]['name']}" if start.asset_id else name(start.space_id)
        result = [{"maneuver": "depart", "text": f"Start at {origin}", "detail": self.map.floor_name(floor_of(start.space_id)),
                   "distance_m": 0.0, "floor_id": floor_of(start.space_id), "index": 0}]
        walks = [p for p in pieces if p["kind"] == "walk" and self.map.spaces[p["space"]]["type"] not in ("lift", "stair")]
        for index, piece in enumerate(pieces):
            if piece["kind"] == "connector":
                connector = self.map.connectors[piece["id"]]
                to_floor, from_floor = floor_of(piece["to"]), floor_of(piece["from"])
                up = self.map.floors[to_floor]["level"] > self.map.floors[from_floor]["level"]
                kind = "lift" if connector["kind"] == LIFT else "stairs"
                if result and result[-1].get("connector") == piece["id"]:  # several floors in one go
                    result.pop()
                result.append({"connector": piece["id"], "maneuver": f"{kind}_{'up' if up else 'down'}",
                               "text": f"Take the {connector['name']} {'up' if up else 'down'} to {self.map.floor_name(to_floor)}",
                               "detail": "", "distance_m": 0.0, "floor_id": to_floor, "index": piece["index"]})
                continue
            if piece not in walks:
                continue
            distance = length(piece["path"])
            marks = self.landmarks(piece["space"], piece["path"], skip)
            detail = f"{distance:.0f} m" + (f", past the {' and the '.join(marks)}" if marks else "")
            ahead = index + 1
            while ahead < len(pieces) and pieces[ahead]["kind"] == "walk" and pieces[ahead] not in walks:
                ahead += 1  # the few steps inside a lift or stairwell are part of taking it
            following = pieces[ahead] if ahead < len(pieces) else None
            if following is None:
                arrival = f"the {self.map.assets[goal.asset_id]['name']}" if goal.asset_id else name(goal.space_id)
                maneuver, text = "arrive", f"Arrive at {arrival}"
            elif following["kind"] == "connector":
                maneuver, text = "straight", f"Walk to the {self.map.connectors[following['id']]['name']}"
            else:
                maneuver = self.turn(piece["path"], following["path"])
                verb = {"left": "Turn left", "right": "Turn right", "straight": "Continue straight"}[maneuver]
                text = f"{verb} into {name(following['space'])}"
            result.append({"maneuver": maneuver, "text": text, "detail": detail, "distance_m": round(distance, 1),
                           "floor_id": piece["floor_id"], "index": piece["index"]})
        return result

    def landmarks(self, space_id: str, path: list[tuple[float, float]], skip: set[str | None]) -> list[str]:
        line = LineString(path) if len(path) > 1 else Point(path[0])
        found = []
        for asset in self.map.assets.values():
            if asset["space_id"] != space_id or asset["category"] not in LANDMARK_CATEGORIES or asset["id"] in skip:
                continue
            distance = footprint(asset).distance(line)
            if distance <= LANDMARK_DISTANCE_M:
                found.append((line.project(Point(asset["position"])) if len(path) > 1 else 0, asset["name"]))
        return [name for _, name in sorted(found)[:MAX_LANDMARKS_PER_LEG]]

    @staticmethod
    def turn(before: list[tuple[float, float]], after: list[tuple[float, float]]) -> str:
        def heading(a, b):
            return math.atan2(b[1] - a[1], b[0] - a[0])

        incoming = next((heading(p, q) for p, q in reversed(list(itertools.pairwise(before))) if math.dist(p, q) > 0.3), None)
        outgoing = next((heading(p, q) for p, q in itertools.pairwise(after) if math.dist(p, q) > 0.3), None)
        if incoming is None or outgoing is None:
            return "straight"
        angle = math.degrees((outgoing - incoming + math.pi) % (2 * math.pi) - math.pi)
        if angle > TURN_THRESHOLD_DEG:
            return "left"
        if angle < -TURN_THRESHOLD_DEG:
            return "right"
        return "straight"
