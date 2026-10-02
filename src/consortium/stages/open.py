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
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from consortium.board.lease import acquire_lease
from consortium.board.writer import Spy, migrate
from consortium.config import load as config_load
from consortium.config.models import StudyConfig
from consortium.core.cost import usd
from consortium.core.errors import ConsortiumError
from consortium.core.plan import Plan
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

    With ``resume`` the Test is never re-planned: ``test_not_open`` (no Trials)
    replaces ``test_already_open``, ``reissue_mismatch`` is raised when an archived
    request no longer re-renders byte-identically, and confirmation counts only
    the non-terminal Trials.
    """
    new_ceiling = parse_ceiling(ceiling)
    study = Path(study_dir)
    if dry_run:
        return engine.dry_run(study, engine.prepare_test(study, test, READER, plan=True),
                              new_ceiling)

    def load() -> Prepared:
        return engine.prepare_test(study, test, READER, plan=not resume)

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
    prepared = engine.prepare_test(Path(study_dir), test, READER, plan=True)
    assert prepared.plan is not None
    return prepared.plan, prepared.requests(), prepared.ctx.instrument_order
