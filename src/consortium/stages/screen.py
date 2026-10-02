"""Use case: screen the Panel (story 3.1: Persona fidelity, ``screen personas``).

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from consortium.board.db import connect, read_only
from consortium.board.lease import acquire_lease
from consortium.board.screening import (
    abandon_run,
    new_run,
    next_run_id,
    open_run,
    run_open_error,
)
from consortium.board.screening import complete_run as store_results
from consortium.board.tests import get_test
from consortium.board.trials import TERMINAL_STATES, chosen_answer, load_trials
from consortium.board.writer import Spy, migrate
from consortium.config import load as config_load
from consortium.config.models import CardWording, InstrumentDef, StudyConfig
from consortium.core.errors import ConsortiumError
from consortium.core.fidelity import ScoredItem, fidelity_keys, score_fidelity
from consortium.core.hashes import fidelity_hash, settings_hash
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

FIDELITY = "fidelity"
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
        if self.results:
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
