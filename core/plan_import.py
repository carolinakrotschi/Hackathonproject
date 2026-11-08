"""Build a building.json from clean floor-plan images (one image per floor), in the kit's plan style.

Geometry is computed, not guessed: each room is a connected area of one fill colour (the colour gives its kind),
doors, openings and entrances are the small coloured markers on the walls, and stairs and lifts that overlap from
one floor to the next become vertical connectors. Only the text (room numbers and the title) is read by Claude, from
a contact sheet of cropped, enlarged labels (see core/plan_labels.py), so every label maps to exactly one room.

Kit plan conventions (checked against the kit SVGs): rooms #f3efe6, corridors #dcdcdc, halls #fff2b3, lifts #b4a7d6,
stairs #f4b183, outdoor #cfe8c4; brown dot = door, grey dot = opening, red triangle = entrance. Procedural plans are
drawn at 13.86 px per metre; other images need the scale from the user."""

import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from shapely.affinity import translate
from shapely.geometry import Point, Polygon

KIT_PX_PER_M = 9.979 * 100 / 72  # matplotlib points per metre in the kit renderer, at 100 dpi
FILLS = {
    "room": (243, 239, 230), "corridor": (220, 220, 220), "hall": (255, 242, 179),
    "lift": (180, 167, 214), "stair": (244, 177, 131), "outdoor": (207, 232, 196),
}
MARKERS = {"door": (139, 69, 19), "opening": (158, 158, 158), "entrance": (211, 47, 47)}
ROOM_TYPES = {"room": "generic_room", "corridor": "corridor", "hall": "hall", "lift": "lift", "stair": "stairwell",
              "outdoor": "balcony"}
NAMES = {"room": "Room", "corridor": "Corridor", "hall": "Hall", "lift": "Lift", "stair": "Staircase", "outdoor": "Outdoor"}
FILL_TOLERANCE = 14
MARKER_TOLERANCE = 45
MIN_ROOM_PX = 40
MAX_TEXT_COMPONENT_PX = (70, 22)  # a glyph or label fragment, never a wall line
MARKER_SIZE_PX = (3, 13)
DOOR_SNAP_PX = 5.0
SEAM_PX = 1.1  # rooms are separated by an anti-aliased outline; grow each one to the middle of it
NOTCH_PX = 9  # larger than a door marker
DOOR_WIDTH_M, WIDE_DOOR_WIDTH_M, ENTRANCE_WIDTH_M, MAX_OPENING_WIDTH_M = 0.9, 1.6, 1.8, 4.0
FLOOR_HEIGHT_M = 4.0
CONNECTOR_MIN_OVERLAP = 0.3


@dataclass
class FloorScan:
    """What the image analysis found on one floor, in pixels."""

    image: Path
    size: tuple[int, int]
    rooms: list[dict] = field(default_factory=list)      # {"kind", "polygon": [(x, y)], "text_box": (x0, y0, x1, y1) | None}
    markers: list[dict] = field(default_factory=list)    # {"kind", "x", "y"}
    title_box: tuple[int, int, int, int] | None = None
    rgb: np.ndarray | None = None  # the image the labels are cropped from (straightened for photos)
    photo: bool = False
    seam_px: float = SEAM_PX
    snap_px: float = DOOR_SNAP_PX


def colour_mask(rgb: np.ndarray, colour: tuple[int, int, int], tolerance: float) -> np.ndarray:
    return np.linalg.norm(rgb - np.array(colour, dtype=np.int16), axis=2) < tolerance


def scan_floor(path: Path) -> FloorScan:
    rgb = np.asarray(Image.open(path).convert("RGB"))
    if looks_clean(rgb):
        return scan_clean(path, rgb)
    from core.plan_photo import scan_photo  # noqa: PLC0415
    return scan_photo(path, rgb)


def looks_clean(rgb: np.ndarray) -> bool:
    """A rendered plan is almost entirely exact palette colours (white paper and room fills); a photo never is."""
    sample = rgb[::4, ::4].reshape(-1, 3).astype(np.int16)
    palette = np.array([(255, 255, 255), *FILLS.values()], dtype=np.int16)
    distance = np.abs(sample[:, None, :] - palette[None, :, :]).max(axis=2).min(axis=1)
    return (distance <= 3).mean() > 0.8


