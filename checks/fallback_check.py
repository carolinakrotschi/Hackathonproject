"""Check the fallback agent on its own: every question goes straight to core/fallback.py (no parser). One dev
question per benchmark kind, one per warm-up kind, and a few question types the parser has never seen, whose
answers are computed here. Costs API credits (about $0.10 per question).

  uv run --env-file .env python -m checks.fallback_check"""

import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic

from core import queries
from core.building_map import BuildingMap
from core.fallback import fallback_answer
from core.score import committed_answer, numbers_in, score_reply

SPLIT = Path("data/dev")


def sample(filename: str) -> list[tuple[Path, dict]]:
    by_kind: dict[str, list] = {}
    for path in sorted(SPLIT.glob(f"*/{filename}")):
        for line in path.read_text(encoding="utf-8").splitlines():
            question = json.loads(line)
            by_kind.setdefault(question["kind"], []).append((path.parent, question))
    rng = random.Random(5)
    return [rng.choice(items) for _, items in sorted(by_kind.items())]


def warmup_ok(question: dict, text: str, bmap: BuildingMap) -> bool:
    answer, line = question["answer"], (committed_answer(text) or "")
    if answer.get("reachable") is False:
        return "no route" in line.lower()
    if "optimal_length_m" in answer:
        numbers = numbers_in(line)
        return len(numbers) == 1 and abs(numbers[0] - answer["optimal_length_m"]) <= max(0.01 * answer["optimal_length_m"], 0.05)
    if question["kind"] == "locate":
        space = bmap.spaces[answer["space_id"]]
        return space["name"].lower() in line.lower() or (space.get("ref") or "§").lower() in line.lower()
    return numbers_in(line) == [answer["count"]]


def novel(directory: Path) -> list[tuple[str, callable]]:
    """New question types, answered here from the data."""
    bmap = BuildingMap.load(directory)
    lab = next(s for s in bmap.spaces.values() if s["room_type"] == "quality_lab")
    corridor = next(s for s in bmap.spaces.values() if s["type"] == "corridor")
    machine = next(a for a in bmap.assets.values() if a["type"] == "coordinate_measuring_machine")
    floor1 = next(f for f in bmap.floors.values() if f["level"] == 1)
    doors = sum(corridor["id"] in o["space_ids"] for o in bmap.openings.values())
    offices = [s for s in bmap.spaces.values() if s["floor_id"] == floor1["id"] and s["room_type"] == "office"]
    mean_office = sum(queries.area(bmap, s["id"]) for s in offices) / len(offices)
    lift = next(c for c in bmap.connectors.values() if c["kind"] == "lift")
    number = lambda value, tolerance: lambda line: (lambda n: len(n) == 1 and abs(n[0] - value) <= tolerance)(numbers_in(line))  # noqa: E731
    return [
        (f"What is the floor area of {lab['name']} in square metres? Finish with 'Answer: <number>'.",
         number(queries.area(bmap, lab["id"]), 0.05 * queries.area(bmap, lab["id"]))),
        (f"How many doors or openings does {corridor['name']} have? Finish with 'Answer: <number>'.", number(doors, 0)),
        ("On which floor is the coordinate measuring machine? Finish with 'Answer: <floor>'.",
         lambda line: bmap.floor_name(machine["floor_id"]).lower() in line.lower()),
        ("What is the average size of an office on floor 1, in m²? Finish with 'Answer: <number>'.",
         number(mean_office, 0.05 * mean_office)),
        (f"How many floors does the {lift['name']} serve? Finish with 'Answer: <number>'.", number(len(lift["floor_ids"]), 0)),
    ]


def main() -> None:
    client = anthropic.Anthropic()
    usage: dict = {}
    jobs = []
    for directory, question in sample("questions.jsonl"):
        jobs.append(("benchmark " + question["kind"], directory, question["question"],
                     lambda text, q=question: score_reply(q["answer"], text)))
    for directory, question in sample("questions_basic.jsonl"):
        jobs.append(("warm-up " + question["kind"], directory, question["question"],
                     lambda text, q=question, d=directory: warmup_ok(q, text, BuildingMap.load(d))))
    factory = Path("data/dev/proc-factory-dev-003")
    for text, check in novel(factory):
        jobs.append(("new question", factory, text, lambda reply, c=check: c(committed_answer(reply) or "")))

    def work(job):
        label, directory, text, check = job
        reply = fallback_answer(BuildingMap.load(directory), text, client, usage)
        return label, text, reply, check(reply)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(work, jobs))
    for label, text, reply, ok in results:
        print(f"{'✓' if ok else '✗'} {label:34} {text[:70]}")
        if not ok:
            print("      " + reply.replace("\n", "\n      ")[:600])
    print(f"\n{sum(r[3] for r in results)}/{len(results)} correct")
    cost = usage.get("input_tokens", 0) * 5 / 1e6 + usage.get("output_tokens", 0) * 25 / 1e6
    print(f"usage {usage} ~${cost:.2f}")


if __name__ == "__main__":
    main()
