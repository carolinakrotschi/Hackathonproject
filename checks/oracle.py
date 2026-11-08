"""Free check of the core: feed each benchmark question's ground-truth `inputs` straight into core.queries
and score the reply with the official scorer. No LLM involved.

  uv run python -m checks.oracle data/train data/dev [--kind errand] [--show 5]"""

import argparse
import json
import traceback
from collections import Counter, defaultdict
from pathlib import Path

from core.building_map import BuildingMap
from core.queries import reply, run
from core.score import score_reply


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("splits", nargs="+", type=Path)
    parser.add_argument("--kind")
    parser.add_argument("--show", type=int, default=3, help="failures to print per kind")
    args = parser.parse_args()

    totals, correct = Counter(), Counter()
    failures = defaultdict(list)
    for split in args.splits:
        for path in sorted(split.glob("*/questions.jsonl")):
            bmap = BuildingMap.load(path.parent)
            for line in path.read_text(encoding="utf-8").splitlines():
                question = json.loads(line)
                kind = question["kind"]
                if args.kind and kind != args.kind:
                    continue
                totals[kind] += 1
                try:
                    result = run(bmap, kind, question["inputs"])
                    text = reply(kind, result)
                except Exception:
                    result, text = {"error": traceback.format_exc(limit=2)}, ""
                if score_reply(question["answer"], text):
                    correct[kind] += 1
                else:
                    failures[kind].append((question["id"], question["answer"], result))

    for kind in sorted(totals):
        print(f"{kind:28} {correct[kind]:4}/{totals[kind]:<4} {100 * correct[kind] / totals[kind]:5.1f}%")
        for question_id, answer, result in failures[kind][: args.show]:
            expected = answer.get("value", answer.get("accept"))
            print(f"    {question_id}: expected {expected} got {json.dumps(result, default=str)[:300]}")
    print(f"{'TOTAL':28} {sum(correct.values()):4}/{sum(totals.values()):<4}")


if __name__ == "__main__":
    main()
