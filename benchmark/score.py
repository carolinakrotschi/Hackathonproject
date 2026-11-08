"""Score a free-text reply against a benchmark answer. Works for any agent: only the reply text is used.

Every benchmark question asks the agent to finish with a line "Answer: ...", and only that committed
answer is scored, so hedging ("it's one of these six", "between 92 and 108 m") doesn't earn credit.
A reply without an "Answer:" line scores as wrong.

- space answers: the expected room (its unique ref or name) must be in the answer, and come before any
  runner-up room.
- number answers: the answer must hold exactly one number, within tolerance.
- largest_room also needs the area: some number in the answer must match it."""

import re

ANSWER_LINE = re.compile(r"^\W*answer\W*:\s*(.+)$", flags=re.IGNORECASE | re.MULTILINE)
NUMBER_PATTERN = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])-?\d+(?:\.\d+)?")


def committed_answer(reply: str) -> str | None:
    matches = ANSWER_LINE.findall(reply)
    return matches[-1].strip() if matches else None


def first_mention(text: str, labels: list[str]) -> int | None:
    positions = []
    for label in labels:
        match = re.search(rf"(?<![\w.]){re.escape(label)}(?![\w])", text, flags=re.IGNORECASE)
        if match:
            positions.append(match.start())
    return min(positions) if positions else None


def numbers_in(text: str) -> list[float]:
    values = []
    for match in NUMBER_PATTERN.findall(text.replace("**", "")):
        try:
            values.append(float(match.replace(",", "")))
        except ValueError:
            continue
    return values


def within(value: float, answer: dict) -> bool:
    expected = float(answer["value"])
    tolerance = max(abs(expected) * float(answer["relative_tolerance"]), float(answer["absolute_tolerance"]))
    return abs(value - expected) <= tolerance


def space_matches(text: str, answer: dict) -> bool:
    accepted = first_mention(text, answer["accept"])
    if accepted is None:
        return False
    rejected = first_mention(text, answer["reject"])
    return rejected is None or accepted < rejected


def score_reply(answer: dict, reply: str) -> bool:
    text = committed_answer(reply)
    if text is None:
        return False
    if answer["type"] == "number":
        numbers = numbers_in(text)
        return len(numbers) == 1 and within(numbers[0], answer)
    if answer["type"] == "space":
        correct = space_matches(text, answer)
        if correct and "area" in answer:
            correct = any(within(value, answer["area"]) for value in numbers_in(text))
        return correct
    raise ValueError(f"unknown answer type {answer['type']}")
