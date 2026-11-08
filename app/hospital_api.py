"""Hospital mode: pages and /api/hospital/* on top of the hospital package. One in-memory Hospital per process."""

import os
import re
import uuid
from pathlib import Path

import anthropic
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel

from hospital import agent, dispatch, finder, optimizer, pager_tools, rules, stops, tracking, tts, voice
from hospital.sim import Simulator
from hospital.state import IMPORTED, Hospital, available_buildings, building_dir

DEFAULT_BUILDING = "demo-campus"

PAGES = Path(__file__).resolve().parent / "static" / "hospital"

router = APIRouter()
state: dict = {"hospital": None, "sim": None}
proposals: dict[str, dict] = {}
agents: dict[tuple[int, str], "agent.PagerAgent"] = {}  # one conversation per (hospital, pager)
placements: dict[str, dict] = {}
screens: dict[str, dict] = {}  # what a pager's screen shows besides guidance (status line, highlight, demo walk)


def default_hospital() -> Hospital:
    """The campus (two buildings, staff pagers) unless HOSPITAL_BUILDING names another one, e.g. demo-klinikum."""
    h = Hospital.load(building_dir(os.environ.get("HOSPITAL_BUILDING", DEFAULT_BUILDING)))
    demo_start(h)
    return h


DEMO_START = {"PG-01": "emergency"}  # demo: these pagers start (and reset to) a room of this type
DEMO_NAMES = {"Room 2.14": "Cardiology"}  # demo: an upstairs goal with a name worth saying


def demo_start(h: Hospital) -> None:
    for space in h.bmap.spaces.values():
        space["name"] = DEMO_NAMES.get(space["name"], space["name"])
    for pager_id, room_type in DEMO_START.items():
        space_id = next((sid for sid, s in h.bmap.spaces.items() if s["room_type"] == room_type), None)
        holder = next((p for p in h.patients.values() if p.pager_id == pager_id), None)
        if space_id is None or holder is None:
            continue
        holder.appointment, holder.kind = None, "staff"  # nothing to show until it is told where to go
        h.home[holder.pager_tag_id] = (h.bmap.spaces[space_id]["floor_id"], *h.bmap.anchor(space_id))
        h.set_position(holder.pager_tag_id, *h.home[holder.pager_tag_id])


def hospital() -> Hospital:
    if state["hospital"] is None:
        state["hospital"] = default_hospital()
    return state["hospital"]


def set_hospital(h: Hospital) -> None:
    stop_sim()
    state["hospital"] = h
    agents.clear()
    proposals.clear()
    placements.clear()


def stop_sim() -> None:
    if state["sim"]:
        state["sim"].stop()
        state["sim"] = None


def pager_tag(h: Hospital, pager_id: str):
    try:
        return h.tags[h.patient_by_pager(pager_id).pager_tag_id]
    except KeyError as error:
        raise HTTPException(404, f"unknown pager {pager_id}") from error


def with_position(h: Hospital, pager_id: str, result: dict) -> dict:
    tag = pager_tag(h, pager_id)
    return result | {"position": {"floor_id": tag.floor_id, "x": tag.x, "y": tag.y, "space_id": tag.space_id},
                     "now": h.now()}


def guidance_payload(h: Hospital, pager_id: str) -> dict:
    pager_tag(h, pager_id)
    return with_position(h, pager_id, dispatch.guidance(h, pager_id)) | {"screen": screens.get(pager_id, {})}


def building_source(h: Hospital) -> str:
    return "imported" if h.root.parent == IMPORTED else "demo"


# ---------- pages ----------


@router.get("/hospital")
def leitstand_page() -> FileResponse:
    return FileResponse(PAGES / "index.html")


@router.get("/hospital/maps")
def maps_page() -> FileResponse:
    return FileResponse(PAGES / "maps.html")


@router.get("/hospital/pager")
def pager_chooser() -> FileResponse:
    return FileResponse(PAGES / "pager.html")


