"""Use case: screen the Panel (story 3.1: Persona fidelity, ``screen personas``;
story 3.2: Model perception, ``screen models TEST``, see ``screen_models``).

``screen_personas`` creates a screening run ``s<n>`` (n = 1 + the highest run
number in use, chosen before confirmation and checked again under the lease).
Every Agent (every Persona x every Model) answers the clip-less self-report
fidelity Instruments (``config.load.load_fidelity_instruments``: BFI-10, plus the
selected NARS Instrument when the frame has NARS bands), ``screening.fidelity_repeats``
Sessions each. The run goes through the same runner as ``open`` (``engine.run``):
estimate, confirmation, ceiling, lease, under-lease recheck, Archive, retries,
pause and resume. One writer transaction (``board.screening.new_run``) stores the
run, its ``tests`` row (kind ``screening``, not openable) and its Trials.

The run records its stamps when it is created: each Model's ``settings_hash`` and the
fidelity ``instrument_hash`` (``core.hashes.fidelity_hash``). A resume refuses
``test_changed`` when a current stamp differs. Once every Trial of the run is
terminal, one writer operation scores each Agent (``core.fidelity.score_fidelity``),
stores one row per Agent stamped with the recorded values, sets the run ``complete``
and marks earlier runs whose every result key is now covered ``superseded_by`` it.
``abandon_personas`` marks a stuck open run ``abandoned`` (no results). A run
that paused at the ceiling or stopped stays ``open`` with no results; ``--resume``
finishes it and scores. Nothing is deleted. No screening code reads the Blinding key.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from consortium.board.db import connect, read_only
from consortium.board.lease import acquire_lease
from consortium.board.screening import (
    abandon_run,
    current_results,
    new_run,
    next_run_id,
    open_run,
    run_number,
    run_open_error,
)
from consortium.board.screening import complete_run as store_results
from consortium.board.tests import get_test, tests_of_kind
from consortium.board.trials import TERMINAL_STATES, chosen_answer, load_trials, trial_from_row
from consortium.board.writer import Spy, migrate
from consortium.config import load as config_load
from consortium.config.models import CardWording, InstrumentDef, StudyConfig
from consortium.core.errors import ConsortiumError
from consortium.core.fidelity import ScoredItem, fidelity_keys, score_fidelity
from consortium.core.hashes import fidelity_hash, instrument_hash, settings_hash
from consortium.core.perception import (
    NEUTRAL_CARD,
    NEUTRAL_PERSONA_ID,
    NeutralPersona,
    PerceptionTrial,
    check_shapes,
    coverage,
    covered_instruments,
    pair_checks_by_instrument,
    passes,
    score_perception,
)
from consortium.core.personas import TRAITS, Persona
from consortium.core.plan import plan_test
from consortium.core.render import canonical_json
from consortium.engine import run as engine
from consortium.engine.run import (
    Announce,
    ConfigReader,
    Confirm,
    OpenSummary,
    Prepared,
    TestContext,
    parse_ceiling,
)
from consortium.raters.base import Rater
from consortium.raters.fake import FidelityCues

log = logging.getLogger(__name__)

FIDELITY = "fidelity"
PERCEPTION = "perception"
SCREENING_KIND = "screening"
FIDELITY_PATH = "<built-in>/screening/personas"
NARS = "nars"

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


@dataclass(frozen=True)
class ScreenSummary:
    """A screening run's runner summary plus, once complete, its stored results."""

    run_id: str
    kind: str
    run: OpenSummary
    results: list[dict[str, Any]] | None  # None: the run is not complete yet
    superseded: list[str]
    gaps: dict[str, list[str]] = field(default_factory=dict)  # perception: coverage gaps

    @property
    def complete(self) -> bool:
        return self.results is not None

    def by_model(self) -> dict[str, dict[str, int]]:
        """``{model_id: {"pass": n, "fail": n, "insufficient": n}}`` in result order;
        ``insufficient`` counts Agents with a check short of valid answers."""
        out: dict[str, dict[str, int]] = {}
        for row in self.results or []:
            counts = out.setdefault(row["model_id"], {"pass": 0, "fail": 0, "insufficient": 0})
            counts[row["outcome"]] += 1
            counts["insufficient"] += bool(json.loads(row["detail"]).get("insufficient_data"))
        return out

    def result_lines(self) -> list[str]:
        """The run status and the pass/fail counts per Model."""
        if self.run.dry_run:
            status = "dry run"
        else:
            status = "complete" if self.complete else "open (no results yet)"
        lines = [f"screening run: {self.run_id} ({self.kind}) {status}"]
        if self.kind == PERCEPTION:
            for row in self.results or []:
                detail = json.loads(row["detail"])
                lines.append(
                    f"perception {row['model_id']} {row['instrument']}: {row['outcome']} "
                    f"{row['score']:.4g} ({detail['passed']}/{detail['units']}), "
                    f"pair checks {row['pair_checks']}, threshold {row['threshold']:g}"
                )
            lines.extend(gap_line(name, tests) for name, tests in self.gaps.items())
        elif self.results:
            threshold = self.results[0]["threshold"]
            for model_id, counts in self.by_model().items():
                lines.append(
                    f"{self.kind} {model_id}: pass {counts['pass']}, fail {counts['fail']} "
                    f"(insufficient data {counts['insufficient']}), threshold {threshold:g}"
                )
        if self.superseded:
            lines.append(f"superseded: {', '.join(self.superseded)}")
        return lines


