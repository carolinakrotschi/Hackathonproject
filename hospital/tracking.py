"""BLE-style tracking: beacon sightings → tag positions.

A sighting says tag T heard beacon B with signal strength rssi (dBm) at time t. The floor is the majority floor of
the tag's sightings in the last WINDOW_S; the position is the weighted centroid of the strongest beacons on it."""

from collections import Counter

from hospital.state import Hospital, Tag

WINDOW_S = 10.0
STRONGEST_BEACONS = 3


def weight(rssi: float) -> float:
    # Free-space path loss: rssi = P0 - 20·log10(d), so 10^(rssi/20) is proportional to 1/d.
    return 10 ** (rssi / 20)


def ingest(h: Hospital, sightings: list[dict]) -> list[Tag]:
    now = h.now()
    touched: list[str] = []
    for sighting in sightings:
        if sighting["tag_id"] not in h.tags:
            raise KeyError(f"unknown tag {sighting['tag_id']}")
        if sighting["beacon_id"] not in h.beacons:
            raise KeyError(f"unknown beacon {sighting['beacon_id']}")
    for sighting in sightings:
        tag_id, beacon_id = sighting["tag_id"], sighting["beacon_id"]
        t = sighting.get("t")
        h.sightings.setdefault(tag_id, []).append(
            {"beacon_id": beacon_id, "rssi": float(sighting["rssi"]), "t": now if t is None else float(t)})
        if tag_id not in touched:
            touched.append(tag_id)
    updated = []
    for tag_id in touched:
        h.sightings[tag_id] = [s for s in h.sightings[tag_id] if s["t"] >= now - WINDOW_S]
        found = estimate(h, tag_id)
        if found:
            updated.append(h.set_position(tag_id, *found))
    return updated


def estimate(h: Hospital, tag_id: str) -> tuple[str, float, float] | None:
    now = h.now()
    recent = [s for s in h.sightings.get(tag_id, []) if s["t"] >= now - WINDOW_S]
    if not recent:
        return None
    floors = Counter(h.beacons[s["beacon_id"]]["floor_id"] for s in recent)
    floor_id = max(floors, key=lambda f: (floors[f], sum(weight(s["rssi"]) for s in recent
                                                         if h.beacons[s["beacon_id"]]["floor_id"] == f)))
    readings: dict[str, list[float]] = {}
    for s in recent:
        if h.beacons[s["beacon_id"]]["floor_id"] == floor_id:
            readings.setdefault(s["beacon_id"], []).append(s["rssi"])
    mean = {beacon_id: sum(values) / len(values) for beacon_id, values in readings.items()}
    strongest = sorted(mean, key=lambda beacon_id: (-mean[beacon_id], beacon_id))[:STRONGEST_BEACONS]
    weights = {beacon_id: weight(mean[beacon_id]) for beacon_id in strongest}
    total = sum(weights.values())
    x = sum(h.beacons[b]["x"] * w for b, w in weights.items()) / total
    y = sum(h.beacons[b]["y"] * w for b, w in weights.items()) / total
    return floor_id, round(x, 3), round(y, 3)


def snapshot(h: Hospital, kind: str | None = None) -> list[dict]:
    rows = []
    for tag in h.tags.values():
        if kind and tag.kind != kind:
            continue
        row = {"tag_id": tag.tag_id, "kind": tag.kind, "label": tag.label, "role": tag.role, "ref": tag.ref,
               "floor_id": tag.floor_id, "x": tag.x, "y": tag.y, "space_id": tag.space_id, "seen_at": tag.seen_at}
        if tag.kind == "asset":
            asset = h.bmap.assets.get(tag.ref)
            if asset:
                row |= {"floor_id": asset["floor_id"], "x": asset["position"][0], "y": asset["position"][1],
                        "space_id": asset["space_id"]}
            row["in_use"] = tag.ref in h.in_use
        rows.append(row)
    return rows
