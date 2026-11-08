"""100 spoken staff requests for the test pager PG-10 on the demo campus, each with the trip it must produce.

Every case starts from a clean slate at the main entrance of Building A (dispatch.walk_reset, the R key on the
pager). A case is one request, or a short conversation whose last reply is checked. Expected:
  goal      the room the route ends in (a tuple: any of these), None: no route afterwards
  stops     in order: a room name, "@room_type" (any room of that type), "#asset_type" (a free item of that type)
  step_free True / False (None: not checked)
  avoid     lifts, stairs, links or rooms the route must not use
  uses      lifts, stairs, links or rooms the route must use
  urgent    the reply starts with "Urgent."
  intent    the phrase rules' intent of the last reply

`python -m hospital.route_cases` simulates all of them and writes docs/route-tests.md, the checklist with a link per
case that opens PG-10, resets it and sends the request."""

import sys
from pathlib import Path
from urllib.parse import quote

from hospital import dispatch, voice
from hospital.state import Hospital

PAGER = "PG-10"
DOC = Path(__file__).resolve().parent.parent / "docs" / "route-tests.md"

CASES: list[dict] = [
    # ---------- locate and navigate: names, numbers, department words, spoken variants ----------
    {"say": "Take me to the emergency department", "goal": "Emergency Department (ED)"},
    {"say": "Bring me to CT", "goal": "CT"},
    {"say": "Navigate to the MRI", "goal": "MRI", "uses": ["Covered walkway A–B"]},
    {"say": "Go to theatre 2", "goal": "Theatre 2"},
    {"say": "I need to get to recovery", "goal": "Recovery"},
    {"say": "Take me to room A2.14", "goal": "Room A2.14"},
    {"say": "Route to A1.05", "goal": "Room A1.05"},
    {"say": "Head to room 2.07", "goal": "Room A2.07"},
    {"say": "Take me to operating theater one", "goal": "Theatre 1"},
    {"say": "Where is the pharmacy?", "goal": None, "intent": "find"},
    {"say": "Take me to the X-ray", "goal": ("X-ray 1", "X-ray 2")},
    {"say": "Go to radiology", "goal": "Radiology reception"},
    {"say": "I have to go to phlebotomy for a blood test", "goal": "Phlebotomy", "stops": []},
    {"say": "Bring me to the outpatients", "goal": "Outpatients reception"},
    {"say": "Navigate to ward 2A", "goal": "Nurses' station 2A"},
    {"say": "Take me to the nurses station on ward 1A", "goal": "Nurses' station 1A"},
    {"say": "Take me to the sterile store", "goal": "Sterile store"},
    {"say": "Get me to A and E", "goal": "Emergency Department (ED)"},
    {"say": "Bring me to the staff room in building B", "goal": "Staff room"},
    {"say": "Take me to the doctors office on ward 2", "goal": "Doctors' office 2A"},
    {"say": "Take me to the consulting room B0.04", "goal": "Consulting room B0.04"},
    {"say": "Take me to theatre one, I'm in the cafeteria", "goal": "Theatre 1", "stops": []},
    {"say": "Go to the anesthetic room", "goal": "Anaesthetic room"},
    {"say": "Take me to the day room on the second floor", "goal": "Day room 2A"},

    # ---------- patient transports: bed, wheelchair, stretcher (step-free, a bed takes a lift ride) ----------
    {"say": "Take the patient in bed from room A1.12 to CT", "goal": "CT", "stops": ["Room A1.12"],
     "step_free": True, "avoid": ["Stairs A"]},
    {"say": "Bring a wheelchair to room A2.03", "goal": "Room A2.03", "stops": ["#wheelchair"], "step_free": True},
    {"say": "Transfer the patient on a stretcher from ED Bay 3 to the MRI", "goal": "MRI", "stops": ["ED Bay 3"],
     "step_free": True},
    {"say": "Move the bed from recovery to room A2.20", "goal": "Room A2.20", "stops": ["Recovery"],
     "step_free": True, "avoid": ["Stairs A", "Stairs B"]},
    {"say": "Take me to theatre 1 with a bed", "goal": "Theatre 1", "step_free": True,
     "avoid": ["Stairs A", "Stairs B"]},
    {"say": "Patient in a wheelchair needs to go to physiotherapy", "goal": "Physiotherapy", "step_free": True},
    {"say": "Take Mr Smith by wheelchair to X-ray 2", "goal": "X-ray 2", "stops": [], "step_free": True},
    {"say": "Bed transport from room A2.05 to recovery", "goal": "Recovery", "stops": ["Room A2.05"],
     "step_free": True},
    {"say": "Stretcher to the CT please, patient comes from ED Bay 1", "goal": "CT", "stops": ["ED Bay 1"],
     "step_free": True},
    {"say": "Wheelchair patient to the cafeteria", "goal": "Cafeteria", "step_free": True},
    {"say": "Take this incubator up to ward 2A", "goal": "Nurses' station 2A", "step_free": True,
     "avoid": ["Stairs A"]},
    {"say": "Use the stairs to go to ward 1A", "goal": "Nurses' station 1A", "uses": ["Stairs A"],
     "avoid": ["Lift A", "Lift B"]},
    {"say": "Take the lift to ward 2A", "goal": "Nurses' station 2A", "step_free": True, "avoid": ["Stairs A"]},

    # ---------- errands: stops in the order said ----------
    {"say": "Take me to CT via the pharmacy", "goal": "CT", "stops": ["Pharmacy"]},
    {"say": "Stop by the pharmacy, then go to theatre 1", "goal": "Theatre 1", "stops": ["Pharmacy"]},
    {"say": "First the lab, then the CT, then the MRI", "goal": "MRI", "stops": ["Pathology lab", "CT"]},
    {"say": "Grab a coffee on the way to recovery", "goal": "Recovery", "stops": ["@cafe"]},
    {"say": "Go to the shop and then to ward 2A", "goal": "Nurses' station 2A", "stops": ["Shop"]},
    {"say": "Pick up the samples from phlebotomy and drop them at the pathology lab", "goal": "Pathology lab",
     "stops": ["Phlebotomy"]},
    {"say": "Collect a free infusion pump and bring it to room A1.08", "goal": "Room A1.08",
     "stops": ["#infusion_pump"]},
    {"say": "Take me to the MRI, but stop at the WC first", "goal": "MRI", "stops": ["@toilet"]},
    {"say": "Get the anaesthesia machine and bring it to theatre 2", "goal": "Theatre 2",
     "stops": ["#anaesthesia_machine"]},
    {"say": "Drop off the prescription at the pharmacy, then pick up a wheelchair and go to room A1.20",
     "goal": "Room A1.20", "stops": ["Pharmacy", "#wheelchair"]},
    {"say": "From the cafeteria to the outpatients waiting area", "goal": "Outpatients waiting area",
     "stops": ["Cafeteria"]},
    {"say": "Go to the kiosk, then the cafeteria, then back to the emergency department",
     "goal": "Emergency Department (ED)", "stops": ["Shop", "Cafeteria"]},
    {"say": "Take me to room A2.10 via day room 2A", "goal": "Room A2.10", "stops": ["Day room 2A"]},
    {"say": "Swing by nurses station 1A and then go to doctors office 1A", "goal": "Doctors' office 1A",
     "stops": ["Nurses' station 1A"]},
    {"say": "Fetch a defibrillator and meet me in ED Resus", "goal": "ED Resus", "stops": ["#defibrillator"],
     "avoid": ["Covered walkway A–B"]},
    {"say": "Before going to CT, stop at the toilet", "goal": "CT", "stops": ["@toilet"]},
    {"say": "Bring the blood samples to the lab, then come back to the ED", "goal": "Emergency Department (ED)",
     "stops": ["Pathology lab"]},
    {"say": "Take me past the cafeteria to the radiology reception", "goal": "Radiology reception",
     "stops": ["Cafeteria"]},
    {"say": "Get a wheelchair, pick up the patient in room A1.03 and take them to physiotherapy",
     "goal": "Physiotherapy", "stops": ["#wheelchair", "Room A1.03"], "step_free": True},
    {"say": "Go to theatre 2 after the sterile store", "goal": "Theatre 2", "stops": ["Sterile store"]},
    {"say": "Pharmacy, then admissions, then the staff room", "goal": "Staff room",
     "stops": ["Pharmacy", "Admissions"]},
    {"say": "On the way to the MRI, stop at the outpatients reception", "goal": "MRI",
     "stops": ["Outpatients reception"]},

    # ---------- lifts, stairs and links: avoid, prefer, closures ----------
    {"say": "Take me to ward 2A, don't use lift A", "goal": "Nurses' station 2A", "avoid": ["Lift A"]},
    {"say": "Lift B is crowded, take me to room A2.15", "goal": "Room A2.15", "avoid": ["Lift B"]},
    {"say": "Take me to theatre 1, avoid the bridge", "goal": "Theatre 1", "avoid": ["Bridge A–B"],
     "uses": ["Covered walkway A–B"]},
    {"say": "Go to recovery over the bridge", "goal": "Recovery", "uses": ["Bridge A–B"],
     "avoid": ["Covered walkway A–B"]},
    {"say": "Take the covered walkway to the MRI", "goal": "MRI", "uses": ["Covered walkway A–B"]},
    {"say": "Lift A is out of order. Bring me to ward 1A with a bed", "goal": "Nurses' station 1A",
     "step_free": True, "avoid": ["Lift A", "Stairs A"], "uses": ["Lift B"]},
    {"say": "Take me to theatre 2 using lift C", "goal": "Theatre 2", "uses": ["Lift C"]},
    {"say": "Without stairs to day room 2A", "goal": "Day room 2A", "step_free": True, "avoid": ["Stairs A"]},
    {"say": "Avoid stairs A, take me to room A1.10", "goal": "Room A1.10", "avoid": ["Stairs A"],
     "step_free": False},
    {"say": "There are a lot of people at lift A and lift B, take me to ward 2A", "goal": "Nurses' station 2A",
     "avoid": ["Lift A", "Lift B"], "uses": ["Stairs A"]},
    {"say": "Stairs B are blocked, take me to the recovery", "goal": "Recovery", "avoid": ["Stairs B"]},
    {"say": "Don't go through the emergency department, take me to the CT", "goal": "CT", "stops": [],
     "avoid": ["Emergency Department (ED)"]},
    {"say": "Take me to theatre 1 but not over the bridge, the bridge is packed", "goal": "Theatre 1",
     "avoid": ["Bridge A–B"]},
    {"say": ["Take me to ward 2A", "Lift A is busy"], "goal": "Nurses' station 2A", "avoid": ["Lift A"],
     "intent": "avoid"},
    {"say": "Take elevator B to room A2.01", "goal": "Room A2.01", "uses": ["Lift B"]},
    {"say": "The bridge is closed, bring me to the theatres corridor... I mean theatre 2", "goal": "Theatre 2",
     "avoid": ["Bridge A–B"]},
    {"say": "Take the stairs to recovery, lift C is too slow", "goal": "Recovery", "avoid": ["Lift C"]},

    # ---------- emergencies: urgent, free equipment on the way (staff decide the destination) ----------
    {"say": "We have a critical person with a collapsed right lung wing, bring me to the next emergency room with a "
            "free surgery table", "goal": "Theatre 2", "urgent": True, "step_free": True},
    {"say": "Take me to theatre 1 with a bed, stop by the pharmacy and don't use lift A as there are a lot of people",
     "goal": "Theatre 1", "stops": ["Pharmacy"], "avoid": ["Lift A"], "step_free": True},
    {"say": "Cardiac arrest in room A2.05, grab a defibrillator on the way", "goal": "Room A2.05",
     "stops": ["#defibrillator"], "urgent": True, "avoid": ["Covered walkway A–B"]},
    {"say": "Code blue on ward 1A, bring me to room A1.14", "goal": "Room A1.14", "stops": [], "urgent": True},
    {"say": "Critical patient, take us to ED Resus with a stretcher", "goal": "ED Resus", "urgent": True,
     "step_free": True},
    {"say": "Trauma patient incoming, get me to the shock room", "goal": "ED Resus", "urgent": True},
    {"say": "Patient is deteriorating in recovery, I need a ventilator there", "goal": "Recovery",
     "stops": ["#ventilator"], "urgent": True},
    {"say": "Stroke patient, urgent CT, step free please", "goal": "CT", "urgent": True, "step_free": True},
    {"say": "Massive bleeding in ED Bay 2, bring an infusion pump", "goal": "ED Bay 2", "stops": ["#infusion_pump"],
     "urgent": True},
    {"say": "Unconscious patient in the cafeteria, bring the nearest defibrillator", "goal": "Cafeteria",
     "stops": ["#defibrillator"], "urgent": True},
    {"say": "Emergency caesarean, bring me to the next free operating table, the patient is on a bed",
     "goal": "Theatre 2", "urgent": True, "step_free": True},
    {"say": "Anaphylaxis in the outpatients waiting area, I need to get there fast with adrenaline from the pharmacy",
     "goal": "Outpatients waiting area", "stops": ["Pharmacy"], "urgent": True},
    {"say": "Get me to triage quickly", "goal": "ED Triage", "urgent": True},
    {"say": "Patient with chest pain from the waiting area to ED Bay 4 by wheelchair", "goal": "ED Bay 4",
     "stops": ["Outpatients waiting area"], "step_free": True},
    {"say": "Rapid response to room A2.22", "goal": "Room A2.22", "urgent": True},

    # ---------- equipment ----------
    {"say": "Find me a free wheelchair", "intent": "find", "search": "wheelchair"},
    {"say": "Where is the nearest free infusion pump?", "intent": "find", "search": "infusion_pump"},
    {"say": "I need an ultrasound machine", "intent": "find", "search": "ultrasound"},
    {"say": "Nearest defibrillator, hurry!", "intent": "find", "search": "defibrillator", "urgent": True},
    {"say": "Take me to the nearest free operating table", "goal": "Theatre 2", "intent": "find"},
    {"say": "Find an available monitor and bring it to ED Bay 5", "goal": "ED Bay 5", "stops": ["#monitor"]},

    # ---------- during a trip ----------
    {"say": ["Take me to the MRI", "Stop by the shop on the way"], "goal": "MRI", "stops": ["Shop"]},
    {"say": ["Take me to theatre 1", "Cancel"], "goal": None, "intent": "cancel_go_to"},
    {"say": ["Take me to CT", "Actually, take me to the MRI instead"], "goal": "MRI"},
]