@router.get("/hospital/pager/{pager_id}", response_model=None)
def pager_page(pager_id: str, request: Request) -> FileResponse | RedirectResponse:
    """Short links for demos: /hospital/pager/1 -> PG-01, /hospital/pager/s1 -> PG-S01."""
    short = re.fullmatch(r"(s?)(\d{1,2})", pager_id, flags=re.IGNORECASE)
    if short:
        prefix = "S" if short.group(1) else ""
        target = f"/hospital/pager/PG-{prefix}{int(short.group(2)):02d}"
        return RedirectResponse(target + (f"?{request.url.query}" if request.url.query else ""), status_code=307)
    return FileResponse(PAGES / "pager.html")


@router.get("/hospital/visit")
def visit_page() -> FileResponse:
    return FileResponse(PAGES / "visit.html")


# ---------- map, pagers, tracking ----------


@router.get("/api/hospital/map")
def hospital_map() -> dict:
    from app.server import map_payload

    h = hospital()
    with h.lock:
        payload = map_payload(h.bmap)
        for space in payload["spaces"]:  # a campus says which building each space is in
            space["building"] = h.bmap.spaces[space["id"]].get("building")
        return payload | {
            "buildings": h.bmap.data.get("buildings", []),
            "beacons": list(h.beacons.values()),
            "patients": [{"id": p.id, "number": p.number, "name": p.name, "pager_id": p.pager_id, "room_id": p.room_id}
                         for p in h.patients.values() if p.kind == "patient"],
            "staff": [{"id": s.get("id") or s["tag_id"], "label": s["label"], "role": s.get("role", ""), "pager_id": s.get("pager_id"),
                       "tag_id": s["tag_id"]} for s in h.scenario.get("staff", [])],
            "building_source": building_source(h),
            "stop_categories": {key: {"label": c["label"], "dwell_s": c["dwell_s"]}
                                for key, c in stops.STOP_CATEGORIES.items()},
            "now": h.now(),
        }


@router.get("/api/hospital/pagers/{pager_id}/guidance")
def pager_guidance(pager_id: str) -> dict:
    h = hospital()
    with h.lock:
        return guidance_payload(h, pager_id)


@router.post("/api/hospital/pagers/{pager_id}/screen")
def pager_screen(pager_id: str, patch: dict) -> dict:
    """Mirror a pager's screen to every browser showing it (demo only: no checks)."""
    screens.setdefault(pager_id, {}).update(patch)
    return screens[pager_id]


class FindRequest(BaseModel):
    type: str


@router.post("/api/hospital/pagers/{pager_id}/find")
def pager_find(pager_id: str, request: FindRequest) -> dict:
    h = hospital()
    with h.lock:
        pager_tag(h, pager_id)
        try:
            result = dispatch.start_search(h, pager_id, request.type)
        except KeyError as error:
            raise HTTPException(422, str(error.args[0])) from error
        except LookupError as error:
            raise HTTPException(409, str(error)) from error
        return with_position(h, pager_id, result)


@router.delete("/api/hospital/pagers/{pager_id}/find")
def pager_stop_find(pager_id: str) -> dict:
    h = hospital()
    with h.lock:
        pager_tag(h, pager_id)
        return with_position(h, pager_id, dispatch.stop_search(h, pager_id))


class DestinationRequest(BaseModel):
    place: str


@router.post("/api/hospital/pagers/{pager_id}/destination")
def pager_go_to(pager_id: str, request: DestinationRequest) -> dict:
    h = hospital()
    with h.lock:
        pager_tag(h, pager_id)
        space_ids = voice.place_mention(h, request.place)
        space_id = voice.nearest_space(h, pager_id, space_ids) if space_ids else None
        if space_id is None:
            raise HTTPException(422, f"I don't know a reachable place called '{request.place}'.")
        return with_position(h, pager_id, dispatch.go_to(h, pager_id, space_id))