@dataclass(frozen=True)
class _FidelityTest:
    """The ``core.plan`` view of a fidelity run: every Model, no Clips."""

    test: str
    kind: str
    instruments: list[str]
    clips: list[str]
    repeats: int

    def model_ids(self, study: StudyConfig) -> list[str]:
        return [m.id for m in study.models]

    def effective_session(self, study: StudyConfig) -> Any:
        return SimpleNamespace(repeats=self.repeats)


def fidelity_cues(wording: CardWording, instruments: Sequence[InstrumentDef]) -> FidelityCues:
    """Card sentence -> ``(construct, pole)`` and keyed Item -> ``(construct, reversed)``."""
    sentences: dict[str, tuple[str, str]] = {}
    for trait in TRAITS:
        pair = getattr(wording.traits, trait)
        sentences[pair.high] = (trait, "high")
        sentences[pair.low] = (trait, "low")
    for band, sentence in wording.nars.items():
        sentences[sentence] = (NARS, band)
    keys = {item_id: (k.construct, k.reversed) for item_id, k in
            fidelity_keys(instruments).items()}
    return FidelityCues(sentences=sentences, keys=keys)


def _context(
    study: Path, cfg: StudyConfig, personas: list[Persona], instruments: list[InstrumentDef]
) -> TestContext:
    names = [i.name for i in instruments]
    return TestContext(
        cfg=cfg,
        kind=SCREENING_KIND,
        instrument_order=names,
        rel=FIDELITY_PATH,
        test_sha256=fidelity_hash(instruments, config_load.card_wording_sha256()),
        cards={p.id: READER.read_card(study, p.id) for p in personas},
        instruments={i.name: i for i in instruments},
        practice={name: [] for name in names},
        clip_sha256={},
        prices=READER.load_prices(study),
        clip_seconds={},
        max_retries=cfg.session.max_retries,
    )


@dataclass(frozen=True)
class _Loaded:
    prepared: Prepared
    cues: FidelityCues
    settings: Mapping[str, str]  # the settings stamps the run records (or recorded)


