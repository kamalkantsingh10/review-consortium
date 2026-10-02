"""consortium open --resume: the story 1.8 I/O matrix, kill-and-resume and the re-issue check."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from consortium.archive.jsonl import (
    REQUESTS_FILE,
    RESPONSES_FILE,
    append_request,
    append_response,
    read_lines,
    read_requests,
    read_responses,
)
from consortium.board import trials as board_trials
from consortium.board.clips import insert_clip
from consortium.board.db import connect, transaction
from consortium.board.lease import LOCK_FILE, acquire_lease
from consortium.board.ledger import Spend
from consortium.cli import app
from consortium.core.errors import ConsortiumError
from consortium.core.render import canonical_json
from consortium.raters.base import RaterCall
from consortium.raters.fake import FakeRater, fake_answer
from consortium.stages import personas as personas_stage
from consortium.stages.init import init_study
from consortium.stages.open import check_reissue, open_test, plan_and_render
from consortium.stages.push import push_test

runner = CliRunner()

TARGETS = ["c_aaaaaaaa", "c_bbbbbbbb"]
PRACTICE = ["c_ppppppaa", "c_ppppppab", "c_ppppppac"]
GODSPEED = {f"animacy_{i}": 3 for i in range(1, 7)} | {f"likeability_{i}": 3 for i in range(1, 6)}
STEPS = ["begin_attempt", "append_request", "mark_sent", "set_handle", "append_response",
         "record_actual", "record_validation", "set_state"]
FOOTER = "cost: committed 0 USD, ceiling none"
TRIALS = 64 * (2 + 2)  # 64 Personas x 1 Model x 1 Repeat x (2 godspeed + 2 pairwise)
KILL_AT = 37  # the k-th occurrence of the chosen writer op is the last write before the kill


class Killed(Exception):
    """Stands in for the process dying: no writer op runs after it."""


def _add_clips(study: Path, ids: list[str]) -> None:
    conn = connect(study)
    try:
        with transaction(conn):
            for i, clip_id in enumerate(ids):
                info = SimpleNamespace(duration_s=2.0, size_bytes=1000, width=640, height=480,
                                       fps=25.0, loudness_lufs=-23.0)
                insert_clip(conn, clip_id, f"{i + 1:064x}", info)
    finally:
        conn.close()


def _write_test(study: Path, name: str, kind: str = "pilot", clips: list[str] = TARGETS) -> Path:
    doc: dict[str, Any] = {
        "schema_version": 1, "test": name, "kind": kind,
        "instruments": ["godspeed", "pairwise_alive"], "clips": clips,
        "practice": [
            {"instrument": "godspeed", "clips": [PRACTICE[0]], "answer": GODSPEED},
            {"instrument": "pairwise_alive", "clips": PRACTICE[1:3], "answer": {"alive": "A"}},
        ],
        "session": {"repeats": 1, "practice_clips": 1},
    }
    src = study.parent / f"{name}.yaml"
    src.write_text(yaml.safe_dump(doc, sort_keys=False))
    return src


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    study = init_study(tmp_path_factory.mktemp("resume") / "study")
    personas_stage.generate(study)
    _add_clips(study, TARGETS + PRACTICE)
    push_test(study, _write_test(study, "pilot1"))
    return study


@pytest.fixture()
def study(tmp_path: Path, template: Path) -> Path:
    copy = tmp_path / "study"
    shutil.copytree(template, copy)
    return copy


def _cli(*args: str, input: str | None = None):
    return runner.invoke(app, ["open", *args], input=input)


def _raw_lines(study: Path, rel: str) -> list[dict]:
    path = study / rel
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def _trials(study: Path) -> dict[str, dict]:
    conn = connect(study)
    try:
        return {r["trial_id"]: r for r in board_trials.load_trials(conn, "pilot1")}
    finally:
        conn.close()


def _attempts(study: Path) -> dict[tuple[str, int], dict]:
    conn = connect(study)
    try:
        cols = ("trial_id", "attempt", "seed", "handle", "sent_at", "answered_at", "category")
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM attempts").fetchall()
    finally:
        conn.close()
    return {(r[0], r[1]): dict(zip(cols, r, strict=True)) for r in rows}


def _killer(op_name: str, k: int = KILL_AT):
    state = {"count": 0, "dead": False}

    def spy(op: str, key: tuple) -> None:
        if state["dead"]:
            raise Killed(f"killed before {op} {key}")
        if op == op_name:
            state["count"] += 1
            if state["count"] == k:
                state["dead"] = True  # this op still runs; every later one does not

    return spy


def _kill_run(study: Path, op_name: str, k: int = KILL_AT) -> None:
    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, yes=True, writer_spy=_killer(op_name, k))
    assert info.value.code == "run_failed"
    assert "Killed" in info.value.message


def _snapshot(study: Path) -> dict[str, Any]:
    return {
        "trials": _trials(study),
        "attempts": _attempts(study),
        "requests": (study / REQUESTS_FILE).read_bytes() if (study / REQUESTS_FILE).exists()
        else b"",
        "responses": (study / RESPONSES_FILE).read_bytes()
        if (study / RESPONSES_FILE).exists() else b"",
    }


def _resume(study: Path, **kw: Any):
    return open_test(study, "pilot1", dry_run=False, yes=True, resume=True, **kw)


def _assert_resumed_correctly(study: Path, before: dict[str, Any]) -> None:
    """The acceptance criterion plus the per-state resume rules."""
    trials = _trials(study)
    attempts = _attempts(study)
    requests = read_requests(study)
    responses = read_responses(study)
    request_lines = _raw_lines(study, REQUESTS_FILE)

    assert all(t["state"] == "valid" for t in trials.values())
    # Every sent attempt has a request line.
    for key, a in attempts.items():
        if a["sent_at"] is not None:
            assert key in requests, key
    # Every terminal Trial has a response line for its final attempt.
    for tid, t in trials.items():
        assert (tid, t["attempt"]) in responses
        assert responses[(tid, t["attempt"])]["category"] == "ok"
    # Attempt numbers are 1..n per Trial, never reused; one request line per key.
    per_trial: dict[str, list[int]] = {}
    for tid, n in attempts:
        per_trial.setdefault(tid, []).append(n)
    for tid, ns in per_trial.items():
        assert sorted(ns) == list(range(1, trials[tid]["attempt"] + 1))
    keys = [(r["trial_id"], r["attempt"]) for r in request_lines]
    assert len(keys) == len(set(keys))

    old_lines = {(r["trial_id"], r["attempt"]) for r in
                 (json.loads(x) for x in before["requests"].splitlines())}
    for tid, old in before["trials"].items():
        new = trials[tid]
        handle = before["attempts"].get((tid, old["attempt"]), {}).get("handle")
        if old["state"] in board_trials.TERMINAL_STATES:
            # No completed Trial got a new attempt, nor any new line.
            assert new["attempt"] == old["attempt"]
            assert new["state"] == old["state"]
            assert {k for k in attempts if k[0] == tid} == {
                k for k in before["attempts"] if k[0] == tid}
        elif old["state"] == "sent" and handle is not None:
            # Collected at the same attempt: no new attempt, no new request line.
            assert new["attempt"] == old["attempt"]
            assert {k for k in keys if k[0] == tid} == {k for k in old_lines if k[0] == tid}
        else:
            # planned (any attempt) or sent without a handle: one new attempt.
            assert new["attempt"] == old["attempt"] + 1
            assert (tid, new["attempt"]) in requests
            assert (tid, new["attempt"]) not in old_lines
            if old["attempt"]:
                orphan = attempts[(tid, old["attempt"])]
                assert orphan["answered_at"] is None and orphan["category"] is None

    # The Archive only grew; the old bytes are an unchanged prefix.
    assert (study / REQUESTS_FILE).read_bytes().startswith(before["requests"])
    assert (study / RESPONSES_FILE).read_bytes().startswith(before["responses"])
    # And every archived request re-issues byte-identically.
    assert check_reissue(study, "pilot1") == len(requests)


# --------------------------------------------------------------------------- matrix


def _craft_mixed_state(study: Path) -> dict[str, str]:
    """A stopped Run with one Trial in each resumable state, written step by step."""
    plan, requests, _ = plan_and_render(study, "pilot1")
    pairs = list(zip(plan.trials, requests, strict=True))
    names = ["valid", "sent_handle", "sent_no_handle", "planned_after_begin",
             "planned_after_request", "planned_fresh"]
    chosen = dict(zip(names, pairs[:len(names)], strict=True))
    conn = connect(study)
    try:
        board_trials.insert_plan(conn, "pilot1", plan.trials)
        cfg_seed = _study_seed(study)
        rater = FakeRater()
        for name, (trial, request) in chosen.items():
            if name == "planned_fresh":
                continue
            tid = trial.trial_id
            attempt, seed = board_trials.begin_attempt(
                conn, tid, cfg_seed, Decimal(0), None, Spend()
            )
            if name == "planned_after_begin":
                continue
            record = append_request(study, trial_id=tid, attempt=attempt, seed=seed,
                                    model_id=trial.model_id, request=request)
            if name == "planned_after_request":
                continue
            board_trials.mark_sent(conn, tid, attempt)
            if name == "sent_no_handle":
                continue
            (handle,) = asyncio.run(rater.submit([RaterCall(tid, attempt, seed, request, ())]))
            board_trials.set_handle(conn, tid, attempt, canonical_json(handle).decode())
            if name == "sent_handle":
                continue
            append_response(study, trial_id=tid, attempt=attempt,
                            request_sha256=record["request_sha256"],
                            raw=handle["raw"], usage={"input_tokens": 0, "output_tokens": 0},
                            model_build="fake-1", category="ok")
            board_trials.set_state(conn, tid, attempt, "valid", "ok")
    finally:
        conn.close()
    return {name: pair[0].trial_id for name, pair in chosen.items()}


def _study_seed(study: Path) -> int:
    return yaml.safe_load((study / "study.yaml").read_text())["seed"]


def test_resume_after_kill_mixed_states(study: Path) -> None:
    ids = _craft_mixed_state(study)
    before = _snapshot(study)
    states = {name: before["trials"][tid]["state"] for name, tid in ids.items()}
    assert states == {"valid": "valid", "sent_handle": "sent", "sent_no_handle": "sent",
                      "planned_after_begin": "planned", "planned_after_request": "planned",
                      "planned_fresh": "planned"}
    ops: list[tuple[str, tuple]] = []
    summary = _resume(study, writer_spy=lambda op, key: ops.append((op, key)))
    assert summary.states == {"valid": TRIALS}
    assert summary.resumed == {"collect": 1, "new attempt": TRIALS - 2, "settled": 0, "terminal": 1,
                               "archive fragments": 0}
    _assert_resumed_correctly(study, before)

    after = _trials(study)
    assert after[ids["valid"]]["attempt"] == 1
    assert after[ids["sent_handle"]]["attempt"] == 1
    assert after[ids["sent_no_handle"]]["attempt"] == 2
    assert after[ids["planned_after_begin"]]["attempt"] == 2
    assert after[ids["planned_after_request"]]["attempt"] == 2
    assert after[ids["planned_fresh"]]["attempt"] == 1
    # The terminal Trial was never touched by any writer op.
    assert not [op for op, key in ops if key and key[0] == ids["valid"]]
    # The handled Trial was only collected: append_response then set_state, attempt 1.
    assert [(op, key) for op, key in ops if key and key[0] == ids["sent_handle"]] == [
        ("append_response", (ids["sent_handle"], 1)), ("record_actual", (ids["sent_handle"], 1)),
        ("record_validation", (ids["sent_handle"], 1)), ("set_state", (ids["sent_handle"], 1))]
    # Its response carries the archived request's SHA-256.
    tid = ids["sent_handle"]
    assert read_responses(study)[(tid, 1)]["request_sha256"] == \
        read_requests(study)[(tid, 1)]["request_sha256"]
    # The orphan attempt (begin_attempt, never sent) has no request line and was never sent.
    orphan = _attempts(study)[(ids["planned_after_begin"], 1)]
    assert orphan["sent_at"] is None and orphan["handle"] is None
    assert (ids["planned_after_begin"], 1) not in read_requests(study)


@pytest.mark.parametrize("op", ["insert_plan", *STEPS])
def test_kill_after_each_writer_op_then_resume(study: Path, op: str) -> None:
    _kill_run(study, op, 1 if op == "insert_plan" else KILL_AT)
    before = _snapshot(study)
    assert len(before["trials"]) == TRIALS
    assert any(t["state"] not in board_trials.TERMINAL_STATES
               for t in before["trials"].values())
    summary = _resume(study)
    assert summary.states == {"valid": TRIALS}
    _assert_resumed_correctly(study, before)


def test_killed_after_response_append_duplicates_are_last_line_wins(study: Path) -> None:
    _kill_run(study, "append_response")
    before = _snapshot(study)
    responses_before = read_responses(study)
    # The Trial whose response was the last write is still sent, with a handle.
    sent_with_response = [
        (tid, t["attempt"]) for tid, t in before["trials"].items()
        if t["state"] == "sent" and (tid, t["attempt"]) in responses_before
    ]
    assert sent_with_response
    _resume(study)
    _assert_resumed_correctly(study, before)

    lines = _raw_lines(study, RESPONSES_FILE)
    for key in sent_with_response:
        dupes = [r for r in lines if (r["trial_id"], r["attempt"]) == key]
        assert len(dupes) == 2
        assert dupes[0]["raw"] == dupes[1]["raw"]  # same answer, collected again
        assert read_responses(study)[key] == dupes[-1]  # readers take the last line
        assert _trials(study)[key[0]]["attempt"] == key[1]  # no new attempt


def test_killed_after_begin_attempt_gets_new_attempt(study: Path) -> None:
    _kill_run(study, "begin_attempt")
    before = _snapshot(study)
    orphans = [
        (tid, t["attempt"]) for tid, t in before["trials"].items()
        if t["state"] == "planned" and t["attempt"] >= 1
        and (tid, t["attempt"]) not in read_requests(study)
    ]
    assert orphans
    _resume(study)
    _assert_resumed_correctly(study, before)
    attempts = _attempts(study)
    for tid, n in orphans:
        assert attempts[(tid, n)]["sent_at"] is None  # attempt n never sent
        assert _trials(study)[tid]["attempt"] == n + 1
        assert (tid, n) not in read_requests(study)


def test_all_terminal_dispatches_nothing(study: Path) -> None:
    assert _cli("pilot1", "--yes", "--study", str(study)).exit_code == 0
    before = _snapshot(study)
    result = _cli("pilot1", "--resume", "--study", str(study))  # nothing to confirm
    assert result.exit_code == 0, result.stderr
    out = result.stdout.splitlines()
    assert out[0] == "test: pilot1 (pilot)"
    assert (f"resume: collect 0, new attempt 0, settled 0, terminal {TRIALS},"
            " archive fragments 0") in out
    assert out[-2:] == [f"states: valid {TRIALS}", FOOTER]
    assert result.stderr == ""
    assert _snapshot(study) == before


def test_resume_digest_matches_dry_run(study: Path) -> None:
    dry = _cli("pilot1", "--dry-run", "--study", str(study))
    digest = [x for x in dry.stdout.splitlines() if x.startswith("requests sha256:")]
    _kill_run(study, "set_handle")
    result = _cli("pilot1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert digest[0] in result.stdout.splitlines()
    head = dry.stdout.splitlines()[1:8]  # sessions .. requests sha256: identical counts
    assert result.stdout.splitlines()[1:len(head) + 1] == head


def test_resume_nothing_open(study: Path) -> None:
    result = _cli("pilot1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("test_not_open:")
    assert not (study / "archive").exists()
    assert _trials(study) == {}


def test_resume_busy(study: Path) -> None:
    _kill_run(study, "mark_sent")
    before = _snapshot(study)
    with acquire_lease(study):
        result = _cli("pilot1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("study_busy:")
    assert _snapshot(study) == before


def test_resume_refuses_main(study: Path) -> None:
    targets = ["c_mmmmmmma", "c_mmmmmmmb"]
    _add_clips(study, targets)
    push_test(study, _write_test(study, "main1", kind="main", clips=targets))
    result = _cli("main1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("protocol_lock_unavailable:")
    assert not (study / LOCK_FILE).exists()


def test_resume_confirmation(study: Path) -> None:
    _kill_run(study, "set_state")
    before = _snapshot(study)
    open_count = sum(t["state"] not in board_trials.TERMINAL_STATES
                     for t in before["trials"].values())
    assert 0 < open_count < TRIALS
    prompts: list[str] = []

    def decline(prompt: str) -> bool:
        prompts.append(prompt)
        return False

    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, resume=True, confirm=decline)
    assert info.value.code == "not_confirmed"
    assert prompts[0].splitlines()[-1] == f"Resume {open_count} Trials on fake?"
    assert _snapshot(study) == before

    result = _cli("pilot1", "--resume", "--study", str(study), input="y\n")  # not a TTY
    assert result.exit_code == 1
    assert result.stderr.startswith("confirmation_required:")
    assert _snapshot(study) == before

    summary = open_test(study, "pilot1", dry_run=False, resume=True, confirm=lambda p: True)
    assert summary.states == {"valid": TRIALS}


def test_resume_test_changed_after_confirmation(study: Path) -> None:
    _kill_run(study, "set_handle")
    before = _snapshot(study)
    card = study / "panel" / "personas" / "p1.md"

    def edit_then_confirm(prompt: str) -> bool:
        card.write_text(card.read_text() + "\nedited\n")
        return True

    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, resume=True, confirm=edit_then_confirm)
    assert info.value.code == "test_changed"
    assert _snapshot(study) == before


def test_dry_run_ignores_resume(study: Path) -> None:
    dry = _cli("pilot1", "--dry-run", "--resume", "--study", str(study))
    assert dry.exit_code == 0, dry.stderr


# --------------------------------------------------------------------------- re-issue


def test_reissue_after_completed_fake_run(study: Path) -> None:
    assert _cli("pilot1", "--yes", "--study", str(study)).exit_code == 0
    assert check_reissue(study, "pilot1") == TRIALS
    # Independently: re-render from the stored rows and compare the archived bytes.
    trials = _trials(study)
    plan, requests, _ = plan_and_render(study, "pilot1")
    rendered = {t.trial_id: r for t, r in zip(plan.trials, requests, strict=True)}
    for line in (study / REQUESTS_FILE).read_bytes().splitlines():
        record = json.loads(line)
        assert board_trials.trial_from_row(trials[record["trial_id"]]).trial_id in rendered
        body = canonical_json(rendered[record["trial_id"]])
        assert body in line  # the archived request bytes, verbatim
        assert canonical_json(record["request"]) == body
        assert hashlib.sha256(body).hexdigest() == record["request_sha256"]
    # The Fake answers re-issue too: same request + archived seed -> same raw answer.
    responses = read_responses(study)
    for (tid, attempt), record in read_requests(study).items():
        answer = fake_answer(rendered[tid], record["seed"])
        assert responses[(tid, attempt)]["raw"] == answer


def test_reissue_fails_loudly_after_edit(study: Path) -> None:
    _kill_run(study, "set_handle")
    card = study / "panel" / "personas" / "p1.md"
    card.write_text(card.read_text() + "\nedited\n")
    with pytest.raises(ConsortiumError) as info:
        check_reissue(study, "pilot1")
    assert info.value.code == "reissue_mismatch"
    before = _snapshot(study)
    result = _cli("pilot1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("reissue_mismatch:")
    assert _snapshot(study) == before


def test_reissue_not_open(study: Path) -> None:
    with pytest.raises(ConsortiumError) as info:
        check_reissue(study, "pilot1")
    assert info.value.code == "test_not_open"


# --------------------------------------------------------------------------- readers


def test_readers_last_line_wins_and_skip_fragments(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    lines = [
        {"trial_id": "t/a", "attempt": 1, "raw": "first"},
        {"trial_id": "t/b", "attempt": 1, "raw": "b"},
        {"trial_id": "t/a", "attempt": 1, "raw": "second"},
    ]
    body = b"".join(canonical_json(x) + b"\n" for x in lines)
    body = body.replace(b"\n", b'\n{"trial_id":"t/c","att\n', 1)  # a crash fragment, ended later
    (archive / "responses.jsonl").write_bytes(body + b'{"trial_id":"t/d"')  # unterminated
    got = read_responses(tmp_path)
    assert list(got) == [("t/b", 1), ("t/a", 1)]
    assert got[("t/a", 1)]["raw"] == "second"
    assert read_lines(tmp_path, RESPONSES_FILE)[1] == 1  # one fragment counted
    assert read_requests(tmp_path) == {}


@pytest.mark.parametrize("bad", [
    b'{"trial_id":"t/a"}', b'{"trial_id":"t/a","attempt":"1"}',
    b'{"trial_id":"t/a","attempt":true}', b'{"trial_id":7,"attempt":1}', b"[1,2]",
])
def test_reader_parseable_bad_key_is_corrupt(tmp_path: Path, bad: bytes) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "requests.jsonl").write_bytes(b'{"trial_id":"t/a","attempt":1}\n' + bad + b"\n")
    with pytest.raises(ConsortiumError) as info:
        read_requests(tmp_path)
    assert info.value.code == "archive_corrupt"
    assert "archive/requests.jsonl:2" in info.value.message


# --------------------------------------------------------------------------- real kill

KILLED_RUN = """
import os, signal, sys
from consortium.stages.open import open_test
count = 0
def spy(op, key):
    global count
    if op == "append_response":
        count += 1
        if count == int(sys.argv[2]):
            os.kill(os.getpid(), signal.SIGKILL)  # dies before this op runs
