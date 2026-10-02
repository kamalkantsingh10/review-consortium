"""Use case: open a registered Test: ``--dry-run`` (story 1.6) or a Run (story 1.7).

Both load the Study config, the registered Test, its Instruments, the Persona
Panel and the Clip hashes; plan every Session and Trial; and render every
request (so a broken Instrument or missing Clip fails before anything is sent).

A dry run returns counts plus a SHA-256 digest of every request and writes no
Study data (``board.db`` is opened read-only).

A Run refuses a Test that already has Trials, asks for confirmation (unless
``yes``) showing the Trial count and requests digest, then takes the
``board.lock`` lease, re-checks that the Test is still unopened and that the
registered Test file and the requests digest are unchanged, announces the
summary, stores every planned Trial as ``planned`` in one transaction and sends
them all through ``engine.dispatch``. Every ``board.db`` write and Archive
append happens on the single writer task. A Run on an older ``board.db`` layout
migrates it first, under the lease.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from consortium.board.clips import list_clips
from consortium.board.db import read_only
from consortium.board.lease import acquire_lease
from consortium.board.tests import get_test
from consortium.board.trials import count_trials, insert_plan, state_counts
from consortium.board.writer import Spy, migrate, start_writer
from consortium.config.load import PERSONAS_DIR, load_instruments, load_personas, load_study
from consortium.config.load import load_test as load_test_file
from consortium.config.models import StudyConfig
from consortium.core.errors import ConsortiumError
from consortium.core.plan import Plan, Trial, plan_test
from consortium.core.render import TrialRequest, canonical_json, practice_for, render
from consortium.core.test_checks import check_media_limits, check_plan
from consortium.engine.dispatch import dispatch
from consortium.raters.base import Rater
from consortium.raters.fake import FakeRater

Confirm = Callable[[str], bool]
Announce = Callable[[list[str]], None]


@dataclass(frozen=True)
class OpenSummary:
    test: str
    kind: str
    dry_run: bool
    sessions: int
    trials_per_session: dict[str, int]  # by Instrument, in Test order
    trials: int
    by_model: dict[str, int]
    by_instrument: dict[str, int]
    by_type: dict[str, int]  # single / pairwise
    requests_sha256: str  # over the concatenated canonical JSON of every request, plan order
    states: dict[str, int] | None = None  # Trials by state after a Run; None for a dry run

    @property
    def not_valid(self) -> int:
        """Trials that did not end ``valid`` after a Run (0 for a dry run)."""
        if self.states is None:
            return 0
        return self.trials - self.states.get("valid", 0)

    def lines(self) -> list[str]:
        """The summary as printed by ``consortium open``."""
        per = self.trials_per_session
        per_total = sum(per.values())
        parts = " + ".join(str(n) for n in per.values())
        names = ", ".join(f"{k} {v}" for k, v in per.items())

        def fmt(counts: dict[str, int]) -> str:
            return ", ".join(f"{k} {v}" for k, v in counts.items())

        return [
            f"test: {self.test} ({self.kind}){' dry run' if self.dry_run else ''}",
            f"sessions: {self.sessions}",
            f"trials per session: {parts} = {per_total} ({names})",
            f"trials: {self.trials}",
            f"by model: {fmt(self.by_model)}",
            f"by instrument: {fmt(self.by_instrument)}",
            f"by type: {fmt(self.by_type)}",
            f"requests sha256: {self.requests_sha256}",
            *([f"states: {fmt(self.states)}"] if self.states is not None else []),
        ]


def _summarize(
    plan: Plan, instrument_order: list[str], dry_run: bool, requests_sha256: str
) -> OpenSummary:
    by_model: Counter[str] = Counter()
    by_instrument: Counter[str] = Counter()
    by_type: Counter[str] = Counter()
    shapes = set()
    for session in plan.sessions:
        per = Counter(t.instrument for t in session.trials)
        shapes.add(tuple(per[name] for name in instrument_order))
        for trial in session.trials:
            by_model[trial.model_id] += 1
            by_instrument[trial.instrument] += 1
            by_type["pairwise" if trial.pairwise else "single"] += 1
    if len(shapes) != 1:
        raise AssertionError(f"Sessions differ in shape or the plan is empty: {shapes}")
    (per_session,) = shapes
    models = list(dict.fromkeys(s.model_id for s in plan.sessions))
    return OpenSummary(
        test=plan.test,
        kind=plan.kind,
        dry_run=dry_run,
        sessions=len(plan.sessions),
        trials_per_session=dict(zip(instrument_order, per_session, strict=True)),
        trials=len(plan),
        by_model={m: by_model[m] for m in models},
        by_instrument={name: by_instrument[name] for name in instrument_order},
        by_type={k: by_type[k] for k in ("single", "pairwise")},
        requests_sha256=requests_sha256,
    )


def parse_ceiling(ceiling: str | None) -> Decimal | None:
    """``--ceiling`` as a USD ``Decimal`` > 0; ``bad_ceiling`` otherwise."""
    if ceiling is None:
        return None
    try:
        value = Decimal(ceiling.strip())
    except InvalidOperation as err:
        raise ConsortiumError(
            "bad_ceiling", f"--ceiling: {ceiling!r} is not a USD amount such as 5.00"
        ) from err
    if not value.is_finite() or value <= 0:
        raise ConsortiumError(
            "bad_ceiling", f"--ceiling: {ceiling!r} must be a USD amount greater than 0"
        )
    return value


def _read_card(study: Path, persona_id: str) -> str:
    rel = f"{PERSONAS_DIR}/{persona_id}.md"
    try:
        return (study / rel).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ConsortiumError("panel_invalid", f"cannot read {rel}: {err}", path=rel) from err


def open_test(
    study_dir: Path | str,
    test: str,
    *,
    dry_run: bool,
    yes: bool = False,
    ceiling: str | None = None,
    resume: bool = False,
    confirm: Confirm | None = None,
    announce: Announce | None = None,
    writer_spy: Spy | None = None,
) -> OpenSummary:
    """Plan and render every Trial of the registered Test ``test``; unless ``dry_run``, run it.

    ``ceiling`` is validated (``bad_ceiling``) but not yet used (story 1.9);
    ``resume`` is refused with ``resume_unavailable`` for a Run (story 1.8) and
    ignored for a dry run. Planning raises
    ``unknown_test`` (not registered, or no ``board.db``), ``protocol_lock_unavailable``
    (before any planning), ``test_exists`` (the registered file is missing or was
    edited), ``test_changed``, ``board_unreadable``, ``panel_missing`` /
    ``panel_invalid``, ``unknown_clip``, ``bad_pairing``, ``bad_practice``,
    ``media_limit_exceeded``, ``unknown_instrument`` and any config error.

    A Run additionally raises ``provider_unavailable`` (a Model's provider has no
    adapter yet), ``study_busy`` (another dispatcher holds ``board.lock``),
    ``test_already_open`` (the Test already has Trials), ``confirmation_required``
    (``yes`` is false and no ``confirm`` callback is given) and ``not_confirmed``
    (``confirm(prompt)`` returned false), and ``test_changed`` (the registered
    file or the requests changed between the confirmation and the lease); all of
    these write no Trial and no Archive line. Once dispatch starts, an adapter
    failure raises its ``ConsortiumError`` or ``run_failed``. ``announce`` gets the
    summary lines just before dispatch; ``writer_spy`` observes writer operations.
    """
    parse_ceiling(ceiling)
    study = Path(study_dir)
    if dry_run:
        _, plan, requests, instrument_order, _, _ = _plan_and_render(study, test)
        digest = hashlib.sha256()
        for request in requests:  # rendered lazily, one at a time
            digest.update(canonical_json(request))
        return _summarize(plan, instrument_order, dry_run, digest.hexdigest())
    if resume:
        raise ConsortiumError("resume_unavailable", "--resume arrives in story 1.8")
    try:
        prepared = _plan_and_render(study, test)
    except ConsortiumError as err:
        if err.code != "board_version_mismatch":
            raise
        with acquire_lease(study):  # a Run writes board.db, so it migrates an older layout
            migrate(study)
        prepared = _plan_and_render(study, test)
    return _run(
        study, test, prepared, yes=yes, confirm=confirm, announce=announce,
        writer_spy=writer_spy,
    )


def raters_for(cfg: StudyConfig, model_ids: list[str]) -> dict[str, Rater]:
    """``model_id -> Rater``; one Rater per provider. Only ``fake`` exists so far."""
    by_provider: dict[str, Rater] = {}
    out: dict[str, Rater] = {}
    for model_id in model_ids:
        provider = cfg.model_by_id(model_id).provider
        if provider not in by_provider:
            if provider != "fake":
                raise ConsortiumError(
                    "provider_unavailable",
                    f"model {model_id}: provider {provider!r} has no adapter yet (Epic 2)",
                )
            by_provider[provider] = FakeRater()
        out[model_id] = by_provider[provider]
    return out


def _render_all(
    plan: Plan, requests: Iterator[TrialRequest]
) -> tuple[list[tuple[Trial, TrialRequest]], str]:
    digest = hashlib.sha256()
    pairs: list[tuple[Trial, TrialRequest]] = []
    for trial, request in zip(plan.trials, requests, strict=True):
        digest.update(canonical_json(request))
        pairs.append((trial, request))
    return pairs, digest.hexdigest()


def _refuse_if_open(study: Path, test: str) -> None:
    if read_only(study, lambda conn: count_trials(conn, test)):
        raise ConsortiumError("test_already_open", f"Test {test!r} already has Trials")


def _run(
    study: Path,
    test: str,
    prepared: _Prepared,
    *,
    yes: bool,
    confirm: Confirm | None,
    announce: Announce | None,
    writer_spy: Spy | None,
) -> OpenSummary:
    cfg, plan, requests, instrument_order, rel, test_sha256 = prepared
    pairs, digest = _render_all(plan, requests)
    model_ids = list(dict.fromkeys(s.model_id for s in plan.sessions))
    raters = raters_for(cfg, model_ids)
    providers = list(dict.fromkeys(r.provider for r in raters.values()))
    _refuse_if_open(study, test)
    if not yes:
        if confirm is None:
            raise ConsortiumError(
                "confirmation_required", "stdin is not a terminal; pass --yes to confirm"
            )
        prompt = f"requests sha256: {digest}\nRun {len(pairs)} Trials on {', '.join(providers)}?"
        if not confirm(prompt):
            raise ConsortiumError("not_confirmed", "Run not confirmed; nothing was sent")

    with acquire_lease(study):
        _refuse_if_open(study, test)
        changed = ConsortiumError(
            "test_changed", "the Test or its requests changed since the confirmation", path=rel
        )
        try:
            if hashlib.sha256((study / rel).read_bytes()).hexdigest() != test_sha256:
                raise changed
        except OSError as err:
            raise changed from err
        cfg, plan2, requests2, _, _, sha_now = _plan_and_render(study, test)
        _, digest_now = _render_all(plan2, requests2)
        raters_now = raters_for(cfg, model_ids)
        if (
            sha_now != test_sha256
            or digest_now != digest
            or [r.provider for r in raters_now.values()] != [r.provider for r in raters.values()]
        ):
            raise changed
        summary = _summarize(plan, instrument_order, False, digest)
        if announce is not None:
            announce(summary.lines())
        states = asyncio.run(_dispatch_all(study, cfg, plan, pairs, raters, writer_spy))
    return dataclasses.replace(summary, states=states)


async def _dispatch_all(
    study: Path,
    cfg: StudyConfig,
    plan: Plan,
    pairs: list[tuple[Trial, TrialRequest]],
    raters: dict[str, Rater],
    writer_spy: Spy | None,
) -> dict[str, int]:
    async with start_writer(study, spy=writer_spy) as writer:
        await writer.do("insert_plan", lambda conn: insert_plan(conn, plan.test, plan.trials))
        await dispatch(
            study, pairs, raters, writer=writer, seed=cfg.seed, concurrency=cfg.concurrency
        )
        return await writer.do("state_counts", lambda conn: state_counts(conn, plan.test))


def _read_registration(study: Path, test: str) -> tuple[dict, dict[str, dict]]:
    def read(conn):
        row = get_test(conn, test)
        if row is None:
            return None
        clips = list_clips(conn, ids=[clip_id for clip_id, _ in row["clips"]])
        return row, {c["clip_id"]: c for c in clips}

    found = read_only(study, read)
    if found is None:
        raise ConsortiumError("unknown_test", f"{test!r} is not a registered Test")
    return found


def plan_and_render(
    study_dir: Path | str, test: str
) -> tuple[Plan, Iterator[TrialRequest], list[str]]:
    """The Plan of the registered Test, its requests rendered lazily (in ``plan.trials``
    order) and the Test's Instrument order. Reads only; see ``open_test`` for the errors.
    Every check runs before the Plan is returned; rendering can still raise
    ``unknown_clip`` while the iterator is consumed.
    """
    _, plan, requests, instrument_order, _, _ = _plan_and_render(Path(study_dir), test)
    return plan, requests, instrument_order


# cfg, plan, lazily rendered requests, Instrument order, registered path, registered SHA-256
_Prepared = tuple[StudyConfig, Plan, Iterator[TrialRequest], list[str], str, str]


def _plan_and_render(study: Path, test: str) -> _Prepared:
    cfg = load_study(study)
    row, clip_rows = _read_registration(study, test)
    if row["kind"] == "main":
        raise ConsortiumError(
            "protocol_lock_unavailable",
            f"Test {test!r} is kind main; main Tests open only once the Protocol lock "
            "exists (Epic 4)",
        )
    if not row["openable"]:
        raise ConsortiumError(
            "protocol_lock_unavailable", f"Test {test!r} is registered as not openable"
        )

    rel = row["path"]
    path = study / rel

    def fail(code: str, message: str) -> ConsortiumError:
        return ConsortiumError(code, message, path=rel)

    def file_sha256() -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as err:
            raise fail(
                "test_exists",
                f"test: {test!r} is registered, but its registered file cannot be read",
            ) from err

    if file_sha256() != row["sha256"]:
        raise fail(
            "test_exists", f"test: {test!r} is registered, but its registered file was edited"
        )
    test_cfg = load_test_file(path, study, cfg=cfg)
    if file_sha256() != row["sha256"]:
        raise fail("test_changed", "the Test file changed while it was being validated")
    if test_cfg.kind == "main":  # registration says otherwise only if the DB was edited
        raise fail("protocol_lock_unavailable", f"Test {test!r} is kind main (Epic 4)")

    # Config may have changed since push test: re-run its pure checks.
    all_instruments = load_instruments(study, cfg)
    instruments = {n: all_instruments[n] for n in test_cfg.instruments if n in all_instruments}
    practice_per_instrument = test_cfg.effective_session(cfg).practice_clips
    shapes = check_plan(test_cfg, instruments, practice_per_instrument, fail)
    for i, clip in enumerate(test_cfg.clips):
        if clip not in clip_rows:
            raise fail("unknown_clip", f"clips[{i}]: {clip!r} is not a pushed Clip")
    for i, example in enumerate(test_cfg.practice):
        for clip in example.clips:
            if clip not in clip_rows:
                raise fail("bad_practice", f"practice[{i}]: {clip!r} is not a pushed Clip")
    models = [cfg.model_by_id(m) for m in test_cfg.model_ids(cfg)]
    check_media_limits(shapes, clip_rows, models, fail)

    personas = load_personas(study)
    cards = {p.id: _read_card(study, p.id) for p in personas}
    plan = plan_test(test_cfg, cfg, personas, instruments)

    practice = {
        name: practice_for(test_cfg.practice, name, practice_per_instrument)
        for name in test_cfg.instruments
    }
    clip_sha256 = {clip_id: c["sha256"] for clip_id, c in clip_rows.items()}
    requests = (
        render(
            trial,
            persona_card=cards[trial.persona_id],
            instrument=instruments[trial.instrument],
            practice=practice[trial.instrument],
            clip_sha256=clip_sha256,
        )
        for trial in plan.trials
    )
    return cfg, plan, requests, list(test_cfg.instruments), rel, row["sha256"]
