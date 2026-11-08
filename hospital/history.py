"""Movement history: position samples of every tag, filtered queries, per-floor heatmaps and room visits.

Samples are kept column-wise (tags, kinds, roles, floors and spaces as interned codes) so a heatmap over a week of
samples is a few numpy passes."""

import gzip
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import shapely

INTERPOLATE_MAX_GAP_S = 120.0
FILE_VERSION = 1


@dataclass
class Sample:
    t: float
    tag_id: str
    kind: str
    role: str
    floor_id: str
    x: float
    y: float
    space_id: str | None


class Codes:
    def __init__(self) -> None:
        self.values: list[str | None] = []
        self.index: dict[str | None, int] = {}

    def code(self, value: str | None) -> int:
        found = self.index.get(value)
        if found is None:
            found = self.index[value] = len(self.values)
            self.values.append(value)
        return found


class History:
    def __init__(self, max_samples: int = 1_000_000) -> None:
        self.max_samples = max_samples
        self.labels: dict[str, str] = {}
        self.tag_codes, self.kind_codes, self.role_codes = Codes(), Codes(), Codes()
        self.floor_codes, self.space_codes = Codes(), Codes()
        self.t: list[float] = []
        self.tag: list[int] = []
        self.kind: list[int] = []
        self.role: list[int] = []
        self.floor: list[int] = []
        self.x: list[float] = []
        self.y: list[float] = []
        self.space: list[int] = []
        self.cache: dict[str, np.ndarray] = {}

    def __len__(self) -> int:
        return len(self.t)

    def add(self, sample: Sample) -> None:
        self.append(sample)
        self.trim()

    def extend(self, samples: Iterable[Sample]) -> None:
        for sample in samples:
            self.append(sample)
        self.trim()

    def append(self, sample: Sample) -> None:
        self.t.append(float(sample.t))
        self.tag.append(self.tag_codes.code(sample.tag_id))
        self.kind.append(self.kind_codes.code(sample.kind))
        self.role.append(self.role_codes.code(sample.role))
        self.floor.append(self.floor_codes.code(sample.floor_id))
        self.x.append(float(sample.x))
        self.y.append(float(sample.y))
        self.space.append(self.space_codes.code(sample.space_id))

    def trim(self) -> None:
        # Drop a tenth more than needed so a full buffer is not shifted on every live sample.
        if len(self.t) <= self.max_samples:
            return
        drop = len(self.t) - self.max_samples + self.max_samples // 10
        for column in (self.t, self.tag, self.kind, self.role, self.floor, self.x, self.y, self.space):
            del column[:drop]
        self.cache = {}

    def columns(self) -> dict[str, np.ndarray]:
        """The columns as numpy arrays; samples are only appended between trims, so only the new tail is converted."""
        done = len(self.cache.get("t", ()))
        if self.cache and done == len(self.t):
            return self.cache
        fresh = {"t": (self.t, np.float64), "tag": (self.tag, np.int32), "kind": (self.kind, np.int32),
                 "role": (self.role, np.int32), "floor": (self.floor, np.int32), "x": (self.x, np.float64),
                 "y": (self.y, np.float64), "space": (self.space, np.int32)}
        self.cache = {name: np.concatenate([self.cache[name], np.asarray(column[done:], dtype=dtype)]) if done
                      else np.asarray(column, dtype=dtype) for name, (column, dtype) in fresh.items()}
        return self.cache

    def sample(self, i: int) -> Sample:
        return Sample(self.t[i], self.tag_codes.values[self.tag[i]], self.kind_codes.values[self.kind[i]],
                      self.role_codes.values[self.role[i]], self.floor_codes.values[self.floor[i]], self.x[i], self.y[i],
                      self.space_codes.values[self.space[i]])

    def mask(self, *, floor_id=None, tag_id=None, kind=None, role=None, since=None, until=None) -> np.ndarray:
        columns = self.columns()
        keep = np.ones(len(self.t), dtype=bool)
        for value, codes, name in ((floor_id, self.floor_codes, "floor"), (tag_id, self.tag_codes, "tag"),
                                   (kind, self.kind_codes, "kind"), (role, self.role_codes, "role")):
            if value is None:
                continue
            code = codes.index.get(value)
            if code is None:
                return np.zeros(len(self.t), dtype=bool)
            keep &= columns[name] == code
        if since is not None:
            keep &= columns["t"] >= since
        if until is not None:
            keep &= columns["t"] <= until
        return keep

    def ordered(self, keep: np.ndarray) -> np.ndarray:
        """Indices of the kept samples, sorted by tag and time."""
        rows = np.flatnonzero(keep)
        columns = self.columns()
        return rows[np.lexsort((columns["t"][rows], columns["tag"][rows]))]

    def query(self, *, floor_id=None, tag_id=None, kind=None, role=None, since=None, until=None) -> list[Sample]:
        rows = np.flatnonzero(self.mask(floor_id=floor_id, tag_id=tag_id, kind=kind, role=role, since=since,
                                        until=until))
        rows = rows[np.argsort(self.columns()["t"][rows], kind="stable")]
        return [self.sample(int(i)) for i in rows]

    def people(self) -> list[dict]:
        if not self.t:
            return []
        tag, t = self.columns()["tag"], self.columns()["t"]
        rows = []
        for code in np.unique(tag):
            mine = np.flatnonzero(tag == code)
            last = int(mine[np.argmax(t[mine])])
            tag_id = self.tag_codes.values[code]
            row = {"tag_id": tag_id, "kind": self.kind_codes.values[self.kind[last]],
                   "role": self.role_codes.values[self.role[last]], "samples": len(mine),
                   "first": float(t[mine].min()), "last": float(t[last])}
            if tag_id in self.labels:
                row["label"] = self.labels[tag_id]
            rows.append(row)
        return sorted(rows, key=lambda row: row["tag_id"])

    def heatmap(self, bmap, floor_id: str, cell_m: float = 0.5, **filters) -> dict:
        x0, y0, x1, y1 = floor_bounds(bmap, floor_id)
        cols, rows = max(1, math.ceil((x1 - x0) / cell_m)), max(1, math.ceil((y1 - y0) / cell_m))
        grid = {"floor_id": floor_id, "cell_m": cell_m, "origin": [x0, y0], "cols": cols, "rows": rows}
        floor_code = self.floor_codes.index.get(floor_id)
        filters.pop("floor_id", None)
        order = self.ordered(self.mask(**filters)) if floor_code is not None else np.zeros(0, dtype=np.int64)
        if not len(order):
            return grid | {"counts": [[0] * cols for _ in range(rows)], "max": 0}
        columns = self.columns()
        t, tag, floor, x, y = (columns[name][order] for name in ("t", "tag", "floor", "x", "y"))
        on_floor = floor == floor_code
        # Pairs are taken before dropping other floors, so a trip upstairs and back is never drawn as a line.
        pair = (on_floor[:-1] & on_floor[1:] & (tag[:-1] == tag[1:])
                & (np.diff(t) <= INTERPOLATE_MAX_GAP_S))
        start = np.flatnonzero(pair)
        dx, dy = x[start + 1] - x[start], y[start + 1] - y[start]
        steps = np.maximum(1, np.ceil(np.hypot(dx, dy) / cell_m)).astype(np.int64)
        inner = steps - 1
        owner = np.repeat(np.arange(len(start)), inner)
        offsets = np.cumsum(inner) - inner
        k = np.arange(len(owner)) - np.repeat(offsets, inner) + 1
        fraction = k / steps[owner]
        px = np.concatenate([x[on_floor], x[start][owner] + dx[owner] * fraction])
        py = np.concatenate([y[on_floor], y[start][owner] + dy[owner] * fraction])
        ix = np.floor((px - x0) / cell_m).astype(np.int64)
        iy = np.floor((py - y0) / cell_m).astype(np.int64)
        inside = (ix >= 0) & (ix < cols) & (iy >= 0) & (iy < rows)
        counts = np.bincount(iy[inside] * cols + ix[inside], minlength=rows * cols).reshape(rows, cols)
        return grid | {"counts": counts.tolist(), "max": int(counts.max())}

    def visits(self, tag_id: str, since=None, until=None) -> list[tuple[float, str]]:
        rows = self.ordered(self.mask(tag_id=tag_id, since=since, until=until))
        result: list[tuple[float, str]] = []
        current = None
        for i in rows:
            space_id = self.space_codes.values[self.space[i]]
            if space_id is not None and space_id != current:
                result.append((self.t[i], space_id))
                current = space_id
        return result


