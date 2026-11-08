"""Lift timetable: who rides which lift when. One person per trip, so a queue at a lift becomes a sequence of
non-overlapping slots instead of a crowd. Empty return trips are ignored."""

from core.building_map import LIFT, BuildingMap

LIFT_BOARD_S = 20.0
LIFT_PER_FLOOR_S = 6.0


class LiftScheduler:
    def __init__(self, bmap: BuildingMap) -> None:
        self.map = bmap
        self.levels = {floor_id: floor["level"] for floor_id, floor in bmap.floors.items()}
        self.schedule: dict[str, list[dict]] = {connector_id: [] for connector_id in self.lifts()}

    def lifts(self) -> list[str]:
        return [connector_id for connector_id, connector in self.map.connectors.items() if connector["kind"] == LIFT]

    def trip_seconds(self, connector_id: str, from_floor: str, to_floor: str) -> float:
        served = self.map.connectors[connector_id]["floor_ids"]
        if from_floor not in served or to_floor not in served:
            raise ValueError(f"{connector_id} does not serve {from_floor} and {to_floor}.")
        return LIFT_BOARD_S + LIFT_PER_FLOOR_S * abs(self.levels[to_floor] - self.levels[from_floor])

    def earliest(self, connector_id: str, ready_at: float, duration: float) -> float:
        start = ready_at
        for slot in self.schedule[connector_id]:
            if slot["end"] <= start:
                continue
            if slot["start"] >= start + duration:
                break
            start = slot["end"]
        return start

    def reserve(self, connector_id: str, holder: str, start: float, duration: float, from_floor: str, to_floor: str) -> dict:
        slots = self.schedule[connector_id]
        end = start + duration
        clash = next((s for s in slots if s["start"] < end and start < s["end"]), None)
        if clash:
            raise ValueError(f"{connector_id} is taken by {clash['holder']} from {clash['start']} to {clash['end']}.")
        slot = {"connector_id": connector_id, "holder": holder, "start": start, "end": end,
                "from_floor": from_floor, "to_floor": to_floor}
        slots.append(slot)
        slots.sort(key=lambda s: s["start"])
        return dict(slot)

    def release(self, holder: str) -> None:
        for connector_id, slots in self.schedule.items():
            self.schedule[connector_id] = [s for s in slots if s["holder"] != holder]

    def slots(self, connector_id: str | None = None) -> list[dict]:
        chosen = [self.schedule[connector_id]] if connector_id else self.schedule.values()
        return sorted((dict(s) for slots in chosen for s in slots), key=lambda s: (s["start"], s["connector_id"]))
