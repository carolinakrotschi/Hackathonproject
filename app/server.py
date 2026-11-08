"""Demo server: an indoor maps app for the kit's factories. Search, place details and routes are deterministic;
only /api/chat (the "Ask" assistant) calls Claude.

  uv run --env-file .env uvicorn app.server:app --host 127.0.0.1 --port 10000    then open the page"""

from functools import cache
from pathlib import Path

import json
import shutil
import uuid

import anthropic
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent import GuideSession, route_payload
from app.hospital_api import router as hospital_router
from core import queries
from core.building_map import OUTSIDE, BuildingMap
from core.plan_import import assemble, check, default_scale, scan_floor, set_dimensions
from core.plan_labels import read_labels
from core.walk import Place
from hospital.nomenclature import apply_labels

ROOT = Path(__file__).resolve().parent.parent
FACTORIES = ROOT / "factories"
IMPORTED = ROOT / "imported"
SAMPLES = ROOT / "samples"
STATIC = Path(__file__).resolve().parent / "static"
MAX_RESULTS = 25
MODES = {"walk": (False, None), "accessible": (True, None), "cart": (True, 1.2)}

ROOM_ICONS = {
    "toilet": "wc", "kitchen": "restaurant", "canteen": "restaurant", "meeting_room": "groups", "office": "work",
    "server_room": "dns", "locker_room": "checkroom", "lobby": "door_front", "quality_lab": "biotech",
    "production_zone": "precision_manufacturing", "assembly_zone": "build", "additive_zone": "view_in_ar",
    "warehouse_zone": "warehouse", "logistics_zone": "local_shipping", "stairwell": "stairs", "lift": "elevator",
    "corridor": "straight", "storage": "inventory_2", "technical_room": "settings", "lab": "science",
}
ASSET_ICONS = {"machine": "precision_manufacturing", "equipment": "handyman", "fixture": "plumbing", "furniture": "chair"}
CATEGORIES = {
    "machines": ("Machines", "precision_manufacturing"),
    "toilets": ("Toilets", "wc"),
    "food": ("Kitchens", "restaurant"),
    "meeting": ("Meeting rooms", "groups"),
    "lifts": ("Lifts", "elevator"),
    "exits": ("Exits", "door_open"),
    "safety": ("Safety", "fire_extinguisher"),
    "logistics": ("Logistics", "local_shipping"),
}

app = FastAPI(title="Indoor maps demo")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/samples", StaticFiles(directory=SAMPLES), name="samples")
app.include_router(hospital_router)
sessions: dict[str, GuideSession] = {}