def floor_bounds(bmap, floor_id: str) -> tuple[float, float, float, float]:
    points = [point for space in bmap.spaces.values() if space["floor_id"] == floor_id for point in space["polygon"]]
    if not points:
        raise KeyError(floor_id)
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return math.floor(min(xs)), math.floor(min(ys)), math.ceil(max(xs)), math.ceil(max(ys))


def locate(bmap, floor_ids: np.ndarray, x: np.ndarray, y: np.ndarray) -> list[str | None]:
    """Space of each point: the containing polygon on its floor, else the nearest one."""
    found = np.full(len(x), None, dtype=object)
    for floor_id in np.unique(floor_ids):
        rows = np.flatnonzero(floor_ids == floor_id)
        spaces = [(space_id, shapely.Polygon(space["polygon"])) for space_id, space in bmap.spaces.items()
                  if space["floor_id"] == floor_id]
        if not spaces:
            continue
        names = np.array([space_id for space_id, _ in spaces], dtype=object)
        tree = shapely.STRtree([polygon for _, polygon in spaces])
        points = shapely.points(x[rows], y[rows])
        point_index, space_index = tree.query(points, predicate="within")
        found[rows[point_index]] = names[space_index]
        outside = np.setdiff1d(np.arange(len(rows)), point_index)
        if len(outside):
            point_index, space_index = tree.query_nearest(points[outside], all_matches=False)
            found[rows[outside[point_index]]] = names[space_index]
    return found.tolist()


def load_file(path: Path, end: float, tags: dict, bmap=None) -> list[Sample]:
    """Samples of a recorded week shifted to end at `end`; unknown tags count as visitors."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("version") != FILE_VERSION:
        raise ValueError(f"unsupported history version {data.get('version')}")
    raw = sorted(data["samples"], key=lambda row: row[0])
    if not raw:
        return []
    floor_ids = np.array([row[2] for row in raw])
    x = np.array([row[3] for row in raw], dtype=np.float64)
    y = np.array([row[4] for row in raw], dtype=np.float64)
    spaces = locate(bmap, floor_ids, x, y) if bmap is not None else [None] * len(raw)
    samples = []
    for (dt, tag_id, floor_id, px, py), space_id in zip(raw, spaces, strict=True):
        tag = tags.get(tag_id)
        kind, role = (tag.kind, tag.role) if tag else ("visitor", "")
        samples.append(Sample(end + float(dt), tag_id, kind, role, floor_id, float(px), float(py), space_id))
    return samples


def save_file(path: Path, samples: Iterable[Sample], end: float) -> None:
    rows = [[round(s.t - end, 1), s.tag_id, s.floor_id, round(s.x, 2), round(s.y, 2)] for s in samples]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump({"version": FILE_VERSION, "samples": rows}, handle, separators=(",", ":"))