def _finish(
    conn: Any,
    *,
    run_id: str,
    cfg: StudyConfig,
    personas: Mapping[str, Persona],
    keys: Mapping[str, ScoredItem],
    constructs: Mapping[str, list[str]],
    settings: Mapping[str, str],
    stamp: str,
    draft: bool,
) -> dict[str, Any] | None:
    """Score and store the run once every Trial is terminal (writer task); else None.

    Results carry the stamps recorded when the run was created (``settings``, ``stamp``).
    """
    rows = load_trials(conn, run_id)
    if not rows or any(r["state"] not in TERMINAL_STATES for r in rows):
        return None
    agents: dict[str, tuple[str, str]] = {}
    answers: dict[str, list[dict]] = {}
    expected: dict[str, dict[str, int]] = {}
    for r in rows:
        agents.setdefault(r["agent_id"], (r["persona_id"], r["model_id"]))
        answers.setdefault(r["agent_id"], [])
        planned = expected.setdefault(r["agent_id"], {})
        for construct in constructs.get(r["instrument"], []):
            planned[construct] = planned.get(construct, 0) + 1
        if r["state"] == "valid":
            chosen = chosen_answer(conn, r["trial_id"])
            if chosen is not None:
                answers[r["agent_id"]].append(chosen["answers"])
    threshold = cfg.thresholds.persona_fidelity_min
    results = []
    for agent_id in sorted(agents, key=_agent_key):
        persona_id, model_id = agents[agent_id]
        if model_id not in settings:
            raise ConsortiumError(
                "board_unreadable",
                f"screening run {run_id} has no settings stamp for model {model_id!r}",
            )
        persona = personas.get(persona_id)
        if persona is None:
            raise ConsortiumError(
                "panel_mismatch", f"Persona {persona_id} of run {run_id} is not in the Panel"
            )
        score = score_fidelity(answers[agent_id], persona, keys, expected[agent_id])
        detail = {
            "checks": score.per_trait, "matched": score.matched, "total": score.total,
            "insufficient_data": score.insufficient_data, "draft": draft,
        }
        results.append({
            "model_id": model_id,
            "agent_id": agent_id,
            "instrument": FIDELITY,
            "outcome": "pass" if score.ratio >= threshold else "fail",
            "score": score.ratio,
            "threshold": threshold,
            "settings_hash": settings[model_id],
            "instrument_hash": stamp,
            "detail": canonical_json(detail).decode("utf-8"),
        })
    superseded = store_results(conn, run_id, results)
    return {"results": [dict(r, run_id=run_id) for r in results], "superseded": superseded}


def _agent_key(agent_id: str) -> tuple[int, int]:
    persona, _, model = agent_id.partition("-")
    return int(persona.lstrip("p") or 0), int(model.lstrip("m") or 0)


def _state(study: Path) -> tuple[dict[str, Any] | None, str]:
    """``(open fidelity run, next run ID)``; ``board_unreadable`` when the open run has
    no ``tests`` row."""
    def read(conn: Any) -> tuple:
        running = open_run(conn, FIDELITY)
        has_test = running is not None and get_test(conn, running["run_id"]) is not None
        return running, next_run_id(conn), has_test

    found = read_only(study, read)
    if found is None:
        return None, "s1"
    running, next_id, has_test = found
    if running is not None and not has_test:
        raise ConsortiumError(
            "board_unreadable",
            f"screening run {running['run_id']} has no tests row in board.db",
        )
    return running, next_id


def _changed(message: str) -> ConsortiumError:
    return ConsortiumError("test_changed", message, path=FIDELITY_PATH)


