"""Use case: open a registered Test: ``--dry-run`` (1.6), a Run (1.7) or ``--resume`` (1.8).

Both load the Study config, the registered Test, its Instruments, the Persona
Panel and the Clip hashes; plan every Session and Trial; and render every
request (so a broken Instrument or missing Clip fails before anything is sent).

A dry run returns counts plus a SHA-256 digest of every request and writes no
Study data (``board.db`` is opened read-only).

The machinery (estimate, confirmation, ceiling, lease, under-lease recheck,
dispatch, resume) lives in ``engine.run`` (AD-6, story 3.1), shared with
``screen``; this stage builds the ``Prepared`` of a registered Test and passes
``config.load`` in as a ``ConfigReader``. A Run on an older ``board.db`` layout
migrates it first, under the lease. The names below are re-exported unchanged.

Eligibility gate (story 3.3; it lives here, never in ``engine``). A ``main`` Test is
always gated; a ``pilot`` Test is gated once ``board.db`` holds a complete screening
run (open or abandoned runs do not count; before that it runs ungated with the stderr
warning ``unscreened_pilot`` and the summary line ``screening: none``); screening Tests
are never opened. After the Panel loads, ``core.eligibility.eligible`` judges every Agent
against the rows of every complete screening run and the current stamps; ``open``
refuses ``screening_coverage_missing``, then ``screening_stale``, then
``no_eligible_agents`` (for a pilot the message adds which runs gate it), and otherwise
plans only the eligible Agents (``plan_test(agents=...)``; their Trials are
byte-identical to the ungated plan). The dry run and the Run gate identically; under the
lease the Run re-gates, and a different decision (Agents, exclusions, stamps or deciding
runs) or a gate refusal raises ``test_changed``. The Run's ``insert_plan`` transaction
also records one ``screening_exclusions`` row per excluded Agent and one
``screening_stamps`` row per planned ``(model, instrument)`` plus one per planned Model
for ``fidelity`` (the fidelity stamp), each with the run IDs that decided. A resume
never re-gates: the Models and Instruments of the Trials it will send must still match
the recorded stamps, fidelity included (``screening_stale``), checked before the
confirmation and again under the lease.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from consortium.board.db import read_only
from consortium.board.lease import acquire_lease
from consortium.board.screening import (
    complete_results,
    complete_run_ids,
    insert_gated_plan,
    test_exclusions,
    test_stamps,
)
from consortium.board.trials import insert_plan, load_resumable
from consortium.board.writer import Spy, migrate
from consortium.config import load as config_load
from consortium.config.models import StudyConfig, TestConfig
from consortium.core.cost import usd
from consortium.core.eligibility import Eligibility, Stamps, eligible
from consortium.core.errors import ConsortiumError
from consortium.core.hashes import fidelity_hash, instrument_hash, settings_hash
from consortium.core.personas import Persona
from consortium.core.plan import Plan, agent_id, plan_test
from consortium.core.render import TrialRequest
from consortium.engine import run as engine
from consortium.engine.run import (
    CLIPS_DIR,
    Announce,
    ConfigReader,
    Confirm,
    OpenSummary,
    Prepared,
    parse_ceiling,
)
from consortium.engine.run import Resumable as _Resumable
from consortium.engine.run import TestContext as _Context
from consortium.engine.run import resume_models as _resume_models
from consortium.raters.base import Rater
from consortium.raters.fake import FakeRater
from consortium.raters.gemini import GeminiRater
from consortium.raters.qwen import QwenRater

__all__ = [
    "CLIPS_DIR", "Announce", "Confirm", "FakeRater", "GeminiRater", "OpenSummary", "QwenRater",
    "_Context", "_Resumable", "_resume_models", "check_reissue", "open_test", "parse_ceiling",
    "plan_and_render", "raters_for", "usd",
]

log = logging.getLogger(__name__)
Warn = Callable[[str], None]

READER = ConfigReader(
    load_study=config_load.load_study,
    load_prices=config_load.load_prices,
    load_test=config_load.load_test,
    load_instruments=config_load.load_instruments,
    load_personas=config_load.load_personas,
    read_card=config_load.read_card,
    load_card_wording=config_load.load_card_wording,
    load_fidelity_instruments=config_load.load_fidelity_instruments,
)


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
    warn: Warn | None = None,
) -> OpenSummary:
    """Plan and render every Trial of the registered Test ``test``; unless ``dry_run``, run it.

    ``ceiling`` (USD text, > 0, else ``invalid_ceiling``) is the Study's new
    cost ceiling, logged once a Run or resume is confirmed (never for a dry run).
    A Run or resume raises ``ceiling_required`` (no ceiling and a non-Fake Model)
    and a fresh Run ``over_ceiling`` (committed + expected > ceiling), both after
    the estimate is announced and before confirmation; a Run that pauses at the
    ceiling returns with ``paused == "ceiling"``. ``resume`` continues a stopped
    Run (see ``_resume``); it is ignored for a dry run. Planning raises
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

    The eligibility gate (story 3.3, see the module docstring) raises
    ``screening_coverage_missing``, ``screening_stale`` and ``no_eligible_agents``
    (dry run and Run alike); a resume of a gated Test raises ``screening_stale`` when a
    stamp it was opened with no longer matches. An ungated pilot passes
    ``unscreened_pilot: Test <t> runs without screening`` to ``warn`` (default: logged)
    once every refusal checked before confirmation has passed (a dry run: once it
    returns), just before the confirmation prompt.

    With ``resume`` the Test is never re-planned: ``test_not_open`` (no Trials)
    replaces ``test_already_open``, ``reissue_mismatch`` is raised when an archived
    request no longer re-renders byte-identically, and confirmation counts only
    the non-terminal Trials.
    """
    new_ceiling = parse_ceiling(ceiling)
    study = Path(study_dir)
    say = warn or log.warning
    if dry_run:
        prepared, gate = _prepare(study, test, plan=True)
        summary = engine.dry_run(study, prepared, new_ceiling)
        if gate is not None and gate.warn:
            say(_unscreened(test))
        return summary

    def load() -> Prepared:
        return _prepare(study, test, plan=not resume, say=say)[0]

    try:
        prepared = load()
    except ConsortiumError as err:
        if err.code != "board_version_mismatch":
            raise
        with acquire_lease(study):  # a Run writes board.db, so it migrates an older layout
            migrate(study)
        prepared = load()
    step = engine.resume if resume else engine.run
    return step(
        study, prepared, new_ceiling, yes=yes, confirm=confirm, announce=announce,
        writer_spy=writer_spy, raters=_raters,
    )


def _raters(cfg: StudyConfig, model_ids: list[str], study: Path) -> dict[str, Rater]:
    return raters_for(cfg, model_ids, study)  # looked up at call time (tests substitute it)


def raters_for(cfg: StudyConfig, model_ids: list[str], study: Path) -> dict[str, Rater]:
    """``engine.run.raters_for`` with this module's adapter classes (``FakeRater``,
    ``GeminiRater``, ``QwenRater``), looked up at call time."""
    return engine.raters_for(
        cfg, model_ids, study, fake=FakeRater, gemini=GeminiRater, qwen=QwenRater
    )


def check_reissue(study_dir: Path | str, test: str) -> int:
    """Prove re-issue (FR24, NFR6): re-render every archived request of ``test``.

    For every ``archive/requests.jsonl`` line of the Test (last line per key),
    the request rendered by ``core.render`` from the stored Trial row and the
    current Study folder must equal the archived request bytes, its SHA-256 the
    line's ``request_sha256``, and the line's seed and Model those of its
    attempt; each key has exactly one line, and every attempt marked ``sent``
    has one. Reads only; returns the number of lines checked. Raises
    ``reissue_mismatch``, ``archive_corrupt``, ``test_not_open`` and the errors
    of ``open_test``'s checks.

    Run it under the ``board.lock`` lease (``acquire_lease``), as ``open --resume``
    does: a dispatcher writing at the same time can leave a request line
    without its ``sent`` mark yet, or the reverse.
    """
    study = Path(study_dir)
    ctx, _, _ = engine.load_test_context(study, test, READER)
    res = engine.load_resumable_trials(study, test, ctx)
    return engine.check_reissue_lines(study, ctx, test, res)[0]


def plan_and_render(
    study_dir: Path | str, test: str
) -> tuple[Plan, Iterator[TrialRequest], list[str]]:
    """The Plan of the registered Test, its requests rendered lazily (in ``plan.trials``
    order) and the Test's Instrument order. Reads only; see ``open_test`` for the errors.
    Every check runs before the Plan is returned; rendering can still raise
    ``unknown_clip`` while the iterator is consumed.
    """
    prepared, _ = _prepare(Path(study_dir), test, plan=True)
    assert prepared.plan is not None
    return prepared.plan, prepared.requests(), prepared.ctx.instrument_order


# --------------------------------------------------------------------------- gate

GATE_REFUSALS = ("screening_coverage_missing", "screening_stale", "no_eligible_agents")


@dataclass(frozen=True)
class Gate:
    """The eligibility decision of one open (``gated`` False: ungated)."""

    gated: bool
    warn: bool = False  # an unscreened pilot: ``unscreened_pilot``
    eligibility: Eligibility | None = None
    stamps: tuple[dict[str, Any], ...] = ()  # per planned (model_id, instrument|fidelity)
    lines: tuple[str, ...] = ("screening: none",)
    excluded: tuple[dict[str, Any], ...] = ()

    @property
    def agents(self) -> tuple[str, ...] | None:
        """The Agents to plan; None plans every Agent (ungated)."""
        return self.eligibility.agents_ok if self.eligibility is not None else None

    def key(self) -> tuple:
        runs = tuple(sorted(self.eligibility.deciding.items())) if self.eligibility else ()
        return (self.gated, self.agents, self.excluded, self.stamps, runs)


def _unscreened(test: str) -> str:
    return f"unscreened_pilot: Test {test} runs without screening"


def _runs_text(runs: tuple[str, ...]) -> str:
    return "+".join(runs) if runs else "none"


def _lines(e: Eligibility) -> tuple[str, ...]:
    parts = []
    for reason, n in e.counts().items():
        details = e.details(reason)
        shown = ", ".join(f"{m}: {', '.join(names)}" for m, names in details.items())
        parts.append(f"{reason} {n}" + (f" ({shown})" if shown else ""))
    runs = e.runs
    return (
        f"screening: fidelity {_runs_text(runs['fidelity'])}, "
        f"perception {_runs_text(runs['perception'])}",
        f"eligible agents: {len(e.agents_ok)} of {e.total}",
        f"excluded: {', '.join(parts) if parts else 'none'}",
    )


def _fidelity_stamp(study: Path, cfg: StudyConfig) -> str:
    return fidelity_hash(
        config_load.load_fidelity_instruments(study, cfg, warn_draft=False),
        config_load.card_wording_sha256(),
    )


def gate_test(
    study: Path, ctx: _Context, test_cfg: TestConfig, personas: list[Persona]
) -> Gate:
    """Judge every Agent of the Test (see the module docstring); raise its refusal."""
    if ctx.kind not in ("main", "pilot"):  # screening Tests never reach here (3.2)
        return Gate(gated=False, lines=())

    def read(conn: Any) -> tuple[list[str], list[dict[str, Any]]]:
        return complete_run_ids(conn), complete_results(conn)

    run_ids, results = read_only(study, read) or ([], [])
    if ctx.kind == "pilot" and not run_ids:
        return Gate(gated=False, warn=True)
    cfg = ctx.cfg
    model_ids = test_cfg.model_ids(cfg)
    settings = {m.id: settings_hash(m) for m in cfg.models}
    instruments = {n: instrument_hash(ctx.instruments[n]) for n in ctx.instrument_order}
    fidelity = _fidelity_stamp(study, cfg)
    agents = [(p.id, m) for p in personas for m in model_ids]
    decided = eligible(
        agents, ctx.instrument_order, results, Stamps(settings, instruments, fidelity),
        self_report={n for n in ctx.instrument_order if ctx.instruments[n].self_report},
    )
    refusal = decided.refusal()
    if refusal is not None:
        if ctx.kind == "pilot":
            refusal = ConsortiumError(
                refusal.code,
                f"{refusal.message} (pilots are gated once a screening run exists: "
                f"{', '.join(run_ids)})",
                path=refusal.path,
            )
        raise refusal
    ok = set(decided.agents_ok)
    planned_models = [m for m in model_ids if any(agent_id(p.id, m) in ok for p in personas)]
    stamps = []
    for m in planned_models:
        for n in ctx.instrument_order:
            stamps.append({"model_id": m, "instrument": n, "settings_hash": settings[m],
                           "instrument_hash": instruments[n],
                           "runs": decided.deciding.get((m, n), ())})
        stamps.append({"model_id": m, "instrument": "fidelity", "settings_hash": settings[m],
                       "instrument_hash": fidelity,
                       "runs": decided.deciding.get((m, "fidelity"), ())})
    return Gate(
        gated=True,
        eligibility=decided,
        stamps=tuple(stamps),
        lines=_lines(decided),
        excluded=tuple(
            {"agent_id": e.agent_id, "persona_id": e.persona_id, "model_id": e.model_id,
             "instrument": e.instrument, "reason": e.reason}
            for e in decided.excluded
        ),
    )


def _insert(plan: Plan, gate: Gate, conn: Any) -> int:
    """The Run's ``insert_plan`` transaction: the Trials, plus a gated Test's decision."""
    if gate.gated:
        return insert_gated_plan(conn, plan.test, plan.trials, gate.excluded, gate.stamps)
    return insert_plan(conn, plan.test, plan.trials)


def _resume_check(study: Path, ctx: _Context, test: str, conn: Any) -> None:
    """A gated Test resumes only while the Models and Instruments of the Trials it will
    send (non-terminal, not settled from the board) still match the stamps recorded at
    open, fidelity included (``screening_stale``)."""
    stamps = test_stamps(conn, test)
    if not stamps:
        return  # ungated (an unscreened pilot): nothing was frozen
    known = {m.id: m for m in ctx.cfg.models}
    gone: dict[str, None] = {}
    changed_models: dict[str, None] = {}
    changed_instruments: dict[str, None] = {}
    fidelity: str | None = None
    sent = [r for r in load_resumable(conn, test, ctx.caps) if r["settle"] is None]
    for model, name in dict.fromkeys((r["model_id"], r["instrument"]) for r in sent):
        if model not in known:
            gone[f"Model {model}"] = None
            continue
        if name not in ctx.instruments:
            gone[f"Instrument {name}"] = None
            continue
        current = settings_hash(known[model])
        recorded = stamps.get((model, name))
        if recorded is None or recorded[0] != current:
            changed_models[model] = None
        if recorded is None or recorded[1] != instrument_hash(ctx.instruments[name]):
            changed_instruments[name] = None
        recorded_fidelity = stamps.get((model, "fidelity"))
        if recorded_fidelity is not None:
            if fidelity is None:
                fidelity = _fidelity_stamp(study, ctx.cfg)
            if recorded_fidelity[0] != current:
                changed_models[model] = None
            if recorded_fidelity[1] != fidelity:
                changed_instruments["the fidelity Instruments or Persona card wording"] = None
    parts = [f"{what} (no longer in the Study)" for what in gone]
    parts += [f"Model {m} (settings changed)" for m in changed_models]
    parts += [f"Instrument {n} (definition changed)" for n in changed_instruments]
    if parts:
        raise ConsortiumError(
            "screening_stale",
            f"Test {test!r} was opened under other screening stamps: {', '.join(parts)}; "
            "a resume never re-gates, so restore them to continue",
        )


def _frozen_lines(study: Path, test: str) -> tuple[str, ...]:
    """A resume's screening line: the decision frozen at open (``none``: ungated)."""
    found = read_only(
        study, lambda conn: (test_stamps(conn, test), test_exclusions(conn, test))
    )
    stamps, excluded = found or ({}, [])
    if not stamps:
        return ("screening: none",)
    n = len(excluded)
    return (f"screening: frozen at open ({n} Agent{'' if n == 1 else 's'} excluded)",)


