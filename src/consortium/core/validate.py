"""Response validation (AD-7) and the invalid-answer rate: pure, no I/O.

``validate_response`` parses one raw Model response and checks it against the
Instrument's response schema. It accepts exactly one JSON object, optionally
wrapped in a single fenced code block (```` ```json ... ``` ````), with every
Item of the Instrument and no other key. Each value must fit its Item type:

- ``likert``: an integer (not a bool, not a float) from 1 to ``points``;
- ``pairwise``: one of the Item's ``options`` (``A`` or ``B`` by default);
- ``free_text``: a string that is not empty or only whitespace.

Answers are never repaired or coerced. On failure it raises
``ConsortiumError("invalid_response", <reason>)`` with a stable snake_case
reason. The structural checks run in this order:

- ``not_json``: not one JSON value (or one fenced block holding one), trailing
  text, or a non-standard constant such as ``NaN``;
- ``not_object``: valid JSON, but not an object;
- ``duplicate_item``: the top-level object repeats a key;
- ``unknown_item``: a key that is not an Item of the Instrument;
- ``missing_item``: an Item of the Instrument has no key;

then each Item's value, in Item order:

- ``wrong_type:<item>``: a Likert value that is not an integer, or a pairwise
  or free-text value that is not a string;
- ``out_of_range:<item>``: a Likert integer outside 1..``points``;
- ``bad_choice:<item>``: a pairwise string that is not one of the options;
- ``empty_text:<item>``: an empty (or whitespace-only) free-text answer.

An Instrument the config layer should have rejected (a Likert Item without
``points``, an unknown Item type) raises ``ValueError``: a programming error,
not a Model's.

``invalid_rate`` is the one definition of the invalid-answer rate:
invalid / (valid + invalid); refused and failed Trials are not in it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from consortium.core.errors import ConsortiumError

INVALID_RESPONSE = "invalid_response"

_FENCE = re.compile(r"\A```[A-Za-z0-9_+-]*[ \t]*\n?(?P<body>.*?)\n?[ \t]*```\Z", re.DOTALL)


class _Item(Protocol):
    id: str
    type: str
    points: int | None
    options: Sequence[str] | None


class _Instrument(Protocol):
    name: str
    items: Sequence[_Item]


@dataclass(frozen=True)
class ParsedAnswer:
    """A valid answer: the Instrument's name and ``{item_id: value}`` in Item order."""

    instrument: str
    answers: dict[str, int | str]


def _invalid(reason: str) -> ConsortiumError:
    return ConsortiumError(INVALID_RESPONSE, reason)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


class _Object(dict):
    """A parsed JSON object that remembers whether it repeated a key."""

    duplicate = False


def _object(pairs: list[tuple[str, Any]]) -> _Object:
    out = _Object(pairs)
    out.duplicate = len(out) != len(pairs)
    return out


def _parse(raw: str) -> Any:
    if not isinstance(raw, str):
        raise _invalid("not_json")
    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced is not None:  # a second block, if any, makes the body fail to parse
        text = fenced.group("body").strip()
    try:
        return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_object)
    except (ValueError, RecursionError) as err:
        raise _invalid("not_json") from err


def _check_value(item: _Item, value: Any) -> None:
    if item.type == "likert":
        if item.points is None:
            raise ValueError(f"likert Item {item.id!r} has no points")
        if isinstance(value, bool) or not isinstance(value, int):
            raise _invalid(f"wrong_type:{item.id}")
        if not 1 <= value <= item.points:
            raise _invalid(f"out_of_range:{item.id}")
    elif item.type == "pairwise":
        options = list(item.options) if item.options is not None else ["A", "B"]
        if not isinstance(value, str):
            raise _invalid(f"wrong_type:{item.id}")
        if value not in options:
            raise _invalid(f"bad_choice:{item.id}")
    elif item.type == "free_text":
        if not isinstance(value, str):
            raise _invalid(f"wrong_type:{item.id}")
        if not value.strip():
            raise _invalid(f"empty_text:{item.id}")
    else:
        raise ValueError(f"Item {item.id!r} has unknown type {item.type!r}")


def validate_response(raw: str, instrument: _Instrument) -> ParsedAnswer:
    """Parse and validate ``raw`` against ``instrument``; raise ``invalid_response``.

    ``instrument`` is an ``InstrumentDef`` (or anything with ``name`` and
    ``items``, each Item having ``id``, ``type``, ``points`` and ``options``).
    """
    obj = _parse(raw)
    if not isinstance(obj, dict):
        raise _invalid("not_object")
    if isinstance(obj, _Object) and obj.duplicate:
        raise _invalid("duplicate_item")
    items = list(instrument.items)
    ids = {item.id for item in items}
    for key in obj:
        if key not in ids:
            raise _invalid("unknown_item")
    for item in items:
        if item.id not in obj:
            raise _invalid("missing_item")
    for item in items:
        _check_value(item, obj[item.id])
    return ParsedAnswer(instrument.name, {item.id: obj[item.id] for item in items})


def invalid_rate(counts: Mapping[str, int]) -> float | None:
    """invalid / (valid + invalid) from Trial counts by state; ``None`` when both are 0.

    ``refused`` and ``failed`` (and non-terminal states) are not in the
    denominator; report them separately as counts.
    """
    valid = int(counts.get("valid", 0))
    invalid = int(counts.get("invalid", 0))
    total = valid + invalid
    if total == 0:
        return None
    return invalid / total
