"""Check the parser on train/dev: resolved params must equal the question's ground-truth `inputs`, and the
final reply must score. Spends API credits (one call per question, cached in .cache/), so it is capped.

  uv run --env-file .env python -m checks.parse_check data/dev --per-kind 2"""

import argparse
import json
import random
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from pathlib import Path

import anthropic

from core.building_map import BuildingMap
from core.parse import parse
from core.queries import reply, run
from core.score import score_reply


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("split", type=Path)
    parser.add_argument("--per-kind", type=int, default=2)
    args = parser.parse_args()

    by_kind: dict[str, list] = {}
    for path in sorted(args.split.glob("*/questions.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            question = json.loads(line)
            by_kind.setdefault(question["kind"], []).append((path.parent, question))
    rng = random.Random(7)
    sample = [item for kind in sorted(by_kind) for item in rng.sample(by_kind[kind], min(args.per_kind, len(by_kind[kind])))]
    print(f"{len(sample)} questions (at most {len(sample)} API calls)")

    client = anthropic.Anthropic()

    def check(item: tuple[Path, dict]) -> tuple:
        directory, question = item
        bmap = BuildingMap.load(directory)
        try:
            parsed_kind, params = parse(bmap, question["question"], client)
            return parsed_kind, params, reply(parsed_kind, run(bmap, parsed_kind, params))
        except Exception as error:  # report and keep going
            return None, {"error": str(error)}, ""

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(check, sample))
    params_ok, scored = Counter(), Counter()
    for (directory, question), (parsed_kind, params, text) in zip(sample, outcomes):
        kind = question["kind"]
        same = parsed_kind == kind and params == question["inputs"]
        params_ok[kind] += same
        scored[kind] += score_reply(question["answer"], text)
        if not same:
            print(f"  {question['id']} [{kind}] expected {question['inputs']}\n      got {parsed_kind} {params}")
    for kind in sorted(by_kind):
        total = min(args.per_kind, len(by_kind[kind]))
        print(f"{kind:28} params {params_ok[kind]}/{total}  scored {scored[kind]}/{total}")
    print(f"TOTAL scored {sum(scored.values())}/{len(sample)}")


if __name__ == "__main__":
    main()
