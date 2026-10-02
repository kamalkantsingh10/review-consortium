"""core.prompt.compose: golden parts for single, pairwise and practice requests (story 2.2)."""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

from consortium.core.prompt import PROMPT_FORMAT, Media, Text, compose
from consortium.core.render import (
    Anchors,
    ClipRef,
    PracticeExample,
    RequestItem,
    TrialRequest,
)

LIKERT = RequestItem("animacy_1", "likert", "Dead / Alive", 5, Anchors("Dead", "Alive"), None)
PAIR = RequestItem("alive", "pairwise", "Which seems more alive?", None, None, ("A", "B"))
LIKERT_SCHEMA = {
    "type": "object", "properties": {"animacy_1": {"type": "integer", "minimum": 1, "maximum": 5}},
    "required": ["animacy_1"], "additionalProperties": False,
}
PAIR_SCHEMA = {
    "type": "object", "properties": {"alive": {"type": "string", "enum": ["A", "B"]}},
    "required": ["alive"], "additionalProperties": False,
}
CA, CB, CP = ClipRef("c_aaaaaaaa", "a" * 64), ClipRef("c_bbbbbbbb", "b" * 64), \
    ClipRef("c_pppppppp", "p" * 64)
LIKERT_ITEMS_JSON = (
    'Items: [{"anchors":{"high":"Alive","low":"Dead"},"id":"animacy_1","options":null,'
    '"points":5,"text":"Dead / Alive","type":"likert"}]'
)
LIKERT_SCHEMA_JSON = (
    'Response schema: {"additionalProperties":false,"properties":{"animacy_1":'
    '{"maximum":5,"minimum":1,"type":"integer"}},"required":["animacy_1"],"type":"object"}'
)


def single(practice: tuple[PracticeExample, ...] = ()) -> TrialRequest:
    return TrialRequest("You are 34.", "Rate the robot.", (LIKERT,), LIKERT_SCHEMA,
                        "Watch and answer.", practice, (CA,))


def test_prompt_format_is_one() -> None:
    assert PROMPT_FORMAT == 1


def test_single_golden() -> None:
    assert compose(single()) == (
        Text("You are 34."),
        Text("Rate the robot."),
        Text("Video to rate:"),
        Media("c_aaaaaaaa", "a" * 64, "target"),
        Text("Watch and answer."),
        Text(LIKERT_ITEMS_JSON),
        Text(LIKERT_SCHEMA_JSON),
    )


def test_pairwise_golden_uses_option_labels() -> None:
    item = RequestItem("alive", "pairwise", "Which?", None, None, ("Left", "Right"))
    req = TrialRequest("card", "instr", (item,), PAIR_SCHEMA, "prompt", (), (CA, CB))
    parts = compose(req)
    assert parts[:7] == (
        Text("card"), Text("instr"),
        Text("Video Left:"), Media("c_aaaaaaaa", "a" * 64, "target"),
        Text("Video Right:"), Media("c_bbbbbbbb", "b" * 64, "target"),
        Text("prompt"),
    )
    assert parts[7] == Text(
        'Items: [{"anchors":null,"id":"alive","options":["Left","Right"],"points":null,'
        '"text":"Which?","type":"pairwise"}]'
    )
    assert parts[8].text.startswith("Response schema: {")
    assert len(parts) == 9


def test_practice_golden() -> None:
    practice = (
        PracticeExample((CP,), {"animacy_1": 3}),
        PracticeExample((CB,), {"animacy_1": "Ä"}),
    )
    assert compose(single(practice)) == (
        Text("You are 34."),
        Text("Rate the robot."),
        Text("Practice example 1:"),
        Media("c_pppppppp", "p" * 64, "practice"),
        Text('Intended answer: {"animacy_1":3}'),
        Text("Practice example 2:"),
        Media("c_bbbbbbbb", "b" * 64, "practice"),
        Text('Intended answer: {"animacy_1":"Ä"}'),
        Text("Video to rate:"),
        Media("c_aaaaaaaa", "a" * 64, "target"),
        Text("Watch and answer."),
        Text(LIKERT_ITEMS_JSON),
        Text(LIKERT_SCHEMA_JSON),
    )


def test_pairwise_practice_pair_is_labelled_like_targets() -> None:
    req = TrialRequest("card", "instr", (PAIR,), PAIR_SCHEMA, "prompt",
                       (PracticeExample((CP, CB), {"alive": "B"}),), (CA, CB))
    assert compose(req)[2:12] == (
        Text("Practice example 1:"),
        Text("Video A:"), Media("c_pppppppp", "p" * 64, "practice"),
        Text("Video B:"), Media("c_bbbbbbbb", "b" * 64, "practice"),
        Text('Intended answer: {"alive":"B"}'),
        Text("Video A:"), Media("c_aaaaaaaa", "a" * 64, "target"),
        Text("Video B:"), Media("c_bbbbbbbb", "b" * 64, "target"),
    )


@pytest.mark.parametrize("req", [
    TrialRequest("c", "i", (PAIR,), PAIR_SCHEMA, "p", (), (CA, CB, CP)),  # 3 targets
    TrialRequest("c", "i", (LIKERT,), LIKERT_SCHEMA, "p", (), (CA, CB)),  # pair, no pairwise
    TrialRequest("c", "i", (RequestItem("x", "pairwise", "?", None, None, ("A", "B", "C")),),
                 PAIR_SCHEMA, "p", (), (CA, CB)),  # 3 option labels
    TrialRequest("c", "i", (PAIR,), PAIR_SCHEMA, "p",
                 (PracticeExample((CA, CB, CP), {"alive": "A"}),), (CA, CB)),  # 3 practice
])
def test_bad_requests_raise_value_error(req: TrialRequest) -> None:
    with pytest.raises(ValueError):
        compose(req)


def test_pure_same_input_equal_tuple() -> None:
    req = single((PracticeExample((CP,), {"animacy_1": 3}),))
    assert compose(req) == compose(req)
    assert compose(req) == compose(pickle.loads(pickle.dumps(req)))


def test_same_parts_in_another_process() -> None:
    code = (
        f"import sys; sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "from test_prompt import single\n"
        "from consortium.core.prompt import compose\n"
        "sys.stdout.write(repr(compose(single())))\n"
    )
    env = {**os.environ, "PYTHONHASHSEED": "123"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, env=env)
    assert out.stdout == repr(compose(single()))