def space_name(h: Hospital, element_id: str) -> str:
    if element_id in h.bmap.assets:
        element_id = h.bmap.assets[element_id]["space_id"]
    return h.bmap.spaces[element_id]["name"]


def used(h: Hospital, route: dict) -> set[str]:
    """Names of the rooms, links, lifts and stairs a route passes."""
    names = {h.bmap.spaces[s]["name"] for s in route.get("spaces", [])}
    names |= {h.bmap.connectors[i["connector"]]["name"] for i in route["instructions"] if "connector" in i}
    return names


def stop_matches(h: Hospital, expected: str, stop: dict) -> bool:
    if expected.startswith("#"):
        return stop["id"] in h.bmap.assets and h.bmap.assets[stop["id"]]["type"] == expected[1:]
    space = h.bmap.spaces[h.bmap.assets[stop["id"]]["space_id"] if stop["id"] in h.bmap.assets else stop["id"]]
    if expected.startswith("@"):
        return space["room_type"] == expected[1:]
    return space["name"] == expected


def run_case(h: Hospital, case: dict) -> tuple[list[str], dict]:
    """Reset PG-10, say the request(s), check the trip; returns (problems, what happened)."""
    dispatch.walk_reset(h, PAGER)
    says = case["say"] if isinstance(case["say"], list) else [case["say"]]
    reply = {}
    for text in says:
        reply = voice.handle(h, PAGER, text)
    g = dispatch.guidance(h, PAGER)
    problems = []
    if not reply.get("ok"):
        problems.append(f"reply not ok: {reply.get('say')}")
    if "intent" in case and reply.get("intent") != case["intent"]:
        problems.append(f"intent {reply.get('intent')} != {case['intent']}")
    route = g.get("route")
    goal = g["goal"]["space_id"] if g.get("goal") and route else None
    got_goal = space_name(h, goal) if goal else None
    if "goal" in case:
        wanted = case["goal"] if isinstance(case["goal"], tuple) else (case["goal"],)
        if got_goal not in wanted:
            problems.append(f"goal {got_goal} not in {wanted}")
    if "search" in case and (g.get("search") or {}).get("type") != case["search"]:
        problems.append(f"search {(g.get('search') or {}).get('type')} != {case['search']}")
    stops = g.get("stops") or []
    if "stops" in case:
        if len(stops) != len(case["stops"]) or not all(stop_matches(h, e, s) for e, s in zip(case["stops"], stops)):
            problems.append(f"stops {[space_name(h, s['id']) for s in stops]} != {case['stops']}")
    if case.get("step_free") is not None and g.get("step_free") != case["step_free"]:
        problems.append(f"step_free {g.get('step_free')} != {case['step_free']}")
    names = used(h, route) if route else set()
    for name in case.get("avoid", []):
        if name in names:
            problems.append(f"route uses {name}")
    for name in case.get("uses", []):
        if name not in names:
            problems.append(f"route does not use {name}")
    if "urgent" in case and reply.get("say", "").startswith("Urgent") != case["urgent"]:
        problems.append(f"urgent {not case['urgent']}")
    happened = {"say": reply.get("say", ""), "intent": reply.get("intent"), "goal": got_goal,
                "stops": [space_name(h, s["id"]) for s in stops], "choice": g.get("choice"),
                "minutes": route["minutes"] if route else None, "step_free": g.get("step_free")}
    return problems, happened


