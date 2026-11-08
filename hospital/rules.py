"""Room rules: which asset types belong in which room type, how many, and why (English, for UI and voice).
A plausible table kept in code, not a norm check."""

import math

from core.building_map import BuildingMap
from core.queries import area


class RuleError(ValueError):
    pass


ASSET_LABELS = {
    "bed": "Bed", "stretcher": "Stretcher", "wheelchair": "Wheelchair", "chair": "Chair",
    "infusion_pump": "Infusion pump", "monitor": "Monitor", "ultrasound": "Ultrasound unit",
    "ventilator": "Ventilator", "cabinet": "Cabinet", "shelf": "Shelf", "desk": "Desk",
    "table": "Table", "bench": "Bench", "counter": "Counter", "therapy_table": "Therapy table",
    "coffee_machine": "Coffee machine", "vending_machine": "Vending machine", "water_dispenser": "Water dispenser",
    "xray": "X-ray unit", "ct_scanner": "CT scanner",
    "mri_scanner": "MRI scanner", "operating_table": "Operating table", "anaesthesia_machine": "Anaesthesia machine",
    "defibrillator": "Defibrillator",
}
ASSET_PLURALS = {
    "bed": "Beds", "stretcher": "Stretchers", "wheelchair": "Wheelchairs", "chair": "Chairs",
    "infusion_pump": "Infusion pumps", "monitor": "Monitors", "ultrasound": "Ultrasound units",
    "ventilator": "Ventilators", "cabinet": "Cabinets", "shelf": "Shelves", "desk": "Desks",
    "table": "Tables", "bench": "Benches", "counter": "Counters", "therapy_table": "Therapy tables",
    "coffee_machine": "Coffee machines", "vending_machine": "Vending machines", "water_dispenser": "Water dispensers",
    "xray": "X-ray units", "ct_scanner": "CT scanners",
    "mri_scanner": "MRI scanners", "operating_table": "Operating tables", "anaesthesia_machine": "Anaesthesia machines",
    "defibrillator": "Defibrillators",
}
# Installed with the room, never planned by the optimiser.
FIXED = {"xray", "ct_scanner", "mri_scanner", "operating_table"}
PLANNABLE = tuple(t for t in ASSET_LABELS if t not in FIXED)
SMALL_MOBILE = {"chair": None, "wheelchair": None, "infusion_pump": None}

ROOM_LABELS = {
    "bathroom": "Bathroom", "cafe": "Cafeteria", "corridor": "Corridor", "emergency": "Emergency", "kiosk": "Kiosk",
    "lab": "Lab", "lift": "Lift", "lobby": "Entrance hall", "lounge": "Lounge",
    "nurse_station": "Nurses' station", "office": "Office", "patient_room": "Patient room", "pharmacy": "Pharmacy",
    "radiology": "Radiology", "reception": "Reception", "resuscitation": "Resus room", "stairwell": "Stairwell",
    "storage": "Store", "therapy": "Therapy room", "toilet": "Toilet", "treatment_bay": "Treatment bay",
    "triage": "Triage", "utility": "Utility room", "ward": "Ward corridor",
    "waiting": "Waiting area", "consulting": "Consulting room", "operating_theatre": "Operating theatre",
    "recovery": "Recovery", "walkway": "Walkway",
}
ROOM_IN = {
    "bathroom": "in a bathroom", "cafe": "in the cafeteria", "corridor": "in the corridor",
    "emergency": "in the emergency department", "kiosk": "in the kiosk", "lab": "in the lab", "lift": "in the lift",
    "lobby": "in the entrance hall", "lounge": "in the lounge", "nurse_station": "in the nurses' station",
    "office": "in an office", "patient_room": "in a patient room", "pharmacy": "in the pharmacy",
    "radiology": "in radiology", "reception": "at reception", "resuscitation": "in the resus room",
    "stairwell": "in the stairwell", "storage": "in the store", "therapy": "in the therapy room",
    "toilet": "in a toilet", "treatment_bay": "in a treatment bay", "triage": "in triage",
    "utility": "in the utility room", "ward": "in the ward corridor",
    "waiting": "in the waiting area", "consulting": "in a consulting room", "operating_theatre": "in theatre",
    "recovery": "in recovery", "walkway": "on the walkway",
}

