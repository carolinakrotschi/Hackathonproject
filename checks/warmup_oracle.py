"""Free check of the warm-up kinds (route, step-free route, route with a closure, tour, locate, count) against
questions_basic.jsonl. Those lines have no parsed `inputs`, so the parameters come from the answer's ids.

  uv run python -m checks.warmup_oracle data/train data/dev"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from core.building_map import BuildingMap
from core.queries import ROUTE_KINDS, reply, run
from core.score import committed_answer, numbers_in

ROUTE_TOLERANCE = 0.01


def params(bmap: BuildingMap, kind: str, answer: dict) -> dict:
    if kind == "tour":
        return {"stops": answer["stops"]}
    if kind in ROUTE_KINDS:
        out = {"start": answer["from_space_id"], "goal": answer["to_space_id"]}
        if kind == "route_with_closure":
            out["closed"] = answer["closed_element_id"]
        return out
    floor = next((f["id"] for f in bmap.floors.values() if f["level"] == answer.get("floor_level")), None)
    if kind == "locate":
        return {"asset_type": bmap.assets[answer["asset_id"]]["type"]}
    return {"asset_type": answer["asset_type"], "floor_id": floor}


def correct(kind: str, answer: dict, result: dict, text: str) -> bool:
    line = committed_answer(text) or ""
    if kind in ROUTE_KINDS:
        if answer.get("reachable") is False:
            return "no route" in line.lower()
        numbers = numbers_in(line)
        optimal = answer["optimal_length_m"]
        return len(numbers) == 1 and abs(numbers[0] - optimal) <= max(ROUTE_TOLERANCE * optimal, 0.01)
    if kind == "locate":
        return result.get("space_id") == answer["space_id"]
    return numbers_in(line) == [answer["count"]]


def parse_check(splits: list[Path], per_kind: int) -> None:
    """End to end on a sample: question text -> Claude parse -> core -> reply, scored like above."""
    import random  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    import anthropic  # noqa: PLC0415

    from core.parse import parse  # noqa: PLC0415

    by_kind: dict[str, list] = {}
    for split in splits:
        for path in sorted(split.glob("*/questions_basic.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                question = json.loads(line)
                by_kind.setdefault(question["kind"], []).append((path.parent, question))
    rng = random.Random(11)
    sample = [item for kind in sorted(by_kind) for item in rng.sample(by_kind[kind], min(per_kind, len(by_kind[kind])))]
    client = anthropic.Anthropic()

    def check(item):
        directory, question = item
        bmap = BuildingMap.load(directory)
        try:
            kind, found = parse(bmap, question["question"], client)
            result = run(bmap, kind, found)
            return kind, found, correct(question["kind"], question["answer"], result, reply(kind, result))
        except Exception as error:  # report and keep going
            return None, {"error": str(error)}, False

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(check, sample))
    good = Counter()
    for (directory, question), (kind, found, ok) in zip(sample, outcomes):
        good[question["kind"]] += ok
        if not ok:
            print(f"  {question['id']} [{question['kind']}] {question['question'][:110]}\n      got {kind} {found}")
    for kind in sorted(by_kind):
        print(f"{kind:22} {good[kind]}/{min(per_kind, len(by_kind[kind]))}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("splits", nargs="+", type=Path)
    parser.add_argument("--show", type=int, default=3)
    parser.add_argument("--parse", type=int, default=0, help="parse N questions per kind with Claude (costs credits)")
    args = parser.parse_args()
    if args.parse:
        parse_check(args.splits, args.parse)
        return
    totals, good, failures = Counter(), Counter(), {}
    for split in args.splits:
        for path in sorted(split.glob("*/questions_basic.jsonl")):
            bmap = BuildingMap.load(path.parent)
            for line in path.read_text(encoding="utf-8").splitlines():
                question = json.loads(line)
                kind, answer = question["kind"], question["answer"]
                label = kind + (" (no route)" if answer.get("reachable") is False else "")
                totals[label] += 1
                result = run(bmap, kind, params(bmap, kind, answer))
                text = reply(kind, result)
                if correct(kind, answer, result, text):
                    good[label] += 1
                else:
                    failures.setdefault(label, []).append((question["id"], answer.get("optimal_length_m", answer.get("space_name", answer.get("count"))), result.get("value", result.get("rooms"))))
    for label in sorted(totals):
        print(f"{label:30} {good[label]:5}/{totals[label]:<5} {100 * good[label] / totals[label]:5.1f}%")
        for item in failures.get(label, [])[: args.show]:
            print("    ", item)
    print(f"{'TOTAL':30} {sum(good.values()):5}/{sum(totals.values())}")


if __name__ == "__main__":
    main()