@router.delete("/api/hospital/pagers/{pager_id}/destination")
def pager_stop_go_to(pager_id: str) -> dict:
    h = hospital()
    with h.lock:
        pager_tag(h, pager_id)
        return with_position(h, pager_id, dispatch.stop_go_to(h, pager_id))


class WalkRequest(BaseModel):
    metres: float = 2.0


@router.post("/api/hospital/pagers/{pager_id}/walk")
def pager_walk(pager_id: str, request: WalkRequest) -> dict:
    """Simulated walking for demos (the L key on the pager)."""
    h = hospital()
    with h.lock:
        pager_tag(h, pager_id)
        return with_position(h, pager_id, dispatch.walk(h, pager_id, max(0.0, min(request.metres, 50.0))))


@router.delete("/api/hospital/pagers/{pager_id}/walk")
def pager_walk_reset(pager_id: str) -> dict:
    """A clean slate (the R key on the pager): back at the start, no route, closures reopened, a fresh conversation."""
    h = hospital()
    agents.pop((id(h), pager_id), None)
    with h.lock:
        pager_tag(h, pager_id)
        return with_position(h, pager_id, dispatch.walk_reset(h, pager_id))


@router.post("/api/hospital/pagers/{pager_id}/demo-reset")
def pager_demo_reset(pager_id: str) -> dict:
    """Demo: back to the start point with no destination, stops, search or closures, and a fresh conversation."""
    h = hospital()
    with h.lock:
        pager_tag(h, pager_id)
        holder = h.patient_by_pager(pager_id)
        holder.destination, holder.search, holder.stops, holder.stops_ordered = None, None, [], False
        h.bmap.closed.clear()
        pager_tools.replan_everyone(h)
        agents.pop((id(h), pager_id), None)
        screens.pop(pager_id, None)
        return with_position(h, pager_id, dispatch.walk_reset(h, pager_id))


class DirectionsRequest(BaseModel):
    origin: str  # a space or asset id from /api/hospital/map, or a name
    destination: str
    step_free: bool = False
    via: list[str] = []


class AskRequest(BaseModel):
    text: str
    session: str  # one conversation per browser tab
    page: str = ""  # what the map shows, e.g. "Showing Floor 2. Selected: Cardiology (Floor 2)."


@router.post("/api/hospital/maps/ask")
def maps_ask(request: AskRequest) -> dict:
    """The maps page's assistant (levels 1-4 without a pager): what to say, what to show, a route to draw, and
    whether the plan changed."""
    if not agent.available():
        raise HTTPException(503, "The assistant needs an Anthropic API key on the server.")
    h = hospital()
    key = (id(h), f"maps:{request.session[:64]}")
    if key not in agents:
        agents[key] = agent.MapsAgent(h, anthropic.Anthropic())
    maps = agents[key]
    maps.page = request.page[:300]
    try:
        say, view = maps.ask(request.text)
    except (anthropic.APIError, RuntimeError) as error:
        agents.pop(key, None)
        raise HTTPException(502, f"The assistant is unavailable ({error.__class__.__name__}).") from error
    print(f"maps ask: {request.text!r} -> ok={maps.acted}: {say[:100]}", flush=True)
    return {"say": say, "ok": maps.acted, "view": view, "route": maps.route, "changed": maps.changed}


@router.post("/api/hospital/directions")
def hospital_directions(request: DirectionsRequest) -> dict:
    """A to B for the maps page, with the pager's planner (lifts, stairs, closures); no pager involved."""
    h = hospital()
    with h.lock:
        try:
            return pager_tools.directions(h, request.origin, request.destination, request.step_free, request.via)
        except pager_tools.PagerError as error:
            raise HTTPException(422, str(error)) from error


@router.get("/api/hospital/pagers/{pager_id}/where")
def pager_where(pager_id: str) -> dict:
    h = hospital()
    with h.lock:
        pager_tag(h, pager_id)
        return voice.where(h, pager_id)


class VoiceRequest(BaseModel):
    text: str


