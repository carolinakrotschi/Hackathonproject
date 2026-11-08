"""Answer a question file about one building and write the submission CSV (question_id,answer).

Made for the Sunday release, whose format is announced only then: the building is a directory with a
building.json or the JSON file itself; questions can be JSONL or JSON (objects with an id and the question text,
under the usual key names), CSV (question_id/id and question columns) or plain text (one question per line).

  uv run --env-file .env python sunday.py <building dir or building.json> <questions file> --out answers.json"""

import argparse
import csv
import json
from pathlib import Path

from bench import answer

ID_KEYS = ("question_id", "id", "qid")
TEXT_KEYS = ("question", "text", "query", "prompt")


def pick(row: dict, keys: tuple[str, ...]) -> str | None:
    return next((str(row[k]) for k in keys if row.get(k) not in (None, "")), None)


def read_questions(path: Path) -> list[tuple[str, str, str | None]]:
    """(question id, question text, building id if the row names one)."""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix == ".csv":
        rows = list(csv.DictReader(text.splitlines()))
    elif path.suffix in (".jsonl", ".ndjson"):
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif path.suffix == ".json":
        data = json.loads(text)
        rows = data if isinstance(data, list) else data.get("questions", [])
    else:
        rows = [{"question": line.strip()} for line in text.splitlines() if line.strip()]
    out = []
    for number, row in enumerate(rows, 1):
        if isinstance(row, str):
            row = {"question": row}
        question = pick(row, TEXT_KEYS)
        if question:
            out.append((pick(row, ID_KEYS) or f"Q{number:03d}", question, row.get("building_id")))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("building", type=Path, help="building directory or building.json; a parent directory of buildings also works")
    parser.add_argument("questions", type=Path)
    parser.add_argument("--out", type=Path, default=Path("answers.json"))
    args = parser.parse_args()
    questions = read_questions(args.questions)
    rows = []
    for number, (question_id, text, building_id) in enumerate(questions, 1):
        building = args.building
        if building_id and (building / building_id / "building.json").exists():
            building = building / building_id  # a folder of several buildings, picked per question
        reply = answer(building, text)
        rows.append((question_id, reply))
        last = reply.strip().splitlines()[-1] if reply.strip() else ""
        print(f"[{number}/{len(questions)}] {question_id}: {last[:90]}", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.suffix == ".json":  # the organisers' format: a list of {question_id, answer}
        args.out.write_text(json.dumps([{"question_id": q, "answer": r} for q, r in rows], ensure_ascii=False, indent=1),
                            encoding="utf-8")
    else:
        with args.out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("question_id", "answer"))
            writer.writerows(rows)
    print(f"{len(rows)} answers written to {args.out}")


if __name__ == "__main__":
    main()
