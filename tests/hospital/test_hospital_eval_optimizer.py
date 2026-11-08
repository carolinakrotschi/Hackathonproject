"""Evaluator: when n does not fit, the max-count proposal must still satisfy overlap/inside/aisle rules."""
import itertools

from hospital import optimizer
from test_hospital_optimizer import AISLE, REACH_TYPES, aisle_region, door_points, footprints, room_polygon, standing_spots

from core.walk import footprint
from klinikum import ER


def test_infeasible_request_max_proposal_is_valid(hospital):
    proposal = optimizer.optimize_room(hospital.bmap, hospital.router, ER, "stretcher", 200)
    assert proposal["feasible"] is False
    assert 0 < proposal["max_count"] < 200
    after = proposal["after"]
    polygon = room_polygon(hospital.bmap, ER)
    shapes = footprints(after)
    for (a, sa), (b, sb) in itertools.combinations(shapes, 2):
        assert sa.intersection(sb).area < 1e-6, f"{a} overlaps {b}"
    for asset_id, shape in shapes:
        assert polygon.buffer(1e-6).contains(shape), f"{asset_id} outside room"
    region = aisle_region(polygon, [s for _, s in shapes], door_points(hospital.bmap, ER))
    for asset in after:
        if asset["type"] in REACH_TYPES:
            assert len(standing_spots(footprint(asset), region)) >= 3, f"{asset['id']} unreachable with {AISLE} m aisle"