def pager_agent(h: Hospital, pager_id: str) -> agent.PagerAgent:
    key = (id(h), pager_id)
    if key not in agents:
        agents[key] = agent.PagerAgent(h, pager_id, anthropic.Anthropic())
    return agents[key]


@router.post("/api/hospital/pagers/{pager_id}/voice")
def pager_voice(pager_id: str, request: VoiceRequest) -> dict:
    """Claude (hospital/agent.py) when credentials are set, the phrase rules (hospital/voice.py) otherwise or on error.
    The hospital lock is held per tool call, not while Claude thinks, so other pagers keep updating."""
    h = hospital()
    with h.lock:
        pager_tag(h, pager_id)
    result = None
    if agent.available():
        try:
            pager = pager_agent(h, pager_id)
            say, view = pager.ask(request.text)
            result = {"heard": request.text, "intent": "agent", "ok": pager.acted, "say": say, "view": view}
        except (anthropic.APIError, RuntimeError) as error:
            agents.pop((id(h), pager_id), None)
            print(f"voice {pager_id}: agent failed ({error}); using phrase rules", flush=True)
    if result is None:
        with h.lock:
            result = voice.handle(h, pager_id, request.text)
    print(f"voice {pager_id}: {request.text!r} -> {result['intent']} ok={result['ok']}: {result['say'][:100]}", flush=True)
    return result


@router.get("/api/hospital/voice/tools")
def voice_tools(request: Request) -> dict:
    h = hospital()
    with h.lock:
        return {"tools": voice.tools_for(h), "base_url_hint": str(request.base_url).rstrip("/")}


class PositionRequest(BaseModel):
    space_id: str | None = None
    floor_id: str | None = None
    x: float | None = None
    y: float | None = None


@router.post("/api/hospital/pagers/{pager_id}/position")
def pager_position(pager_id: str, request: PositionRequest) -> dict:
    h = hospital()
    with h.lock:
        tag = pager_tag(h, pager_id)
        if request.space_id:
            if request.space_id not in h.bmap.spaces:
                raise HTTPException(404, f"unknown space {request.space_id}")
            floor_id = h.bmap.spaces[request.space_id]["floor_id"]
            x, y = h.bmap.anchor(request.space_id)
        elif request.floor_id and request.x is not None and request.y is not None:
            if request.floor_id not in h.bmap.floors:
                raise HTTPException(404, f"unknown floor {request.floor_id}")
            floor_id, x, y = request.floor_id, request.x, request.y
        else:
            raise HTTPException(422, "Give space_id or floor_id, x and y.")
        h.set_position(tag.tag_id, floor_id, x, y)
        return guidance_payload(h, pager_id)


class StopsRequest(BaseModel):
    stops: list[dict]
    ordered: bool = False
    dry_run: bool = False


@router.post("/api/hospital/pagers/{pager_id}/stops")
def pager_stops(pager_id: str, request: StopsRequest) -> dict:
    h = hospital()
    with h.lock:
        try:
            return stops.set_stops(h, pager_id, request.stops, request.ordered, request.dry_run)
        except KeyError as error:
            raise HTTPException(404, f"unknown pager or stop {error}") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error


@router.delete("/api/hospital/pagers/{pager_id}/stops")
def pager_clear_stops(pager_id: str) -> dict:
    h = hospital()
    with h.lock:
        try:
            stops.clear_stops(h, pager_id)
        except KeyError as error:
            raise HTTPException(404, f"unknown pager {pager_id}") from error
        return {"stops": []}


class Sighting(BaseModel):
    tag_id: str
    beacon_id: str
    rssi: float
    t: float | None = None


@router.post("/api/hospital/sightings")
def sightings(request: list[Sighting]) -> dict:
    h = hospital()
    with h.lock:
        try:
            updated = tracking.ingest(h, [s.model_dump() for s in request])
        except KeyError as error:
            raise HTTPException(422, str(error.args[0])) from error
        touched = {tag.tag_id for tag in updated}
        return {"updated": [row for row in tracking.snapshot(h) if row["tag_id"] in touched]}


