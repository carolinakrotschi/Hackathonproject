"""Run an agent on the benchmark questions (questions.jsonl) and score its replies.

  uv run --env-file .env python -m benchmark.evaluate data/dev --per-kind 4

By default it runs the unchanged starter agent. Pass --agent module:function to evaluate your own; the
function gets (building_directory: Path, question_text: str) and returns the reply text."""

import argparse
import csv
import importlib
import json
import random
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import anthropic

from benchmark.score import score_reply

QUESTIONS_FILENAME = "questions.jsonl"
SUBMISSION_COLUMNS = ("question_id", "answer")
RESULTS_DIRECTORY = Path("agent_output") / "benchmark"
SAMPLE_SEED = 7
OUTPUT_DIRECTORY = RESULTS_DIRECTORY / "scratch"
# claude-opus-5-5 prices, dollars per million tokens; cache writes are 1.25x input (5-minute cache)
PRICE_PER_MILLION = {
    "input_tokens": 4.00,
    "output_tokens": 20.00,
    "cache_read_input_tokens": 0.20,
    "cache_creation_input_tokens": 5.00,
}

AgentFunction = Callable[[Path, str], str]


def load_questions(directory: Path) -> list[tuple[Path, dict]]:
    questions = []
    for path in sorted(directory.glob(f"*/{QUESTIONS_FILENAME}")):
        with path.open(encoding="utf-8") as handle:
            questions += [(path.parent, json.loads(line)) for line in handle if line.strip()]
    return questions


def sample_per_kind(questions: list[tuple[Path, dict]], per_kind: int | None) -> list[tuple[Path, dict]]:
    if per_kind is None:
        return questions
    rng = random.Random(SAMPLE_SEED)
    by_kind: dict[str, list[tuple[Path, dict]]] = {}
    for item in questions:
        by_kind.setdefault(item[1]["kind"], []).append(item)
    sampled = []
    for kind in sorted(by_kind):
        members = by_kind[kind]
        sampled += rng.sample(members, min(per_kind, len(members)))
    return sampled


class StarterAgentRunner:
    """Runs the starter agent exactly as shipped, one fresh conversation per question."""

    def __init__(self) -> None:
        from starter_agent.agent import BuildingAgent
        from starter_agent.building_map import BuildingMap

        self.agent_class = BuildingAgent
        self.map_class = BuildingMap
        self.client = anthropic.Anthropic()
        self.usage: Counter[str] = Counter()

    def __call__(self, building_directory: Path, question_text: str) -> str:
        agent = self.agent_class(self.map_class.load(building_directory), OUTPUT_DIRECTORY, self.client)
        try:
            return agent.ask(question_text)
        finally:
            self.usage.update(agent.usage)


def load_agent(reference: str | None) -> AgentFunction:
    if reference is None:
        return StarterAgentRunner()
    module_name, function_name = reference.split(":")
    return getattr(importlib.import_module(module_name), function_name)


def cost_dollars(usage: Counter[str]) -> float:
    return sum(usage[key] * price / 1_000_000 for key, price in PRICE_PER_MILLION.items())


def write_submission_csv(path: Path, replies: list[tuple[str, str]]) -> None:
    """question_id,answer with the full replies: the file the judge app grades."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(SUBMISSION_COLUMNS)
        writer.writerows(replies)
    print(f"submission CSV with {len(replies)} answers saved to {path}")


def run(
    directory: Path,
    per_kind: int | None,
    agent_reference: str | None,
    results_name: str,
    submission_csv: Path | None = None,
    kinds: list[str] | None = None,
) -> None:
    agent = load_agent(agent_reference)
    results: Counter[tuple[str, bool]] = Counter()
    replies: list[tuple[str, str]] = []
    OUTPUT_DIRECTORY.mkdir(
        parents=True, exist_ok=True
    )  # the starter agent saves edits here without creating it
    results_path = RESULTS_DIRECTORY / f"{results_name}.jsonl"
    with results_path.open("w", encoding="utf-8") as log:
        questions = [item for item in load_questions(directory) if not kinds or item[1]["kind"] in kinds]
        for building_directory, question in sample_per_kind(questions, per_kind):
            try:
                reply = agent(building_directory, question["question"])
            except anthropic.APIError as error:
                print(f"[benchmark] {question['id']}: API error {error}")
                reply = ""
            replies.append((question["id"], reply))
            # The public test set has no answers: then replies are only saved, for the judge to grade.
            correct = score_reply(question["answer"], reply) if "answer" in question else None
            if correct is not None:
                results[(question["kind"], correct)] += 1
            log.write(
                json.dumps(
                    {
                        "id": question["id"],
                        "kind": question["kind"],
                        "correct": correct,
                        "question": question["question"],
                        "answer": question.get("answer"),
                        "reply": reply,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            outcome = {True: "OK ", False: "BAD", None: "saved"}[correct]
            print(f"[benchmark] {question['id']:34s} {question['kind']:20s} {outcome}")
    if results:
        report(results)
    if submission_csv is not None:
        write_submission_csv(submission_csv, replies)
    usage = getattr(agent, "usage", None)
    if usage:
        print(f"tokens: {dict(usage)}  cost: ${cost_dollars(usage):.2f}")
    print(f"replies saved to {results_path}")


def report(results: Counter[tuple[str, bool]]) -> None:
    print("\nkind                  correct / total")
    for kind in sorted({kind for kind, _ in results}):
        correct, wrong = results[(kind, True)], results[(kind, False)]
        print(f"  {kind:20s} {correct:4d} / {correct + wrong}")
    total_correct = sum(count for (_, correct), count in results.items() if correct)
    total = sum(results.values())
    print(f"  {'all':20s} {total_correct:4d} / {total}  ({total_correct / max(total, 1):.0%})")


def main() -> None:
    parser = argparse.ArgumentParser(prog="benchmark.evaluate")
    parser.add_argument("directory", type=Path, help="a split directory such as data/dev")
    parser.add_argument("--per-kind", type=int, default=None, help="sample this many questions per kind")
    parser.add_argument("--kinds", nargs="+", default=None, help="only these question kinds")
    parser.add_argument(
        "--agent", default=None, help="module:function of your agent (default: starter agent)"
    )
    parser.add_argument("--name", default="results", help="file name for the saved replies")
    parser.add_argument(
        "--submission-csv", type=Path, default=None, help="also write question_id,answer for the judge app"
    )
    arguments = parser.parse_args()
    run(
        arguments.directory,
        arguments.per_kind,
        arguments.agent,
        arguments.name,
        arguments.submission_csv,
        arguments.kinds,
    )


if __name__ == "__main__":
    main()
