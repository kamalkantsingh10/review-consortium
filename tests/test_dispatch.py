"""consortium open (Run) with the Fake rater: the story 1.7 I/O matrix and acceptance criteria."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import consortium.cli as cli_module
from consortium.archive.jsonl import REQUESTS_FILE, RESPONSES_FILE
from consortium.board import trials as board_trials
from consortium.board.blinding import append_conditions
from consortium.board.clips import insert_clip
from consortium.board.db import DB_FILE, connect, transaction
from consortium.board.lease import LOCK_FILE, acquire_lease
from consortium.board.ledger import Spend
from consortium.board.writer import start_writer
from consortium.cli import app
from consortium.core.errors import ConsortiumError
from consortium.core.render import ClipRef, canonical_json
from consortium.core.seeds import derive_seed
from consortium.engine.dispatch import Budget, _Prepared, dispatch
from consortium.raters.base import RaterResult
from consortium.raters.fake import FakeRater, fake_answer
from consortium.stages import personas as personas_stage
from consortium.stages.init import init_study
from consortium.stages.open import open_test, plan_and_render
from consortium.stages.push import push_test

runner = CliRunner()
SRC = Path(__file__).resolve().parents[1] / "src" / "consortium"

TARGETS = ["c_aaaaaaaa", "c_bbbbbbbb"]
PRACTICE = ["c_ppppppaa", "c_ppppppab", "c_ppppppac"]
GODSPEED = {f"animacy_{i}": 3 for i in range(1, 7)} | {f"likeability_{i}": 3 for i in range(1, 6)}
CONDITIONS = {"c_aaaaaaaa": {"gait": "smoothwalk"}, "c_bbbbbbbb": {"gait": "jerkystep"}}
SOURCE_NAME = "secret_source_take3.mov"
STEPS = ["begin_attempt", "append_request", "mark_sent", "set_handle", "append_response",
         "record_actual", "record_validation", "set_state"]
FOOTER = "cost: committed 0 USD, ceiling none"
TRIALS = 64 * (2 + 2)  # 64 Personas x 1 Model x 1 Repeat x (2 godspeed + 2 pairwise)
UNSCREENED = "unscreened_pilot: Test pilot1 runs without screening\n"


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
    study = init_study(tmp_path_factory.mktemp("dispatch") / "study")
    personas_stage.generate(study)
    _add_clips(study, TARGETS + PRACTICE)
    for clip_id, cond in CONDITIONS.items():
        append_conditions(study, clip_id, cond)
    push_test(study, _write_test(study, "pilot1"))
    return study


@pytest.fixture()
def study(tmp_path: Path, template: Path) -> Path:
    copy = tmp_path / "study"
    shutil.copytree(template, copy)
    return copy


def _cli(*args: str, input: str | None = None):
    return runner.invoke(app, ["open", *args], input=input)


def _lines(study: Path, rel: str) -> list[dict]:
    path = study / rel
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def _trials(study: Path) -> list[dict]:
    conn = connect(study)
    try:
        return board_trials.load_trials(conn, "pilot1")
    finally:
        conn.close()


def _attempts(study: Path) -> list[tuple]:
    conn = connect(study)
    try:
        return conn.execute(
            "SELECT trial_id, attempt, seed, handle, sent_at, answered_at, category FROM attempts"
        ).fetchall()
    finally:
        conn.close()


def _nothing_written(study: Path) -> None:
    assert _trials(study) == []
    assert _attempts(study) == []
    assert not (study / "archive").exists()


# --------------------------------------------------------------------------- matrix


def test_happy_run(study: Path) -> None:
    dry = _cli("pilot1", "--dry-run", "--study", str(study))
    assert dry.exit_code == 0, dry.stderr
    result = _cli("pilot1", "--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    out = result.stdout.splitlines()
    assert out[0] == "test: pilot1 (pilot)"
    assert out[-2:] == [f"states: valid {TRIALS}", FOOTER]
    # The Run dispatched exactly the requests the dry run digested, at the same estimate.
    assert out[1:-2] == dry.stdout.splitlines()[1:]

    trials = _trials(study)
    assert len(trials) == TRIALS
    assert {(t["state"], t["attempt"]) for t in trials} == {("valid", 1)}
    plan, requests, _ = plan_and_render(study, "pilot1")
    objs = {t.trial_id: r for t, r in zip(plan.trials, requests, strict=True)}
    rendered = {tid: canonical_json(r) for tid, r in objs.items()}
    assert [t["trial_id"] for t in trials] == list(rendered)  # stored in plan order

    reqs = _lines(study, REQUESTS_FILE)
    resps = _lines(study, RESPONSES_FILE)
    assert len(reqs) == len(resps) == TRIALS
    assert {(r["trial_id"], r["attempt"]) for r in reqs} == {(t, 1) for t in rendered}
    assert {(r["trial_id"], r["attempt"]) for r in resps} == {(t, 1) for t in rendered}
    by_id = {t["trial_id"]: t for t in trials}
    for rec in reqs:
        assert set(rec) == {"trial_id", "attempt", "seed", "model_id", "request",
                            "request_sha256", "ts"}
        body = canonical_json(rec["request"])
        assert body == rendered[rec["trial_id"]]
        assert rec["request_sha256"] == hashlib.sha256(body).hexdigest()
        t = by_id[rec["trial_id"]]
        assert rec["seed"] == derive_seed(1, "model", f"{t['session_id']}:{t['trial_index']}:1")
        assert rec["model_id"] == "m1"
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", rec["ts"])
    req_by_id = {r["trial_id"]: r for r in reqs}
    for rec in resps:
        assert set(rec) == {"trial_id", "attempt", "request_sha256", "raw", "usage",
                            "model_build", "category", "ts"}
        req = req_by_id[rec["trial_id"]]
        assert rec["request_sha256"] == req["request_sha256"]
        assert rec["raw"] == fake_answer(objs[rec["trial_id"]], req["seed"])
        assert rec["usage"] == {"input_tokens": 0, "output_tokens": 0}
        assert rec["model_build"] == "fake-1" and rec["category"] == "ok"
    # Archive lines are canonical JSON.
    for rel in (REQUESTS_FILE, RESPONSES_FILE):
        for line in (study / rel).read_bytes().splitlines():
            assert canonical_json(json.loads(line)) == line

    attempts = _attempts(study)
    assert len(attempts) == TRIALS
    seeds = {r["trial_id"]: r["seed"] for r in reqs}
    for trial_id, attempt, seed, handle, sent_at, answered_at, category in attempts:
        assert attempt == 1 and seed == seeds[trial_id]
        assert json.loads(handle)["trial_id"] == trial_id
        assert sent_at and answered_at and category == "ok"


def test_declined_writes_nothing(study: Path) -> None:
    prompts: list[str] = []

    def no(prompt: str) -> bool:
        prompts.append(prompt)
        return False

    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, confirm=no)
    assert info.value.code == "not_confirmed"
    digest = open_test(study, "pilot1", dry_run=True).requests_sha256
    assert prompts == [f"requests sha256: {digest}\nRun {TRIALS} Trials on fake?"]
    _nothing_written(study)
    assert not (study / LOCK_FILE).exists()  # the prompt comes before the lease


def test_declined_on_terminal(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = _cli("pilot1", "--study", str(study), input="n\n")
    assert result.exit_code == 1
    assert f"Run {TRIALS} Trials on fake? [y/N]" in result.stderr
    assert "not_confirmed:" in result.stderr
    # The summary and cost estimate are printed before the question (story 1.9), no states.
    assert ("cost estimate: expected 0 USD, worst case 0 USD (max_retries 2, transient_retries 3)"
            in result.stdout)
    assert "states:" not in result.stdout
    _nothing_written(study)


def test_eof_at_prompt_is_not_confirmed(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = _cli("pilot1", "--study", str(study), input="")
    assert result.exit_code == 1
    assert "not_confirmed:" in result.stderr
    assert "Aborted" not in result.stderr + result.stdout
    _nothing_written(study)


def test_yes_on_terminal_asks_nothing(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = _cli("pilot1", "--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert "Run " not in result.stderr and "[y/N]" not in result.stderr
    assert result.stderr == UNSCREENED  # story 3.3: no screening run yet, so ungated


def test_confirmed_on_terminal(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = _cli("pilot1", "--study", str(study), input="y\n")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines()[-2:] == [f"states: valid {TRIALS}", FOOTER]


def test_no_tty_requires_confirmation(study: Path) -> None:
    result = _cli("pilot1", "--study", str(study))  # CliRunner's stdin is not a TTY
    assert result.exit_code == 1
    assert result.stderr.startswith("confirmation_required:")
    _nothing_written(study)


def test_second_dispatcher_is_busy(study: Path) -> None:
    with acquire_lease(study):
        result = _cli("pilot1", "--yes", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("study_busy:")
    _nothing_written(study)


def test_reopen_refused(study: Path) -> None:
    assert _cli("pilot1", "--yes", "--study", str(study)).exit_code == 0
    archive = {rel: (study / rel).read_bytes() for rel in (REQUESTS_FILE, RESPONSES_FILE)}
    before = _attempts(study)
    result = _cli("pilot1", "--yes", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("test_already_open:")
    assert {rel: (study / rel).read_bytes() for rel in archive} == archive
    assert _attempts(study) == before


def test_insert_plan_refuses_existing_trials(study: Path) -> None:
    plan, _, _ = plan_and_render(study, "pilot1")
    conn = connect(study)
    try:
        assert board_trials.insert_plan(conn, "pilot1", plan.trials) == TRIALS
        with pytest.raises(ConsortiumError) as info:
            board_trials.insert_plan(conn, "pilot1", plan.trials)
        assert info.value.code == "test_already_open"
        assert board_trials.count_trials(conn, "pilot1") == TRIALS
    finally:
        conn.close()


def test_main_refused(study: Path) -> None:
    targets = ["c_mmmmmmma", "c_mmmmmmmb"]
    _add_clips(study, targets)
    push_test(study, _write_test(study, "main1", kind="main", clips=targets))
    result = _cli("main1", "--yes", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("protocol_lock_unavailable:")
    assert not (study / LOCK_FILE).exists()
    _nothing_written(study)


def test_qwen_without_key_is_api_key_missing(
    study: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    cfg = study / "study.yaml"
    doc = yaml.safe_load(cfg.read_text())
    model = doc["models"][0]
    del model["fake"]
    model["provider"] = "qwen"  # an adapter since story 2.3
    model["settings"]["base_url"] = "https://ws-1.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
    model["limits"]["max_bytes"] = 9_900_000  # hosted qwen: base64 under 10 MB
    cfg.write_text(yaml.safe_dump(doc, sort_keys=False))
    result = _cli("pilot1", "--yes", "--ceiling", "5", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("api_key_missing: m1: set DASHSCOPE_API_KEY")
    _nothing_written(study)
    assert not (study / LOCK_FILE).exists()


def test_dry_run_on_open_test(study: Path) -> None:
    before = _cli("pilot1", "--dry-run", "--study", str(study)).stdout
    assert _cli("pilot1", "--yes", "--study", str(study)).exit_code == 0
    after = _cli("pilot1", "--dry-run", "--study", str(study))
    assert after.exit_code == 0, after.stderr
    assert after.stdout == before


def test_run_migrates_older_board(study: Path) -> None:
    raw = sqlite3.connect(study / DB_FILE, isolation_level=None)
    raw.execute("DROP TABLE ledger")
    raw.execute("DROP TABLE ceiling_changes")
    raw.execute("ALTER TABLE tests DROP COLUMN paused_reason")
    raw.execute("DROP TABLE attempts")
    raw.execute("DROP TABLE trials")
    raw.execute("PRAGMA user_version = 2")
    raw.close()
    assert _cli("pilot1", "--dry-run", "--study", str(study)).stderr.startswith(
        "board_version_mismatch:"
    )
    result = _cli("pilot1", "--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines()[-2:] == [f"states: valid {TRIALS}", FOOTER]


def test_test_file_changed_after_confirmation(study: Path) -> None:
    path = study / "tests" / "pilot1.yaml"

    def edit_then_yes(prompt: str) -> bool:
        path.write_text(path.read_text() + "# edited\n")
        return True

    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, confirm=edit_then_yes)
    assert info.value.code == "test_changed"
    _nothing_written(study)


class _FailingRater(FakeRater):
    """Raises on the Nth ``submit`` call."""

    def __init__(self, n: int = 5, **usage: int) -> None:
        super().__init__(**usage)
        self.n = n
        self.calls = 0

    async def submit(self, calls):
        self.calls += 1
        if self.calls == self.n:
            raise RuntimeError("boom")
        await asyncio.sleep(0)
        return await super().submit(calls)


def _check_stopped_run(study: Path) -> None:
    trials = {t["trial_id"]: t for t in _trials(study)}
    assert len(trials) == TRIALS
    attempts = _attempts(study)
    resp_keys = {(r["trial_id"], r["attempt"]) for r in _lines(study, RESPONSES_FILE)}
    req_keys = {(r["trial_id"], r["attempt"]) for r in _lines(study, REQUESTS_FILE)}
    states = {t["state"] for t in trials.values()}
    assert "sent" in states and "planned" in states  # the Run stopped part way
    for trial_id, attempt, _seed, handle, sent_at, _answered, _category in attempts:
        state = trials[trial_id]["state"]
        if sent_at is not None:  # mark_sent ran
            assert (trial_id, attempt) in req_keys
            assert state in ("sent", "valid")
        if state == "valid":
            assert (trial_id, attempt) in resp_keys and handle is not None
    for t in trials.values():
        if t["state"] in ("valid", "invalid", "refused", "failed"):
            assert (t["trial_id"], t["attempt"]) in resp_keys
    # The failing Trial was marked sent and has no handle.
    no_handle_sent = [a for a in attempts if a[4] is not None and a[3] is None]
    assert no_handle_sent and all(trials[a[0]]["state"] == "sent" for a in no_handle_sent)


def test_adapter_error_stops_run(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import consortium.stages.open as open_stage

    monkeypatch.setattr(open_stage, "FakeRater", _FailingRater)
    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, yes=True)
    assert info.value.code == "run_failed"
    assert info.value.message == "RuntimeError: boom"
    _check_stopped_run(study)


def test_adapter_error_stops_run_cli(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import consortium.stages.open as open_stage

    monkeypatch.setattr(open_stage, "FakeRater", _FailingRater)
    result = _cli("pilot1", "--yes", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1] == "run_failed: RuntimeError: boom"
    assert "Traceback" not in result.stderr
    assert result.stdout.splitlines()[0] == "test: pilot1 (pilot)"  # announced before dispatch
    _check_stopped_run(study)


def test_failed_trials_warn_but_exit_zero(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import consortium.stages.open as open_stage

    class _ErrorRater(FakeRater):
        async def collect(self, handles):
            return [RaterResult(r.raw, r.usage, r.model_build, "fatal")
                    for r in await super().collect(handles)]

    monkeypatch.setattr(open_stage, "FakeRater", _ErrorRater)
    result = _cli("pilot1", "--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines()[-2:] == [f"states: failed {TRIALS}", FOOTER]
    assert result.stderr == UNSCREENED + f"warning: {TRIALS} Trials did not end valid\n"


# --------------------------------------------------------------------------- ordering


def test_per_attempt_order_via_writer_spy(study: Path) -> None:
    ops: list[tuple[str, tuple]] = []
    summary = open_test(study, "pilot1", dry_run=False, yes=True,
                        writer_spy=lambda op, key: ops.append((op, key)))
    assert summary.states == {"valid": TRIALS}
    assert ops[0] == ("insert_plan", ())
    assert ops[-1] == ("state_counts", ())
    per_trial: dict[str, list[str]] = {}
    for op, key in ops[1:-1]:
        trial_id = key[0]
        if op != "begin_attempt":
            assert key[1] == 1
        per_trial.setdefault(trial_id, []).append(op)
    assert len(per_trial) == TRIALS
    assert all(seq == STEPS for seq in per_trial.values())


def test_every_request_line_precedes_its_sent_write(study: Path) -> None:
    """Archive before state: when ``mark_sent`` runs, the request line is already on disk."""
    seen: list[str] = []

    def spy(op: str, key: tuple) -> None:
        reqs = {(r["trial_id"], r["attempt"]) for r in _lines(study, REQUESTS_FILE)}
        resps = {(r["trial_id"], r["attempt"]) for r in _lines(study, RESPONSES_FILE)}
        if op == "mark_sent":
            assert key in reqs
        if op == "set_state":
            assert key in resps
        seen.append(op)

    # A small subset keeps the O(n^2) file re-reads cheap.
    plan, requests, _ = plan_and_render(study, "pilot1")
    pairs = list(zip(plan.trials, requests, strict=True))[:20]

    async def run() -> None:
        async with start_writer(study, spy=spy) as writer:
            await writer.do("insert_plan",
                            lambda c: board_trials.insert_plan(c, "pilot1", plan.trials))
            await dispatch(study, pairs, {"m1": FakeRater()}, writer=writer, seed=1,
                           concurrency=4, budget=Budget.zero("pilot1"), max_retries=2)

    asyncio.run(run())
    assert seen.count("mark_sent") == seen.count("set_state") == 20


# --------------------------------------------------------------------------- engine


class _CountingRater:
    provider = "fake"

    def __init__(self, category: str = "ok") -> None:
        self.category = category
        self.prepared: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.inner = FakeRater()

    async def prepare(self, clip):
        self.prepared.append(clip.clip_id)
        await asyncio.sleep(0)
        return await self.inner.prepare(clip)

    async def submit(self, calls):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0)
        return await self.inner.submit(calls)

    async def collect(self, handles):
        await asyncio.sleep(0)
        self.in_flight -= 1
        return [
            RaterResult(r.raw, r.usage, r.model_build, self.category)
            for r in await self.inner.collect(handles)
        ]


def _run_engine(study: Path, rater: _CountingRater, concurrency: int) -> None:
    plan, requests, _ = plan_and_render(study, "pilot1")
    pairs = list(zip(plan.trials, requests, strict=True))

    async def run() -> None:
        async with start_writer(study) as writer:
            await writer.do("insert_plan",
                            lambda c: board_trials.insert_plan(c, "pilot1", plan.trials))
            await dispatch(study, pairs, {"m1": rater}, writer=writer, seed=1,
                           concurrency=concurrency, budget=Budget.zero("pilot1"), max_retries=2)

    asyncio.run(run())


def test_prepare_once_per_clip_and_semaphore(study: Path) -> None:
    rater = _CountingRater()
    _run_engine(study, rater, concurrency=3)
    assert sorted(rater.prepared) == sorted(TARGETS + PRACTICE)
    assert 1 < rater.max_in_flight <= 3


class _BadLengthRater(FakeRater):
    async def submit(self, calls):
        return (await super().submit(calls)) * 2


class _FlakyPrepareRater(FakeRater):
    def __init__(self) -> None:
        self.failed = False
        self.prepares = 0

    async def prepare(self, clip):
        self.prepares += 1
        if not self.failed:
            self.failed = True
            raise RuntimeError("upload failed")
        return await super().prepare(clip)


def _engine(study: Path, rater, concurrency: int = 4, models: dict | None = None) -> None:
    plan, requests, _ = plan_and_render(study, "pilot1")
    pairs = list(zip(plan.trials, requests, strict=True))

    async def run() -> None:
        async with start_writer(study) as writer:
            await writer.do("insert_plan",
                            lambda c: board_trials.insert_plan(c, "pilot1", plan.trials))
            await dispatch(study, pairs, models if models is not None else {"m1": rater},
                           writer=writer, seed=1, concurrency=concurrency,
                           budget=Budget.zero("pilot1"), max_retries=2)

    asyncio.run(run())


def test_wrong_result_count_is_adapter_error(study: Path) -> None:
    with pytest.raises(ConsortiumError) as info:
        _engine(study, _BadLengthRater())
    assert info.value.code == "adapter_error"


def test_failed_prepare_is_not_cached(study: Path) -> None:
    rater = _FlakyPrepareRater()
    with pytest.raises(ConsortiumError) as info:
        _engine(study, rater, concurrency=1)
    assert info.value.code == "run_failed"
    prepared = _Prepared()

    async def twice() -> None:
        clip = ClipRef("c_aaaaaaaa", "a" * 64)
        r = _FlakyPrepareRater()
        with pytest.raises(RuntimeError):
            await prepared.get(r, clip)
        assert (await prepared.get(r, clip)).clip_id == "c_aaaaaaaa"
        await prepared.get(r, clip)
        assert r.prepares == 2

    asyncio.run(twice())


def test_bad_concurrency_and_missing_rater(study: Path) -> None:
    with pytest.raises(ConsortiumError) as info:
        _engine(study, FakeRater(), concurrency=0)
    assert info.value.code == "bad_concurrency"
    with pytest.raises(ConsortiumError) as info:
        _engine(fresh_copy(study), FakeRater(), models={})
    assert info.value.code == "provider_unavailable"


def fresh_copy(study: Path) -> Path:
    copy = study.parent / "copy2"
    shutil.copytree(study, copy)
    conn = connect(copy)
    try:
        with transaction(conn):
            conn.execute("DELETE FROM attempts")
            conn.execute("DELETE FROM trials")
    finally:
        conn.close()
    return copy


def test_non_ok_category_fails(study: Path) -> None:
    _run_engine(study, _CountingRater(category="fatal"), concurrency=4)
    assert {(t["state"], t["attempt"]) for t in _trials(study)} == {("failed", 1)}
    assert {r["category"] for r in _lines(study, RESPONSES_FILE)} == {"fatal"}


def test_terminal_states_never_change(study: Path) -> None:
    open_test(study, "pilot1", dry_run=False, yes=True)
    trial_id = _trials(study)[0]["trial_id"]
    conn = connect(study)
    try:
        with pytest.raises(ValueError):
            board_trials.set_state(conn, trial_id, 1, "failed", "x")
        with pytest.raises(ValueError):
            board_trials.begin_attempt(conn, trial_id, 1, Decimal(0), None, Spend())
        with pytest.raises(sqlite3.IntegrityError):  # (trial_id, attempt) recorded once
            conn.execute("INSERT INTO attempts (trial_id, attempt, seed) VALUES (?, 1, 0)",
                         (trial_id,))
    finally:
        conn.close()
    assert _trials(study)[0]["state"] == "valid"


# --------------------------------------------------------------------------- blinding and AC


def test_no_condition_or_source_name_in_board_or_archive(study: Path) -> None:
    assert open_test(study, "pilot1", dry_run=False, yes=True).states == {"valid": TRIALS}
    conn = connect(study)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    blobs = [(study / DB_FILE).read_bytes()] + [
        (study / rel).read_bytes() for rel in (REQUESTS_FILE, RESPONSES_FILE)
    ]
    for blob in blobs:
        for needle in ("smoothwalk", "jerkystep", "gait", SOURCE_NAME):
            assert needle.encode() not in blob


def _calls(path: Path, names: set[str]) -> bool:
    import ast

    tree = ast.parse(path.read_text())
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in names
        for n in ast.walk(tree)
    )


def test_only_engine_calls_submit_and_collect() -> None:
    callers = {
        str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if _calls(p, {"submit", "collect"})
    }
    assert callers == {"engine/dispatch.py"}


def test_open_writes_board_only_through_writer() -> None:
    text = (SRC / "stages" / "open.py").read_text()
    assert "sqlite3" not in text
    for forbidden in ("connect(", "transaction(", ".execute("):
        assert forbidden not in text
    engine = (SRC / "engine" / "dispatch.py").read_text()
    assert "sqlite3" not in engine and ".execute(" not in engine


def test_archive_ends_an_unterminated_line_before_appending(tmp_path: Path) -> None:
    from consortium.archive.jsonl import append_response

    (tmp_path / "archive").mkdir()
    (tmp_path / RESPONSES_FILE).write_bytes(b'{"partial":')  # a crash mid-write
    append_response(tmp_path, trial_id="t", attempt=1, request_sha256="0" * 64, raw="{}",
                    usage={"input_tokens": 0, "output_tokens": 0}, model_build=None,
                    category="ok")
    lines = (tmp_path / RESPONSES_FILE).read_bytes().split(b"\n")
    assert lines[0] == b'{"partial":' and lines[-1] == b""
    assert json.loads(lines[1])["trial_id"] == "t"
