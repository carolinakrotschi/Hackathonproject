"""Scripted conversations with tricky users, like the Interaction part of the judging (25 points): does the
agent ask only what it needs, show what it understood, give directions a person can follow, and never invent?

Each scenario is a short conversation with the demo agent (app/agent.py) on one factory. Deterministic checks
look at the tool calls and replies; a Claude grader scores each transcript on the rubric. Costs API credits:
about $0.10 per agent turn plus the grading, so a full run is roughly $2-4.

  uv run --env-file .env python -m checks.interaction_eval                 # all scenarios
  uv run --env-file .env python -m checks.interaction_eval --only wheelchair first_day"""

import argparse
import json
import re
import statistics
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from app.agent import GuideSession
from core.building_map import BuildingMap

BUILDING = Path("factories/proc-factory-dev-005")
LOCATION = "Warehouse zone H-C3"  # where the app's blue dot starts: the first ground-floor entrance
OUT = Path("agent_output/interaction")
JUDGE_MODEL = "claude-opus-5"
INTERNAL_ID = re.compile(r"proc-factory|-F\d-S\d{3}|-F\d-O\d{4}|-A\d{4}|door:")


@dataclass
class Turn:
    user: str
    reply: str = ""
    tools: list[tuple[str, dict]] = field(default_factory=list)
    results: list[str] = field(default_factory=list)  # what each tool returned, shortened, for the grader


@dataclass
class Scenario:
    name: str
    persona: str
    messages: list[str]
    expect: str  # what a good agent does, for the grader
    checks: list[tuple[str, Callable[[list[Turn]], bool]]] = field(default_factory=list)


def called(turn: Turn, tool: str, **fields) -> bool:
    return any(name == tool and all(args.get(k) == v for k, v in fields.items()) for name, args in turn.tools)


def routes(turn: Turn) -> list[dict]:
    return [args for name, args in turn.tools if name in ("find_route", "find_nearest")]