def _load(study: Path, *, resume: bool, run_id: str | None = None) -> _Loaded:
    """The ``Prepared`` of a fresh fidelity run (or, ``resume``, of the open one)."""
    cfg = READER.load_study(study)
    personas = READER.load_personas(study)  # panel_missing before anything is planned
    if bool(cfg.personas.nars_bands) != any(p.nars is not None for p in personas):
        raise ConsortiumError(
            "panel_frame_mismatch",
            "study.yaml personas.nars_bands and the Panel disagree on NARS bands "
            f"(nars_bands {list(cfg.personas.nars_bands)}); regenerate the Panel in a new "
            "Study or restore nars_bands",
            path="study.yaml",
        )
    running, next_id = _state(study)
    if resume:
        if running is None:
            raise ConsortiumError(
                "screening_not_open",
                "no fidelity screening run is open; start one with consortium screen personas",
            )
        run_id = running["run_id"]
    else:
        if running is not None:
            raise run_open_error(running["run_id"])
        run_id = run_id or next_id
    instruments = READER.load_fidelity_instruments(study, cfg)
    drafts = [i.name for i in instruments if i.draft]
    real = [m.id for m in cfg.models if m.provider != "fake"]
    if drafts and real:
        raise ConsortiumError(
            "draft_instrument_not_allowed",
            f"{', '.join(drafts)} is a draft (placeholder) Instrument; Model(s) "
            f"{', '.join(real)} may not be screened with it (set screening.nars_instrument "
            "to a filled-in NARS Instrument)",
            path="study.yaml",
        )
    ctx = _context(study, cfg, personas, instruments)
    current = {m.id: settings_hash(m) for m in cfg.models}
    if resume:
        assert running is not None
        if running["instrument_hash"] != ctx.test_sha256:
            raise _changed(
                f"the fidelity Instruments or card wording changed since screening run "
                f"{run_id} started (screening.nars_instrument or an Instrument file); "
                "use --abandon to drop the run"
            )
        recorded: dict[str, str] = running["settings_hashes"]
        moved = sorted(m for m, h in recorded.items() if current.get(m) != h)
        if moved:
            raise _changed(
                f"Model settings changed since screening run {run_id} started: "
                f"{', '.join(moved)}; use --abandon to drop the run"
            )
        settings = recorded
    else:
        settings = current
    keys = fidelity_keys(instruments)
    constructs = {
        i.name: [keys[item.id].construct for item in i.items if item.id in keys]
        for i in instruments
    }
    cues = fidelity_cues(READER.load_card_wording(), instruments)
    finish = functools.partial(
        _finish, run_id=run_id, cfg=cfg, personas={p.id: p for p in personas}, keys=keys,
        constructs=constructs, settings=settings, stamp=ctx.test_sha256, draft=bool(drafts),
    )
    fixed_id = run_id

    def reload(changed: ConsortiumError) -> Prepared:
        now = _load(study, resume=resume, run_id=fixed_id)
        if now.settings != settings:  # a Model's settings changed since the confirmation
            raise changed
        return now.prepared

    if resume:
        prepared = Prepared(test=run_id, ctx=ctx, plan=None, reload=reload, finish=finish)
        return _Loaded(prepared, cues, settings)
    view = _FidelityTest(run_id, SCREENING_KIND, ctx.instrument_order, [],
                         cfg.screening.fidelity_repeats)
    plan = plan_test(view, cfg, personas, ctx.instruments)
    prepared = Prepared(
        test=run_id,
        ctx=ctx,
        plan=plan,
        reload=reload,
        insert=lambda conn: new_run(conn, fixed_id, FIDELITY, None, FIDELITY_PATH,
                                    ctx.test_sha256, plan.trials, settings),
        guard=functools.partial(_guard, study, run_id),
        finish=finish,
    )
    return _Loaded(prepared, cues, settings)


def _guard(study: Path, run_id: str) -> None:
    """``screening_run_open`` while a fidelity run is open; ``test_changed`` when
    ``run_id`` is no longer the next run ID."""
    running, next_id = _state(study)
    if running is not None:
        raise run_open_error(running["run_id"])
    if next_id != run_id:
        raise _changed(f"screening run {run_id} was taken since the confirmation")


def _raters(
    cfg: StudyConfig, model_ids: list[str], study: Path, *, cues: FidelityCues
) -> dict[str, Rater]:
    return engine.raters_for(cfg, model_ids, study, cues)


def abandon_personas(study_dir: Path | str) -> str:
    """Mark the open fidelity run ``abandoned`` under the lease and return its ID.

    It writes no results and keeps its Trials and Archive lines; a fresh run is then
    allowed. ``screening_not_open`` when no fidelity run is open; ``study_busy`` when a
    dispatcher holds the lease.
    """
    study = Path(study_dir)
    with acquire_lease(study):
        try:
            running, _ = _state(study)
        except ConsortiumError as err:
            if err.code != "board_version_mismatch":
                raise
            migrate(study)
            running, _ = _state(study)
        if running is None:
            raise ConsortiumError(
                "screening_not_open", "no fidelity screening run is open; nothing to abandon"
            )
        conn = connect(study)
        try:
            abandon_run(conn, running["run_id"])
        finally:
            conn.close()
    return running["run_id"]