def scan_clean(path: Path, image: np.ndarray) -> FloorScan:
    rgb = image.astype(np.int16)
    height, width = rgb.shape[:2]
    scan = FloorScan(path, (width, height), rgb=image)
    kind_of = np.full((height, width), -1, dtype=np.int8)
    kinds = list(FILLS)
    for index, kind in enumerate(kinds):
        kind_of[colour_mask(rgb, FILLS[kind], FILL_TOLERANCE)] = index
    white = colour_mask(rgb, (255, 255, 255), 10)

    # Text (labels, title) breaks rooms apart; give each small non-fill blob the kind that surrounds it.
    other = ((kind_of < 0) & ~white).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(other, connectivity=8)
    text_owner: dict[int, int] = {}
    ring = np.ones((5, 5), np.uint8)
    title_boxes = []
    for blob in range(1, count):
        x, y, w, h, area = stats[blob]
        if w > MAX_TEXT_COMPONENT_PX[0] or h > MAX_TEXT_COMPONENT_PX[1]:
            continue  # walls and long outlines
        y0, y1, x0, x1 = max(0, y - 3), min(height, y + h + 3), max(0, x - 3), min(width, x + w + 3)
        blob_mask = (labels[y0:y1, x0:x1] == blob).astype(np.uint8)
        around = cv2.dilate(blob_mask, ring).astype(bool) & ~blob_mask.astype(bool)
        neighbours = kind_of[y0:y1, x0:x1][around]
        on_white = white[y0:y1, x0:x1][around]
        if on_white.mean() > 0.6:
            title_boxes.append((x, y, x + w, y + h))
            continue
        filled = neighbours[neighbours >= 0]
        if filled.size and (filled == np.bincount(filled).argmax()).mean() > 0.8:
            owner = int(np.bincount(filled).argmax())
            kind_of[y:y + h, x:x + w][labels[y:y + h, x:x + w] == blob] = owner
            text_owner[blob] = owner
    if title_boxes:
        top = min(box[1] for box in title_boxes)
        row = [box for box in title_boxes if box[1] < top + 30]
        scan.title_box = (min(b[0] for b in row), min(b[1] for b in row), max(b[2] for b in row), max(b[3] for b in row))

    for index, kind in enumerate(kinds):
        mask = (kind_of == index).astype(np.uint8)
        rooms, room_labels, room_stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
        for room in range(1, rooms):
            if room_stats[room][4] < MIN_ROOM_PX:
                continue
            room_mask = (room_labels == room).astype(np.uint8)
            # door markers sit on the walls and bite small notches out of the fill: close them, or walks detour
            outline_mask = cv2.morphologyEx(room_mask, cv2.MORPH_CLOSE, np.ones((NOTCH_PX, NOTCH_PX), np.uint8))
            contours, _ = cv2.findContours(outline_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            contour = max(contours, key=cv2.contourArea)
            approx = cv2.approxPolyDP(contour, 1.0, True).reshape(-1, 2)
            if len(approx) < 3:
                continue
            text = [b for b, owner in text_owner.items() if owner == index and room_mask[
                int(stats[b][1] + stats[b][3] / 2), int(stats[b][0] + stats[b][2] / 2)]]
            box = None
            if text:
                box = (min(stats[b][0] for b in text), min(stats[b][1] for b in text),
                       max(stats[b][0] + stats[b][2] for b in text), max(stats[b][1] + stats[b][3] for b in text))
            scan.rooms.append({"kind": kind, "polygon": [(float(x) + 0.5, float(y) + 0.5) for x, y in approx], "text_box": box})

    for kind, colour in MARKERS.items():
        mask = colour_mask(rgb, colour, MARKER_TOLERANCE).astype(np.uint8)
        found, _, marker_stats, centres = cv2.connectedComponentsWithStats(mask, connectivity=8)
        for blob in range(1, found):
            x, y, w, h, area = marker_stats[blob]
            if MARKER_SIZE_PX[0] <= w <= MARKER_SIZE_PX[1] and MARKER_SIZE_PX[0] <= h <= MARKER_SIZE_PX[1] and area >= 6:
                scan.markers.append({"kind": kind, "x": float(centres[blob][0]), "y": float(centres[blob][1])})
    return scan


# ---------- assembling the building ----------

LEVEL_WORDS = [(r"ground", 0), (r"basement\s*(\d+)", -1), (r"(?:floor|level|og|etage)\s*(-?\d+)", 1), (r"(\d+)\.\s*og", 1)]


def level_from_title(title: str | None) -> int | None:
    text = (title or "").lower()
    for pattern, sign in LEVEL_WORDS:
        match = re.search(pattern, text)
        if match:
            return 0 if sign == 0 else sign * int(match.group(1))
    return None


def split_title(title: str | None) -> tuple[str | None, str | None]:
    if not title:
        return None, None
    parts = re.split(r"\s+[—–-]\s+", title.strip(), maxsplit=1)
    return (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "building"


def floor_name(level: int) -> str:
    return "Ground floor" if level == 0 else (f"Basement {-level}" if level < 0 else f"Floor {level}")


Transform = tuple[float, float, float]  # scale, dx, dy: base pixel = scale * image pixel + (dx, dy)


def place(transform: Transform, x: float, y: float) -> tuple[float, float]:
    scale, dx, dy = transform
    return scale * x + dx, scale * y + dy


def core_shapes(scan: FloorScan, transform: Transform = (1.0, 0.0, 0.0)) -> list[tuple[str, Polygon]]:
    return [(room["kind"], Polygon([place(transform, x, y) for x, y in room["polygon"]]).buffer(0))
            for room in scan.rooms if room["kind"] in ("stair", "lift")]


def align(placed: FloorScan, transform: Transform, scan: FloorScan) -> Transform:
    """Where `scan` goes on top of an already placed floor: the scale and shift that best overlay stairs and lifts
    (they sit in the same place on every floor). Images are often cropped per floor, and photos of different
    floors are taken from different distances, so both are needed. Rendered plans share one scale."""
    below, here = core_shapes(placed, transform), core_shapes(scan)
    same_scale = not (placed.photo or scan.photo)
    pairs = [(a, b) for kind_a, a in below for kind_b, b in here if kind_a == kind_b and b.area > 0]
    candidates = []
    for a, b in pairs:  # one core: scale from its size (photos) and shift from its centre
        scale = transform[0] if same_scale else (a.area / b.area) ** 0.5
        candidates.append((scale, a.centroid.x - scale * b.centroid.x, a.centroid.y - scale * b.centroid.y))
    if not same_scale:  # two cores: their distance apart gives a much more precise scale
        for (a1, b1), (a2, b2) in itertools.combinations(pairs, 2):
            gap_b = b1.centroid.distance(b2.centroid)
            if a1 is a2 or b1 is b2 or gap_b < 1:
                continue
            scale = a1.centroid.distance(a2.centroid) / gap_b
            centre_a = ((a1.centroid.x + a2.centroid.x) / 2, (a1.centroid.y + a2.centroid.y) / 2)
            centre_b = ((b1.centroid.x + b2.centroid.x) / 2, (b1.centroid.y + b2.centroid.y) / 2)
            candidates.append((scale, centre_a[0] - scale * centre_b[0], centre_a[1] - scale * centre_b[1]))
    best, best_score = None, 0.0
    for scale, dx, dy in candidates:
        moved = [(kind, Polygon([(scale * x + dx, scale * y + dy) for x, y in d.exterior.coords])) for kind, d in here]
        score = sum(max((c.intersection(m).area for k, c in below if k == kind), default=0.0) for kind, m in moved)
        if score > best_score:
            best, best_score = (scale, dx, dy), score
    if best is not None:
        return best
    # no shared stairs or lifts: centre the floors on each other at the same scale
    if not placed.rooms or not scan.rooms:
        return transform
    def centre(points):
        xs, ys = [x for x, _ in points], [y for _, y in points]
        return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    scale = transform[0]
    cx, cy = centre([place(transform, x, y) for r in placed.rooms for x, y in r["polygon"]])
    nx_, ny_ = centre([p for r in scan.rooms for p in r["polygon"]])
    return scale, cx - scale * nx_, cy - scale * ny_


def assemble(scans: list[FloorScan], px_per_m: float, labels: list[dict] | None = None, name: str | None = None) -> dict:
    """labels[i] = {"title": str | None, "rooms": {room_index: text}} for scans[i] (from plan_labels)."""
    labels = labels or [{} for _ in scans]
    titles = [split_title(entry.get("title")) for entry in labels]
    building_name = name or next((t[0] for t in titles if t[0]), None) or "Imported building"
    levels = []
    for index, (_, floor_title) in enumerate(titles):
        level = level_from_title(floor_title)
        levels.append(level if level is not None and level not in levels else (max(levels, default=-1) + 1))
    building_id = f"imported-{slug(building_name)}"
    order = sorted(range(len(scans)), key=lambda i: levels[i])
    base = min(order, key=lambda i: abs(levels[i]))  # ground floor (or the closest to it) stays put
    offsets: dict[int, Transform] = {base: (1.0, 0.0, 0.0)}
    position = order.index(base)
    for step in (1, -1):  # walk up, then down, aligning each floor to its placed neighbour
        index = position
        while 0 <= index + step < len(order):
            placed, new = order[index], order[index + step]
            offsets[new] = align(scans[placed], offsets[placed], scans[new])
            index += step
    height_px = scans[base].size[1]

    floors, spaces, openings, shapes = [], [], [], {}
    for number in order:
        scan, level, entry = scans[number], levels[number], labels[number]
        transform = offsets[number]

        def to_m(x, y, transform=transform):
            bx, by = place(transform, x, y)
            return round(bx / px_per_m, 3), round((height_px - by) / px_per_m, 3)
        floor_id = f"{building_id}-F{level}"
        floors.append({"id": floor_id, "level": level, "name": floor_name(level), "elevation_m": level * FLOOR_HEIGHT_M,
                       "height_m": FLOOR_HEIGHT_M})
        room_text = {int(k): v for k, v in (entry.get("rooms") or {}).items()}
        floor_spaces = []
        for index, room in enumerate(scan.rooms):
            polygon = Polygon([to_m(x, y) for x, y in room["polygon"]])
            if scan.seam_px:
                polygon = polygon.buffer(scan.seam_px * transform[0] / px_per_m, join_style="mitre")
            if not polygon.is_valid or polygon.is_empty or polygon.geom_type != "Polygon":
                polygon = polygon.buffer(0)
                if polygon.geom_type != "Polygon":
                    polygon = max(polygon.geoms, key=lambda part: part.area)
            ref = (room_text.get(index) or "").strip() or None
            space_id = f"{floor_id}-S{index:03d}"
            kind = room["kind"]
            space = {
                "id": space_id, "floor_id": floor_id, "type": kind if kind != "room" else "room",
                "room_type": ROOM_TYPES[kind], "name": f"{NAMES[kind]} {ref or f'{level}.{index:02d}'}", "ref": ref,
                "polygon": [[round(x, 3), round(y, 3)] for x, y in list(polygon.exterior.coords)[:-1]],
                "synthetic": False,
            }
            spaces.append(space)
            floor_spaces.append((space, polygon))
            shapes[space_id] = polygon
        for number, marker in enumerate(scan.markers):
            point = Point(to_m(marker["x"], marker["y"]))
            near = sorted((polygon.distance(point), space["id"], space) for space, polygon in floor_spaces)
            near = [item for item in near if item[0] <= scan.snap_px * transform[0] / px_per_m]
            if marker["kind"] == "entrance":
                if not near:
                    continue
                joins = [near[0][1], "outside"]
                width = ENTRANCE_WIDTH_M
            else:
                if len(near) < 2:
                    continue
                joins = [near[0][1], near[1][1]]
                halls = any(item[2]["type"] == "hall" for item in near[:2])
                if marker["kind"] == "opening":
                    shared = shapes[joins[0]].buffer(0.2).intersection(shapes[joins[1]].buffer(0.2)).length / 2
                    width = round(min(MAX_OPENING_WIDTH_M, max(DOOR_WIDTH_M, shared)), 2)
                else:
                    width = WIDE_DOOR_WIDTH_M if halls else DOOR_WIDTH_M
            openings.append({
                "id": f"{floor_id}-O{number:04d}", "floor_id": floor_id, "space_ids": joins,
                "kind": {"door": "door", "opening": "opening", "entrance": "entrance"}[marker["kind"]],
                "position": [round(point.x, 3), round(point.y, 3)], "width_m": width, "step_free": True, "synthetic": False,
            })

    connectors = vertical_connectors(floors, spaces, shapes, building_id)
    return {
        "building": {"id": building_id, "name": building_name, "source": "imported_plan",
                     "source_ref": {"images": [scan.image.name for scan in scans], "px_per_m": round(px_per_m, 3)},
                     "license": "user-provided", "attribution": "Imported from floor plan images", "units": "m"},
        "floors": floors, "spaces": spaces, "openings": openings, "vertical_connectors": connectors, "assets": [],
    }


def vertical_connectors(floors: list[dict], spaces: list[dict], shapes: dict, building_id: str) -> list[dict]:
    """Stairs and lifts that overlap on consecutive floors are one connector."""
    by_level = sorted(floors, key=lambda f: f["level"])
    chains: list[list[dict]] = []
    owner: dict[str, list[dict]] = {}
    for lower, upper in itertools.pairwise(by_level):
        for space in (s for s in spaces if s["floor_id"] == lower["id"] and s["type"] in ("stair", "lift")):
            candidates = [s for s in spaces if s["floor_id"] == upper["id"] and s["type"] == space["type"]]
            scored = [(shapes[space["id"]].intersection(shapes[c["id"]]).area / min(shapes[space["id"]].area, shapes[c["id"]].area), c)
                      for c in candidates]
            best = max(scored, key=lambda item: item[0], default=(0, None))
            if best[0] < CONNECTOR_MIN_OVERLAP:
                continue
            chain = owner.get(space["id"])
            if chain is None:
                chain = [space]
                chains.append(chain)
                owner[space["id"]] = chain
            chain.append(best[1])
            owner[best[1]["id"]] = chain
    connectors = []
    counts = {"stair": 0, "lift": 0}
    for chain in chains:
        kind = chain[0]["type"]
        counts[kind] += 1
        label = next((s["ref"] for s in chain if s["ref"]), None)
        connectors.append({
            "id": f"{building_id}-V{len(connectors) + 1:02d}", "kind": "lift" if kind == "lift" else "stairs",
            "name": label and f"{NAMES[kind]} {label}" or f"{'Lift' if kind == 'lift' else 'Staircase'} {counts[kind]}",
            "floor_ids": [s["floor_id"] for s in chain], "space_ids": [s["id"] for s in chain],
            "step_free": kind == "lift", "synthetic": False,
        })
    return connectors


# ---------- checks ----------


def check(building: dict) -> list[str]:
    """Problems a person should look at: rooms you cannot reach, rooms without doors, missing or repeated labels."""
    issues = []
    spaces = {s["id"]: s for s in building["spaces"]}
    neighbours: dict[str, set[str]] = {s: set() for s in spaces}
    for opening in building["openings"]:
        a, b = opening["space_ids"]
        neighbours.setdefault(a, set()).add(b)
        neighbours.setdefault(b, set()).add(a)
    for connector in building["vertical_connectors"]:
        for a, b in itertools.pairwise(connector["space_ids"]):
            neighbours[a].add(b)
            neighbours[b].add(a)
    reached, frontier = {"outside"}, ["outside"]
    while frontier:
        for nxt in neighbours.get(frontier.pop(), ()):
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)
    if not any("outside" in o["space_ids"] for o in building["openings"]):
        issues.append("No entrance found: mark where people come in.")
    unreachable = [s for s in spaces.values() if s["id"] not in reached]
    if unreachable and len(reached) > 1:
        issues.append(f"{len(unreachable)} spaces can't be reached from an entrance: "
                      + ", ".join(s["name"] for s in unreachable[:6]) + ("…" if len(unreachable) > 6 else ""))
    doorless = [s for s in spaces.values() if not neighbours.get(s["id"])]
    if doorless:
        issues.append(f"{len(doorless)} spaces have no door: " + ", ".join(s["name"] for s in doorless[:6]))
    unlabelled = [s for s in spaces.values() if not s["ref"] and s["type"] != "corridor"]
    if unlabelled:
        issues.append(f"{len(unlabelled)} rooms have no readable number.")
    refs = [s["ref"] for s in spaces.values() if s["ref"]]
    repeated = sorted({r for r in refs if refs.count(r) > 1})
    if repeated:
        issues.append("Repeated room numbers: " + ", ".join(repeated[:6]))
    return issues


def import_plans(images: list[Path], px_per_m: float = KIT_PX_PER_M, read_labels=None, name: str | None = None) -> tuple[dict, list[str]]:
    """read_labels(scans) -> labels per floor (Claude); None keeps rooms unlabelled and costs nothing."""
    scans = [scan_floor(path) for path in images]
    labels = read_labels(scans) if read_labels else None
    building = assemble(scans, px_per_m, labels, name)
    return building, check(building)


def photo_scale_guess(scans: list[FloorScan]) -> float:
    """Pixels per metre if the median room (not hall) is about 4 m across: rough, so the user should confirm."""
    sizes = []
    for scan in scans:
        for room in scan.rooms:
            if room["kind"] == "room":
                xs, ys = [x for x, _ in room["polygon"]], [y for _, y in room["polygon"]]
                sizes.append(min(max(xs) - min(xs), max(ys) - min(ys)))
    sizes.sort()
    return (sizes[len(sizes) // 2] / 4.0) if sizes else KIT_PX_PER_M


def default_scale(scans: list[FloorScan], width_m: float | None = None) -> tuple[float, bool]:
    """(px per metre, guessed?). A known width wins; rendered kit-style plans have a known scale; photos are guessed."""
    if width_m:
        return footprint_px(scans[0])[0] / width_m, False
    if any(scan.photo for scan in scans):
        return photo_scale_guess(scans), True
    return KIT_PX_PER_M, False


def set_dimensions(building: dict, width_m: float | None = None, depth_m: float | None = None,
                   floor_heights_m: list[float] | None = None) -> dict:
    """Apply what the user knows: the building's real width (and depth), and the storey heights: the floor-to-floor
    distance above each floor, lowest floor first, i.e. what a staircase climbs (stairs cost 2 m per metre
    climbed, so this matters for routes). Not the ceiling height: a factory hall can be 8 m tall while the office
    floor above it starts 3.5 m up. Changes `building` in place."""
    floors = sorted(building["floors"], key=lambda f: f["level"])
    if floor_heights_m:
        heights = (list(floor_heights_m) + list(floor_heights_m[-1:]) * len(floors))[: len(floors)]
        base = next((i for i, f in enumerate(floors) if f["level"] >= 0), 0)
        elevation = 0.0
        for i in range(base, len(floors)):  # ground floor at 0, upwards
            floors[i]["elevation_m"], floors[i]["height_m"] = round(elevation, 3), heights[i]
            elevation += heights[i]
        elevation = 0.0
        for i in range(base - 1, -1, -1):  # basements below
            elevation -= heights[i]
            floors[i]["elevation_m"], floors[i]["height_m"] = round(elevation, 3), heights[i]
        building["building"]["source_ref"]["heights_guessed"] = False
    if width_m:
        xs = [x for s in building["spaces"] for x, _ in s["polygon"]]
        ys = [y for s in building["spaces"] for _, y in s["polygon"]]
        fx = width_m / (max(xs) - min(xs))
        fy = depth_m / (max(ys) - min(ys)) if depth_m else fx
        for space in building["spaces"]:
            space["polygon"] = [[round(x * fx, 3), round(y * fy, 3)] for x, y in space["polygon"]]
        for element in [*building["openings"], *building["assets"]]:
            element["position"] = [round(element["position"][0] * fx, 3), round(element["position"][1] * fy, 3)]
        ref = building["building"]["source_ref"]
        ref["px_per_m"] = round(ref.get("px_per_m", KIT_PX_PER_M) / fx, 3)
        ref["size_guessed"] = False
    return building


def footprint_px(scan: FloorScan) -> tuple[float, float]:
    """Width and height of one drawn floor in pixels, for turning a known width into a scale."""
    xs = [x for room in scan.rooms for x, _ in room["polygon"]]
    ys = [y for room in scan.rooms for _, y in room["polygon"]]
    return (max(xs) - min(xs), max(ys) - min(ys)) if xs else (0.0, 0.0)