def expectation(case: dict) -> str:
    parts = []
    if case.get("goal"):
        goal = case["goal"]
        parts.append("→ " + (" or ".join(goal) if isinstance(goal, tuple) else goal))
    elif "search" in case:
        parts.append(f"→ nearest free {case['search'].replace('_', ' ')}")
    elif "intent" in case:
        parts.append({"find": "shows it on the map, no route", "where": "says where you are, no route",
                      "set_closed": "reopens it, no route", "cancel_go_to": "route cancelled, map empty"}.get(case["intent"],
                                                                                                  case["intent"]))
    if case.get("stops"):
        parts.append("via " + ", ".join(s.replace("@", "any ").replace("#", "free ").replace("_", " ")
                                        for s in case["stops"]))
    if case.get("step_free"):
        parts.append("step-free")
    if case.get("avoid"):
        parts.append("not " + ", ".join(case["avoid"]))
    if case.get("uses"):
        parts.append("uses " + ", ".join(case["uses"]))
    if case.get("urgent"):
        parts.append("urgent")
    return "; ".join(parts)


def main() -> int:
    from app.hospital_api import default_hospital

    h = default_hospital()
    rows, failed = [], 0
    for number, case in enumerate(CASES, start=1):
        with h.lock:
            problems, happened = run_case(h, case)
        failed += bool(problems)
        says = case["say"] if isinstance(case["say"], list) else [case["say"]]
        link = f"/hospital/pager/{PAGER}?reset=1" + "".join(f"&ask={quote(s)}" for s in says)
        status = "ok" if not problems else "FAIL: " + "; ".join(problems)
        print(f"{number:3d} {status:4s} {' / '.join(says)}\n      {happened['say']}")
        request = " → ".join(f"“{s}”" for s in says)
        route = (f"{happened['goal']}" + (f" via {', '.join(happened['stops'])}" if happened["stops"] else "")
                 + (f" ({happened['choice']}, {happened['minutes']} min)" if happened["minutes"] is not None else "")
                 if happened["goal"] else "no route")
        rows.append(f"| {number} | [{request}]({link}) | {expectation(case)} | {route} | "
                    f"{'✅' if not problems else '❌ ' + '; '.join(problems)} | |")
    DOC.parent.mkdir(exist_ok=True)
    DOC.write_text(
        "# Route tests for pager PG-10\n\n"
        "Generated by `python -m hospital.route_cases` from `hospital/route_cases.py`; the simulation column is what "
        "the phrase rules and the routing core produced.\n\n"
        "How to test in the UI: open a case link (local server or tunnel + the path). It resets PG-10 to the main "
        "entrance with no route and sends the request. Or open `/hospital/pager/PG-10`, press **R** (clean slate: no "
        "route, closures reopened), then speak or type the request. Press **L** to walk the route, **R** again before "
        "the next case.\n\n"
        f"Simulated: {len(CASES) - failed}/{len(CASES)} as expected.\n\n"
        "| # | Request (opens PG-10) | Expected | Simulated route | Sim | UI |\n|---|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n", encoding="utf-8")
    print(f"\n{len(CASES) - failed}/{len(CASES)} as expected; wrote {DOC}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