open_test(sys.argv[1], "pilot1", dry_run=False, yes=True, writer_spy=spy)
"""


def test_sigkilled_run_resumes(study: Path) -> None:
    """A really killed Run: the OS frees board.lock and resume completes it."""
    proc = subprocess.run([sys.executable, "-c", KILLED_RUN, str(study), str(KILL_AT)],
                          capture_output=True, text=True, timeout=120, env=os.environ)
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    before = _snapshot(study)
    assert sum(t["state"] == "valid" for t in before["trials"].values()) < TRIALS
    summary = _resume(study)
    assert summary.states == {"valid": TRIALS}
    _assert_resumed_correctly(study, before)


# --------------------------------------------------------------------------- review additions


def _rewrite_requests(study: Path, edit) -> None:
    """Rewrite requests.jsonl (tests only: the Archive is append-only in real use)."""
    path = study / REQUESTS_FILE
    records = [json.loads(x) for x in path.read_bytes().splitlines()]
    path.write_bytes(b"".join(canonical_json(r) + b"\n" for r in edit(records)))


def _expect_mismatch(study: Path) -> None:
    with pytest.raises(ConsortiumError) as info:
        check_reissue(study, "pilot1")
    assert info.value.code == "reissue_mismatch", info.value.message
    before = _snapshot(study)
    result = _cli("pilot1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("reissue_mismatch:"), result.stderr
    assert _snapshot(study) == before


def test_seed_edited_after_kill_is_reissue_mismatch(study: Path) -> None:
    _kill_run(study, "set_handle")
    cfg = study / "study.yaml"
    seed = _study_seed(study)
    text = cfg.read_text()
    assert f"seed: {seed}" in text
    cfg.write_text(text.replace(f"seed: {seed}", f"seed: {seed + 1}", 1))
    _expect_mismatch(study)


@pytest.mark.parametrize("field", ["request_sha256", "model_id"])
def test_tampered_request_line_is_reissue_mismatch(study: Path, field: str) -> None:
    _kill_run(study, "set_handle")

    def edit(records):
        records[3][field] = "0" * 64 if field == "request_sha256" else "m9"
        return records

    _rewrite_requests(study, edit)
    _expect_mismatch(study)


def test_sent_attempt_without_request_line_is_reissue_mismatch(study: Path) -> None:
    _kill_run(study, "set_handle")
    sent = next(tid for tid, t in _trials(study).items() if t["state"] == "sent"
                and _attempts(study)[(tid, t["attempt"])]["handle"] is not None)
    _rewrite_requests(study, lambda rs: [r for r in rs if r["trial_id"] != sent])
    _expect_mismatch(study)


def test_duplicated_request_line_is_reissue_mismatch(study: Path) -> None:
    _kill_run(study, "set_handle")
    _rewrite_requests(study, lambda rs: [*rs, rs[5]])
    _expect_mismatch(study)


def test_parseable_bad_line_stops_resume_as_archive_corrupt(study: Path) -> None:
    _kill_run(study, "set_handle")
    with (study / REQUESTS_FILE).open("ab") as f:
        f.write(b'{"attempt":1}\n')
    before = _snapshot(study)
    result = _cli("pilot1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("archive_corrupt:"), result.stderr
    assert _snapshot(study) == before


def test_fragment_counted_in_resume_line(study: Path) -> None:
    _kill_run(study, "append_response")
    with (study / RESPONSES_FILE).open("ab") as f:
        f.write(b'{"trial_id":"pilot1/p1')  # a crash mid-append; the next append ends it
    before = _snapshot(study)
    summary = _resume(study)
    assert summary.resumed["archive fragments"] == 0  # unterminated last line: ignored
    assert summary.states == {"valid": TRIALS}
    _assert_resumed_correctly(study, before)
    # Now the fragment is a whole (ended) line: the next resume counts it.
    assert _resume(study).resumed["archive fragments"] == 1


def test_kill_a_resume_then_resume_again(study: Path) -> None:
    _kill_run(study, "mark_sent")
    first = _snapshot(study)
    with pytest.raises(ConsortiumError) as info:
        _resume(study, writer_spy=_killer("set_handle", 20))
    assert info.value.code == "run_failed"
    middle = _snapshot(study)
    assert sum(t["state"] == "valid" for t in middle["trials"].values()) < TRIALS
    summary = _resume(study)
    assert summary.states == {"valid": TRIALS}
    _assert_resumed_correctly(study, middle)
    # Across both resumes: attempts 1..n per Trial, no Trial terminal at the first kill moved.
    final = _trials(study)
    for tid, old in first["trials"].items():
        if old["state"] in board_trials.TERMINAL_STATES:
            assert final[tid]["attempt"] == old["attempt"]


def test_resume_migrates_older_board(study: Path) -> None:
    import sqlite3

    from consortium.board.db import DB_FILE
    raw = sqlite3.connect(study / DB_FILE, isolation_level=None)
    raw.execute("DROP TABLE ledger")
    raw.execute("DROP TABLE ceiling_changes")
    raw.execute("ALTER TABLE tests DROP COLUMN paused_reason")
    raw.execute("DROP TABLE attempts")
    raw.execute("DROP TABLE trials")
    raw.execute("PRAGMA user_version = 2")
    raw.close()
    result = _cli("pilot1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("test_not_open:")  # migrated, then: no Trials yet
    assert _cli("pilot1", "--dry-run", "--study", str(study)).exit_code == 0  # at m3 now


def test_trial_state_changed_inside_confirm_is_test_changed(study: Path) -> None:
    _kill_run(study, "set_handle")
    fresh = next(tid for tid, t in _trials(study).items()
                 if t["state"] == "planned" and t["attempt"] == 0)

    def change_then_yes(prompt: str) -> bool:
        conn = connect(study)
        try:
            board_trials.begin_attempt(conn, fresh, _study_seed(study), Decimal(0), None, Spend())
        finally:
            conn.close()
        return True

    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, resume=True, confirm=change_then_yes)
    assert info.value.code == "test_changed"


def test_test_file_edited_inside_confirm_is_test_changed(study: Path) -> None:
    _kill_run(study, "set_handle")
    path = study / "tests" / "pilot1.yaml"

    def edit_then_yes(prompt: str) -> bool:
        path.write_text(path.read_text() + "# edited\n")
        return True

    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, resume=True, confirm=edit_then_yes)
    assert info.value.code == "test_changed"


class _CollectFails(FakeRater):
    async def collect(self, handles):
        raise ConsortiumError("provider_error", "collect failed")


def test_adapter_failing_on_collect_during_resume(
    study: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kill_run(study, "set_handle")
    before = _trials(study)
    handled = {tid: t["attempt"] for tid, t in before.items() if t["state"] == "sent"
               and _attempts(study)[(tid, t["attempt"])]["handle"] is not None}
    assert handled
    handles = {tid: _attempts(study)[(tid, n)]["handle"] for tid, n in handled.items()}
    monkeypatch.setattr("consortium.stages.open.FakeRater", _CollectFails)
    result = _cli("pilot1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("provider_error: collect failed")
    after = _trials(study)
    for tid, n in handled.items():
        assert after[tid]["state"] == "sent" and after[tid]["attempt"] == n
        assert _attempts(study)[(tid, n)]["handle"] == handles[tid]


def test_unreadable_stored_handle_is_adapter_error(study: Path) -> None:
    _kill_run(study, "set_handle")
    tid, n = next((tid, t["attempt"]) for tid, t in _trials(study).items()
                  if t["state"] == "sent" and _attempts(study)[(tid, t["attempt"])]["handle"])
    conn = connect(study)
    try:
        conn.execute("UPDATE attempts SET handle = 'not json' WHERE trial_id = ? AND attempt = ?",
                     (tid, n))
    finally:
        conn.close()
    with pytest.raises(ConsortiumError) as info:
        _resume(study)
    assert info.value.code == "adapter_error"
    assert info.value.message == f"stored handle unreadable for {tid}"


def test_removed_model_of_terminal_trials_does_not_block(study: Path) -> None:
    assert _cli("pilot1", "--yes", "--study", str(study)).exit_code == 0
    from consortium.stages.open import _Resumable, _resume_models
    res = _Resumable([], {}, "", [{"model_id": "m9"}], 0)
    cfg = SimpleNamespace(models=[SimpleNamespace(id="m1", provider="fake")])
    with pytest.raises(ConsortiumError) as info:
        _resume_models(cfg, "pilot1", res)  # type: ignore[arg-type]
    assert info.value.code == "unknown_model"
    assert _resume_models(cfg, "pilot1", _Resumable([], {}, "", [], 0)) == []  # type: ignore