def screen_personas(
    study_dir: Path | str,
    *,
    yes: bool = False,
    ceiling: str | None = None,
    resume: bool = False,
    dry_run: bool = False,
    confirm: Confirm | None = None,
    announce: Announce | None = None,
    writer_spy: Spy | None = None,
) -> ScreenSummary:
    """Run (or, ``resume``, continue) a Persona-fidelity screening run.

    Refuses, before anything is written: ``panel_missing`` / ``panel_invalid`` (no
    Panel), ``panel_frame_mismatch`` (``nars_bands`` and the Panel disagree),
    ``screening_run_open`` (a fidelity run is not complete; use ``resume`` or
    ``abandon_personas``), ``screening_not_open`` (``resume`` with no open run),
    ``config_invalid`` / ``unknown_instrument`` (a bad ``screening.nars_instrument``),
    ``draft_instrument_not_allowed`` (a draft fidelity Instrument with a non-Fake
    Model), ``test_changed`` (on resume: the fidelity stamp or a Model's settings stamp
    differs from the run's), and every refusal of ``open``. A run that pauses at the
    ceiling returns with ``run.paused == "ceiling"`` and no results. ``dry_run`` plans
    and renders the next run and writes nothing.
    """
    new_ceiling = parse_ceiling(ceiling)
    study = Path(study_dir)
    if dry_run:
        loaded = _load(study, resume=False)
        summary = engine.dry_run(study, loaded.prepared, new_ceiling)
        return ScreenSummary(loaded.prepared.test, FIDELITY, summary, None, [])
    try:
        loaded = _load(study, resume=resume)
    except ConsortiumError as err:
        if err.code != "board_version_mismatch":
            raise
        with acquire_lease(study):  # a run writes board.db, so it migrates an older layout
            migrate(study)
        loaded = _load(study, resume=resume)
    step = engine.resume if resume else engine.run
    summary = step(
        study, loaded.prepared, new_ceiling, yes=yes, confirm=confirm, announce=announce,
        writer_spy=writer_spy, raters=functools.partial(_raters, cues=loaded.cues),
    )
    finished = summary.finished
    return ScreenSummary(
        run_id=loaded.prepared.test,
        kind=FIDELITY,
        run=summary,
        results=finished["results"] if finished else None,
        superseded=finished["superseded"] if finished else [],
    )


# --------------------------------------------------------------------------- perception


Warn = Callable[[str], None]


def gap_line(instrument: str, tests: Sequence[str]) -> str:
    """``coverage_gap: <instrument> (tests: a, b)``."""
    return f"coverage_gap: {instrument} (tests: {', '.join(tests)})"


@dataclass(frozen=True)
class _PerceptionTest:
    """The ``core.plan`` view of a perception run: the run name, the screening Test's
    Instruments, Clips, Models and effective session."""

    test: str
    kind: str
    instruments: list[str]
    clips: list[str]
    models: list[str]
    session: Any

    def model_ids(self, study: StudyConfig) -> list[str]:
        return list(self.models)

    def effective_session(self, study: StudyConfig) -> Any:
        return self.session


@dataclass(frozen=True)
class _PerceptionLoaded:
    prepared: Prepared
    settings: Mapping[str, str]
    own_covered: frozenset[str]  # this Test's pair-checked Instruments
    stamp: str  # the run's instrument stamp (the Test's Instruments)


def _registration(study: Path, test: str) -> dict[str, Any]:
    """The registered screening Test row; ``unknown_test`` / ``not_a_screening_test`` (the
    one place this is enforced: a pilot or main Test, or a screening run ``s<n>``)."""
    row = read_only(study, lambda conn: get_test(conn, test))
    if row is None:
        raise ConsortiumError("unknown_test", f"{test!r} is not a registered Test")
    if row["kind"] != SCREENING_KIND or run_number(test):
        what = "a screening run" if run_number(test) else f"a kind: {row['kind']} Test"
        raise ConsortiumError(
            "not_a_screening_test",
            f"{test!r} is {what}; screen models runs a registered kind: screening Test",
        )
    return row


