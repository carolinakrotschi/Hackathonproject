"""Read a phone photo of a floor plan: find and straighten the sheet, even out the light, then find rooms between
the wall lines and classify each by its colour. Produces the same FloorScan as the clean-image path.

Steps:
1. Sheet: the paper is the largest bright quadrilateral; warp it to a rectangle (perspective, rotation).
2. Walls and text: thin dark lines, found with a black-hat filter, which ignores slow changes in brightness.
3. Light: fit a smooth (quadratic) brightness model to the white paper around the building and divide it out,
   which also removes the colour cast, so fills come back close to the plan's palette.
4. Rooms: areas enclosed by walls, classified by their median colour; markers by colour, size and roundness."""

from pathlib import Path

import cv2
import numpy as np

from core.plan_import import FILLS, MARKERS, FloorScan

WALL_CONTRAST = 12
MARKER_TOLERANCE = 70
FILL_MAX_DISTANCE = 30  # in Lab units, after light correction


def order_corners(points: np.ndarray) -> np.ndarray:
    points = points.reshape(-1, 2).astype(np.float32)
    total, diff = points.sum(axis=1), np.diff(points, axis=1).ravel()
    return np.array([points[total.argmin()], points[diff.argmin()], points[total.argmax()], points[diff.argmax()]], np.float32)


def sheet_outline(rgb: np.ndarray) -> np.ndarray | None:
    """Corners of the paper: the largest four-cornered outline among the image's strong edges, like a document
    scanner. Falls back to the brightest large blob."""
    small = max(rgb.shape[:2]) / 1000
    preview = cv2.resize(rgb, None, fx=1 / small, fy=1 / small, interpolation=cv2.INTER_AREA) if small > 1 else rgb
    scale = max(small, 1.0)
    gray = cv2.GaussianBlur(cv2.cvtColor(preview, cv2.COLOR_RGB2GRAY), (5, 5), 0)
    area = gray.shape[0] * gray.shape[1]
    candidates = []
    edges = cv2.dilate(cv2.Canny(gray, 40, 120), np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    _, bright = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    blobs, _ = cv2.findContours(cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8)),
                                cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in [*contours, *blobs]:
        hull = cv2.convexHull(contour)
        if cv2.contourArea(hull) < 0.15 * area:
            continue
        quad = cv2.approxPolyDP(hull, 0.02 * cv2.arcLength(hull, True), True)
        if len(quad) == 4:
            candidates.append((cv2.contourArea(quad), quad, contour))
    if not candidates:
        return None
    _, quad, contour = max(candidates, key=lambda c: c[0])
    if cv2.contourArea(quad) > 0.97 * area:
        return None  # the "sheet" is the whole frame: already a straight scan or screenshot
    return refine_corners(order_corners(quad), contour.reshape(-1, 2).astype(np.float32)) * scale


