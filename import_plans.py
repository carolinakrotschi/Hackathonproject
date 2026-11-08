"""Floor plans in, building.json out: the importer behind the app's "Add a building", as one command.

One image per floor: clean plan renders in the kit's style or phone photos of printed plans. Rooms, doors,
openings, entrances, stairs and lifts come from image analysis (free); room numbers and the title are read by
Claude, one call per floor (about $0.03; --no-labels skips it). Rendered kit-style plans have a known scale; for
photos give --width-m (and --depth-m), otherwise the size is estimated. Plans don't show how far apart the floors
are (what a staircase climbs): give --storey-heights-m, otherwise 4 m is assumed.

  uv run --env-file .env python import_plans.py floor_0.png floor_1.png --out building.json
  uv run python import_plans.py plans/*.png --out building.json --no-labels --storey-heights-m 3.5"""

import argparse
import json
from pathlib import Path

from core.plan_import import assemble, check, default_scale, scan_floor, set_dimensions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("images", nargs="+", type=Path, help="one plan image or photo per floor, any order")
    parser.add_argument("--out", type=Path, default=Path("building.json"))
    parser.add_argument("--name", help="building name (default: read from the plan titles)")
    parser.add_argument("--width-m", type=float, help="real width of the building (the ground floor)")
    parser.add_argument("--depth-m", type=float, help="real depth, if photos may be off in proportion")
    parser.add_argument("--storey-heights-m", type=float, nargs="+",
                        help="floor-to-floor distance above each floor, lowest floor first (one value = all floors)")
    parser.add_argument("--no-labels", action="store_true", help="don't read room numbers with Claude (offline, free)")
    args = parser.parse_args()

    scans = [scan_floor(path) for path in args.images]
    px_per_m, guessed = default_scale(scans, args.width_m)
    labels = None
    if not args.no_labels:
        from core.plan_labels import read_labels  # noqa: PLC0415
        labels = read_labels(scans)
    building = assemble(scans, px_per_m, labels, args.name)
    building["building"]["source_ref"] |= {"photos": any(s.photo for s in scans), "size_guessed": guessed,
                                           "heights_guessed": not args.storey_heights_m}
    if args.depth_m and args.width_m:
        set_dimensions(building, args.width_m, args.depth_m)
    if args.storey_heights_m:
        set_dimensions(building, floor_heights_m=args.storey_heights_m)
    args.out.write_text(json.dumps(building, indent=1, ensure_ascii=False), encoding="utf-8")

    xs = [x for s in building["spaces"] for x, _ in s["polygon"]]
    ys = [y for s in building["spaces"] for _, y in s["polygon"]]
    rooms = sum(s["type"] in ("room", "hall") for s in building["spaces"])
    doors = sum("outside" not in o["space_ids"] for o in building["openings"])
    print(f"{building['building']['name']}: {len(building['floors'])} floors, {len(building['spaces'])} spaces "
          f"({rooms} rooms), {doors} doors, {len(building['openings']) - doors} entrances, "
          f"{len(building['vertical_connectors'])} stairs/lifts, {max(xs) - min(xs):.1f} x {max(ys) - min(ys):.1f} m"
          + (" (size estimated: pass --width-m)" if guessed else ""))
    for issue in check(building):
        print(f"  check: {issue}")
    print(f"written to {args.out}")


if __name__ == "__main__":
    main()