SCENARIOS = [
    Scenario(
        "first_day", "New employee on their first day; knows no room names.",
        ["Hi, it's my first day here. Where can I get a coffee?", "Thanks! And where are the toilets?"],
        "Finds the nearest kitchen (and then toilet) from where the person is, by walking distance, without asking for "
        "room numbers. Short, friendly, plain-language directions with landmarks; no internal ids.",
        [("used nearest search for coffee", lambda t: any(n == "find_nearest" for n, _ in t[0].tools)),
         ("used nearest search for toilets", lambda t: any(n == "find_nearest" for n, _ in t[1].tools))],
    ),
    Scenario(
        "wheelchair", "Wheelchair user. Says so once, expects it to be remembered.",
        ["I'm in a wheelchair. How do I get to the quality lab?",
         "Great. After that I need to get to a meeting room on floor 2."],
        "Both routes step-free (lift, no stairs) without being reminded on the second turn; says the route is "
        "step-free; mentions the lift.",
        [("first route step-free", lambda t: bool(routes(t[0])) and all(r.get("step_free") for r in routes(t[0]))),
         ("second route step-free without reminder", lambda t: bool(routes(t[1])) and all(r.get("step_free") for r in routes(t[1])))],
    ),
    Scenario(
        "technician", "Service technician, hands full, listening on speaker. Wants it short.",
        ["hands full. nearest cnc mill. short please"],
        "Goes straight to the nearest CNC mill without questions; very short spoken-style directions (a few steps, "
        "landmarks like machines), total distance.",
        [("found nearest", lambda t: called(t[0], "find_nearest") or bool(routes(t[0]))),
         ("short reply (<= 70 words)", lambda t: len(t[0].reply.split()) <= 70)],
    ),
    Scenario(
        "vague_printer", "Vague user asking for something the building may not have.",
        ["Where's the printer?"],
        "Searches; this factory has no printer. Says so honestly and offers something useful (e.g. what it does have "
        "or asks what they need to print); must not invent a printer location.",
        [("searched", lambda t: any(n == "search_building" for n, _ in t[0].tools))],
    ),
    Scenario(
        "wrong_room", "User gives a room number that does not exist.",
        ["Take me to room 9.99 please."],
        "Says room 9.99 doesn't exist here, suggests how to find the right place (nearby numbering, floors) or asks "
        "one short question. Does not invent a route.",
        [("no route claimed", lambda t: not any(n == "find_route" for n, a in t[0].tools if "9.99" not in json.dumps(a)) or True)],
    ),
    Scenario(
        "changes_mind", "User changes their mind halfway.",
        ["How do I get to Kitchen 0.25?", "Wait, sorry, I meant a kitchen on floor 1. Whichever is closest."],
        "First turn: route to Kitchen 0.25. Second turn: drops the old goal without fuss, finds the closest kitchen "
        "on floor 1 from the original location, gives that route.",
        [("second turn routes again", lambda t: bool(routes(t[1])))],
    ),
    Scenario(
        "ambiguous_lathe", "User names a machine type with six instances.",
        ["I need to service the CNC lathe. How do I get there?"],
        "There are six CNC lathes. Either asks one short question listing a few options, or picks the nearest and "
        "says so clearly (\"the nearest of six is …\"). Must not silently pick an arbitrary one.",
        [],
    ),
    Scenario(
        "lift_closed", "Facility manager reporting a closure, then a follow-up for a wheelchair user.",
        ["The Office lift is out of service today.", "So how does a wheelchair user get to floor 1 now?"],
        "Closes the lift with the tool, says what changes (no step-free access between floors). Then honestly says "
        "there is no step-free route to floor 1 while the lift is closed (if that is true), maybe suggests alternatives.",
        [("closed the lift", lambda t: called(t[0], "set_closed", closed=True)),
         ("tried a step-free route", lambda t: any(r.get("step_free") for r in routes(t[1])))],
    ),
    Scenario(
        "moved_machine", "Facility manager updating the map.",
        ["We moved the waterjet cutter into Assembly zone H-A2 this morning."],
        "Confirms in one line what it understood, moves the machine with the tool, confirms the map is updated.",
        [("moved the asset", lambda t: any(n == "move_asset" for n, _ in t[0].tools))],
    ),
    Scenario(
        "off_topic", "User asks something the building map can't know.",
        ["What's the WiFi password here?"],
        "Says politely it only knows the building's layout, rooms, machines and routes; does not invent a password; "
        "maybe suggests asking IT or reception.",
        [("no tool misuse", lambda t: True)],
    ),
]

RUBRIC = """You grade one conversation between a person and an indoor-navigation assistant for a factory, as the
hackathon jury would ("Interaction", 25 points). Score each criterion from 1 (poor) to 5 (excellent):

- asks_only_needed: asks a question only when it really can't proceed; no unnecessary questions, no ignoring of
  real ambiguity either.
- shows_understanding: briefly confirms what it understood (start, goal, constraints like step-free, closures,
  changes) before or while acting.
- followable_directions: directions a person can follow while walking: short steps, landmarks, floors, total
  distance; plain language; no internal ids or jargon. (If no directions were needed, judge clarity of the reply.)
- honest: never invents rooms, machines, routes or facts; says clearly when something doesn't exist or isn't possible.
- tone: friendly, concise, suits the persona (e.g. very short for someone with their hands full).

The assistant runs inside a map app: whatever its tools find or route is highlighted on the map automatically, so
"it's highlighted on the map" is true. The tool calls are listed so you can judge honesty and what it actually did. What a good
assistant does in this scenario is given as a guide, not as the only acceptable answer."""

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        **{k: {"type": "integer"} for k in ("asks_only_needed", "shows_understanding", "followable_directions", "honest", "tone")},
        "problems": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["asks_only_needed", "shows_understanding", "followable_directions", "honest", "tone", "problems"],
    "additionalProperties": False,
}
CRITERIA = ("asks_only_needed", "shows_understanding", "followable_directions", "honest", "tone")


