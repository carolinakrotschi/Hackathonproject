import math

import pytest
from hospital import tracking

from klinikum import CAFE, F0, F1, NURSE_F1

TAG = "T-S04"


def beacons_on(hospital, floor_id: str, near: tuple[float, float], count: int) -> list[dict]:
    on_floor = [b for b in hospital.beacons.values() if b["floor_id"] == floor_id]
    return sorted(on_floor, key=lambda b: math.dist(near, (b["x"], b["y"])))[:count]


def weighted_centroid(readings: list[tuple[dict, float]]) -> tuple[float, float]:
    weights = [(beacon, 10 ** (rssi / 20)) for beacon, rssi in readings]
    total = sum(w for _, w in weights)
    return (sum(b["x"] * w for b, w in weights) / total, sum(b["y"] * w for b, w in weights) / total)


def test_sightings_place_tag_on_majority_floor_near_weighted_centroid(hospital, clock):
    tag = hospital.tags[TAG]
    assert tag.floor_id == F0
    target = hospital.bmap.anchor(NURSE_F1)
    near = beacons_on(hospital, F1, target, 4)
    assert len(near) == 4
    rssi = [-55.0, -61.0, -67.0, -85.0]
    stray = beacons_on(hospital, F0, target, 1)[0]
    sightings = [{"tag_id": TAG, "beacon_id": b["id"], "rssi": r} for b, r in zip(near, rssi, strict=True)]
    sightings.append({"tag_id": TAG, "beacon_id": stray["id"], "rssi": -40.0})

    updated = tracking.ingest(hospital, sightings)

    assert [t.tag_id for t in updated] == [TAG]
    expected = weighted_centroid(list(zip(near[:3], rssi[:3], strict=True)))
    floor_id, x, y = tracking.estimate(hospital, TAG)
    assert floor_id == F1
    assert math.dist((x, y), expected) <= 3.0
    assert tag.floor_id == F1
    assert math.dist((tag.x, tag.y), expected) <= 3.0
    assert tag.space_id == hospital.place_at(F1, tag.x, tag.y).space_id
    assert tag.seen_at == clock.t


def test_two_beacons_are_enough(hospital):
    target = hospital.bmap.anchor(CAFE)
    near = beacons_on(hospital, F0, target, 2)
    readings = [(near[0], -60.0), (near[1], -66.0)]
    tracking.ingest(hospital, [{"tag_id": TAG, "beacon_id": b["id"], "rssi": r} for b, r in readings])
    tag = hospital.tags[TAG]
    assert tag.floor_id == F0
    assert math.dist((tag.x, tag.y), weighted_centroid(readings)) <= 3.0


def test_stale_sightings_are_ignored(hospital, clock):
    before = (hospital.tags[TAG].floor_id, hospital.tags[TAG].x, hospital.tags[TAG].y)
    far = beacons_on(hospital, F1, (0.0, 0.0), 2)
    old = clock.t - tracking.WINDOW_S - 5
    updated = tracking.ingest(hospital, [{"tag_id": TAG, "beacon_id": b["id"], "rssi": -50.0, "t": old} for b in far])
    assert updated == []
    assert tracking.estimate(hospital, TAG) is None
    assert (hospital.tags[TAG].floor_id, hospital.tags[TAG].x, hospital.tags[TAG].y) == before


def test_unknown_tag_or_beacon_rejected(hospital):
    beacon_id = next(iter(hospital.beacons))
    with pytest.raises(KeyError):
        tracking.ingest(hospital, [{"tag_id": "T-NOPE", "beacon_id": beacon_id, "rssi": -60}])
    with pytest.raises(KeyError):
        tracking.ingest(hospital, [{"tag_id": TAG, "beacon_id": "B-NOPE", "rssi": -60}])


def test_pager_sightings_move_the_patient(hospital):
    patient = hospital.patient_by_pager("PG-12")
    target = hospital.bmap.anchor(CAFE)
    near = beacons_on(hospital, F0, target, 3)
    tracking.ingest(hospital, [{"tag_id": patient.pager_tag_id, "beacon_id": b["id"], "rssi": r}
                               for b, r in zip(near, (-58.0, -62.0, -66.0), strict=True)])
    pager, body = hospital.tags[patient.pager_tag_id], hospital.tags[patient.tag_id]
    assert (body.floor_id, body.x, body.y) == (pager.floor_id, pager.x, pager.y)
    assert hospital.place_of(patient).point == (pager.x, pager.y)


def test_snapshot_lists_all_kinds(hospital):
    rows = tracking.snapshot(hospital)
    assert {row["kind"] for row in rows} == {"patient", "pager", "asset", "staff"}
    assert len(rows) == len(hospital.tags)
    for row in rows:
        assert {"tag_id", "kind", "label", "floor_id", "x", "y", "space_id", "seen_at"} <= row.keys()
        assert row["floor_id"] in hospital.bmap.floors
        assert row["space_id"] in hospital.bmap.spaces
        assert row["x"] is not None and row["y"] is not None
        assert row["seen_at"] is not None
        if row["kind"] == "asset":
            assert row["in_use"] == (row["ref"] in hospital.in_use)
    assert {row["kind"] for row in tracking.snapshot(hospital, "asset")} == {"asset"}