LYING = ("bed", "stretcher")
DRINKS = ("coffee_machine", "vending_machine", "water_dispenser")
ESCAPE_ROUTE = "Corridors are escape routes; beds and stretchers must not stand there permanently."
NO_FOOD = "For hygiene reasons there are no drinks or snacks here."


def rule(allowed: dict, note: str, m2_per_item: dict | None = None, why: dict | None = None) -> dict:
    return {"allowed": allowed, "m2_per_item": m2_per_item or {}, "note": note, "why": why or {}}


ROOM_RULES = {
    "patient_room": rule(
        {"bed": 3, "chair": 4, "wheelchair": 1, "infusion_pump": 3, "monitor": 3, "ventilator": 1, "cabinet": 3,
         "shelf": 2, "table": 1},
        "At least 8 m² per bed, at most 3 beds.", {"bed": 8.0},
        {"stretcher": "In a patient room, patients lie in a bed.", **dict.fromkeys(DRINKS, NO_FOOD)}),
    "emergency": rule(
        {"stretcher": None, "bed": None, "wheelchair": None, "chair": None, "infusion_pump": None, "monitor": None,
         "ultrasound": 2, "ventilator": 2, "cabinet": None, "shelf": None, "desk": 2, "table": 2, "counter": 1},
        "At least 8 m² per stretcher or bed.", {"stretcher": 8.0, "bed": 8.0}, dict.fromkeys(DRINKS, NO_FOOD)),
    "treatment_bay": rule(
        {"stretcher": 1, "chair": 2, "wheelchair": 1, "infusion_pump": 2, "monitor": 1, "ultrasound": 1,
         "ventilator": 1, "cabinet": 2, "shelf": 1},
        "A treatment bay has exactly one stretcher.", None,
        {"bed": "Treatment bays get a stretcher, not a bed.", **dict.fromkeys(DRINKS, NO_FOOD)}),
    "resuscitation": rule(
        {"stretcher": 1, "monitor": 2, "ventilator": 2, "infusion_pump": 4, "ultrasound": 1, "cabinet": 4, "shelf": 2},
        "The resus room stays clear for one patient.", None,
        {"bed": "In the resus room, patients are treated on the stretcher.", **dict.fromkeys(DRINKS, NO_FOOD)}),
    "triage": rule(
        {"stretcher": 2, "chair": 8, "wheelchair": 2, "desk": 1, "monitor": 1, "cabinet": 2, "table": 1, "bench": 2},
        "Triage: few stretchers, seats for waiting.", None, dict.fromkeys(DRINKS, NO_FOOD)),
    "radiology": rule(
        {"stretcher": 1, "wheelchair": 1, "chair": 4, "desk": 2, "monitor": 2, "cabinet": 2, "shelf": 2,
         "ultrasound": 1, "bench": 2, "counter": 1, "xray": 1, "ct_scanner": 1},
        "One stretcher for transfers, otherwise equipment and workstations.", None,
        {"bed": "No beds stand permanently in radiology.", **dict.fromkeys(DRINKS, NO_FOOD)}),
    "storage": rule(
        {"stretcher": None, "bed": None, "wheelchair": None, "infusion_pump": None, "monitor": None,
         "ultrasound": None, "ventilator": None, "cabinet": None, "shelf": None, "chair": None, "table": None},
        "Parking space for equipment, beds and stretchers.", None, dict.fromkeys(DRINKS, NO_FOOD)),
    "utility": rule(
        {"cabinet": None, "shelf": None, "infusion_pump": None, "wheelchair": 2, "table": 1},
        "Utility room for supplies, no patient places.", None, dict.fromkeys(DRINKS, NO_FOOD)),
    "ward": rule(
        {"wheelchair": 4, "chair": 8, "bench": 4, "water_dispenser": 2, "vending_machine": 2, "coffee_machine": 1},
        "Ward corridor: alcove furniture only, the escape route stays clear.", None,
        dict.fromkeys(LYING, ESCAPE_ROUTE)),
    "corridor": rule(
        {"wheelchair": 4, "chair": 8, "bench": 4, "water_dispenser": 2, "vending_machine": 2, "coffee_machine": 1},
        "Corridor: alcove furniture only, the escape route stays clear.", None, dict.fromkeys(LYING, ESCAPE_ROUTE)),
    "lobby": rule(
        {"wheelchair": None, "chair": None, "bench": None, "table": None, "counter": 2, "vending_machine": 3,
         "coffee_machine": 2, "water_dispenser": 2},
        "Entrance hall: seating, wheelchairs and drinks.", None,
        dict.fromkeys(LYING, "No patients are kept lying in the entrance hall.")),
    "reception": rule(
        {"desk": 4, "chair": None, "cabinet": 4, "shelf": 2, "counter": 2, "wheelchair": 4, "water_dispenser": 1,
         "bench": 2},
        "Reception: workstations and waiting area.", None),
    "cafe": rule(
        {"chair": None, "table": None, "bench": None, "counter": 2, "coffee_machine": 3, "vending_machine": 3,
         "water_dispenser": 2, "shelf": 2, "cabinet": 2},
        "Cafeteria: tables, chairs and drinks.", None,
        dict.fromkeys(LYING, "The cafeteria is for eating, not for treatment.")),
    "kiosk": rule(
        {"shelf": None, "cabinet": 4, "counter": 1, "vending_machine": 2, "coffee_machine": 1, "chair": 2,
         "table": 1},
        "Kiosk: shelves and sales.", None),
    "lounge": rule(
        {"chair": None, "table": None, "bench": 2, "coffee_machine": 2, "vending_machine": 2, "water_dispenser": 2,
         "cabinet": 2, "shelf": 2, "wheelchair": 2},
        "Lounge: seating and drinks.", None),
    "nurse_station": rule(
        {"desk": None, "chair": None, "cabinet": None, "shelf": None, "table": 2, "monitor": 4, "infusion_pump": None,
         "wheelchair": 2, "counter": 1, "coffee_machine": 1, "water_dispenser": 1},
        "Nurses' station: workstations, supplies and a coffee machine.", None),
    "office": rule(
        {"desk": None, "chair": None, "cabinet": None, "shelf": None, "table": 2, "coffee_machine": 1,
         "water_dispenser": 1},
        "At least 8 m² per desk.", {"desk": 8.0}),
    "pharmacy": rule(
        {"shelf": None, "cabinet": None, "desk": 2, "chair": 4, "table": 2, "counter": 2},
        "Pharmacy: shelves and dispensing.", None, dict.fromkeys(DRINKS, NO_FOOD)),
    "lab": rule(
        {"table": None, "desk": 4, "chair": None, "cabinet": None, "shelf": None},
        "Lab: workbenches, no food.", None,
        dict.fromkeys(DRINKS, "Eating and drinking are not allowed in the lab.")),
    "therapy": rule(
        {"therapy_table": 4, "stretcher": 2, "chair": None, "table": 4, "wheelchair": 4, "cabinet": None,
         "shelf": None, "bench": 2},
        "Therapy room: therapy tables and exercise space.", None),
    "toilet": rule({}, "Toilets stay clear, sanitary fittings only.", None),
    "bathroom": rule({"chair": 1}, "Only a shower chair is planned in a bathroom.", None),
    "lift": rule({}, "Lifts stay clear.", None),
    "stairwell": rule({}, "Stairwells are escape routes and stay clear.", None),
}
UNKNOWN_RULE = rule(SMALL_MOBILE, "Unknown room type: small, mobile items only.", None)