@cache
def client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def building_paths() -> dict[str, Path]:
    imported = sorted(IMPORTED.glob("*/building.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {path.parent.name: path.parent for path in [*imported, *sorted(FACTORIES.glob("*/building.json"))]}


def session_for(session_id: str, building_id: str | None = None) -> GuideSession:
    session = sessions.get(session_id)
    if building_id and (session is None or session.map.data["building"]["id"] != building_id):
        if building_id not in building_paths():
            raise HTTPException(404, f"unknown building {building_id}")
        directory = building_paths()[building_id]
        session = GuideSession(BuildingMap.load(directory), client())
        if directory.parent == IMPORTED:
            session.save_path = directory / "building.json"  # corrections made in the chat are kept
        sessions[session_id] = session
    if session is None:
        raise HTTPException(404, "unknown session, load the map first")
    return session


# ---------- describing things ----------


def floor_label(level: int) -> str:
    return "G" if level == 0 else (f"B{-level}" if level < 0 else str(level))


def item(bmap: BuildingMap, element_id: str) -> dict:
    """Uniform card data for a space or an asset."""
    if element_id in bmap.assets:
        asset = bmap.assets[element_id]
        space = bmap.spaces[asset["space_id"]]
        return {
            "id": element_id, "kind": "asset", "name": asset["name"],
            "subtitle": space["name"],
            "floor_id": asset["floor_id"], "floor_name": bmap.floor_name(asset["floor_id"]),
            "x": asset["position"][0], "y": asset["position"][1],
            "icon": ASSET_ICONS.get(asset["category"], "place"), "category": asset["category"],
        }
    space = bmap.spaces[element_id]
    x, y = bmap.anchor(element_id)
    return {
        "id": element_id, "kind": "space", "name": space["name"],
        "subtitle": space["room_type"].replace("_", " ").capitalize(),
        "floor_id": space["floor_id"], "floor_name": bmap.floor_name(space["floor_id"]),
        "x": x, "y": y, "icon": ROOM_ICONS.get(space["room_type"], "meeting_room"), "category": space["type"],
    }


def entrance_spaces(bmap: BuildingMap) -> list[str]:
    spaces = []
    for opening in bmap.openings.values():
        if OUTSIDE in opening["space_ids"]:
            spaces.append(bmap.other_side(opening, OUTSIDE))
    return list(dict.fromkeys(spaces))


def category_ids(bmap: BuildingMap, category: str) -> list[str]:
    spaces, assets = bmap.spaces.values(), bmap.assets.values()
    if category == "machines":
        return [a["id"] for a in assets if a["category"] == "machine"]
    if category == "toilets":
        return [s["id"] for s in spaces if s["room_type"] == "toilet"]
    if category == "food":
        return [s["id"] for s in spaces if s["room_type"] in ("kitchen", "canteen")]
    if category == "meeting":
        return [s["id"] for s in spaces if s["room_type"] in ("meeting_room", "seminar_room")]
    if category == "lifts":
        return [s["id"] for s in spaces if s["type"] == "lift"]
    if category == "exits":
        return entrance_spaces(bmap)
    if category == "safety":
        return [a["id"] for a in assets if any(w in a["type"] for w in ("fire", "first_aid", "eyewash", "defibrillator"))]
    if category == "logistics":
        return [s["id"] for s in spaces if s["room_type"] in ("logistics_zone", "warehouse_zone")]
    raise HTTPException(404, f"unknown category {category}")


def place_of(session: GuideSession, element_id: str) -> Place:
    if element_id in session.map.assets:
        return session.router.place_for_asset(element_id)
    if element_id in session.map.spaces:
        return session.router.place_for_space(element_id)
    raise HTTPException(404, f"unknown place {element_id}")


def map_payload(bmap: BuildingMap) -> dict:
    entrances = entrance_spaces(bmap)
    ground = [s for s in entrances if bmap.floors[bmap.spaces[s]["floor_id"]]["level"] == 0]
    return {
        "building": {"id": bmap.data["building"]["id"], "name": bmap.data["building"]["name"]},
        "floors": [
            {**floor, "label": floor_label(floor["level"])}
            for floor in sorted(bmap.floors.values(), key=lambda floor: floor["level"])
        ],
        "spaces": [
            {key: space[key] for key in ("id", "floor_id", "type", "room_type", "name", "polygon")}
            | {"ref": space.get("ref"), "anchor": bmap.anchor(space["id"]), "icon": ROOM_ICONS.get(space["room_type"])}
            for space in bmap.spaces.values()
        ],
        "openings": [
            {key: opening[key] for key in ("id", "floor_id", "kind", "position", "width_m", "step_free", "space_ids")}
            for opening in bmap.openings.values()
        ],
        "connectors": [
            {key: connector[key] for key in ("id", "name", "kind", "space_ids")}
            for connector in bmap.connectors.values()
        ],
        "assets": [
            {key: asset[key] for key in ("id", "name", "type", "category", "floor_id", "space_id", "position", "footprint")}
            for asset in bmap.assets.values()
        ],
        "closed": sorted(bmap.closed),
        "default_location": (ground or entrances or [next(iter(bmap.spaces))])[0],
        "categories": [{"id": key, "label": label, "icon": icon} for key, (label, icon) in CATEGORIES.items()],
    }


# ---------- endpoints ----------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/buildings")
def buildings() -> list[dict]:
    result = []
    for building_id, path in sorted(building_paths().items()):
        bmap = BuildingMap.load(path)
        result.append({"id": building_id, "name": bmap.data["building"]["name"], "floors": len(bmap.floors),
                       "imported": path.parent == IMPORTED})
    return result


# ---------- floor-plan import (level 4) ----------


def import_summary(building: dict) -> dict:
    xs = [x for s in building["spaces"] for x, _ in s["polygon"]]
    ys = [y for s in building["spaces"] for _, y in s["polygon"]]
    return {
        "id": building["building"]["id"], "name": building["building"]["name"],
        "floors": [f["name"] for f in sorted(building["floors"], key=lambda f: f["level"])],
        "floor_heights_m": [f["height_m"] for f in sorted(building["floors"], key=lambda f: f["level"])],
        "heights_guessed": building["building"]["source_ref"].get("heights_guessed", True),
        "rooms": sum(s["type"] in ("room", "hall") for s in building["spaces"]), "spaces": len(building["spaces"]),
        "doors": sum("outside" not in o["space_ids"] for o in building["openings"]),
        "entrances": sum("outside" in o["space_ids"] for o in building["openings"]),
        "stairs_lifts": len(building["vertical_connectors"]),
        "size_m": [round(max(xs) - min(xs), 1), round(max(ys) - min(ys), 1)] if xs else [0, 0],
        "photos": building["building"]["source_ref"].get("photos", False),
        "size_guessed": building["building"]["source_ref"].get("size_guessed", False),
        "issues": check(building),
    }


def unique_id(base: str) -> str:
    building_id, number = base, 2
    while building_id in building_paths() or (ROOT / "hospitals" / building_id).exists():
        building_id, number = f"{base}-{number}", number + 1
    return building_id


def sample_images(directory: Path) -> list[Path]:
    return sorted([*directory.glob("*.png"), *directory.glob("*.jpg")])


@app.get("/api/samples")
def samples() -> list[dict]:
    """Clean plan images and simulated phone photos of two kit factories (CC0), for trying the import."""
    return [{"id": f"{kind}/{d.name}", "kind": kind, "name": d.name.replace("proc-factory-", "Factory "),
             "images": [f"/samples/{kind}/{d.name}/{p.name}" for p in sample_images(d)]}
            for kind in ("photos", "plans") for d in sorted((SAMPLES / kind).iterdir()) if d.is_dir()]


@app.post("/api/import")
def import_building(files: list[UploadFile] = File(default=[]), sample: str = Form(default=""),
                    width_m: float | None = Form(default=None), name: str = Form(default="")) -> dict:
    """Floor plans (one image per floor) → a new building. Geometry is computed; Claude reads the labels."""
    work = IMPORTED / "_uploads" / uuid.uuid4().hex
    work.mkdir(parents=True)
    if sample:
        kind, folder = (sample.split("/", 1) + [""])[:2] if "/" in sample else ("plans", sample)
        if kind not in ("plans", "photos"):
            raise HTTPException(404, "unknown sample")
        sources = sample_images(SAMPLES / kind / Path(folder).name)
        paths = [Path(shutil.copy(source, work / source.name)) for source in sources]
    else:
        paths = []
        for number, upload in enumerate(files):
            path = work / f"{number:02d}_{Path(upload.filename or 'plan.png').name}"
            path.write_bytes(upload.file.read())
            paths.append(path)
    if not paths:
        raise HTTPException(400, "Choose at least one floor-plan image.")
    try:
        scans = [scan_floor(path) for path in paths]
    except Exception as error:  # unreadable image
        raise HTTPException(400, f"Could not read the images: {error}") from error
    if not any(scan.rooms for scan in scans):
        raise HTTPException(422, "No rooms found. The importer expects clean plans with coloured rooms.")
    photos = any(scan.photo for scan in scans)
    px_per_m, _ = default_scale(scans, width_m)
    try:
        labels = read_labels(scans, client())
    except (anthropic.APIError, TypeError):  # TypeError: the SDK found no API key
        labels = None  # still a usable building, just without room numbers
    building = assemble(scans, px_per_m, labels, name or None)
    apply_labels(building)  # hospital signs ('CT A0.33') name and type their rooms; other labels stay
    building["building"]["source_ref"] |= {"photos": photos, "size_guessed": photos and not width_m}
    building_id = unique_id(building["building"]["id"])
    building["building"]["id"] = building_id
    directory = IMPORTED / building_id
    directory.mkdir(parents=True)
    for path in paths:
        shutil.move(path, directory / path.name)
    shutil.rmtree(work, ignore_errors=True)
    (directory / "building.json").write_text(json.dumps(building, ensure_ascii=False), encoding="utf-8")
    return import_summary(building)


class ScaleRequest(BaseModel):
    width_m: float
    depth_m: float | None = None  # photos can be slightly off in proportion: let the user give both
    floor_heights_m: list[float] | None = None  # lowest floor first; stairs cost 2 m per metre climbed


@app.post("/api/import/{building_id}/scale")
def rescale(building_id: str, request: ScaleRequest) -> dict:
    """The user knows how wide the building really is: stretch every coordinate to match."""
    directory = building_paths().get(building_id)
    if directory is None or directory.parent != IMPORTED or request.width_m <= 0:
        raise HTTPException(404, "not an imported building")
    path = directory / "building.json"
    building = json.loads(path.read_text(encoding="utf-8"))
    set_dimensions(building, request.width_m, request.depth_m, request.floor_heights_m)
    path.write_text(json.dumps(building, ensure_ascii=False), encoding="utf-8")
    for session_id in [k for k, v in sessions.items() if v.map.data["building"]["id"] == building_id]:
        del sessions[session_id]
    return import_summary(building)


@app.get("/api/sessions/{session_id}/map")
def session_map(session_id: str, building_id: str) -> dict:
    return map_payload(session_for(session_id, building_id).map)


@app.get("/api/sessions/{session_id}/search")
def search(session_id: str, q: str = "", category: str = "", from_id: str = "") -> dict:
    session = session_for(session_id)
    bmap = session.map
    if category:
        ids = category_ids(bmap, category)
    else:
        ids = [result["id"] for result in bmap.search(q)] if q.strip() else []
    items = [item(bmap, element_id) for element_id in ids[: MAX_RESULTS * 2]]
    if from_id:
        origin = place_of(session, from_id)
        for entry in items:
            try:
                entry["distance_m"] = session.router.route(origin, place_of(session, entry["id"])).walk_m
            except ValueError:
                entry["distance_m"] = None
        items.sort(key=lambda entry: (entry["distance_m"] is None, entry["distance_m"] or 0))
    return {"results": items[:MAX_RESULTS]}


@app.get("/api/sessions/{session_id}/place/{element_id}")
def place(session_id: str, element_id: str) -> dict:
    session = session_for(session_id)
    bmap = session.map
    details = item(bmap, element_id)
    space_id = bmap.assets[element_id]["space_id"] if element_id in bmap.assets else element_id
    space = bmap.spaces[space_id]
    contents: dict[str, int] = {}
    for asset in bmap.assets.values():
        if asset["space_id"] == space_id and asset["category"] in ("machine", "equipment"):
            label = asset["type"].replace("_", " ")
            contents[label] = contents.get(label, 0) + 1
    doors = [o for o in bmap.openings.values() if space_id in o["space_ids"]]
    details |= {
        "room": {"id": space_id, "name": space["name"], "type": space["room_type"].replace("_", " ")},
        "area_m2": round(queries.area(bmap, space_id), 1),
        "contents": sorted(contents.items(), key=lambda pair: -pair[1])[:6],
        "widest_door_m": max((o["width_m"] for o in doors), default=None),
        "step_free": all(o["step_free"] for o in doors) if doors else True,
        "closed": space_id in bmap.closed,
    }
    return details


class RouteRequest(BaseModel):
    from_id: str
    to_id: str
    via_ids: list[str] = []
    mode: str = "walk"


@app.post("/api/sessions/{session_id}/route")
def route(session_id: str, request: RouteRequest) -> dict:
    session = session_for(session_id)
    step_free, min_width = MODES.get(request.mode, MODES["walk"])
    begin, goal = place_of(session, request.from_id), place_of(session, request.to_id)
    via = [place_of(session, element_id) for element_id in request.via_ids]
    try:
        result = session.router.route(begin, goal, step_free, min_width, via)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return route_payload(session.map, result, begin, goal, request.mode, via)


class ChatRequest(BaseModel):
    session_id: str
    building_id: str
    message: str
    location_id: str | None = None


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict:
    session = session_for(request.session_id, request.building_id)
    message = request.message
    if request.location_id:
        here = item(session.map, request.location_id)
        message += f"\n\n(The app's blue dot says I am at: {here['name']}, {here['floor_name']}.)"
    try:
        reply, view = session.ask(message)
    except anthropic.APIError as error:
        raise HTTPException(502, f"Claude API error: {error}") from error
    return {"reply": reply, "view": view, "map": map_payload(session.map)}
