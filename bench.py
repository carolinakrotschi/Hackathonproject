"""Benchmark entry point: answer(building_directory, question_text) -> reply text.

First the fast path: one Claude call parses the question, code computes the answer. If the question fits no known
kind or its names can't be resolved, a tool-using agent over the same core takes over (core/fallback.py).

  uv run --env-file .env python -m benchmark.evaluate data/test --agent bench:answer --submission-csv out.csv"""

import os
from functools import cache
from pathlib import Path

import anthropic

from core.building_map import BuildingMap
from core.fallback import fallback_answer
from core.parse import ParseError, parse
from core.queries import reply, run

USE_FALLBACK = os.environ.get("BENCH_FALLBACK", "1") != "0"


@cache
def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


@cache
def _building(directory: str) -> BuildingMap:
    return BuildingMap.load(Path(directory))


def answer(building_directory: Path, question_text: str) -> str:
    bmap = _building(str(building_directory))
    try:
        kind, params = parse(bmap, question_text, _client())
        return reply(kind, run(bmap, kind, params))
    except (ParseError, ValueError, KeyError) as error:
        if not USE_FALLBACK:
            return f"I could not answer this: {error}"
    return fallback_answer(bmap, question_text, _client())
