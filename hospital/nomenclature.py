"""Hospital sign names: which room type a name like "CT", "Dirty utility 2A" or "ED Resus" means, and the room
number in it ("A0.33"). Used when staff label rooms by conversation and when an imported plan's labels are read."""

import re

# Most specific first: "ED Resus" is resuscitation, not emergency; "Dirty utility" is utility, not a store.
ROOM_WORDS: list[tuple[str, str]] = [
    (r"resus|resuscitation", "resuscitation"),
    (r"triage", "triage"),
    (r"(ed|treatment|cubicle) ?bay|\bbay\b|cubicle|majors|minors", "treatment_bay"),
    (r"emergency|\bed\b|\ba ?& ?e\b|\ba and e\b|casualty|notaufnahme", "emergency"),
    (r"\bicu\b|\bitu\b|intensive care|critical care|\bhdu\b", "icu"),
    (r"recovery|\bpacu\b", "recovery"),
    (r"theatre|theater|operating room|\bor\b|\bop\b", "operating_theatre"),
    (r"anaesthe|anesthe|scrub|sluice|utility", "utility"),
    (r"\bct\b|x-?ray|\bmri\b|radiology|imaging|ultrasound|fluoroscopy|mammograph", "radiology"),
    (r"pharmacy|dispensary|apotheke", "pharmacy"),
    (r"pathology|\blab\b|laboratory|phlebotomy|blood (test|collection)", "lab"),
    (r"physio|therapy|rehab", "therapy"),
    (r"nurses'? ?station|nurse station|\bns\b", "nurse_station"),
    (r"\bwc\b|toilet|restroom|lavatory", "toilet"),
    (r"bathroom|shower", "bathroom"),
    (r"waiting", "waiting"),
    (r"reception|admissions|registration|front desk|information", "reception"),
    (r"consult|clinic|exam(ination)? room|outpatient", "consulting"),
    (r"cafeteria|\bcafe\b|café|canteen|restaurant|coffee", "cafe"),
    (r"\bshop\b|kiosk", "kiosk"),
    (r"day room|lounge|staff room|break room|rest room", "lounge"),
    (r"store|storage|stock|linen|equipment room|supplies", "storage"),
    (r"office|reporting|admin|doctors|secretar|seminar|meeting", "office"),
    (r"entrance|lobby|foyer|atrium", "lobby"),
    (r"patient room|single room|double room|\bbed ?room\b|\broom \w?\d", "patient_room"),
    (r"walkway|bridge|link", "walkway"),
    (r"corridor|hallway|passage", "corridor"),
    (r"\blift\b|elevator", "lift"),
    (r"stairs|staircase|stairwell", "stairwell"),
]
ROOM_NUMBER = re.compile(r"\b([A-Z]{0,2}-?\d{1,2}\.\d{1,3}[A-Z]?|[A-Z]{0,2}\d{3,4}[A-Z]?)\b")


def room_type_for(name: str) -> str | None:
    """The room type a sign name stands for, or None if no hospital word is in it."""
    text = name.lower()
    return next((room_type for pattern, room_type in ROOM_WORDS if re.search(pattern, text)), None)


def room_number(label: str) -> str | None:
    """The room number printed in a label: 'CT A0.33' -> 'A0.33', '1.07' -> '1.07'."""
    match = ROOM_NUMBER.search(label)
    return match.group(1) if match else None


def apply_labels(building: dict) -> int:
    """After a plan import: a label with a hospital word ('CT A0.33', 'Dirty utility 2A') names the room and sets its
    type and number; other labels (factory codes like 'H-A3', bare numbers) stay as the importer named them.
    Returns how many rooms were renamed."""
    renamed = 0
    for space in building["spaces"]:
        label = (space.get("ref") or "").strip()
        words = " ".join(ROOM_NUMBER.sub(" ", label).split())
        room_type = room_type_for(words) if words else None
        if room_type is None:
            continue
        space["name"] = words
        space["ref"] = room_number(label)
        if space["type"] not in ("lift", "stair", "corridor", "outdoor"):
            space["room_type"] = room_type
        renamed += 1
    return renamed


def sign_name(label: str) -> str:
    """How the room is shown and spoken: the label as printed, or 'Room <number>' for a bare number."""
    label = " ".join(label.split())
    if room_number(label) == label:
        return f"Room {label}"
    return label
