"""Room optimiser: push movable furniture against the walls, then pack as many new items as fit, keeping every
bed or stretcher reachable from a door through an aisle at least AISLE_M wide. Deterministic, and pure until
apply_proposal.

Reachability works on the configuration space of a person AISLE_M wide: the room shrunk by AISLE_M / 2 minus
every footprint grown by AISLE_M / 2 (square corners, like core/walk.py). An item is reachable when the part of
that space connected to a door comes within AISLE_M / 2 of it."""

import copy

import numpy as np
import shapely
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from core.building_map import BuildingMap
from core.walk import CLEARANCE_M, WalkRouter, footprint, polygons
from hospital import rules

RuleError = rules.RuleError
EQUIPMENT = {"stretcher": (0.7, 2.0), "bed": (1.0, 2.2), "wheelchair": (0.7, 1.1), "chair": (0.5, 0.5),
             "infusion_pump": (0.6, 0.6), "monitor": (0.6, 0.6), "ultrasound": (0.6, 0.9), "ventilator": (0.6, 0.6),
             "cabinet": (1.0, 0.6), "shelf": (1.0, 0.5), "desk": (1.4, 0.7), "table": (1.2, 0.8), "bench": (1.6, 0.5),
             "counter": (2.0, 0.6), "therapy_table": (0.8, 2.0), "coffee_machine": (0.6, 0.6),
             "vending_machine": (0.9, 0.8), "water_dispenser": (0.4, 0.4)}
AISLE_M = 0.9
# A gap of exactly AISLE_M leaves a zero-width corridor that SpaceNav (and float noise) treats as closed.
HALF_AISLE_M = AISLE_M / 2 + 0.01
DOOR_CLEAR_M = 1.2
WALL_GAP_M = 0.1
ITEM_GAP_M = 0.1
GRID_M = 0.25
ROTATIONS = (0.0, 90.0)
REACH_TYPES = {"bed", "stretcher"}
REACH_TOL_M = 0.05
EPSILON = 1e-4


def lattice(low: float, high: float) -> np.ndarray:
    """Grid steps from both ends, so a row can sit flush against either wall."""
    if high < low:
        return np.empty(0)
    span = high - low
    steps = np.arange(0.0, span + EPSILON, GRID_M)
    return np.unique(np.round(np.concatenate([low + steps, high - steps]), 3))


class Layout:
    def __init__(self, polygon: Polygon, doors: list, fixed: list[Polygon]) -> None:
        self.polygon = polygon
        self.walls = polygon.boundary
        self.inner = polygon.buffer(-WALL_GAP_M + EPSILON, join_style="mitre")
        self.aisle = polygon.buffer(-HALF_AISLE_M, join_style="mitre")
        self.entries = unary_union([d.buffer(HALF_AISLE_M + REACH_TOL_M) for d in doors]) if doors else None
        self.keep_clear = unary_union([d.buffer(DOOR_CLEAR_M) for d in doors]) if doors else Polygon()
        shapely.prepare(self.inner)
        shapely.prepare(self.keep_clear)
        self.shapes: dict[str, Polygon] = {f"#fixed{i}": shape for i, shape in enumerate(fixed)}
        self.must: set[str] = set()
        self.free = self.reach = Polygon()

    def copy(self) -> "Layout":
        other = copy.copy(self)
        other.shapes, other.must = dict(self.shapes), set(self.must)
        return other

    def rebuild(self) -> None:
        blocked = unary_union([s.buffer(HALF_AISLE_M, join_style="mitre") for s in self.shapes.values()])
        self.free = self.aisle.difference(blocked)
        self.reach = self.connected(self.free)

    def connected(self, free) -> Polygon:
        if self.entries is None:
            return free
        return unary_union([part for part in polygons(free) if part.intersects(self.entries)])

    def reaches(self, reach, shape: Polygon) -> bool:
        return self.entries is None or (not reach.is_empty and reach.distance(shape) <= HALF_AISLE_M + REACH_TOL_M)

    def reachable(self) -> set[str]:
        return {key for key, shape in self.shapes.items() if self.reaches(self.reach, shape)}

    def try_add(self, key: str, shape: Polygon, needs_reach: bool) -> bool:
        """Add shape if every item that must stay reachable still is (and shape itself, if needs_reach)."""
        if needs_reach and not self.reaches(self.reach, shape):
            return False
        free = self.free.difference(shape.buffer(HALF_AISLE_M, join_style="mitre"))
        reach = self.connected(free)
        if needs_reach and not self.reaches(reach, shape):
            return False
        if not all(self.reaches(reach, self.shapes[k]) for k in self.must):
            return False
        self.shapes[key], self.free, self.reach = shape, free, reach
        if needs_reach:
            self.must.add(key)
        return True

    def candidates(self, width: float, depth: float) -> dict[str, np.ndarray]:
        """Every grid pose inside the room, clear of the door zones, with its distance to the walls."""
        minx, miny, maxx, maxy = self.inner.bounds
        xs, ys, rots, boxes = [], [], [], []
        for rotation in ROTATIONS:
            w, d = (width, depth) if rotation == 0 else (depth, width)
            grid_x, grid_y = np.meshgrid(lattice(minx + w / 2, maxx - w / 2), lattice(miny + d / 2, maxy - d / 2))
            grid_x, grid_y = grid_x.ravel(), grid_y.ravel()
            xs.append(grid_x)
            ys.append(grid_y)
            rots.append(np.full(grid_x.shape, rotation))
            boxes.append(shapely.box(grid_x - w / 2, grid_y - d / 2, grid_x + w / 2, grid_y + d / 2))
        x, y, rot, box = (np.concatenate(parts) for parts in (xs, ys, rots, boxes))
        ok = shapely.contains(self.inner, box) & ~shapely.intersects(self.keep_clear, box)
        x, y, rot, box = x[ok], y[ok], rot[ok], box[ok]
        return {"x": x, "y": y, "rot": rot, "box": box, "wall": shapely.distance(self.walls, box),
                "centre": shapely.distance(self.walls, shapely.points(x, y))}

    def clear_of(self, boxes: np.ndarray, shapes) -> np.ndarray:
        shapes = list(shapes)
        if not shapes:
            return np.ones(len(boxes), dtype=bool)
        blocked = unary_union([s.buffer(ITEM_GAP_M, join_style="mitre") for s in shapes])
        return ~(shapely.intersects(blocked, boxes) & ~shapely.touches(blocked, boxes))