def _perception_state(study: Path, test: str) -> tuple[dict[str, Any] | None, str]:
    """``(open perception run of ``test``, next run ID)``; ``board_unreadable`` when the
    open run has no ``tests`` row."""
    def read(conn: Any) -> tuple:
        running = open_run(conn, PERCEPTION, test)
        has_test = running is not None and get_test(conn, running["run_id"]) is not None
        return running, next_run_id(conn), has_test

    found = read_only(study, read)
    if found is None:
        return None, "s1"
    running, next_id, has_test = found
    if running is not None and not has_test:
        raise ConsortiumError(
            "board_unreadable",
            f"screening run {running['run_id']} has no tests row in board.db",
        )
    return running, next_id


def _finish_perception(
    conn: Any,
    *,
    run_id: str,
    models: Sequence[str],
    checks: Sequence[Any],
    instruments: Mapping[str, InstrumentDef],
    pair_counts: Mapping[str, int],
    settings: Mapping[str, str],
    threshold: float,
) -> dict[str, Any] | None:
    """Score and store the run once every Trial is terminal (writer task); else None."""
    rows = load_trials(conn, run_id)
    if not rows or any(r["state"] not in TERMINAL_STATES for r in rows):
        return None
    trials = []
    for r in rows:
        trial = trial_from_row(r)
        chosen = chosen_answer(conn, r["trial_id"]) if r["state"] == "valid" else None
        trials.append(PerceptionTrial(
            model_id=trial.model_id, repeat=trial.repeat, instrument=trial.instrument,
            clip_ids=tuple(trial.clip_ids), state=r["state"],
            answers=chosen["answers"] if chosen is not None else None,
        ))
    scores = score_perception(trials, checks, instruments)
    results = []
    for model_id in models:
        if model_id not in settings:
            raise ConsortiumError(
                "board_unreadable",
                f"screening run {run_id} has no settings stamp for model {model_id!r}",
            )
        for name, pairs in pair_counts.items():
            passed, units, ratio = scores.get((model_id, name), (0, 0, 0.0))
            detail = {
                "passed": passed, "units": units,
                "checks": sum(c.instrument == name for c in checks),
                "draft": instruments[name].draft,
            }
            results.append({
                "model_id": model_id,
                "agent_id": None,
                "instrument": name,
                "outcome": "pass" if passes(units, ratio, threshold) else "fail",
                "score": ratio,
                "threshold": threshold,
                "settings_hash": settings[model_id],
                "instrument_hash": instrument_hash(instruments[name]),
                "detail": canonical_json(detail).decode("utf-8"),
                "pair_checks": pairs,
            })
    superseded = store_results(conn, run_id, results)
    return {"results": [dict(r, run_id=run_id) for r in results], "superseded": superseded}


