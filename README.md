# AIdgent

Industrial Agents Hackathon 2026, Track 01 ("Google Maps for every building").
Team: Carolina Krotsch, Ferdinand Lieben-Seutter, Ceyhun Aydin and Tobias Klockau.

We asked where an agent that knows a floor plan would help people most, and picked a hospital. The building stays
put while everything inside it moves: patients are admitted and moved, staff cross the same corridors dozens of
times a shift, and beds and pumps are never where you left them. AIdgent is a voice pager for hospital staff that
knows the building, knows where people and equipment are, and plans routes around both. The same routing core
answers the kit's benchmark questions on any building in the kit format.

## Submission

The `SUBMISSION/` folder holds exactly the two files the organisers asked for:

| File | What it is |
|---|---|
| [`SUBMISSION/answers.json`](SUBMISSION/answers.json) | Our replies to all 145 test questions, a list of `{"question_id", "answer"}`, each reply ending in an `Answer:` line |
| [`SUBMISSION/BENCHMARK.md`](SUBMISSION/BENCHMARK.md) | How to run our system on a new building (the secret building), and how the answers were produced |

The replies come from our agent running on the kit data, with no hand labelling:

```bash
uv run --env-file .env python -m benchmark.evaluate data/test --agent bench:answer --submission-csv answers.csv
```

The same command on dev scores 138/138 with the kit's scorer (`benchmark/score.py`, kit 1.1.0 route rules).

## What it does, by level

| Level | In the benchmark agent | In the hospital pager |
|---|---|---|
| 1 Locate | Rooms and items by name, type or number; counts; largest room; total floor area | "Where is the MRI?" outlines it on the map without starting a route |
| 2 Route | Shortest routes on the judges' model, step-free, closures, minimum door width, tours | "Take me to CT via the pharmacy, don't use lift A", turn by turn, with the next turn always on screen |
| 3 Scale | Floors stacked in 3D, stairs and lifts with their real cost, reachability, what-if doors | Two buildings joined by a ground-floor walkway and a first-floor bridge; beds always ride a lift |
| 4 Build | `import_plans.py`: floor-plan images or phone photos to `building.json` | Upload a ground-floor plan, then name rooms by voice ("A0.05 is the plaster room"), add doors, move equipment |
| 5 Extend | | Moving assets and several users: live positions, lift booking, free-equipment search, voice in and out, a control room with heatmaps |

## How it works

Claude handles language; code handles everything that has a right answer. That is the architecture the brief
recommends: the agent is the conversation layer and calls tools, the core is a deterministic graph.

### Routing core (`core/`)

`core/building_map.py` reads `building.json` into a networkx graph and runs Dijkstra on the judges' route model:
door-to-door walking 15 cm off the walls, stairs at 2 m per metre climbed, lifts at 15 m plus 1 m per metre, no
way round the outside. Constraints are step-free, minimum door width, closed rooms, doors or connectors, and extra
doors. `core/walk.py` turns routes into paths a person can follow: inside each room it walks around furniture and
writes turn-by-turn steps with landmarks. `core/queries.py` computes areas, centroids, shared walls, stacking and
reachability for the benchmark kinds.

### Benchmark agent (`bench.py`, `core/parse.py`, `core/fallback.py`)

1. One Claude call (`core/parse.py`, low effort, structured output) reads the question kind and the names exactly
   as written: rooms, floors, room or asset type, closed lift, door width.
2. Code matches the names to ids: floors by name, rooms by exact name, then ref, then a unique substring.
3. `core/queries.py` computes the answer and a template writes a short explanation and the `Answer:` line.

When the parser can't place a question (a new type, odd wording, a name it can't resolve), a tool-using Claude
agent (`core/fallback.py`) takes over with the same computations as tools and ends with the same `Answer:` line.
Besides the 9 benchmark kinds it answers the warm-up kinds: route, step-free route, route with a closure, tour,
locate and count.

### Floor plans in (`core/plan_import.py`, `core/plan_photo.py`, `core/plan_labels.py`)