def rule_for(room_type: str) -> dict:
    return ROOM_RULES.get(room_type, UNKNOWN_RULE)


def label(asset_type: str, n: int = 1) -> str:
    names = ASSET_LABELS if n == 1 else ASSET_PLURALS
    return names.get(asset_type, asset_type.replace("_", " "))


def noun(asset_type: str, n: int = 1) -> str:
    """label() for use mid-sentence: lower-case unless it starts with an abbreviation (CT, X-ray)."""
    text = label(asset_type, n)
    return text if text[1:2].isupper() or text[1:2] == "-" else text[:1].lower() + text[1:]


def room_label(room_type: str) -> str:
    return ROOM_LABELS.get(room_type, room_type.replace("_", " ").capitalize())


def assess(bmap: BuildingMap, space_id: str, asset_type: str, ignore: str | None = None) -> dict:
    """{permitted, existing, max_count (total, None: only space limits), can_add (None: likewise), reason}."""
    room_type = bmap.spaces[space_id]["room_type"]
    rule_ = rule_for(room_type)
    existing = sum(1 for a in bmap.assets.values()
                   if a["space_id"] == space_id and a["type"] == asset_type and a["id"] != ignore)
    found = {"permitted": asset_type in rule_["allowed"], "existing": existing, "max_count": 0, "can_add": 0}
    if not found["permitted"]:
        where = ROOM_IN.get(room_type, f"in a room of type {room_label(room_type).lower()}")
        why = rule_["why"].get(asset_type) or ("" if rule_["allowed"] and rule_ is not UNKNOWN_RULE else rule_["note"])
        return found | {"reason": f"No {noun(asset_type, 2)} are planned {where}. {why}".strip()}
    if asset_type in FIXED:
        return found | {"max_count": existing,
                        "reason": f"{label(asset_type, 2)} are built in and are not planned."}
    room = room_label(room_type)
    limit = rule_["allowed"][asset_type]
    per_item = rule_["m2_per_item"].get(asset_type)
    m2 = area(bmap, space_id)
    by_area = math.floor(m2 / per_item + 1e-6) if per_item else None
    if limit is None and by_area is None:
        return found | {"max_count": None, "can_add": None,
                        "reason": f"{room}: {noun(asset_type, 2)} allowed, only the space limits the number."}
    cap = min(c for c in (limit, by_area) if c is not None)
    more = max(0, cap - existing)
    text = f"{room}: at most {cap} {noun(asset_type, cap)}"
    if by_area is not None and (limit is None or by_area < limit):
        text = (f"{room} with {m2:.0f} m²: at most {cap} {noun(asset_type, cap)} "
                f"at {per_item:g} m² per {noun(asset_type)}")
    if existing:
        text += f", {existing} already there"
    text += f" – {more} more possible." if more else " – no more are planned."
    return found | {"max_count": cap, "can_add": more, "reason": text}