def _load_models(
    study: Path, test: str, *, resume: bool, run_id: str | None = None
) -> _PerceptionLoaded:
    """The ``Prepared`` of a fresh perception run of ``test`` (or, ``resume``, of its open
    one)."""
    cfg = READER.load_study(study)
    registration = _registration(study, test)
    running, next_id = _perception_state(study, test)
    if resume:
        if running is None:
            raise ConsortiumError(
                "screening_not_open",
                f"no perception screening run of {test!r} is open; start one with "
                f"consortium screen models {test}",
            )
        run_id = running["run_id"]
    else:
        if running is not None:
            raise run_open_error(running["run_id"])
        run_id = run_id or next_id
    ctx, test_cfg, _ = engine.load_test_context(
        study, test, READER, run=run_id, cards={NEUTRAL_PERSONA_ID: NEUTRAL_CARD}
    )
    if not test_cfg.checks:
        raise ConsortiumError(
            "no_screening_checks",
            f"screening Test {test!r} declares no checks; add checks to a new screening Test",
            path=ctx.rel,
        )
    instruments = ctx.instruments
    models = test_cfg.model_ids(cfg)
    current = {m: settings_hash(cfg.model_by_id(m)) for m in models}
    stamp = instrument_hash(list(instruments.values()))

    def changed(message: str) -> ConsortiumError:
        return ConsortiumError("test_changed", message, path=ctx.rel)

    if resume:
        assert running is not None
        if running["instrument_hash"] != stamp:
            raise changed(
                f"the Instruments of {test!r} (or the prompt format) changed since screening "
                f"run {run_id} started"
            )
        recorded: dict[str, str] = running["settings_hashes"]
        moved = sorted(m for m in set(recorded) | set(current)
                       if current.get(m) != recorded.get(m))
        if moved:
            raise changed(
                f"Model settings changed since screening run {run_id} started: "
                f"{', '.join(moved)}"
            )
        settings: Mapping[str, str] = recorded
    else:
        settings = current
    pair_counts = pair_checks_by_instrument(test_cfg.checks, instruments)
    ordered = {name: pair_counts[name] for name in test_cfg.instruments if name in pair_counts}
    finish = functools.partial(
        _finish_perception, run_id=run_id, models=models, checks=list(test_cfg.checks),
        instruments=instruments, pair_counts=ordered, settings=settings,
        threshold=cfg.thresholds.perception_min,
    )
    fixed_id = run_id

    def reload(err: ConsortiumError) -> Prepared:
        engine.check_file(study, ctx, err)  # the screening Test file itself
        now = _load_models(study, test, resume=resume, run_id=fixed_id)
        # A Model's settings or an Instrument of the Test changed since the confirmation.
        if now.settings != settings or now.stamp != stamp:
            raise err
        return now.prepared

    def stamped(conn: Any) -> None:
        """Every Model of the run's Trials has a recorded settings stamp, so scoring
        cannot fail once every Trial is terminal (checked before a resume dispatches)."""
        found = {t["model_id"] for t in load_trials(conn, fixed_id)}
        missing = sorted(found - set(settings))
        if missing:
            raise ConsortiumError(
                "board_unreadable",
                f"screening run {fixed_id} has no settings stamp for model(s) "
                f"{', '.join(missing)}; drop it with --abandon",
            )

    own = frozenset(name for name, n in ordered.items() if n > 0)
    if resume:
        prepared = Prepared(test=run_id, ctx=ctx, plan=None, reload=reload, finish=finish,
                            resume_check=stamped)
        return _PerceptionLoaded(prepared, settings, own, stamp)
    view = _PerceptionTest(
        run_id, SCREENING_KIND, list(test_cfg.instruments), list(test_cfg.clips), models,
        test_cfg.effective_session(cfg),
    )
    plan = plan_test(view, cfg, [NeutralPersona()], instruments,
                     shapes=check_shapes(test_cfg.checks, instruments))
    clips = list(registration["clips"])
    prepared = Prepared(
        test=run_id,
        ctx=ctx,
        plan=plan,
        reload=reload,
        insert=lambda conn: new_run(
            conn, fixed_id, PERCEPTION, test, ctx.rel, ctx.test_sha256, plan.trials, settings,
            clips=clips, instrument_hash=stamp,
        ),
        guard=functools.partial(_models_guard, study, test, run_id),
        finish=finish,
    )
    return _PerceptionLoaded(prepared, settings, own, stamp)


def _models_guard(study: Path, test: str, run_id: str) -> None:
    """``screening_run_open`` while a perception run of ``test`` is open; ``test_changed``
    when ``run_id`` is no longer the next run ID."""
    running, next_id = _perception_state(study, test)
    if running is not None:
        raise run_open_error(running["run_id"])
    if next_id != run_id:
        raise ConsortiumError(
            "test_changed", f"screening run {run_id} was taken since the confirmation"
        )