def doors_of(bmap: BuildingMap, space_id: str, polygon: Polygon) -> list:
    """The stretch of wall each door of the space occupies."""
    walls = polygon.boundary
    result = []
    for opening in bmap.openings.values():
        if space_id not in opening["space_ids"]:
            continue
        on_wall = walls.interpolate(walls.project(Point(opening["position"])))
        stretch = walls.intersection(on_wall.buffer(max(opening.get("width_m") or 0.9, 0.1) / 2))
        result.append(on_wall if stretch.is_empty else stretch)
    return result


def size_of(asset: dict) -> tuple[float, float]:
    size = asset.get("footprint") or {}
    return size.get("width_m") or 0.6, size.get("depth_m") or 0.6


def pose(asset: dict) -> dict:
    x, y = asset["position"]
    return {"x": x, "y": y, "rotation_deg": (asset.get("footprint") or {}).get("rotation_deg") or 0.0}


def is_flush(shape: Polygon, layout: Layout) -> bool:
    return shape.distance(layout.walls) <= WALL_GAP_M + 0.01 and not shape.intersects(layout.keep_clear)


def push_to_walls(layout: Layout, room: list[dict]) -> list[dict]:
    """Largest first: each movable asset not yet against a wall goes to the nearest wall spot that fits."""
    moves = []
    movable = [a for a in room if a.get("movable") is not False]
    for asset in sorted(movable, key=lambda a: (-footprint(a).area, a["id"])):
        current = layout.shapes[asset["id"]]
        if is_flush(current, layout):
            continue
        needs_reach = asset["id"] in layout.must
        del layout.shapes[asset["id"]]
        layout.must.discard(asset["id"])
        layout.rebuild()
        options = layout.candidates(*size_of(asset))
        ok = (options["wall"] <= WALL_GAP_M + 0.01) & layout.clear_of(options["box"], layout.shapes.values())
        distance = np.hypot(options["x"] - asset["position"][0], options["y"] - asset["position"][1])
        order = [i for i in np.lexsort((options["rot"], distance)) if ok[i]]
        placed = next((i for i in order if layout.try_add(asset["id"], options["box"][i], needs_reach)), None)
        if placed is None:
            layout.shapes[asset["id"]] = current
            if needs_reach:
                layout.must.add(asset["id"])
            layout.rebuild()
            continue
        before = pose(asset)
        asset["position"] = [round(float(options["x"][placed]), 3), round(float(options["y"][placed]), 3)]
        asset["footprint"] = {**(asset.get("footprint") or {}), "rotation_deg": float(options["rot"][placed])}
        moves.append({"asset_id": asset["id"], "from": before, "to": pose(asset), "rotation_deg": pose(asset)["rotation_deg"]})
    layout.rebuild()
    return moves


def pack(layout: Layout, width: float, depth: float, count: int, perpendicular: bool) -> list[tuple]:
    """Greedy over grid poses, wall-adjacent first. Placing only ever shrinks the free area, so a pose that
    overlaps or cuts off a reachable item now never fits later: one pass in score order is enough."""
    options = layout.candidates(width, depth)
    alive = layout.clear_of(options["box"], layout.shapes.values())
    bucket = np.round((options["wall"] - WALL_GAP_M) / GRID_M)
    centre = -options["centre"] if perpendicular else options["centre"]
    placed = []
    for i in np.lexsort((options["rot"], options["x"], options["y"], centre, bucket)):
        if len(placed) == count:
            break
        if not alive[i] or not layout.try_add(f"#new{len(placed)}", options["box"][i], True):
            continue
        placed.append((float(options["x"][i]), float(options["y"][i]), float(options["rot"][i])))
        alive &= layout.clear_of(options["box"], [options["box"][i]])
    return placed