Rooms come from fill colour; walls, doors, openings, entrances, stairs and lifts from image analysis; floors are
aligned on their stairs and lifts. Phone photos are straightened first (sheet detection, perspective from the
four corners, light correction against the white paper). Room numbers and the title are read by Claude, one call
per floor (about $0.03). On the 14 dev factory and campus buildings every room is found (IoU 0.94) and every door,
and 56/56 warm-up routes come out within 5 %, from clean plans and from simulated photos.

```bash
uv run --env-file .env python import_plans.py floor_0.png floor_1.png --out building.json --storey-heights-m 3.5
uv run python import_plans.py photos/*.jpg --out building.json --width-m 55.5 --no-labels
```

### Hospital (`hospital/`)

All hospital logic is plain Python on top of the core, behind one lock; `app/hospital_api.py` serves it under
`/api/hospital/*`.

- Trips planned in time (`dispatch.py`, `lifts.py`). Each lift has a list of booked slots. For every leg the
  planner tries the shortest route and one route per lift (and the stairs unless the trip is step-free), walks each
  at 1.2 m/s, queues at the lift and keeps the one that arrives first. Patients with an appointment are told when
  to leave so they meet their lift slot instead of waiting in the corridor.
- Stops (`stops.py`). Coffee, toilet, kiosk, pharmacy or a free item are chosen by total detour, not by distance
  from where you stand. Named stops keep the order they were said in.
- Free equipment (`finder.py`). The nearest free wheelchair, bed, pump or defibrillator by real walking cost. If it
  gets taken while you walk, the search runs again from where you are.
- Positions (`tracking.py`). BLE sightings `[{tag_id, beacon_id, rssi, t}]` become a floor and a weighted centroid
  of the 3 strongest beacons. A position more than 5 m off the planned route triggers a new plan. The simulator
  emits the same sightings, so the pipeline runs as it would with hardware.
- History and insights (`history.py`, `insights.py`). Heatmaps on a 0.5 m grid, recurring routes, and where a
  frequently used item should stand to save walking.
- Room rules and packing (`rules.py`, `optimizer.py`). What may stand in which room and how many, and greedy
  placement with real footprints, door clearances and a reachability check for a 0.9 m wide person.

### Staff pager (`agent.py`, `pager_tools.py`, `utterance.py`, `voice.py`)

The pager is for staff only. Staff decide where a patient goes and how urgent it is; the pager finds the way and
never triages. `pager_tools.py` is everything a pager can do, as plain functions over the core: find, go to (with
stops, step-free, avoided lifts, urgent), find free, cancel, where am I, close or reopen, add a door, label a room,
move equipment. `agent.py` gives these functions to Claude (`claude-opus-5-5`, low effort) as tools, one
conversation per pager, holding the hospital lock only while a tool runs.

Without an API key, or if the API fails, `utterance.py` parses the request itself and calls the same functions.
"Take me to theatre 1 with a bed, stop by the pharmacy and don't use lift A, there are a lot of people" becomes
goal Theatre 1, stop Pharmacy, avoid Lift A, step-free. It splits the request into clauses and gives every room,
item and lift a role from the words before it ("to", "via", "pick up", "avoid", "lift B is crowded"). If avoiding
a crowded lift leaves a bed no way up, it takes the lift anyway and says so; if no wheelchair is free, it skips
that stop and says so. 100 spoken requests with their expected trips are in `hospital/route_cases.py`; the
checklist with one link per case is [docs/route-tests.md](docs/route-tests.md).

On screen: one next-turn card, one "then" line, one status line, and labels only for the rooms that matter now.
That follows the cognitive-load principles from lawsofux.com. Voice in is push-to-talk (hold the mic or Space).
Voice out is Fish Audio through the server, with the browser's voice as fallback.

