# Benchmark: how to run our system

## Secret building (one building folder per run)

```bash
uv sync
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
uv run --env-file .env python sunday.py <building folder> <questions file> --out answers.json
```

- `<building folder>`: the directory with `building.json` (as in the kit's `data/test/<building_id>/`); the
  `building.json` file itself also works.
- `<questions file>`: the kit's format (`questions.jsonl`), or JSON, CSV with `question_id`/`question`
  columns, or plain text with one question per line.
- `--out answers.json` writes a list of `{"question_id": ..., "answer": ...}` (the format of `answers.json`
  here); a name ending in `.csv` writes `question_id,answer` instead.

Example with a kit building:

```bash
uv run --env-file .env python sunday.py data/test/swiss-1195 data/test/swiss-1195/questions.jsonl --out answers.json
```

(`data` is the kit's `data/` directory; link or copy it next to this repo's files.)

The kit's own evaluator works too (a copy of the kit's `benchmark/` is in this repo). It takes the folder that
*contains* the building folder(s), as with `data/test`:

```bash
uv run --env-file .env python -m benchmark.evaluate <folder with <building_id>/building.json + questions.jsonl> \
  --agent bench:answer --submission-csv answers.csv
```

## Test set (`answers.json` in this folder)

`answers.json` holds our replies to all 145 test questions, produced by:

```bash
uv run --env-file .env python -m benchmark.evaluate data/test --agent bench:answer --submission-csv answers.csv
```

The same command on dev scores **138/138** with the kit's scorer (`benchmark/score.py`, kit 1.1.0 route rules).

## How it answers

1. **Parse (one Claude call per question).** `core/parse.py` asks Claude (low effort, structured JSON output)
   for the question kind and the names exactly as written: rooms, floors, room/asset type, closed lift, door width.
2. **Resolve (code).** Names are matched to ids in `building.json`: floors by name, rooms by exact name, then
   ref, then a unique substring match.
3. **Compute (code).** `core/queries.py` computes the answer from the plan: polygon areas and centroids, shared
   walls, stacking by overlap, and Dijkstra on the judges' route model (`core/building_map.py`: door-to-door
   walking 15 cm off the walls, stairs 2 m per metre climbed, lifts 15 m + 1 m per metre, no way round the
   outside) with step-free, door-width, closure and what-if-door constraints.
4. **Reply.** A template writes a short explanation and the final `Answer:` line.

If a question doesn't fit the parser (a new type, unexpected wording, names it can't resolve), a tool-using
Claude agent takes over (`core/fallback.py`) with the same computations as tools and ends with the same
`Answer:` line. Besides the 9 benchmark kinds it answers routes, step-free routes, routes with closures, tours,
locate and count questions.

More detail, the plan importer and our checks: `README.md` in the repo root.
