import copy
import itertools

import pytest
from core.walk import footprint, polygons
from hospital import optimizer
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from klinikum import ENTRANCE, ER

AISLE = 0.9
REACH_TYPES = {"stretcher", "bed"}


def room_polygon(bmap, space_id: str) -> Polygon:
    polygon = Polygon(bmap.spaces[space_id]["polygon"])
    return polygon if polygon.is_valid else polygon.buffer(0)


def door_points(bmap, space_id: str) -> list[Point]:
    return [Point(o["position"]) for o in bmap.openings.values() if space_id in o["space_ids"]]


def aisle_region(polygon: Polygon, shapes: list[Polygon], doors: list[Point]):
    """Where the centre of a person AISLE wide can stand, connected to a door."""
    half = AISLE / 2
    free = polygon.buffer(-half, join_style="mitre")
    if shapes:
        free = free.difference(unary_union([s.buffer(half, join_style="mitre") for s in shapes]))
    entries = unary_union([d.buffer(half + 0.3) for d in doors])
    return unary_union([part for part in polygons(free) if part.intersects(entries)])


def standing_spots(shape: Polygon, region) -> list[Point]:
    """Sample points around the item at a person's half width; return those inside the door-connected aisle."""
    spots = []
    for offset in (AISLE / 2 + 0.02, AISLE / 2 + 0.1, AISLE / 2 + 0.25):
        ring = shape.buffer(offset, join_style="mitre").exterior
        steps = max(16, int(ring.length / 0.05))
        spots += [ring.interpolate(i / steps, normalized=True) for i in range(steps)]
    inside = region.buffer(1e-6)
    return [p for p in spots if inside.contains(p)]


def footprints(assets: list[dict]) -> list[tuple[str, Polygon]]:
    return [(a["id"], footprint(a)) for a in assets]


@pytest.fixture
def er_proposal(hospital):
    return optimizer.optimize_room(hospital.bmap, hospital.router, ER, "stretcher", 4)


def test_er_four_stretchers_feasible(er_proposal):
    assert er_proposal["feasible"] is True
    assert er_proposal["max_count"] == 4
    assert er_proposal["requested"] == 4
    assert len(er_proposal["added"]) == 4
    assert all(a["type"] == "stretcher" and a["space_id"] == ER for a in er_proposal["added"])
    assert {"before", "after", "moves", "free_area_m2"} <= er_proposal.keys()
    assert er_proposal["free_area_m2"]["after"] < er_proposal["free_area_m2"]["before"]


def test_er_proposal_no_overlaps(er_proposal):
    shapes = footprints(er_proposal["after"])
    for (a_id, a), (b_id, b) in itertools.combinations(shapes, 2):
        assert a.intersection(b).area < 1e-6, f"{a_id} overlaps {b_id}"


def test_er_proposal_inside_room(hospital, er_proposal):
    polygon = room_polygon(hospital.bmap, ER).buffer(1e-6)
    for asset_id, shape in footprints(er_proposal["after"]):
        assert polygon.contains(shape), asset_id


def test_er_proposal_every_stretcher_reachable_with_aisle(hospital, er_proposal):
    polygon = room_polygon(hospital.bmap, ER)
    after = er_proposal["after"]
    region = aisle_region(polygon, [shape for _, shape in footprints(after)], door_points(hospital.bmap, ER))
    assert not region.is_empty
    for asset in after:
        if asset["type"] not in REACH_TYPES:
            continue
        spots = standing_spots(footprint(asset), region)
        assert len(spots) >= 3, f"{asset['id']} has no {AISLE} m aisle from a door ({len(spots)} spots)"
        assert all(polygon.contains(p) for p in spots)


def test_er_proposal_moves_only_movable(hospital, er_proposal):
    fixed = {a["id"] for a in hospital.bmap.assets.values() if a.get("movable") is False}
    moved = {m["asset_id"] for m in er_proposal["moves"]}
    assert not moved & fixed
    for move in er_proposal["moves"]:
        assert {"x", "y", "rotation_deg"} <= move["from"].keys()
        assert {"x", "y", "rotation_deg"} <= move["to"].keys()
    before = {a["id"]: a for a in er_proposal["before"]}
    for asset in er_proposal["after"]:
        if asset["id"] in fixed:
            assert asset["position"] == before[asset["id"]]["position"]


def test_optimize_is_pure(hospital):
    snapshot = copy.deepcopy(hospital.bmap.assets)
    optimizer.optimize_room(hospital.bmap, hospital.router, ER, "stretcher", 4)
    assert hospital.bmap.assets == snapshot


def test_too_many_reports_max_count(hospital):
    proposal = optimizer.optimize_room(hospital.bmap, hospital.router, ER, "stretcher", 200)
    assert proposal["feasible"] is False
    assert 4 <= proposal["max_count"] < 200
    assert len(proposal["added"]) == proposal["max_count"]
    shapes = footprints(proposal["after"])
    for (a_id, a), (b_id, b) in itertools.combinations(shapes, 2):
        assert a.intersection(b).area < 1e-6, f"{a_id} overlaps {b_id}"
    polygon = room_polygon(hospital.bmap, ER)
    region = aisle_region(polygon, [shape for _, shape in shapes], door_points(hospital.bmap, ER))
    for asset in proposal["after"]:
        if asset["type"] in REACH_TYPES:
            assert len(standing_spots(footprint(asset), region)) >= 3, asset["id"]


def test_unknown_equipment_type(hospital):
    with pytest.raises(ValueError):
        optimizer.optimize_room(hospital.bmap, hospital.router, ER, "piano", 1)


def test_apply_mutates_building(hospital, er_proposal):
    changed = optimizer.apply_proposal(hospital.bmap, hospital.router, er_proposal)
    added_ids = [a["id"] for a in er_proposal["added"]]
    assert set(added_ids) <= set(changed)
    for added in er_proposal["added"]:
        assert hospital.bmap.assets[added["id"]]["position"] == added["position"]
    for move in er_proposal["moves"]:
        assert hospital.bmap.assets[move["asset_id"]]["position"] == [move["to"]["x"], move["to"]["y"]]
    router = hospital.router
    for asset_id in added_ids:
        walk = router.route(router.place_for_space(ENTRANCE), router.place_for_asset(asset_id))
        assert walk.spaces[-1] == ER
    with pytest.raises(ValueError):
        optimizer.apply_proposal(hospital.bmap, hospital.router, er_proposal)
