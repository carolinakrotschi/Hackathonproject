"""Draw a building's floors as clean plan images in the kit's plan style, ready for the floor-plan import (level 4):
`python -m hospital.render_plan hospitals/demo-campus samples/hospital/demo-campus` writes floor_<level>.png.

Same conventions as core/plan_import.py reads them: a fill colour per kind of space, thin dark walls, a brown dot per
door, a grey dot per opening, a red triangle per entrance, the room label in the room and the title on top. Labels
are what a hospital plan prints: the sign name over the room number ("CT" / "A0.33"); small rooms only the number."""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core.plan_import import FILLS, KIT_PX_PER_M, MARKERS

MARGIN_PX = 40
TITLE_PX = 36
WALL = (68, 68, 68)
TEXT = (0, 0, 0)
KIND = {"room": "room", "corridor": "corridor", "hall": "hall", "lift": "lift", "stair": "stair", "outdoor": "outdoor"}
FONT_PX = 9
NAME_MIN_WIDTH_M = 3.5  # narrower rooms print only their number


def font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def printable(text: str) -> str:
    """The plan fonts have no typographic dashes; a box glyph would read as a tiny room."""
    return text.replace("–", "-").replace("—", "-")


def label_lines(space: dict, width_m: float) -> list[str]:
    ref, name = space.get("ref") or "", space["name"]
    if space["type"] in ("lift", "stair") or not ref:
        return [name]
    if name.endswith(ref) or width_m < NAME_MIN_WIDTH_M or len(name) > 3 * width_m:
        return [ref]
    return [name, ref]


def render(building: dict, out_dir: Path, px_per_m: float = KIT_PX_PER_M) -> list[Path]:
    xs = [x for s in building["spaces"] for x, _ in s["polygon"]]
    ys = [y for s in building["spaces"] for _, y in s["polygon"]]
    x0, y1 = min(xs), max(ys)
    width = round((max(xs) - x0) * px_per_m) + 2 * MARGIN_PX
    height = round((y1 - min(ys)) * px_per_m) + 2 * MARGIN_PX + TITLE_PX

    def px(x: float, y: float) -> tuple[float, float]:
        return MARGIN_PX + (x - x0) * px_per_m, MARGIN_PX + TITLE_PX + (y1 - y) * px_per_m

    text_font, title_font = font(FONT_PX), font(14)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for floor in sorted(building["floors"], key=lambda f: f["level"]):
        spaces = [s for s in building["spaces"] if s["floor_id"] == floor["id"]]
        if not spaces:
            continue
        image = Image.new("RGB", (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.fontmode = "1"  # no anti-aliasing: grey glyph edges would read as opening markers
        for space in spaces:
            draw.polygon([px(x, y) for x, y in space["polygon"]], fill=FILLS[KIND.get(space["type"], "room")])
        for space in spaces:
            draw.polygon([px(x, y) for x, y in space["polygon"]], outline=WALL)
        for space in spaces:
            sx = [x for x, _ in space["polygon"]]
            sy = [y for _, y in space["polygon"]]
            lines = label_lines(space, max(sx) - min(sx))
            cx, cy = px((min(sx) + max(sx)) / 2, (min(sy) + max(sy)) / 2)
            for number, line in enumerate(lines):
                draw.text((cx, cy + (number - (len(lines) - 1) / 2) * (FONT_PX + 2)), printable(line), fill=TEXT,
                          font=text_font, anchor="mm")
        for opening in (o for o in building["openings"] if o["floor_id"] == floor["id"]):
            x, y = px(*opening["position"])
            if opening["kind"] == "entrance" or "outside" in opening["space_ids"]:
                draw.polygon([(x - 5, y + 4), (x + 5, y + 4), (x, y - 5)], fill=MARKERS["entrance"])
            else:
                colour = MARKERS["opening" if opening["kind"] == "opening" else "door"]
                draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=colour)
        title = printable(f"{building['building']['name']} - {floor['name']}")
        draw.text((width / 2, MARGIN_PX / 2 + 8), title, fill=TEXT, font=title_font, anchor="mm")
        path = out_dir / f"floor_{floor['level']}.png"
        image.save(path)
        paths.append(path)
    return paths


def main() -> None:
    source, target = Path(sys.argv[1]), Path(sys.argv[2])
    building = json.loads((source / "building.json").read_text(encoding="utf-8"))
    for path in render(building, target):
        print(path)


if __name__ == "__main__":
    main()