def coverage_gaps(
    study: Path, own: Iterable[str], warn: Warn | None = None
) -> dict[str, list[str]]:
    """``core.perception.coverage`` for ``screen models``: every Instrument of a registered
    ``main`` or ``pilot`` Test (not ``self_report``) that is neither pair-checked by the
    Test being run (``own``) nor covered by a current, non-stale perception result of any
    screening Test (``core.perception.covered_instruments``). A registered Test whose
    file cannot be read is reported to ``warn`` as ``coverage_unknown: <test>``."""
    cfg = READER.load_study(study)

    def read(conn: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return current_results(conn, PERCEPTION), tests_of_kind(conn, ("main", "pilot"))

    results, registered = read_only(study, read) or ([], [])
    defs = READER.load_instruments(study, cfg)
    covered = covered_instruments(
        results,
        {name: instrument_hash(d) for name, d in defs.items()},
        {m.id: settings_hash(m) for m in cfg.models},
    )
    needed: dict[str, list[str]] = {}
    for row in registered:
        names = config_load.peek_test(study / row["path"]).get("instruments")
        if not isinstance(names, list):
            (warn or log.warning)(f"coverage_unknown: {row['name']}")
            continue
        for name in names:
            if not isinstance(name, str) or (name in defs and defs[name].self_report):
                continue
            needed.setdefault(name, []).append(row["name"])
    return coverage(needed, set(own) | covered)


def abandon_models(study_dir: Path | str, test: str) -> str:
    """Mark the open perception run of the screening Test ``test`` ``abandoned`` under the
    lease and return its ID (no results; its Trials and Archive lines are kept; a fresh
    run of ``test`` is then allowed). ``unknown_test`` / ``not_a_screening_test``;
    ``screening_not_open`` when no perception run of ``test`` is open; ``study_busy``
    when a dispatcher holds the lease."""
    study = Path(study_dir)
    with acquire_lease(study):
        try:
            _registration(study, test)
        except ConsortiumError as err:
            if err.code != "board_version_mismatch":
                raise
            migrate(study)
            _registration(study, test)
        running, _ = _perception_state(study, test)
        if running is None:
            raise ConsortiumError(
                "screening_not_open",
                f"no perception screening run of {test!r} is open; nothing to abandon",
            )
        conn = connect(study)
        try:
            abandon_run(conn, running["run_id"])
        finally:
            conn.close()
    return running["run_id"]


def screen_models(
    study_dir: Path | str,
    test: str,
    *,
    yes: bool = False,
    ceiling: str | None = None,
    resume: bool = False,
    confirm: Confirm | None = None,
    announce: Announce | None = None,
    warn: Warn | None = None,
    writer_spy: Spy | None = None,
) -> ScreenSummary:
    """Run (or, ``resume``, continue) a perception screening run of the screening Test
    ``test`` (story 3.2).

    Only the Trials the Test's ``checks`` need (``core.perception.check_shapes``), on every
    Model of the Test, answered by the neutral Persona ``p0``, ``repeats`` Sessions per
    Agent, through the same runner as ``open``. Once every Trial is terminal, one row per
    Model x Instrument with checks is stored (``agent_id`` NULL) and earlier complete
    perception runs of the same Test are superseded. Coverage gaps are passed to ``warn``
    (``coverage_gap: <instrument> (tests: a, b)``; default: logged) before confirmation
    and listed in ``ScreenSummary.gaps``; a gap is never refused.

    Refuses, before anything is written: ``unknown_test``, ``not_a_screening_test``,
    ``no_screening_checks``, ``screening_run_open`` (a perception run of ``test`` is open;
    use ``resume``), ``screening_not_open`` (``resume`` with no open run of ``test``),
    ``test_changed`` (on resume: an Instrument or a Model's settings stamp differs from
    the run's) and every refusal of ``open``.
    """
    new_ceiling = parse_ceiling(ceiling)
    study = Path(study_dir)
    try:
        loaded = _load_models(study, test, resume=resume)
    except ConsortiumError as err:
        if err.code != "board_version_mismatch":
            raise
        with acquire_lease(study):  # a run writes board.db, so it migrates an older layout
            migrate(study)
        loaded = _load_models(study, test, resume=resume)
    gaps = coverage_gaps(study, loaded.own_covered, warn)
    for name, tests in gaps.items():
        (warn or log.warning)(gap_line(name, tests))
    step = engine.resume if resume else engine.run
    summary = step(
        study, loaded.prepared, new_ceiling, yes=yes, confirm=confirm, announce=announce,
        writer_spy=writer_spy, raters=engine.raters_for,
    )
    finished = summary.finished
    return ScreenSummary(
        run_id=loaded.prepared.test,
        kind=PERCEPTION,
        run=summary,
        results=finished["results"] if finished else None,
        superseded=finished["superseded"] if finished else [],
        gaps=gaps,
    )