def free_area(polygon: Polygon, shapes: list[Polygon]) -> float:
    return round(polygon.area - unary_union(shapes).intersection(polygon).area, 2) if shapes else round(polygon.area, 2)


def new_ids(bmap: BuildingMap, space_id: str, count: int) -> list[str]:
    ids, number = [], 1
    while len(ids) < count:
        candidate = f"{space_id}-NEW-{number:02d}"
        if candidate not in bmap.assets:
            ids.append(candidate)
        number += 1
    return ids


def optimize_room(bmap: BuildingMap, router: WalkRouter, space_id: str, add_type: str, count: int) -> dict:
    if add_type not in EQUIPMENT:
        raise ValueError(f"Unknown equipment type {add_type!r}; known: {', '.join(EQUIPMENT)}.")
    if count < 0:
        raise ValueError("count must be ≥ 0.")
    ok, more, reason = rules.allowed(bmap, space_id, add_type)
    if not ok:
        raise RuleError(reason)
    target = count if more is None else min(count, more)
    space = bmap.spaces[space_id]
    polygon = Polygon(space["polygon"])
    polygon = polygon if polygon.is_valid else polygon.buffer(0)
    room = [copy.deepcopy(a) for a in bmap.assets.values() if a["space_id"] == space_id]
    before = copy.deepcopy(room)
    own = [footprint(a) for a in room]
    outside = [s for s in router.obstacles(space_id) if not any(s.equals(o) for o in own)]

    layout = Layout(polygon, doors_of(bmap, space_id, polygon), outside)
    layout.shapes |= {a["id"]: shape for a, shape in zip(room, own, strict=True)}
    layout.rebuild()
    layout.must = {a["id"] for a in room if a["type"] in REACH_TYPES} & layout.reachable()
    moves = push_to_walls(layout, room)

    width, depth = EQUIPMENT[add_type]
    placed = []
    for perpendicular in (True, False):
        attempt = pack(layout.copy(), width, depth, target, perpendicular)
        if len(attempt) > len(placed):
            placed = attempt
        if len(placed) == target:
            break

    limited_by = "space" if len(placed) < target else "rule" if target < count else None
    if limited_by == "space":
        reason = (f"Space for only {len(placed)} {rules.noun(add_type, len(placed))} "
                  f"with a {AISLE_M * 100:.0f} cm wide aisle. {reason}")
    label = rules.label(add_type)
    added = [{"id": asset_id, "name": f"{label} {asset_id.rsplit('-', 1)[-1]} (new)", "category": "equipment",
              "type": add_type, "space_id": space_id, "floor_id": space["floor_id"], "position": [round(x, 3), round(y, 3)],
              "footprint": {"width_m": width, "depth_m": depth, "rotation_deg": rotation}, "movable": True,
              "source": "optimizer"}
             for asset_id, (x, y, rotation) in zip(new_ids(bmap, space_id, len(placed)), placed, strict=True)]
    after = room + added
    return {"space_id": space_id, "add_type": add_type, "requested": count, "feasible": len(placed) == count,
            "max_count": len(placed), "limited_by": limited_by, "reason": reason, "moves": moves, "added": added,
            "free_area_m2": {"before": free_area(polygon, [*outside, *own]),
                             "after": free_area(polygon, [*outside, *map(footprint, after)])},
            "before": before, "after": after}


def apply_proposal(bmap: BuildingMap, router: WalkRouter, proposal: dict) -> list[str]:
    missing = [m["asset_id"] for m in proposal["moves"] if m["asset_id"] not in bmap.assets]
    taken = [a["id"] for a in proposal["added"] if a["id"] in bmap.assets]
    if missing or taken:
        raise ValueError(f"Proposal is stale (missing {missing}, already present {taken}).")
    changed = []
    for move in proposal["moves"]:
        asset = bmap.assets[move["asset_id"]]
        old = footprint(asset)
        asset["position"] = [move["to"]["x"], move["to"]["y"]]
        asset["footprint"] = {**(asset.get("footprint") or {}), "rotation_deg": move["to"]["rotation_deg"]}
        changed.append((asset["id"], old.union(footprint(asset))))
    for added in proposal["added"]:
        asset = copy.deepcopy(added)
        bmap.assets[asset["id"]] = asset
        bmap.data["assets"].append(asset)
        changed.append((asset["id"], footprint(asset)))
    space = bmap.spaces[proposal["space_id"]]
    touched = unary_union([shape for _, shape in changed]) if changed else Polygon()
    neighbours = [s["id"] for s in bmap.spaces.values() if s["floor_id"] == space["floor_id"] and not touched.is_empty
                  and Polygon(s["polygon"]).buffer(CLEARANCE_M).intersects(touched)]
    router.invalidate(sorted({proposal["space_id"], *neighbours}))
    return [asset_id for asset_id, _ in changed]
