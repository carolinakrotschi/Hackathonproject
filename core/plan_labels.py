"""Read room numbers and the title of floor-plan images with Claude, one call per floor.

The image analysis (plan_import.scan_floor) already knows where each room's label pixels are. We crop those, enlarge
them, and tile them into numbered contact sheets, so Claude only transcribes text and every answer belongs to exactly
one room; it never has to locate anything. Results are cached by sheet content."""

import base64
import hashlib
import io
import json
import os
import threading
from pathlib import Path

import anthropic
from PIL import Image, ImageDraw, ImageFont

from core.plan_import import FloorScan

MODEL = os.environ.get("LABEL_MODEL", "claude-opus-5")
EFFORT = os.environ.get("LABEL_EFFORT", "low")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CACHE_FILE = Path(__file__).resolve().parent.parent / ".cache" / "labels.jsonl"
UPSCALE = 4
TEXT_HEIGHT = 34  # px of label text on the contact sheet
PAD_PX = 3
PAD_X_PX = 10  # glyphs touching a door marker are not in the text box; widen the crop sideways
SHEET_WIDTH = 1100
MAX_SHEET_HEIGHT = 1500
TAG_WIDTH = 64
GAP = 10
_LOCK = threading.Lock()

PROMPT = """These sheets show text cropped from a building floor plan. Each crop has a number tag on its left.
Tag T is the plan's title (building name and floor). The other tags are room labels, usually room numbers \
such as 0.33, 1.07, H-A3, N8105 or a short name.
Transcribe each crop exactly as printed: keep dots, dashes, letters and their case. Skip a tag if the crop is \
unreadable. Do not guess or invent labels."""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": ["string", "null"]},
        "labels": {"type": "array", "items": {
            "type": "object",
            "properties": {"tag": {"type": "integer"}, "text": {"type": "string"}},
            "required": ["tag", "text"], "additionalProperties": False}},
    },
    "required": ["title", "labels"],
    "additionalProperties": False,
}


def crops(scan: FloorScan) -> list[tuple[str, Image.Image]]:
    image = Image.fromarray(scan.rgb) if scan.rgb is not None else Image.open(scan.image).convert("RGB")
    out = []
    boxes = [("T", scan.title_box)] + [(str(i), room["text_box"]) for i, room in enumerate(scan.rooms)]
    for tag, box in boxes:
        if not box:
            continue
        x0, y0, x1, y1 = box
        unit = max(1, (y1 - y0) / 8)  # kit labels are about 8 px tall; photos are larger
        pad_x, pad_y = int((PAD_PX if tag == "T" else PAD_X_PX) * unit), int(PAD_PX * unit)
        crop = image.crop((max(0, x0 - pad_x), max(0, y0 - pad_y), min(image.width, x1 + pad_x), min(image.height, y1 + pad_y)))
        # enlarge to a readable text height whatever the source resolution (kit renders vs phone photos)
        target = TEXT_HEIGHT if tag != "T" else TEXT_HEIGHT * 0.75
        scale = min(UPSCALE, max(1.0, target / max(1, y1 - y0)))
        crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))), Image.LANCZOS)
        if crop.width > SHEET_WIDTH - TAG_WIDTH - GAP:
            ratio = (SHEET_WIDTH - TAG_WIDTH - GAP) / crop.width
            crop = crop.resize((int(crop.width * ratio), max(1, int(crop.height * ratio))), Image.LANCZOS)
        out.append((tag, crop))
    return out


def sheets(tiles: list[tuple[str, Image.Image]]) -> list[Image.Image]:
    """Pack tiles left to right into rows, rows into sheets of bounded height."""
    font = ImageFont.load_default(size=22)
    rows, row, row_width = [], [], 0
    for tag, crop in tiles:
        width = TAG_WIDTH + crop.width + GAP
        if row and row_width + width > SHEET_WIDTH:
            rows.append(row)
            row, row_width = [], 0
        row.append((tag, crop))
        row_width += width
    if row:
        rows.append(row)
    pages, page, height = [], [], 0
    for row in rows:
        row_height = max(max(c.height for _, c in row), 30) + GAP
        if page and height + row_height > MAX_SHEET_HEIGHT:
            pages.append(page)
            page, height = [], 0
        page.append(row)
        height += row_height
    if page:
        pages.append(page)
    images = []
    for page in pages:
        heights = [max(max(c.height for _, c in row), 30) + GAP for row in page]
        sheet = Image.new("RGB", (SHEET_WIDTH, sum(heights) + GAP), "white")
        draw = ImageDraw.Draw(sheet)
        y = GAP
        for row, row_height in zip(page, heights):
            x = GAP
            for tag, crop in row:
                draw.rectangle((x, y, x + TAG_WIDTH - 8, y + 28), fill="#1a73e8")
                draw.text((x + 6, y + 2), tag, fill="white", font=font)
                sheet.paste(crop, (x + TAG_WIDTH, y))
                draw.rectangle((x + TAG_WIDTH - 1, y - 1, x + TAG_WIDTH + crop.width, y + crop.height), outline="#bbbbbb")
                x += TAG_WIDTH + crop.width + GAP
            y += row_height
        images.append(sheet)
    return images


def png_b64(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return base64.standard_b64encode(buffer.getvalue()).decode()


def _cached(key: str) -> dict | None:
    if not CACHE_FILE.exists():
        return None
    for line in CACHE_FILE.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry["key"] == key:
            return entry["value"]
    return None


def read_floor(scan: FloorScan, client: anthropic.Anthropic, usage: dict | None = None) -> dict:
    tiles = crops(scan)
    if not tiles:
        return {"title": None, "rooms": {}}
    pages = [png_b64(page) for page in sheets(tiles)]
    key = hashlib.sha256((MODEL + PROMPT + "".join(pages)).encode()).hexdigest()
    result = _cached(key)
    if result is None:
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": page}} for page in pages]
        content.append({"type": "text", "text": PROMPT})
        response = client.beta.messages.create(
            model=MODEL, max_tokens=8000, messages=[{"role": "user", "content": content}],
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=[FALLBACK_BETA], fallbacks="default",
        )
        if usage is not None:
            usage["input_tokens"] = usage.get("input_tokens", 0) + response.usage.input_tokens
            usage["output_tokens"] = usage.get("output_tokens", 0) + response.usage.output_tokens
        if response.stop_reason in ("refusal", "max_tokens"):
            return {"title": None, "rooms": {}}
        result = json.loads(next(block.text for block in response.content if block.type == "text"))
        CACHE_FILE.parent.mkdir(exist_ok=True)
        with _LOCK, CACHE_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"key": key, "value": result}, ensure_ascii=False) + "\n")
    rooms = {item["tag"]: item["text"].strip() for item in result["labels"] if item["text"].strip()}
    return {"title": result.get("title"), "rooms": rooms}


def read_labels(scans: list[FloorScan], client: anthropic.Anthropic | None = None, usage: dict | None = None) -> list[dict]:
    client = client or anthropic.Anthropic()
    return [read_floor(scan, client, usage) for scan in scans]