def allowed(bmap: BuildingMap, space_id: str, asset_type: str) -> tuple[bool, int | None, str]:
    """(ok, how many more may be added (None: only space limits it), reason)."""
    found = assess(bmap, space_id, asset_type)
    return found["permitted"] and found["can_add"] != 0, found["can_add"], found["reason"]


def may_stand(bmap: BuildingMap, space_id: str, asset_type: str, asset_id: str | None = None) -> tuple[bool, str]:
    """For moving an existing object: asset_id itself does not count against the room's limit."""
    found = assess(bmap, space_id, asset_type, ignore=asset_id)
    return found["permitted"] and asset_type not in FIXED and found["can_add"] != 0, found["reason"]


def options(bmap: BuildingMap, space_id: str) -> dict:
    space = bmap.spaces[space_id]
    rule_ = rule_for(space["room_type"])
    present = sorted({a["type"] for a in bmap.assets.values() if a["space_id"] == space_id} - set(PLANNABLE))
    items = []
    for asset_type in (*PLANNABLE, *present):
        found = assess(bmap, space_id, asset_type)
        items.append({"type": asset_type, "label": label(asset_type), "allowed": found["permitted"],
                      "existing": found["existing"], "max_count": found["max_count"], "can_add": found["can_add"],
                      "reason": found["reason"]})
    items.sort(key=lambda item: not item["allowed"])
    return {"space_id": space_id, "name": space["name"], "room_type": space["room_type"],
            "room_label": room_label(space["room_type"]), "area_m2": round(area(bmap, space_id), 1), "items": items,
            "note": rule_["note"]}