@router.get("/api/hospital/tracking")
def tracking_snapshot(kind: str | None = None) -> list[dict]:
    h = hospital()
    with h.lock:
        return tracking.snapshot(h, kind or None)


# ---------- assets, visitors, rooms ----------


@router.get("/api/hospital/assets/nearest")
def nearest_asset(type: str, from_ref: str = Query(alias="from"), step_free: bool = False) -> dict:
    h = hospital()
    with h.lock:
        try:
            return finder.nearest_asset(h, type, from_ref, step_free)
        except KeyError as error:
            raise HTTPException(404, f"unknown place {from_ref}") from error
        except LookupError as error:
            raise HTTPException(409, str(error)) from error


class InUseRequest(BaseModel):
    in_use: bool


@router.get("/api/hospital/assets/in_use")
def assets_in_use() -> dict:
    h = hospital()
    with h.lock:
        return {"in_use": sorted(h.in_use)}


@router.patch("/api/hospital/assets/{asset_id}")
def asset_in_use(asset_id: str, request: InUseRequest) -> dict:
    h = hospital()
    with h.lock:
        try:
            return finder.set_in_use(h, asset_id, request.in_use)
        except KeyError as error:
            raise HTTPException(404, f"unknown asset {asset_id}") from error


@router.get("/api/hospital/visit")
def visit(patient: str = "", code: str = "") -> dict:
    h = hospital()
    with h.lock:
        result = finder.visit(h, patient, code)
    if result is None:
        raise HTTPException(403, "Not found or wrong code")
    return result


@router.get("/api/hospital/rooms/{space_id}/options")
def room_options(space_id: str) -> dict:
    h = hospital()
    with h.lock:
        if space_id not in h.bmap.spaces:
            raise HTTPException(404, f"unknown space {space_id}")
        return rules.options(h.bmap, space_id)


class AddRequest(BaseModel):
    type: str
    count: int = 1


class OptimizeRequest(BaseModel):
    add: AddRequest


@router.post("/api/hospital/rooms/{space_id}/optimize")
def optimize(space_id: str, request: OptimizeRequest) -> dict:
    h = hospital()
    with h.lock:
        if space_id not in h.bmap.spaces:
            raise HTTPException(404, f"unknown space {space_id}")
        try:
            proposal = optimizer.optimize_room(h.bmap, h.router, space_id, request.add.type, request.add.count)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        proposal_id = uuid.uuid4().hex
        proposals[proposal_id] = proposal
        return proposal | {"proposal_id": proposal_id}


class ApplyRequest(BaseModel):
    proposal_id: str


@router.post("/api/hospital/rooms/{space_id}/apply")
def apply(space_id: str, request: ApplyRequest) -> dict:
    h = hospital()
    with h.lock:
        if space_id not in h.bmap.spaces:
            raise HTTPException(404, f"unknown space {space_id}")
        proposal = proposals.get(request.proposal_id)
        if proposal is None or proposal["space_id"] != space_id:
            raise HTTPException(409, "unknown or expired proposal, optimize again")
        try:
            changed = optimizer.apply_proposal(h.bmap, h.router, proposal)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        del proposals[request.proposal_id]
        for patient in h.patients.values():
            patient.plan = None
        return {"changed": changed}


# ---------- history and insights ----------


@router.get("/api/hospital/heatmap")
def heatmap(floor_id: str, tag: str | None = None, kind: str | None = None, role: str | None = None,
            since: float | None = None, until: float | None = None,
            cell_m: float = Query(0.5, ge=0.1, le=5.0)) -> dict:
    h = hospital()
    with h.lock:
        if floor_id not in h.bmap.floors:
            raise HTTPException(404, f"unknown floor {floor_id}")
        return h.history.heatmap(h.bmap, floor_id, cell_m, tag_id=tag or None, kind=kind or None, role=role or None,
                                 since=since, until=until)


