"""Measure the floor-plan importer against the kit: import each building's floor_*_plain.png images and compare
with its building.json. Free unless --labels is given (one Claude call per floor to read room numbers).

  uv run python -m checks.import_eval data/dev --source procedural --limit 10
  uv run --env-file .env python -m checks.import_eval data/dev --source procedural --limit 3 --labels"""

import argparse
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

from shapely.affinity import translate
from shapely.geometry import Polygon

from core.building_map import BuildingMap, MapError
from core.photo_sim import simulate
from core.plan_import import assemble, check, footprint_px, scan_floor

ROUTE_KINDS = ("route", "route_step_free")
ROUTE_TOLERANCE = 0.05


def plan_images(directory: Path) -> list[Path]:
    images = list(directory.glob("floor_*_plain.png"))
    return sorted(images, key=lambda p: int(re.search(r"floor_(-?\d+)_plain", p.name).group(1)))


def shape(space: dict) -> Polygon:
    polygon = Polygon(space["polygon"])
    return polygon if polygon.is_valid else polygon.buffer(0)


def evaluate(directory: Path, read_labels=None, photo: str | None = None, work: Path | None = None) -> dict:
    truth = json.loads((directory / "building.json").read_text(encoding="utf-8"))
    images = plan_images(directory)
    if photo:  # simulate phone photos of the plans
        work.mkdir(parents=True, exist_ok=True)
        shots = []
        for image in images:
            shot = work / f"{directory.name}-{image.stem}-{photo}.jpg"
            if not shot.exists():
                simulate(image, photo).save(shot, quality=95)
            shots.append(shot)
        images = shots
    scans = [scan_floor(path) for path in images]
    # oracle scale from the first floor in the list (the lowest): geometry is judged separately from the scale guess
    lowest = min(truth["floors"], key=lambda f: f["level"])
    xs = [x for s in truth["spaces"] if s["floor_id"] == lowest["id"] for x, _ in s["polygon"]]
    width_px, _ = footprint_px(scans[0])
    px_per_m = (width_px or 1.0) / (max(xs) - min(xs))
    labels = read_labels(scans) if read_labels else None
    ours = assemble(scans, px_per_m, labels)
    issues = check(ours)
    # like the app's review step: plans don't show floor heights, so the user confirms them
    for mine, real in zip(sorted(ours["floors"], key=lambda f: f["level"]), sorted(truth["floors"], key=lambda f: f["level"])):
        mine["elevation_m"], mine["height_m"] = real["elevation_m"], real["height_m"]

    # same order of floors; shift ours so the drawings line up
    truth_floors = sorted(truth["floors"], key=lambda f: f["level"])
    our_floors = sorted(ours["floors"], key=lambda f: f["level"])
    # compare floor by floor: line each floor up by its lower-left corner (kit Swiss floors aren't aligned either)
    our_shapes = {}
    for tf, of in zip(truth_floors, our_floors):
        mine = [s for s in ours["spaces"] if s["floor_id"] == of["id"]]
        real = [s for s in truth["spaces"] if s["floor_id"] == tf["id"]]
        if not mine:
            continue
        ox, oy = min(x for s in mine for x, _ in s["polygon"]), min(y for s in mine for _, y in s["polygon"])
        tx, ty = min(x for s in real for x, _ in s["polygon"]), min(y for s in real for _, y in s["polygon"])
        if photo:  # like the app's review step: the user confirms the floor's width and depth
            ow = max(x for s in mine for x, _ in s["polygon"]) - ox
            oh = max(y for s in mine for _, y in s["polygon"]) - oy
            rw = max(x for s in real for x, _ in s["polygon"]) - tx
            rh = max(y for s in real for _, y in s["polygon"]) - ty
            for space in mine:
                space["polygon"] = [[ox + (x - ox) * rw / ow, oy + (y - oy) * rh / oh] for x, y in space["polygon"]]
        our_shapes |= {s["id"]: translate(shape(s), tx - ox, ty - oy) for s in mine}
    match, ious = {}, []
    for tf, of in zip(truth_floors, our_floors):
        candidates = [s for s in ours["spaces"] if s["floor_id"] == of["id"]]
        for space in (s for s in truth["spaces"] if s["floor_id"] == tf["id"]):
            truth_shape = shape(space)
            best, best_iou = None, 0.0
            for candidate in candidates:
                other = our_shapes[candidate["id"]]
                if not truth_shape.intersects(other):
                    continue
                iou = truth_shape.intersection(other).area / truth_shape.union(other).area
                if iou > best_iou:
                    best, best_iou = candidate["id"], iou
            ious.append(best_iou)
            if best and best_iou >= 0.5:
                match[space["id"]] = best
    pairs = lambda building: {frozenset(o["space_ids"]) for o in building["openings"] if "outside" not in o["space_ids"]}  # noqa: E731
    truth_pairs = {frozenset(match.get(s) for s in pair) for pair in pairs(truth)}
    truth_pairs = {p for p in truth_pairs if None not in p and len(p) == 2}
    our_pairs = pairs(ours)
    labelled = [s for s in truth["spaces"] if s["id"] in match and s.get("ref")]
    refs = {s["id"]: s for s in ours["spaces"]}
    result = {
        "building": truth["building"]["id"], "source": truth["building"]["source"],
        "spaces": (len(ours["spaces"]), len(truth["spaces"])),
        "mean_iou": statistics.mean(ious) if ious else 0.0,
        "matched": len(match) / len(truth["spaces"]),
        "door_recall": len(our_pairs & truth_pairs) / len(truth_pairs) if truth_pairs else 1.0,
        "door_precision": len(our_pairs & truth_pairs) / len(our_pairs) if our_pairs else 1.0,
        "entrances": (sum("outside" in o["space_ids"] for o in ours["openings"]),
                      sum("outside" in o["space_ids"] for o in truth["openings"])),
        "connectors": (len(ours["vertical_connectors"]), len(truth["vertical_connectors"])),
        "issues": issues,
        "px_per_m": px_per_m,
    }
    if labels:
        result["labels"] = sum(refs[match[s["id"]]]["ref"] == s["ref"] for s in labelled) / max(1, len(labelled))
    # routes from the warm-up questions, on the imported building with the kit's route model
    imported = BuildingMap(ours)
    errors = []
    questions = directory / "questions_basic.jsonl"
    for line in questions.read_text(encoding="utf-8").splitlines() if questions.exists() else []:
        question = json.loads(line)
        if question["kind"] not in ROUTE_KINDS or question["answer"].get("reachable") is False:
            continue
        start, goal = match.get(question["answer"]["from_space_id"]), match.get(question["answer"]["to_space_id"])
        if not start or not goal:
            errors.append(None)
            continue
        try:
            length = imported.route(start, goal, question["kind"] == "route_step_free").length_m
            errors.append(abs(length - question["answer"]["optimal_length_m"]) / question["answer"]["optimal_length_m"])
        except MapError:
            errors.append(None)
    result["routes_ok"] = (sum(e is not None and e <= ROUTE_TOLERANCE for e in errors), len(errors))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("split", type=Path)
    parser.add_argument("--source", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--labels", action="store_true", help="read room numbers with Claude (costs API credits)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--photo", choices=["mild", "medium", "strong"], help="import simulated phone photos instead")
    args = parser.parse_args()
    read_labels = None
    if args.labels:
        from core.plan_labels import read_labels as read_labels  # noqa: PLC0415
    directories = [d for d in sorted(args.split.iterdir()) if (d / "building.json").exists() and plan_images(d)]
    if args.source:
        directories = [d for d in directories if json.loads((d / "building.json").read_text())["building"]["source"] == args.source]
    directories = directories[: args.limit]
    by_source = defaultdict(list)
    for directory in directories:
        result = evaluate(directory, read_labels, args.photo, Path(".cache/photos"))
        by_source[result["source"]].append(result)
        ok, total = result["routes_ok"]
        print(f"{result['building']:28} spaces {result['spaces'][0]:3}/{result['spaces'][1]:<3} IoU {result['mean_iou']:.2f} "
              f"matched {result['matched']:.0%} doors R{result['door_recall']:.0%} P{result['door_precision']:.0%} "
              f"entr {result['entrances'][0]}/{result['entrances'][1]} conn {result['connectors'][0]}/{result['connectors'][1]} "
              f"routes {ok}/{total}" + (f" labels {result['labels']:.0%}" if "labels" in result else ""))
        if args.verbose and result["issues"]:
            print("    " + "\n    ".join(result["issues"]))
    for source, results in by_source.items():
        routes = [r["routes_ok"] for r in results]
        print(f"\n{source}: {len(results)} buildings, IoU {statistics.mean(r['mean_iou'] for r in results):.2f}, "
              f"matched {statistics.mean(r['matched'] for r in results):.0%}, door recall {statistics.mean(r['door_recall'] for r in results):.0%}, "
              f"precision {statistics.mean(r['door_precision'] for r in results):.0%}, "
              f"routes {sum(o for o, _ in routes)}/{sum(t for _, t in routes)} within {ROUTE_TOLERANCE:.0%}")


if __name__ == "__main__":
    main()