def refine_corners(corners: np.ndarray, outline: np.ndarray) -> np.ndarray:
    """Fit a straight line to the outline points along each side and intersect neighbouring lines: far more precise
    than the polygon approximation's vertices, which matters for recovering the sheet's proportions."""
    lines = []
    for i in range(4):
        a, b = corners[i], corners[(i + 1) % 4]
        direction = (b - a) / max(np.linalg.norm(b - a), 1e-6)
        normal = np.array([-direction[1], direction[0]])
        along = (outline - a) @ direction
        across = np.abs((outline - a) @ normal)
        length = np.linalg.norm(b - a)
        near = outline[(across < 4) & (along > 0.1 * length) & (along < 0.9 * length)]
        if len(near) < 10:
            return corners
        vx, vy, x0, y0 = cv2.fitLine(near, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
        lines.append((np.array([x0, y0]), np.array([vx, vy])))
    refined = []
    for i in range(4):
        (p1, d1), (p2, d2) = lines[i - 1], lines[i]
        matrix = np.array([d1, -d2]).T
        if abs(np.linalg.det(matrix)) < 1e-6:
            return corners
        t = np.linalg.solve(matrix, p2 - p1)
        refined.append(p1 + t[0] * d1)
    refined = np.array(refined, np.float32)
    return refined if np.abs(refined - corners).max() < 25 else corners


def true_aspect(corners: np.ndarray, image_w: int, image_h: int) -> float | None:
    """Width/height of the real sheet from its four corners in the photo, assuming a pinhole camera with square
    pixels and the principal point in the middle (Zhang & He, "Whiteboard scanning", 2004). The side lengths alone
    get this wrong as soon as the camera is tilted. None when the view is too frontal to estimate the focal length."""
    u0, v0 = image_w / 2, image_h / 2
    tl, tr, br, bl = [np.array([x, y, 1.0]) for x, y in corners]
    m1, m2, m3, m4 = tl, tr, bl, br
    k2 = np.dot(np.cross(m1, m4), m3) / np.dot(np.cross(m2, m4), m3)
    k3 = np.dot(np.cross(m1, m4), m2) / np.dot(np.cross(m3, m4), m2)
    n2, n3 = k2 * m2 - m1, k3 * m3 - m1
    if abs(n2[2]) < 1e-6 or abs(n3[2]) < 1e-6:
        return None
    f2 = -((n2[0] * n3[0] - (n2[0] * n3[2] + n2[2] * n3[0]) * u0 + n2[2] * n3[2] * u0 * u0)
           + (n2[1] * n3[1] - (n2[1] * n3[2] + n2[2] * n3[1]) * v0 + n2[2] * n3[2] * v0 * v0)) / (n2[2] * n3[2])
    if not np.isfinite(f2) or f2 <= (0.3 * image_w) ** 2 or f2 >= (5 * image_w) ** 2:
        return None
    inverse = np.linalg.inv(np.array([[np.sqrt(f2), 0, u0], [0, np.sqrt(f2), v0], [0, 0, 1]]))
    metric = inverse.T @ inverse
    return float(np.sqrt((n2 @ metric @ n2) / (n3 @ metric @ n3)))


def straighten(rgb: np.ndarray) -> np.ndarray:
    """Warp the sheet of paper to a rectangle (perspective, rotation); unchanged if no sheet is found."""
    corners = sheet_outline(rgb)
    if corners is None:
        return rgb
    width = int(max(np.linalg.norm(corners[1] - corners[0]), np.linalg.norm(corners[2] - corners[3])))
    height = int(max(np.linalg.norm(corners[3] - corners[0]), np.linalg.norm(corners[2] - corners[1])))
    ratio = true_aspect(corners, rgb.shape[1], rgb.shape[0])
    if ratio:  # keep the larger side, set the other from the recovered aspect ratio
        if width >= height * ratio:
            height = int(width / ratio)
        else:
            width = int(height * ratio)
    target = np.float32([(0, 0), (width, 0), (width, height), (0, height)])
    flat = cv2.warpPerspective(rgb, cv2.getPerspectiveTransform(corners, target), (width, height), flags=cv2.INTER_CUBIC)
    trim = max(3, int(0.01 * min(width, height)))  # the paper edge itself
    return flat[trim:-trim, trim:-trim]


def surface_terms(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Cubic polynomial in x and y: smooth enough for light gradients, vignetting and their product."""
    return np.stack([x ** i * y ** j for i in range(4) for j in range(4 - i)], axis=-1)


def fit_light(rgb: np.ndarray, samples: list[tuple[np.ndarray, np.ndarray, np.ndarray]]) -> np.ndarray:
    """Light falling on the sheet, per channel: observed colour / true colour at reference pixels (white paper,
    and rooms whose colour is known), fitted with a smooth surface. samples: (ys, xs, true_rgb) groups."""
    h, w = rgb.shape[:2]
    ys = np.concatenate([g[0] for g in samples])
    xs = np.concatenate([g[1] for g in samples])
    truth = np.concatenate([np.broadcast_to(g[2], (len(g[0]), 3)) for g in samples]).astype(np.float64)
    if len(xs) < 500:
        return np.ones(rgb.shape, np.float32)
    design = surface_terms(xs / w, ys / h)
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32) / w, np.arange(h, dtype=np.float32) / h)
    full = surface_terms(gx, gy)
    light = np.empty(rgb.shape, np.float32)
    for channel in range(3):
        ratio = rgb[ys, xs, channel] / np.maximum(truth[:, channel], 1)
        coefficients, *_ = np.linalg.lstsq(design, ratio, rcond=None)
        light[..., channel] = full @ coefficients.astype(np.float32)
    return np.clip(light, 0.08, None)


def sample(mask: np.ndarray, count: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    ys, xs = np.nonzero(mask)
    if len(xs) > count:
        pick = np.random.default_rng(seed).choice(len(xs), count, replace=False)
        ys, xs = ys[pick], xs[pick]
    return ys, xs


def wall_unit(walls: np.ndarray) -> float:
    """Kit walls render about 1.6 px thick (outline plus anti-aliasing); measure them here to get the image's scale."""
    distance = cv2.distanceTransform(walls, cv2.DIST_L2, 3)
    ridge = distance[(distance > 0) & (distance >= cv2.dilate(distance, np.ones((3, 3), np.uint8)))]
    if ridge.size < 100:
        return 1.0
    return max(1.0, float(np.median(ridge)) * 2 / 1.6)


def label_box(glyphs: list, centre: tuple[float, float], unit: float) -> tuple[int, int, int, int] | None:
    """One line of text: start from the glyph nearest the room's centre and add neighbours on the same line."""
    if not glyphs:
        return None
    mid = lambda g: (g[0] + g[2] / 2, g[1] + g[3] / 2)  # noqa: E731
    seed = min(glyphs, key=lambda g: (mid(g)[0] - centre[0]) ** 2 + (mid(g)[1] - centre[1]) ** 2)
    x0, y0, x1, y1 = seed[0], seed[1], seed[0] + seed[2], seed[1] + seed[3]
    grew = True
    while grew:
        grew = False
        for g in glyphs:
            gx0, gy0, gx1, gy1 = g[0], g[1], g[0] + g[2], g[1] + g[3]
            if gx0 >= x0 and gx1 <= x1 and gy0 >= y0 and gy1 <= y1:
                continue
            same_line = min(gy1, y1) - max(gy0, y0) > 0.3 * min(gy1 - gy0, y1 - y0)
            close = gx0 - x1 < 4 * unit and x0 - gx1 < 4 * unit
            if same_line and close:
                x0, y0, x1, y1 = min(x0, gx0), min(y0, gy0), max(x1, gx1), max(y1, gy1)
                grew = True
    return int(x0), int(y0), int(x1), int(y1)


def lab(colour) -> np.ndarray:
    return cv2.cvtColor(np.uint8([[colour]]), cv2.COLOR_RGB2LAB)[0, 0].astype(np.float32)


def scan_photo(path: Path, raw: np.ndarray) -> FloorScan:
    rgb = straighten(raw)
    height, width = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    kernel = 11  # wider than any wall line, narrower than any room
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel, kernel)))
    walls = (blackhat > WALL_CONTRAST).astype(np.uint8)
    walls = cv2.morphologyEx(walls, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    unit = wall_unit(walls)
    # entrance triangles sit on the outer wall but are too big for the line filter: without them the wall has holes
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    # strongly saturated red only (orange stairwells are much less saturated), and only small blobs
    red = (((hsv[..., 0] < 8) | (hsv[..., 0] > 170)) & (hsv[..., 1] > 150) & (hsv[..., 2] > 70)).astype(np.uint8)
    found, red_labels, red_stats, _ = cv2.connectedComponentsWithStats(red, connectivity=8)
    small = [i for i in range(1, found) if red_stats[i][2] <= 20 * unit and red_stats[i][3] <= 20 * unit]
    walls |= cv2.dilate(np.isin(red_labels, small).astype(np.uint8), np.ones((5, 5), np.uint8))  # photo pixels per pixel of a rendered kit plan, from how thick the walls are

    count, labels, stats, _ = cv2.connectedComponentsWithStats(1 - walls, connectivity=4)
    border = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))) - {0}
    background = np.isin(labels, list(border)) & (walls == 0)
    background = cv2.erode(background.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    palette = {kind: lab(colour) for kind, colour in FILLS.items()}
    white_lab = lab((255, 255, 255))
    regions = [r for r in range(1, count) if r not in border and stats[r][4] >= 60]
    interiors = {r: cv2.erode((labels[stats[r][1]:stats[r][1] + stats[r][3], stats[r][0]:stats[r][0] + stats[r][2]] == r)
                              .astype(np.uint8), np.ones((5, 5), np.uint8)) for r in regions}

    # light: start from the white paper, then use the rooms (now classified) as extra colour references, twice
    references = [(*sample(background, 20000, 0), np.array([255, 255, 255]))]
    kinds: dict[int, str] = {}
    for _ in range(3):
        light = fit_light(rgb, references)
        flat = np.clip(rgb.astype(np.float32) / light, 0, 255).astype(np.uint8)
        flat_lab = cv2.cvtColor(flat, cv2.COLOR_RGB2LAB).astype(np.float32)
        kinds = {}
        references = references[:1]
        for r in regions:
            x, y = stats[r][0], stats[r][1]
            ys, xs = np.nonzero(interiors[r])
            if len(xs) < 20:
                continue
            colour = np.median(flat_lab[ys + y, xs + x], axis=0)
            kind, distance = min(((k, float(np.linalg.norm(colour - c))) for k, c in palette.items()), key=lambda kv: kv[1])
            if distance > FILL_MAX_DISTANCE or np.linalg.norm(colour - white_lab) < distance:
                continue
            kinds[r] = kind
            pick = np.random.default_rng(r).choice(len(xs), min(len(xs), 400), replace=False)
            references.append((ys[pick] + y, xs[pick] + x, np.array(FILLS[kind])))

    scan = FloorScan(path, (width, height), rgb=flat, photo=True, seam_px=0.0, snap_px=6.0 * unit)
    min_area = max(60, int(30 * unit * unit))
    grow = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(round(unit * 2)) | 1, int(round(unit * 2)) | 1))
    for region, kind in kinds.items():
        if stats[region][4] < min_area:
            continue
        mask = (labels == region).astype(np.uint8)
        grown = cv2.dilate(mask, grow)  # out to the middle of the wall
        contours, hierarchy = cv2.findContours(grown, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        outer = max((c for c, h in zip(contours, hierarchy[0]) if h[3] < 0), key=cv2.contourArea)
        approx = cv2.approxPolyDP(outer, 1.2 * unit, True).reshape(-1, 2)
        if len(approx) < 3:
            continue
        # the room's label is the dark text inside it: the holes of its mask
        filled = np.zeros_like(mask)
        cv2.drawContours(filled, [outer], -1, 1, thickness=-1)
        holes = (filled.astype(bool) & ~cv2.dilate(mask, np.ones((3, 3), np.uint8)).astype(bool)).astype(np.uint8)
        hole_count, _, hole_stats, _ = cv2.connectedComponentsWithStats(holes, connectivity=8)
        glyphs = [hole_stats[i] for i in range(1, hole_count)
                  if hole_stats[i][4] >= 4 * unit and hole_stats[i][3] < 14 * unit and hole_stats[i][2] < 30 * unit]
        moments = cv2.moments(mask)
        centre = (moments["m10"] / max(moments["m00"], 1), moments["m01"] / max(moments["m00"], 1))
        box = label_box(glyphs, centre, unit)
        scan.rooms.append({"kind": kind, "polygon": [(float(x), float(y)) for x, y in approx], "text_box": box})

    # the title: dark text on the white paper above the drawing
    drawing = ((labels > 0) & ~np.isin(labels, list(border))).astype(np.uint8)
    paper_text = walls.astype(bool) & ~cv2.dilate(drawing, np.ones((9, 9), np.uint8)).astype(bool)
    top = int(min((y for room in scan.rooms for _, y in room["polygon"]), default=height // 6))
    ys, xs = np.nonzero(paper_text[: max(0, top - int(3 * unit))])
    if len(xs) > 20:
        scan.title_box = (int(np.percentile(xs, 1)), int(np.percentile(ys, 1)), int(np.percentile(xs, 99)), int(np.percentile(ys, 99)))

    lo, hi = 2.0 * unit, 8.0 * unit
    for kind, colour in MARKERS.items():
        mask = (np.linalg.norm(flat.astype(np.int16) - np.array(colour, np.int16), axis=2) < MARKER_TOLERANCE).astype(np.uint8)
        # a disc a bit thinner than a marker: wipes out the blurred wall lines that share its colour, keeps the dots
        size = int(round(1.8 * unit)) | 1
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size)))
        found, _, marker_stats, centres = cv2.connectedComponentsWithStats(mask, connectivity=8)
        for blob in range(1, found):
            x, y, w, h, area = marker_stats[blob]
            round_enough = 0.6 <= w / max(h, 1) <= 1.7 and area >= 0.4 * w * h
            if lo <= w <= hi and lo <= h <= hi and round_enough:
                scan.markers.append({"kind": kind, "x": float(centres[blob][0]), "y": float(centres[blob][1])})
    return scan
