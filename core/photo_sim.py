"""Turn a clean plan image into a plausible phone photo of it, for testing the importer without a camera.

The plan is printed on paper (white margin), lies on a table, and is photographed at an angle: higher resolution than
the source, perspective and rotation, uneven light and a colour cast, slight defocus, sensor noise and JPEG artefacts.
Everything is driven by a seed, so a test photo is reproducible."""

import io
import random
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

SEVERITY = {
    #          upscale, camera tilt°, rotation°, light range, cast, blur σ, noise σ, jpeg quality
    "mild":   (2.5, 8, 3, (0.92, 1.00), 0.03, 0.6, 2.5, 90),
    "medium": (2.5, 18, 8, (0.80, 1.00), 0.06, 1.0, 4.0, 80),
    "strong": (2.2, 28, 14, (0.65, 1.00), 0.09, 1.4, 6.0, 65),
}
FOCAL = 0.9  # focal length as a fraction of the image width: a typical phone main camera


def simulate(source: Path, severity: str = "medium", seed: int = 0) -> Image.Image:
    rng = random.Random(f"{source.name}-{severity}-{seed}")
    upscale, tilt, rotation, light, cast, blur, noise, quality = SEVERITY[severity]
    plan = cv2.cvtColor(np.asarray(Image.open(source).convert("RGB")), cv2.COLOR_RGB2BGR)
    plan = cv2.resize(plan, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    margin = int(0.04 * max(plan.shape[:2]))
    paper = cv2.copyMakeBorder(plan, margin, margin, margin, margin, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    h, w = paper.shape[:2]

    # table: a canvas larger than the paper, warm wood-ish tone with texture
    # a phone frame is 3:4 (or 4:3), however narrow the sheet is
    if h >= w:
        canvas_h = int(h * 1.25)
        canvas_w = max(int(w * 1.25), int(canvas_h * 3 / 4))
    else:
        canvas_w = int(w * 1.25)
        canvas_h = max(int(h * 1.25), int(canvas_w * 3 / 4))
    base = np.array([rng.randint(60, 110), rng.randint(90, 140), rng.randint(120, 170)], dtype=np.float32)
    table = np.ones((canvas_h, canvas_w, 3), np.float32) * base
    grain = cv2.GaussianBlur(np.random.default_rng(seed).normal(0, 18, (canvas_h, canvas_w)).astype(np.float32), (0, 0), 6)
    table += grain[..., None]

    # where the paper's corners land: the sheet is tilted in 3D and seen through a pinhole camera
    cx, cy = canvas_w / 2, canvas_h / 2
    ax, ay = np.radians(rng.uniform(-tilt, tilt)), np.radians(rng.uniform(-tilt, tilt))
    az = np.radians(rng.uniform(-rotation, rotation))
    rx = np.array([[1, 0, 0], [0, np.cos(ax), -np.sin(ax)], [0, np.sin(ax), np.cos(ax)]])
    ry = np.array([[np.cos(ay), 0, np.sin(ay)], [0, 1, 0], [-np.sin(ay), 0, np.cos(ay)]])
    rz = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]])
    turn = rx @ ry @ rz
    focal = FOCAL * canvas_w
    distance = focal * 1.45 * max(w / canvas_w, h / canvas_h)  # the whole sheet in frame, about 70% of it
    for _ in range(20):  # like a person framing the shot: step back until all four corners are in the picture
        corners = []
        for x, y in ((0, 0), (w, 0), (w, h), (0, h)):
            point = turn @ np.array([x - w / 2, y - h / 2, 0.0]) + np.array([0, 0, distance])
            corners.append((cx + focal * point[0] / point[2], cy + focal * point[1] / point[2]))
        inside = all(0.03 * canvas_w < x < 0.97 * canvas_w and 0.03 * canvas_h < y < 0.97 * canvas_h for x, y in corners)
        if inside:
            break
        distance *= 1.08
    matrix = cv2.getPerspectiveTransform(np.float32([(0, 0), (w, 0), (w, h), (0, h)]), np.float32(corners))
    warped = cv2.warpPerspective(paper, matrix, (canvas_w, canvas_h), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0))
    mask = cv2.warpPerspective(np.ones((h, w), np.float32), matrix, (canvas_w, canvas_h))[..., None]
    photo = warped.astype(np.float32) * mask + table * (1 - mask)

    # light: a smooth gradient across the frame plus vignetting; a colour cast (white balance)
    yy, xx = np.mgrid[0:canvas_h, 0:canvas_w].astype(np.float32)
    direction = rng.uniform(0, 2 * np.pi)
    ramp = (np.cos(direction) * (xx / canvas_w - 0.5) + np.sin(direction) * (yy / canvas_h - 0.5)) + 0.5
    low, high = light
    gain = low + (high - low) * ramp
    vignette = 1 - 0.18 * (((xx - cx) / canvas_w) ** 2 + ((yy - cy) / canvas_h) ** 2) * 2
    photo *= (gain * vignette)[..., None]
    photo *= np.array([1 + rng.uniform(-cast, cast) for _ in range(3)], dtype=np.float32)

    photo = cv2.GaussianBlur(photo, (0, 0), blur)
    photo += np.random.default_rng(seed + 1).normal(0, noise, photo.shape).astype(np.float32)
    photo = np.clip(photo, 0, 255).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(cv2.cvtColor(photo, cv2.COLOR_BGR2RGB)).save(buffer, format="JPEG", quality=quality)
    return Image.open(io.BytesIO(buffer.getvalue())).convert("RGB")