@router.get("/api/hospital/history/people")
def history_people() -> dict:
    h = hospital()
    with h.lock:
        return {"people": h.history.people()}


@router.get("/api/hospital/insights/routines")
def insight_routines(tag: str | None = None, role: str | None = None) -> dict:
    from hospital import insights

    h = hospital()
    with h.lock:
        return {"routines": insights.routines(h, tag_id=tag or None, role=role or None)}


@router.get("/api/hospital/insights/assets")
def insight_assets() -> dict:
    from hospital import insights

    h = hospital()
    with h.lock:
        return {"assets": insights.movable_assets(h)}


class PlacementRequest(BaseModel):
    asset_id: str


@router.post("/api/hospital/insights/placement")
def insight_placement(request: PlacementRequest) -> dict:
    from hospital import insights

    h = hospital()
    with h.lock:
        if request.asset_id not in h.bmap.assets:
            raise HTTPException(404, f"unknown asset {request.asset_id}")
        try:
            result = insights.placement(h, request.asset_id)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        proposal_id = None
        if result.get("proposal") is not None:
            proposal_id = uuid.uuid4().hex
            placements[proposal_id] = result
        return result | {"proposal_id": proposal_id}


@router.post("/api/hospital/insights/placement/apply")
def insight_placement_apply(request: ApplyRequest) -> dict:
    from hospital import insights

    h = hospital()
    with h.lock:
        result = placements.get(request.proposal_id)
        if result is None:
            raise HTTPException(409, "unknown or expired proposal, ask for a placement again")
        try:
            changed = insights.apply_placement(h, result)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        del placements[request.proposal_id]
        return {"changed": changed}


# ---------- buildings ----------


@router.get("/api/hospital/buildings")
def buildings() -> dict:
    h = hospital()
    return {"current": h.root.name, "source": building_source(h), "buildings": available_buildings()}


class BuildingRequest(BaseModel):
    building_id: str


@router.post("/api/hospital/buildings/select")
def select_building(request: BuildingRequest) -> dict:
    try:
        directory = building_dir(request.building_id)
    except KeyError as error:
        raise HTTPException(404, f"unknown building {request.building_id}") from error
    try:
        h = Hospital.load(directory)
    except Exception as error:  # a broken import must not take the running hospital down
        raise HTTPException(422, f"Could not load building: {error}") from error
    set_hospital(h)
    return {"current": directory.name, "source": building_source(h), "running": False, "now": h.now()}


# ---------- lifts, simulator, reset ----------


@router.get("/api/hospital/tts")
def speech(text: str = Query(min_length=1, max_length=tts.MAX_CHARS)) -> Response:
    """Pager voice as MP3; 503 tells the pager to fall back to the browser voice."""
    try:
        audio = tts.speech(text)
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error
    return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/hospital/lifts")
def lifts() -> dict:
    h = hospital()
    with h.lock:
        return {"lifts": [{"id": cid, "name": h.bmap.connectors[cid]["name"], "slots": h.lifts.slots(cid)}
                          for cid in h.lifts.lifts()],
                "now": h.now()}


class SimRequest(BaseModel):
    factor: float = 30.0


@router.post("/api/hospital/sim/start")
def sim_start(request: SimRequest | None = None) -> dict:
    factor = request.factor if request else 30.0
    if factor <= 0:
        raise HTTPException(422, "factor must be > 0")
    stop_sim()
    state["sim"] = Simulator(hospital(), factor=factor)
    state["sim"].start()
    return {"running": True, "factor": factor}


@router.post("/api/hospital/sim/stop")
def sim_stop() -> dict:
    stop_sim()
    return {"running": False}


@router.post("/api/hospital/reset")
def reset() -> dict:
    current = state["hospital"]
    stop_sim()
    fresh = Hospital.load(current.root) if current else default_hospital()
    demo_start(fresh)
    set_hospital(fresh)
    return {"running": False, "now": state["hospital"].now()}