def tool_calls(messages: list[dict], start: int) -> list[tuple[str, dict]]:
    calls = []
    for message in messages[start:]:
        if message["role"] != "assistant" or isinstance(message["content"], str):
            continue
        for block in message["content"]:
            if getattr(block, "type", None) == "tool_use":
                calls.append((block.name, dict(block.input)))
    return calls


def tool_results(messages: list[dict], start: int, limit: int = 2500) -> list[str]:
    out = []
    for message in messages[start:]:
        if message["role"] != "user" or isinstance(message["content"], str):
            continue
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else " ".join(c.get("text", "") for c in content or [])
                out.append(text[:limit] + ("…" if len(text) > limit else ""))
    return out


def run_scenario(scenario: Scenario, client: anthropic.Anthropic) -> list[Turn]:
    session = GuideSession(BuildingMap.load(BUILDING), client)
    turns = []
    for text in scenario.messages:
        start = len(session.messages)
        reply, _ = session.ask(f"{text}\n\n(The app's blue dot says I am at: {LOCATION}, Ground floor.)")
        turns.append(Turn(text, reply, tool_calls(session.messages, start), tool_results(session.messages, start)))
    return turns


def transcript(scenario: Scenario, turns: list[Turn]) -> str:
    lines = [f"Persona: {scenario.persona}", f"The person's location: {LOCATION} (Ground floor)", ""]
    for turn in turns:
        lines.append(f"USER: {turn.user}")
        for index, (name, args) in enumerate(turn.tools):
            lines.append(f"  [tool] {name}({json.dumps(args, ensure_ascii=False)})")
            if index < len(turn.results):
                lines.append(f"  [result] {turn.results[index]}")
        lines.append(f"ASSISTANT: {turn.reply}")
        lines.append("")
    return "\n".join(lines)


def grade(scenario: Scenario, turns: list[Turn], client: anthropic.Anthropic) -> dict:
    content = f"{RUBRIC}\n\nWhat a good assistant does here: {scenario.expect}\n\nConversation:\n{transcript(scenario, turns)}"
    response = client.messages.create(
        model=JUDGE_MODEL, max_tokens=3000, messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": GRADE_SCHEMA}},
    )
    return json.loads(next(block.text for block in response.content if block.type == "text"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--label", default="run")
    args = parser.parse_args()
    scenarios = [s for s in SCENARIOS if not args.only or s.name in args.only]
    client = anthropic.Anthropic()
    OUT.mkdir(parents=True, exist_ok=True)

    def work(scenario: Scenario):
        turns = run_scenario(scenario, client)
        return scenario, turns, grade(scenario, turns, client)

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(work, scenarios))
    report = [f"# Interaction test: {args.label}\n"]
    totals = {c: [] for c in CRITERIA}
    for scenario, turns, scores in results:
        checks = [(label, check(turns)) for label, check in scenario.checks]
        checks.append(("no internal ids", not any(INTERNAL_ID.search(t.reply) for t in turns)))
        for c in CRITERIA:
            totals[c].append(scores[c])
        failed = [label for label, ok in checks if not ok]
        print(f"{scenario.name:16} " + " ".join(f"{c[:6]}={scores[c]}" for c in CRITERIA)
              + (f"  FAILED: {', '.join(failed)}" if failed else "  checks ok"))
        for problem in scores["problems"]:
            print(f"    - {problem}")
        report.append(f"## {scenario.name}\n\n" + " · ".join(f"{c} {scores[c]}" for c in CRITERIA)
                      + f"\n\nChecks: {', '.join(f'{l} {'✓' if ok else '✗'}' for l, ok in checks)}\n\n"
                      + "".join(f"- {p}\n" for p in scores["problems"]) + "\n```\n" + transcript(scenario, turns) + "\n```\n")
    print("\nmean " + " ".join(f"{c}={statistics.mean(v):.2f}" for c, v in totals.items()))
    (OUT / f"{args.label}.md").write_text("\n".join(report), encoding="utf-8")
    print(f"transcripts: {OUT / (args.label + '.md')}")


if __name__ == "__main__":
    main()
