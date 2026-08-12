"""Deterministic check that a drafted answer only says what the tools returned.

The prompt already forbids naming an entity no tool returned, doing arithmetic
in prose, and ranking by a measure a result wasn't ranked by. Those rules work
most of the time, but they're instructions, not guarantees — the failures that
motivated them (a customer recalled from the model's memory of this public
dataset, shares of a total computed in prose and wrong) all looked perfectly
plausible in the answer text. This module re-reads the draft against the raw
tool output and reports what isn't there, so `ask()` can send it back for one
correction pass instead of handing it to the user.

Everything here is pure: the answer, the tool text and the entity vocabulary
all come in as arguments, so the whole policy is testable without an API call
or a database.
"""
import re

# Matches an optionally signed number with optional thousands separators and
# decimals: "1,234.56", "-1983.43", "47.1", "13".
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

# Integers this small are almost always structure rather than data — list
# numbering ("1.", "2."), "top 5", "the three categories", a quarter number —
# and checking them produces far more noise than it catches.
_SMALL_INT_MAX = 12

# Four-digit integers in this range are read as years (they show up in every
# answer about a date range and are part of the question, not of a result).
_YEAR_MIN, _YEAR_MAX = 1900, 2100

# Shorter vocabulary entries are ordinary words too often ("Art", "Paper") to
# be evidence that the model named a specific record.
_MIN_ENTITY_LENGTH = 5


def _parse(raw: str) -> tuple[float, int] | None:
    """`("1,234.50")` -> `(1234.5, 2)`: the value, and how many decimal places
    it was written with. Returns None if it doesn't parse as a number.
    """
    cleaned = raw.replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    decimals = len(cleaned.partition(".")[2])
    return value, decimals


def _is_structural(value: float, decimals: int, unit: bool) -> bool:
    """True for numbers that carry no claim about the data — small counts used
    as structure, and years.

    `unit` says the number was written with a currency symbol or a percent
    sign, which settles it: "$7" and "7%" are measurements however small, while
    the "5" in "top 5" never carries one. Without this, single-digit
    percentages — the shape of the very error this checks for, a share worked
    out in prose — would fall through the small-integer exemption.
    """
    if unit or decimals or not value.is_integer():
        return False
    return abs(value) <= _SMALL_INT_MAX or _YEAR_MIN <= value <= _YEAR_MAX


def tool_numbers(tool_text: str) -> list[float]:
    """Every number appearing in the raw tool output."""
    parsed = (_parse(m.group()) for m in _NUMBER_RE.finditer(tool_text))
    return [value for value, _ in filter(None, parsed)]


def _is_supported(value: float, decimals: int, candidates: list[float]) -> bool:
    """Whether `value`, written with `decimals` decimal places, could have been
    read off one of `candidates`.

    Tolerance is one unit in the answer's own last place, which accepts both
    rounding and truncation of a longer figure ($8,981 or $8,981.32 from
    8981.3239) without accepting a different number. `candidate * 100` is
    allowed too, since a ratio a tool returns as 0.4714 is normally reported as
    47.1%.

    Magnitudes are compared, not signed values: a negative figure is routinely
    written with its sign in words ("losing $1,983", "a loss of $1,983"), and
    treating those as unsupported would fire on correct answers constantly.
    The cost is that a sign flipped in the prose slips through — a narrower
    miss than the false alarms it avoids.
    """
    tolerance = 10.0 ** -decimals
    return any(
        abs(abs(value) - abs(scaled)) <= tolerance
        for candidate in candidates
        for scaled in (candidate, candidate * 100)
    )


def ungrounded_numbers(answer: str, tool_text: str) -> list[str]:
    """The figures in `answer` that no number in `tool_text` accounts for.

    These are almost always arithmetic the model did in prose — a total, or a
    share of one — which is exactly the step that has been getting the wrong
    result while every figure it started from was right.
    """
    candidates = tool_numbers(tool_text)
    found = []
    for match in _NUMBER_RE.finditer(answer):
        parsed = _parse(match.group())
        if parsed is None:
            continue
        value, decimals = parsed
        unit = (answer[match.end():match.end() + 1] == "%"
                or answer[max(match.start() - 1, 0):match.start()] == "$")
        if _is_structural(value, decimals, unit):
            continue
        if not _is_supported(value, decimals, candidates):
            found.append(match.group())
    return found


def ungrounded_entities(answer: str, tool_text: str, vocabulary: set[str]) -> list[str]:
    """Records named in `answer` that are real rows of the database but appear
    in no tool result.

    Checking against the database's own vocabulary rather than "words that look
    like a name" is what makes this precise: a customer or product the model
    supplied from its memory of this well-known public dataset matches, while
    ordinary capitalized prose ("Total", "The West grew") does not.
    """
    return [
        name for name in sorted(vocabulary)
        if len(name) >= _MIN_ENTITY_LENGTH
        and name in answer
        and name not in tool_text
        and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", answer)
    ]


def ungrounded_claims(answer: str, tool_text: str, vocabulary: set[str]) -> list[str]:
    """Everything in `answer` that `tool_text` doesn't support, entities first.

    An empty list means the answer stays within what the tools returned. Note
    that this is a check on provenance, not on reasoning: it cannot tell that a
    correctly quoted number is being described wrongly.
    """
    return (ungrounded_entities(answer, tool_text, vocabulary)
            + ungrounded_numbers(answer, tool_text))