## Try it

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
uv run --env-file .env uvicorn app.server:app --port 8765
```

`HOSPITAL_BUILDING=demo-klinikum` starts the single-building hospital with patients instead of the two-building
campus. The hospital pages run without an API key; the factory chat and reading labels from plans need one.

| Page | What to try |
|---|---|
| `/hospital/pager/PG-10` | Test pager at the main entrance. Hold Space: "Take me to theatre 1 with a bed, stop by the pharmacy, lift A is too busy". L walks the route, R clears everything |
| `/hospital/pager` | Pick a staff pager |
| `/hospital` | Control room: live positions, devices, room optimizer, analytics, plan upload, simulator |
| `/hospital/maps` | The hospital as a map view |
| `/hospital/visit` | Visitor page: patient `100231`, code `4711` |
| `/` | Factory app on the 26 kit factories: "how do I get from the entrance to the CNC mill without stairs?", then "close the east lift" and ask again |

A 3 to 5 minute demo: open PG-10, ask for theatre 1 with a bed via the pharmacy and say lift A is crowded; press L
and watch it take lift B. Press R. Say "Code blue in room A2.05, grab a defibrillator on the way" and walk it.
Upload `samples/hospital/demo-campus/floor_0.png` in the control room and label a room by voice. Finish on the
factory app with a step-free question and a closed lift.

## Sunday building

`sunday.py` takes a building (a directory with `building.json`, the file itself, or a folder of buildings when the
questions name a `building_id`) and a question file (JSONL, JSON, CSV with `question_id` and `question`, or plain
text with one question per line). A name ending in `.csv` writes the submission format.

```bash
uv run --env-file .env python sunday.py <building> <questions file> --out answers.csv
```

`benchmark.evaluate` also works if the release uses the kit's layout (point it at the directory holding
`<building_id>/building.json` and `questions.jsonl`).

## Checks

The kit's `data/` directory goes next to these files (it is git-ignored).

```bash
uv run --group dev pytest -q                                     # 307 tests, free
uv run python -m checks.oracle data/train data/dev               # core on ground-truth inputs: 655/655
uv run python -m checks.warmup_oracle data/train data/dev        # routes, tours, locate, count: 2130/2130
uv run --env-file .env python -m checks.parse_check data/dev --per-kind 1000   # parser + core: 138/138
uv run --env-file .env python -m checks.fallback_check           # fallback agent alone, new question types: 19/20
uv run python -m checks.import_eval data/dev --source procedural  # plan import against building.json
uv run --env-file .env python -m checks.interaction_eval         # scripted tricky users, graded by Claude
uv run python -m hospital.route_cases                            # the 100 pager requests: 100/100
```

## Code

```
bench.py, sunday.py   benchmark entry point and the Sunday runner
import_plans.py       floor-plan images to building.json
app/                  FastAPI server, factory chat agent, hospital API, static pages (plain JS and SVG)
core/                 route model, walking paths, benchmark queries, question parser, fallback agent, plan import
hospital/             hospital state, trip planning, lifts, stops, tracking, history, rules, pager tools and agent
hospitals/            demo-campus (default, two buildings) and demo-klinikum (one building, patients)
factories/            the 26 CC0 procedural factories from the kit, so the demo runs without it
samples/              plan images and simulated phone photos to try the import
benchmark/            the kit's scorer and evaluator
checks/               measurement scripts (above)
tests/                pytest
SUBMISSION/           answers.json (the 145 test replies) and BENCHMARK.md (how to run it)
```

Python 3.12+, FastAPI, shapely, networkx, numpy, OpenCV and the Anthropic SDK on the server; plain JavaScript and
SVG in the browser, no build step.

## Limits

- Hospital state lives in memory and is lost on restart. There's no login and no link to hospital IT.
- The demo hospitals and their week of movement are generated, and the beacons are simulated. A real deployment
  needs BLE tags, gateways, a works council agreement for staff tracking and patient opt-in.
- Without an API key the pager understands the request shapes in `hospital/route_cases.py` and close variants;
  unusual wording needs Claude.
- Imported plans have no furniture, and room types guessed from fill colour are rough for hospital plans; naming
  rooms by voice fixes them.

## Data and licences

`factories/` and `samples/` hold CC0 1.0 procedural buildings from the hackathon kit. The kit's other sources
(Swiss Dwellings and ResPlan under CC BY 4.0, OpenStreetMap under ODbL 1.0) are only read from the kit's `data/`
directory and are not redistributed here; see the kit's `ATTRIBUTION.md`.