def _prepare(
    study: Path, test: str, *, plan: bool, say: Callable[[str], None] | None = None
) -> tuple[Prepared, Gate | None]:
    """The ``Prepared`` of a registered Test, with the eligibility gate (fresh Run, dry
    run) or the stamp check (resume, ``plan`` False; the Gate is then None). ``say`` gets
    the ``unscreened_pilot`` warning just before a fresh Run's confirmation."""
    ctx, test_cfg, personas = engine.load_test_context(study, test, READER)
    gate = gate_test(study, ctx, test_cfg, personas) if plan else None
    planned = (
        plan_test(test_cfg, ctx.cfg, personas, ctx.instruments, agents=gate.agents)
        if gate is not None else None
    )

    def reload(changed: ConsortiumError) -> Prepared:
        engine.check_file(study, ctx, changed)
        try:
            now, gate_now = _prepare(study, test, plan=plan)
        except ConsortiumError as err:
            if gate is not None and err.code in GATE_REFUSALS:
                raise changed from err  # the gate now refuses: changed since confirmation
            raise
        if gate is not None and (gate_now is None or gate_now.key() != gate.key()):
            raise changed  # a different eligibility decision since the confirmation
        return now

    hooks: dict[str, Any] = {}
    if planned is not None and gate is not None:
        hooks["insert"] = functools.partial(_insert, planned, gate)
        hooks["screening"] = gate.lines
        if gate.warn and say is not None:
            hooks["before_confirm"] = functools.partial(say, _unscreened(test))
    else:
        hooks["resume_check"] = functools.partial(_resume_check, study, ctx, test)
        hooks["screening"] = _frozen_lines(study, test)
    prepared = Prepared(
        test=test, ctx=ctx, plan=planned, reload=reload,
        guard=functools.partial(engine.refuse_if_open, study, test), **hooks,
    )
    return prepared, gate
