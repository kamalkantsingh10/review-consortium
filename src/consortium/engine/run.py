"""The one runner (AD-6): plan checks, cost, confirmation, lease, dispatch and resume.

Moved unchanged in behaviour out of ``stages/open.py`` (story 3.1) so that every
dispatching stage (``open``, ``screen``) runs through the same machinery. A stage
describes what it runs as a ``Prepared``: the run or Test name and its render
context (``TestContext``: kind, Trials' render inputs, estimate, budget, retry
caps), the planned Trials of a fresh Run, and hooks:

- ``insert(conn)``: stores the planned Trials (one transaction, on the writer);
- ``guard()``: refusals checked before confirmation and again under the lease;
- ``reload(changed)``: re-reads everything under the lease (raising ``changed``
  when a registered input was edited), so the confirmed requests, providers,
  estimate and fingerprint can be compared (``test_changed``);
- ``resume_check(conn)``: called under the lease before a resume dispatches
  (no-op by default);
- ``finish(conn)``: called on the writer once dispatch returns (screening scores
  its results there); its result is ``OpenSummary.finished``.

A Run refuses a Test that already has Trials, asks for confirmation (unless
``yes``) showing the Trial count and requests digest, then takes the
``board.lock`` lease, re-checks, announces the summary, stores every planned
Trial as ``planned`` in one transaction and sends them all through
``engine.dispatch``. Every ``board.db`` write and Archive append happens on the
single writer task.

``resume`` continues a stopped Run without re-planning: it re-renders every
stored Trial from its row plus the Study folder, checks that every archived
request re-renders byte-identically, and sends only the non-terminal Trials
through the same ``engine.dispatch``.

Cost (story 1.9): every open prints the ``core.cost`` estimate of the Trials it
would send (``expected`` once each, ``worst_case`` with every retry) before
anything reaches a Rater. A Run or resume needs a ceiling (``--ceiling``, or
the latest one logged in ``board.db``) unless every Model it sends to is Fake;
a fresh Run refuses ``over_ceiling`` when committed spend plus ``expected``
exceeds it. ``--ceiling`` is logged (``set_ceiling``) only once the Run is
confirmed, under the lease. The engine reserves per attempt and pauses the Run
at the ceiling (``OpenSummary.paused == "ceiling"``); a resume clears the
pause and continues.

Layering: imports core, board, archive, raters, ``engine.dispatch`` and
``config.models`` types only. Every Study-file read goes through the
``ConfigReader`` a stage passes in (built from ``config.load``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import os
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from consortium.archive.jsonl import REQUESTS_FILE, RESPONSES_FILE, read_lines
from consortium.board.clips import list_clips
from consortium.board.db import read_only
from consortium.board.lease import acquire_lease
from consortium.board.ledger import committed_usd, current_ceiling, set_ceiling
from consortium.board.tests import get_test, paused_reason, set_paused
from consortium.board.trials import (
    MODEL_PURPOSE,
    TERMINAL_STATES,
    RetryCaps,
    attempt_seeds,
    count_trials,
    insert_plan,
    load_resumable,
    load_trials,
    settle,
    state_counts,
    trial_from_row,
)
from consortium.board.writer import Spy, start_writer
from consortium.config.models import (
    CardWording,
    InstrumentDef,
    ModelConfig,
    PricesConfig,
    RetryPolicy,
    StudyConfig,
    TestConfig,
)
from consortium.core.cost import PlanEstimate, estimate_plan, usd
from consortium.core.errors import ConsortiumError
from consortium.core.personas import Persona
from consortium.core.plan import Plan, Trial, plan_test
from consortium.core.render import TrialRequest, canonical_json, practice_for, render
from consortium.core.seeds import derive_seed
from consortium.core.test_checks import check_media_limits, check_plan
from consortium.engine.dispatch import Budget, dispatch
from consortium.raters.base import ModelSpec, Rater, close_raters
from consortium.raters.fake import FakeRater, FidelityCues
from consortium.raters.gemini import GeminiRater
from consortium.raters.qwen import QwenRater

# The Study's Clip and Persona folders, as stages.push and config.load name them
# (the engine never imports config.load).
CLIPS_DIR = "clips"
PERSONAS_DIR = "panel/personas"
Confirm = Callable[[str], bool]
Announce = Callable[[list[str]], None]
RatersFor = Callable[[StudyConfig, list[str], Path], dict[str, Rater]]


@dataclass(frozen=True)
class ConfigReader:
    """The ``config.load`` callables the engine needs; a stage builds it and passes it in."""

    load_study: Callable[[Path], StudyConfig]
    load_prices: Callable[[Path], PricesConfig]
    load_test: Callable[..., TestConfig]  # (path, study_dir, *, cfg)
    load_instruments: Callable[[Path, StudyConfig], dict[str, InstrumentDef]]
    load_personas: Callable[[Path], list[Persona]]
    read_card: Callable[[Path, str], str]
    load_card_wording: Callable[..., CardWording]  # (cfg=None)
    load_fidelity_instruments: Callable[[Path, StudyConfig], list[InstrumentDef]]


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
    resumed: dict[str, int] | None = None  # --resume: collect / new attempt / settled / terminal
    estimate: PlanEstimate | None = None  # cost of the Trials this open sends (all for dry run)
    ceiling: Decimal | None = None  # the ceiling this open runs under; None = uncapped
    committed_before: Decimal | None = None  # Study-wide committed spend before this open
    committed: Decimal | None = None  # Study-wide committed spend after a Run
    paused: str | None = None  # "ceiling" when the Run paused (or, dry run, is paused)
    would_refuse: str | None = None  # dry run: the ceiling refusal a Run would raise
    finished: Any = None  # what ``Prepared.finish`` returned (None when there is no hook)

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
            *self.cost_lines(),
            *([f"resume: {fmt(self.resumed)}"] if self.resumed is not None else []),
            *([f"states: {fmt(self.states)}"] if self.states is not None else []),
            *(
                [f"cost: committed {usd(self.committed)} USD, ceiling {_usd(self.ceiling)}"]
                if self.committed is not None else []
            ),
            *([f"paused: {self.paused}"] if self.paused is not None else []),
        ]

    def cost_lines(self) -> list[str]:
        """The estimate and ceiling lines, printed before any confirmation."""
        if self.estimate is None:
            return []
        lines = self.estimate.lines()
        if self.committed_before is not None:
            lines.append(
                f"ceiling: {_usd(self.ceiling)}, committed before: "
                f"{usd(self.committed_before)} USD"
            )
        if self.would_refuse is not None:
            lines.append(f"would refuse: {self.would_refuse}")
        return lines


def _usd(value: Decimal | None) -> str:
    return "none" if value is None else f"{usd(value)} USD"


def _summarize(
    plan: Plan, instrument_order: list[str], dry_run: bool, requests_sha256: str
) -> OpenSummary:
    return _summarize_trials(
        plan.test, plan.kind, list(plan.trials), instrument_order, dry_run, requests_sha256
    )


def _summarize_trials(
    test: str,
    kind: str,
    trials: Sequence[Trial],
    instrument_order: list[str],
    dry_run: bool,
    requests_sha256: str,
) -> OpenSummary:
    by_model: Counter[str] = Counter()
    by_instrument: Counter[str] = Counter()
    by_type: Counter[str] = Counter()
    sessions: dict[str, Counter[str]] = {}
    for trial in trials:
        sessions.setdefault(trial.session_id, Counter())[trial.instrument] += 1
        by_model[trial.model_id] += 1
        by_instrument[trial.instrument] += 1
        by_type["pairwise" if trial.pairwise else "single"] += 1
    shapes = {tuple(per[name] for name in instrument_order) for per in sessions.values()}
    if len(shapes) != 1:
        raise ConsortiumError(
            "board_unreadable",
            f"Test {test!r}: its Sessions differ in shape or it has no Trials",
        )
    (per_session,) = shapes
    models = list(dict.fromkeys(t.model_id for t in trials))
    return OpenSummary(
        test=test,
        kind=kind,
        dry_run=dry_run,
        sessions=len(sessions),
        trials_per_session=dict(zip(instrument_order, per_session, strict=True)),
        trials=len(trials),
        by_model={m: by_model[m] for m in models},
        by_instrument={name: by_instrument[name] for name in instrument_order},
        by_type={k: by_type[k] for k in ("single", "pairwise")},
        requests_sha256=requests_sha256,
    )


def parse_ceiling(ceiling: str | None) -> Decimal | None:
    """``--ceiling`` as a USD ``Decimal`` > 0; ``invalid_ceiling`` otherwise."""
    if ceiling is None:
        return None
    try:
        value = Decimal(ceiling.strip())
    except InvalidOperation as err:
        raise ConsortiumError(
            "invalid_ceiling", f"--ceiling: {ceiling!r} is not a USD amount such as 5.00"
        ) from err
    if not value.is_finite() or value <= 0:
        raise ConsortiumError(
            "invalid_ceiling", f"--ceiling: {ceiling!r} must be a USD amount greater than 0"
        )
    return value


def _spend(study: Path) -> tuple[Decimal | None, Decimal]:
    """``(current ceiling, Study-wide committed spend)`` read from ``board.db``."""
    found = read_only(study, lambda conn: (current_ceiling(conn), committed_usd(conn)))
    return found if found is not None else (None, Decimal(0))


def _resolve_ceiling(study: Path, given: Decimal | None) -> tuple[Decimal | None, Decimal]:
    """The ceiling this open runs under (``given``, else the logged one) and committed spend."""
    current, committed = _spend(study)
    return (given if given is not None else current), committed


def _enforce_ceiling(
    ceiling: Decimal | None,
    committed: Decimal,
    given: Decimal | None,
    uncapped_ok: bool,
    expected: Decimal,
    *,
    fresh: bool,
) -> None:
    """Apply the ceiling rules before anything is written.

    ``over_ceiling`` when a ``given`` ceiling is below committed spend.
    ``ceiling_required`` when no ceiling is given or logged unless
    ``uncapped_ok`` (every Model is Fake and priced 0). A ``fresh`` Run refuses
    ``over_ceiling`` when committed spend plus ``expected`` exceeds the ceiling
    (a resume does not: the engine pauses it before any send it cannot afford).
    """
    if given is not None and committed > given:
        raise ConsortiumError(
            "over_ceiling",
            f"committed {usd(committed)} > ceiling {usd(given)}; nothing was sent",
        )
    if ceiling is None and not uncapped_ok:
        raise ConsortiumError(
            "ceiling_required",
            "no cost ceiling is set; pass --ceiling <usd> (only Runs whose Models are all "
            "Fake and priced 0 may run uncapped)",
        )
    if fresh and ceiling is not None and committed + expected > ceiling:
        spent = f"committed {usd(committed)} + " if committed else ""
        raise ConsortiumError(
            "over_ceiling",
            f"{spent}expected {usd(expected)} > ceiling {usd(ceiling)}; nothing was sent",
        )


# --------------------------------------------------------------------------- context


@dataclass(frozen=True)
class TestContext:
    """Everything ``core.render`` needs from the Study folder for one Test or run."""

    __test__ = False  # not a pytest test class

    cfg: StudyConfig
    kind: str
    instrument_order: list[str]
    rel: str  # the registered Test file (errors name it)
    test_sha256: str  # the fingerprint compared under the lease
    cards: dict[str, str]
    instruments: dict[str, InstrumentDef]
    practice: dict[str, list]
    clip_sha256: dict[str, str]
    prices: PricesConfig
    clip_seconds: dict[str, float]
    max_retries: int  # the Test's effective session.max_retries

    @property
    def caps(self) -> RetryCaps:
        """The retry budgets: ``max_retries`` and ``study.yaml`` ``session.retry``."""
        return RetryCaps(self.max_retries, self.cfg.session.retry.transient_retries)

    def estimate(
        self, trials: Sequence[Trial], requests: Iterator[TrialRequest]
    ) -> PlanEstimate:
        """``core.cost.estimate_plan`` over ``trials`` and their requests (same order)."""
        return estimate_plan(
            trials, requests, self.cfg, self.prices, self.clip_seconds,
            max_retries=self.max_retries,
            transient_retries=self.cfg.session.retry.transient_retries,
        )

    def uncapped_ok(self, model_ids: Sequence[str]) -> bool:
        """True when every Model is Fake and priced 0, so a Run may go without a ceiling."""
        for model_id in model_ids:
            price = self.prices.models[model_id]
            if (
                self.cfg.model_by_id(model_id).provider != "fake"
                or price.input_usd_per_mtok != 0
                or price.output_usd_per_mtok != 0
            ):
                return False
        return True

    def budget(self, test: str, ceiling: Decimal | None) -> Budget:
        return Budget(
            test=test, models={m.id: m for m in self.cfg.models}, prices=self.prices,
            clip_seconds=self.clip_seconds, ceiling=ceiling,
        )

    def render(self, trial: Trial) -> TrialRequest:
        if trial.persona_id not in self.cards:
            raise ConsortiumError(
                "panel_invalid", f"Trial {trial.trial_id}: Persona {trial.persona_id} is not "
                "in panel/personas", path=PERSONAS_DIR,
            )
        if trial.instrument not in self.instruments:
            raise ConsortiumError(
                "unknown_instrument",
                f"Trial {trial.trial_id}: Instrument {trial.instrument!r} is not in the Test",
            )
        return render(
            trial,
            persona_card=self.cards[trial.persona_id],
            instrument=self.instruments[trial.instrument],
            practice=self.practice[trial.instrument],
            clip_sha256=self.clip_sha256,
        )


def _nothing(*_args: Any) -> None:
    return None


@dataclass(frozen=True)
class Prepared:
    """What a dispatching stage runs (see the module docstring)."""

    test: str  # the Test or screening run name (``trials.test``)
    ctx: TestContext
    plan: Plan | None  # the Trials a fresh Run stores and sends; None for a resume context
    reload: Callable[[ConsortiumError], Prepared]
    insert: Callable[[sqlite3.Connection], object] | None = None
    guard: Callable[[], None] = _nothing
    resume_check: Callable[[sqlite3.Connection], None] = _nothing
    finish: Callable[[sqlite3.Connection], object] | None = None

    @property
    def kind(self) -> str:
        return self.ctx.kind

    @property
    def caps(self) -> RetryCaps:
        return self.ctx.caps

    def render(self, trial: Trial) -> TrialRequest:
        return self.ctx.render(trial)

    def estimate(self, trials: Sequence[Trial], requests: Iterator[TrialRequest]) -> PlanEstimate:
        return self.ctx.estimate(trials, requests)

    def budget(self, ceiling: Decimal | None) -> Budget:
        return self.ctx.budget(self.test, ceiling)

    def requests(self) -> Iterator[TrialRequest]:
        """The planned Trials' requests, rendered lazily in plan order."""
        assert self.plan is not None
        return (self.ctx.render(trial) for trial in self.plan.trials)


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


def load_test_context(
    study: Path, test: str, reader: ConfigReader
) -> tuple[TestContext, TestConfig, list[Persona]]:
    """The registered Test's checks and render inputs (see ``stages.open.open_test``)."""
    cfg = reader.load_study(study)
    prices = reader.load_prices(study)
    row, clip_rows = _read_registration(study, test)
    if row["kind"] == "screening" and not row["openable"]:
        raise ConsortiumError(
            "screening_test_not_openable",
            f"{test!r} is a screening run; it is run by consortium screen personas "
            "(--resume to continue it)",
        )
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
    test_cfg = reader.load_test(path, study, cfg=cfg)
    if file_sha256() != row["sha256"]:
        raise fail("test_changed", "the Test file changed while it was being validated")
    if test_cfg.kind == "main":  # registration says otherwise only if the DB was edited
        raise fail("protocol_lock_unavailable", f"Test {test!r} is kind main (Epic 4)")

    # Config may have changed since push test: re-run its pure checks.
    all_instruments = reader.load_instruments(study, cfg)
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

    personas = reader.load_personas(study)
    ctx = TestContext(
        cfg=cfg,
        kind=test_cfg.kind,
        instrument_order=list(test_cfg.instruments),
        rel=rel,
        test_sha256=row["sha256"],
        cards={p.id: reader.read_card(study, p.id) for p in personas},
        instruments=instruments,
        practice={
            name: practice_for(test_cfg.practice, name, practice_per_instrument)
            for name in test_cfg.instruments
        },
        clip_sha256={clip_id: c["sha256"] for clip_id, c in clip_rows.items()},
        prices=prices,
        clip_seconds={clip_id: c["duration_s"] for clip_id, c in clip_rows.items()},
        max_retries=test_cfg.effective_session(cfg).max_retries,
    )
    return ctx, test_cfg, personas


def _check_file(study: Path, ctx: TestContext, changed: ConsortiumError) -> None:
    """``changed`` unless the registered file still has the confirmed SHA-256."""
    try:
        if hashlib.sha256((study / ctx.rel).read_bytes()).hexdigest() != ctx.test_sha256:
            raise changed  # the file confirmed was the registered one (checked on load)
    except OSError as err:
        raise changed from err


def prepare_test(study: Path, test: str, reader: ConfigReader, *, plan: bool) -> Prepared:
    """The ``Prepared`` of a registered Test: planned (a fresh Run, a dry run) or not (resume).

    Under the lease, ``reload`` re-checks the registered file (``changed``) and loads again;
    a Run's ``guard`` is ``test_already_open``.
    """
    ctx, test_cfg, personas = load_test_context(study, test, reader)
    planned = plan_test(test_cfg, ctx.cfg, personas, ctx.instruments) if plan else None

    def reload(changed: ConsortiumError) -> Prepared:
        _check_file(study, ctx, changed)
        return prepare_test(study, test, reader, plan=plan)

    return Prepared(
        test=test,
        ctx=ctx,
        plan=planned,
        reload=reload,
        insert=(
            (lambda conn: insert_plan(conn, planned.test, planned.trials))
            if planned is not None else None
        ),
        guard=functools.partial(_refuse_if_open, study, test),
    )


def _refuse_if_open(study: Path, test: str) -> None:
    found = read_only(study, lambda conn: (count_trials(conn, test), paused_reason(conn, test)))
    if found and found[0]:
        hint = (
            "; it is paused at the ceiling, use --resume --ceiling <higher>"
            if found[1] == "ceiling" else ""
        )
        raise ConsortiumError(
            "test_already_open",
            f"Test {test!r} already has Trials; use --resume to continue it{hint}",
        )


# --------------------------------------------------------------------------- raters


def raters_for(
    cfg: StudyConfig,
    model_ids: list[str],
    study: Path,
    cues: FidelityCues | None = None,
    *,
    fake: Callable[..., Rater] | None = None,
    gemini: Callable[[ModelSpec], Rater] | None = None,
    qwen: Callable[[ModelSpec], Rater] | None = None,
) -> dict[str, Rater]:
    """``model_id -> Rater``: the one place a provider is mapped to an adapter class.

    ``fake``: one ``FakeRater`` per Model, reporting that Model's ``fake`` usage and
    simulating its ``fake`` rates (and, story 3.1, its ``fake.fidelity`` mode, reading
    ``cues``). ``gemini`` / ``qwen``: a ``GeminiRater`` / ``QwenRater`` with a
    ``ModelSpec`` built from the config plus the key from the Model's key env var
    (``api_key_missing`` when it is unset or empty). Building a Rater makes no network
    call. The adapter classes can be substituted (``fake``, ``gemini``, ``qwen``).
    """
    fake_cls = fake or FakeRater
    gemini_cls = gemini or GeminiRater
    qwen_cls = qwen or QwenRater
    out: dict[str, Rater] = {}
    for model_id in model_ids:
        model = cfg.model_by_id(model_id)
        if model.provider == "fake":
            settings = model.fake_settings
            fidelity = getattr(settings, "fidelity", "random")
            extra: dict[str, Any] = (
                {"fidelity": fidelity, "cues": cues} if fidelity != "random" else {}
            )
            out[model_id] = fake_cls(
                input_tokens=settings.input_tokens, output_tokens=settings.output_tokens,
                invalid_rate=settings.invalid_rate, transient_rate=settings.transient_rate,
                refusal_rate=settings.refusal_rate, fatal_rate=settings.fatal_rate, **extra,
            )
        elif model.provider == "gemini":
            out[model_id] = gemini_cls(_model_spec(model, study))
        elif model.provider == "qwen":
            out[model_id] = qwen_cls(_model_spec(model, study))
        else:
            raise ConsortiumError(
                "provider_unavailable",
                f"model {model_id}: provider {model.provider!r} has no adapter yet (Epic 2)",
            )
    return out


def _model_spec(model: ModelConfig, study: Path) -> ModelSpec:
    env = model.api_key_env_name or ""
    api_key = os.environ.get(env, "").strip() if env else ""
    if not api_key:
        raise ConsortiumError("api_key_missing", f"{model.id}: set {env}")
    s = model.settings
    return ModelSpec(
        model_id=model.id, provider=model.provider, model=model.model,
        temperature=s.temperature, fps=s.fps, seed_supported=s.seed_supported,
        media_resolution=s.media_resolution, thinking_level=s.thinking_level,
        api_key=api_key, max_output_tokens=model.max_output_tokens,
        clips_dir=study / CLIPS_DIR, base_url=s.base_url, reasoning_effort=s.reasoning_effort,
    )


def _providers(cfg: StudyConfig, model_ids: list[str]) -> list[str]:
    return list(dict.fromkeys(cfg.model_by_id(m).provider for m in model_ids))


def _ceiling_prompt(new_ceiling: Decimal | None) -> str:
    return "" if new_ceiling is None else f", ceiling {usd(new_ceiling)} USD (study-wide)"


# --------------------------------------------------------------------------- dry run


def dry_run(study: Path, prepared: Prepared, new_ceiling: Decimal | None) -> OpenSummary:
    """Counts, requests digest, estimate and the ceiling refusal a Run would raise; no writes."""
    assert prepared.plan is not None
    ctx, plan = prepared.ctx, prepared.plan
    digest = hashlib.sha256()

    def hashed() -> Iterator[TrialRequest]:
        for request in prepared.requests():  # rendered lazily, one at a time
            digest.update(canonical_json(request))
            yield request

    estimate = ctx.estimate(plan.trials, hashed())
    dry_ceiling, committed = _resolve_ceiling(study, new_ceiling)
    model_ids = list(dict.fromkeys(s.model_id for s in plan.sessions))
    would_refuse = None
    try:
        _enforce_ceiling(dry_ceiling, committed, new_ceiling, ctx.uncapped_ok(model_ids),
                         estimate.expected, fresh=True)
    except ConsortiumError as err:
        would_refuse = err.code
    test = prepared.test
    return dataclasses.replace(
        _summarize(plan, ctx.instrument_order, True, digest.hexdigest()),
        estimate=estimate, ceiling=dry_ceiling, committed_before=committed,
        would_refuse=would_refuse, paused=read_only(study, lambda c: paused_reason(c, test)),
    )


# --------------------------------------------------------------------------- run


def _render_all(
    plan: Plan, requests: Iterator[TrialRequest]
) -> tuple[list[tuple[Trial, TrialRequest]], str]:
    digest = hashlib.sha256()
    pairs: list[tuple[Trial, TrialRequest]] = []
    for trial, request in zip(plan.trials, requests, strict=True):
        digest.update(canonical_json(request))
        pairs.append((trial, request))
    return pairs, digest.hexdigest()


def run(
    study: Path,
    prepared: Prepared,
    new_ceiling: Decimal | None,
    *,
    yes: bool,
    confirm: Confirm | None,
    announce: Announce | None,
    writer_spy: Spy | None,
    raters: RatersFor,
) -> OpenSummary:
    """A fresh Run of ``prepared.plan`` (see the module docstring)."""
    assert prepared.plan is not None
    ctx, plan, test = prepared.ctx, prepared.plan, prepared.test
    cfg, instrument_order, rel, fingerprint = (
        ctx.cfg, ctx.instrument_order, ctx.rel, ctx.test_sha256
    )
    pairs, digest = _render_all(plan, prepared.requests())
    model_ids = list(dict.fromkeys(s.model_id for s in plan.sessions))
    providers = _providers(cfg, model_ids)
    prepared.guard()
    estimate = ctx.estimate(plan.trials, (r for _, r in pairs))
    ceiling, committed = _resolve_ceiling(study, new_ceiling)
    shown = dataclasses.replace(
        _summarize(plan, instrument_order, False, digest),
        estimate=estimate, ceiling=ceiling, committed_before=committed,
    ).lines()
    if announce is not None:
        announce(shown)
    _enforce_ceiling(ceiling, committed, new_ceiling, ctx.uncapped_ok(model_ids),
                     estimate.expected, fresh=True)
    raters(cfg, model_ids, study)  # provider/key errors before confirmation
    if not yes:
        if confirm is None:
            raise ConsortiumError(
                "confirmation_required", "stdin is not a terminal; pass --yes to confirm"
            )
        prompt = (
            f"requests sha256: {digest}\nRun {len(pairs)} Trials on {', '.join(providers)}"
            f"{_ceiling_prompt(new_ceiling)}?"
        )
        if not confirm(prompt):
            raise ConsortiumError("not_confirmed", "Run not confirmed; nothing was sent")

    with acquire_lease(study):
        prepared.guard()
        changed = ConsortiumError(
            "test_changed", "the Test or its requests changed since the confirmation", path=rel
        )
        now = prepared.reload(changed)
        assert now.plan is not None
        pairs2, digest_now = _render_all(now.plan, now.requests())
        raters_now = raters(now.ctx.cfg, model_ids, study)
        if (
            now.ctx.test_sha256 != fingerprint
            or digest_now != digest
            or _providers(now.ctx.cfg, model_ids) != providers
            or now.ctx.estimate(now.plan.trials, (r for _, r in pairs2)) != estimate
        ):
            raise changed
        before = (ceiling, committed)
        ceiling, committed = _resolve_ceiling(study, new_ceiling)
        _enforce_ceiling(ceiling, committed, new_ceiling, now.ctx.uncapped_ok(model_ids),
                         estimate.expected, fresh=True)
        if (ceiling, committed) != before:
            raise changed
        summary = dataclasses.replace(
            _summarize(plan, instrument_order, False, digest),
            estimate=estimate, ceiling=ceiling, committed_before=committed,
        )
        if announce is not None:
            announce([line for line in summary.lines() if line not in shown])
        states, committed_after, paused, finished = asyncio.run(
            _dispatch_all(study, cfg, test, prepared.insert, now.finish, pairs, raters_now,
                          writer_spy, now.ctx.budget(test, ceiling), new_ceiling,
                          now.ctx.max_retries)
        )
    return dataclasses.replace(
        summary, states=states, committed=committed_after, paused=paused, finished=finished
    )


async def _dispatch_all(
    study: Path,
    cfg: StudyConfig,
    test: str,
    insert: Callable[[sqlite3.Connection], object] | None,
    finish: Callable[[sqlite3.Connection], object] | None,
    pairs: list[tuple[Trial, TrialRequest]],
    raters: dict[str, Rater],
    writer_spy: Spy | None,
    budget: Budget,
    new_ceiling: Decimal | None,
    max_retries: int,
) -> tuple[dict[str, int], Decimal, str | None, Any]:
    assert insert is not None
    async with start_writer(study, spy=writer_spy) as writer:
        await writer.do("insert_plan", insert)
        if new_ceiling is not None:
            await writer.do(
                "set_ceiling", lambda conn: set_ceiling(conn, new_ceiling, test, "run")
            )
        try:
            paused = await dispatch(
                study, pairs, raters, writer=writer, seed=cfg.seed, concurrency=cfg.concurrency,
                budget=budget, max_retries=max_retries, retry=cfg.session.retry,
            )
        finally:
            await close_raters(raters.values())
        states, committed = await writer.do(
            "state_counts", lambda conn: (state_counts(conn, test), committed_usd(conn))
        )
        finished = await writer.do("finish", finish) if finish is not None else None
        return states, committed, paused, finished


# --------------------------------------------------------------------------- resume


@dataclass(frozen=True)
class Resumable:
    """The stored Trials of an open Test, re-rendered from their rows and the Study folder."""

    trials: list[Trial]  # every stored Trial, plan order
    requests: dict[str, TrialRequest]  # trial_id -> re-rendered request
    digest: str  # requests sha256 over every stored Trial, plan order (as at the Run)
    todo: list[dict]  # non-terminal rows (load_resumable), plan order
    terminal: int

    @property
    def collect(self) -> dict[str, tuple[int, str]]:
        """``sent`` Trials whose latest attempt has a handle: collected at that attempt."""
        return {
            r["trial_id"]: (r["attempt"], r["handle"])
            for r in self.todo
            if r["state"] == "sent" and r["handle"] is not None and r["settle"] is None
        }

    @property
    def settles(self) -> list[tuple[str, int, str]]:
        """``(trial_id, attempt, state)`` of Trials settled from the board, no dispatch (1.10)."""
        return [(r["trial_id"], r["attempt"], r["settle"]) for r in self.todo if r["settle"]]

    @property
    def dispatched(self) -> list[dict]:
        """The non-terminal rows that go through the engine (not settled)."""
        return [r for r in self.todo if r["settle"] is None]

    def counts(self) -> dict[str, int]:
        collect = len(self.collect)
        settled = len(self.settles)
        return {
            "collect": collect,
            "new attempt": len(self.todo) - collect - settled,
            "settled": settled,
            "terminal": self.terminal,
        }

    def key(self) -> list[tuple]:
        return [(r["trial_id"], r["state"], r["attempt"], r["handle"], r["settle"])
                for r in self.todo]


def load_resumable_trials(study: Path, test: str, ctx: TestContext) -> Resumable:
    def read(conn):
        return load_trials(conn, test), load_resumable(conn, test, ctx.caps)

    rows, todo = read_only(study, read) or ([], [])
    if not rows:
        raise ConsortiumError(
            "test_not_open", f"Test {test!r} has no Trials; open it without --resume first"
        )
    trials = [trial_from_row(r) for r in rows]
    digest = hashlib.sha256()
    requests: dict[str, TrialRequest] = {}
    for trial in trials:
        request = ctx.render(trial)
        digest.update(canonical_json(request))
        requests[trial.trial_id] = request
    terminal = sum(r["state"] in TERMINAL_STATES for r in rows)
    return Resumable(trials, requests, digest.hexdigest(), todo, terminal)


def check_reissue_lines(
    study: Path, ctx: TestContext, test: str, res: Resumable
) -> tuple[int, int]:
    """Every archived request line of ``test`` equals the re-render of its Trial row.

    Also requires exactly one request line per key and one for every attempt
    marked ``sent``. Returns ``(lines checked, fragments skipped in requests.jsonl)``.
    """
    by_id = {t.trial_id: t for t in res.trials}
    attempts = read_only(study, lambda conn: attempt_seeds(conn, test)) or {}
    lines, fragments = read_lines(study, REQUESTS_FILE)

    def mismatch(tid: str, n: int, what: str) -> ConsortiumError:
        return ConsortiumError(
            "reissue_mismatch",
            f"{tid} attempt {n}: {what}; was a Study input edited after open?",
            path=REQUESTS_FILE,
        )

    seen: set[tuple[str, int]] = set()
    for record in lines:
        trial_id, attempt = record["trial_id"], record["attempt"]
        trial = by_id.get(trial_id)
        if trial is None:
            continue  # another Test's line
        if (trial_id, attempt) in seen:
            raise mismatch(trial_id, attempt, "the request is archived more than once")
        seen.add((trial_id, attempt))
        body = canonical_json(res.requests[trial_id])
        try:
            archived = canonical_json(record["request"])
        except (KeyError, TypeError, ValueError) as err:
            raise mismatch(trial_id, attempt, "the archived request is unreadable") from err
        if archived != body:
            raise mismatch(trial_id, attempt, "the re-rendered request differs from the archive")
        if record.get("request_sha256") != hashlib.sha256(body).hexdigest():
            raise mismatch(trial_id, attempt, "request_sha256 does not match the request")
        expected_seed = derive_seed(
            ctx.cfg.seed, MODEL_PURPOSE, f"{trial.session_id}:{trial.trial_index}:{attempt}"
        )
        stored = attempts.get((trial_id, attempt))
        if record.get("seed") != expected_seed or stored is None or stored[0] != expected_seed:
            raise mismatch(trial_id, attempt, "the archived seed does not match the attempt")
        if record.get("model_id") != trial.model_id:
            raise mismatch(trial_id, attempt, "the archived model_id does not match the Trial")
    for (trial_id, attempt), (_, sent) in attempts.items():
        if sent and (trial_id, attempt) not in seen:
            raise mismatch(trial_id, attempt, "the attempt was sent but has no request line")
    return len(seen), fragments


def resume_models(cfg: StudyConfig, test: str, res: Resumable) -> list[str]:
    """The Models of the non-terminal Trials only; ``unknown_model`` if one is gone."""
    model_ids = list(dict.fromkeys(r["model_id"] for r in res.todo))
    known = {m.id for m in cfg.models}
    for model_id in model_ids:
        if model_id not in known:
            raise ConsortiumError(
                "unknown_model",
                f"model {model_id!r} of Test {test!r}'s open Trials is not in study.yaml",
                path="study.yaml",
            )
    return model_ids


def _resume_estimate(ctx: TestContext, res: Resumable) -> PlanEstimate:
    """The estimate over the non-terminal Trials only."""
    todo = [trial_from_row(r) for r in res.dispatched]
    return ctx.estimate(todo, (res.requests[t.trial_id] for t in todo))


def resume(
    study: Path,
    prepared: Prepared,
    new_ceiling: Decimal | None,
    *,
    yes: bool,
    confirm: Confirm | None,
    announce: Announce | None,
    writer_spy: Spy | None,
    raters: RatersFor,
) -> OpenSummary:
    """Continue a stopped Run at Trial level (story 1.8).

    Never re-plans: every stored Trial is re-rendered from its row plus the Study
    folder. Terminal Trials are untouched; a ``sent`` Trial whose latest attempt
    has a handle is collected at that attempt; every other non-terminal Trial
    gets a new attempt. Before dispatch, under the lease, every archived request
    of the Test must re-render byte-identically (``reissue_mismatch``), so a
    collected attempt's response carries the archived ``request_sha256``.
    """
    test, ctx = prepared.test, prepared.ctx
    res = load_resumable_trials(study, test, ctx)
    model_ids = resume_models(ctx.cfg, test, res)
    providers = _providers(ctx.cfg, model_ids)
    estimate = _resume_estimate(ctx, res)
    ceiling, committed = _resolve_ceiling(study, new_ceiling)
    shown = dataclasses.replace(
        _summarize_trials(test, ctx.kind, res.trials, ctx.instrument_order, False, res.digest),
        estimate=estimate, ceiling=ceiling, committed_before=committed,
    ).lines()
    if announce is not None:
        announce(shown)
    _enforce_ceiling(ceiling, committed, new_ceiling, ctx.uncapped_ok(model_ids),
                     estimate.expected, fresh=False)
    raters(ctx.cfg, model_ids, study)  # provider/key errors before confirmation
    if res.todo and not yes:
        if confirm is None:
            raise ConsortiumError(
                "confirmation_required", "stdin is not a terminal; pass --yes to confirm"
            )
        prompt = (
            f"requests sha256: {res.digest}\n"
            f"Resume {len(res.todo)} Trials on {', '.join(providers)}"
            f"{_ceiling_prompt(new_ceiling)}?"
        )
        if not confirm(prompt):
            raise ConsortiumError("not_confirmed", "Resume not confirmed; nothing was sent")

    with acquire_lease(study):
        changed = ConsortiumError(
            "test_changed", "the Test, its Trials or its requests changed since the confirmation",
            path=ctx.rel,
        )
        now = prepared.reload(changed)
        ctx_now = now.ctx
        if ctx_now.test_sha256 != ctx.test_sha256:
            raise changed
        res_now = load_resumable_trials(study, test, ctx_now)
        models_now = resume_models(ctx_now.cfg, test, res_now)
        raters_now = raters(ctx_now.cfg, models_now, study)
        if (
            res_now.digest != res.digest
            or res_now.key() != res.key()
            or _providers(ctx_now.cfg, models_now) != providers
        ):
            raise changed
        _, fragments = check_reissue_lines(study, ctx_now, test, res_now)
        fragments += read_lines(study, RESPONSES_FILE)[1]
        estimate_now = _resume_estimate(ctx_now, res_now)
        if estimate_now != estimate:
            raise changed
        before = (ceiling, committed)
        ceiling, committed = _resolve_ceiling(study, new_ceiling)
        _enforce_ceiling(ceiling, committed, new_ceiling, ctx_now.uncapped_ok(models_now),
                         estimate.expected, fresh=False)
        if (ceiling, committed) != before:
            raise changed
        if now.resume_check is not _nothing:
            read_only(study, now.resume_check)
        summary = dataclasses.replace(
            _summarize_trials(
                test, ctx_now.kind, res_now.trials, ctx_now.instrument_order, False,
                res_now.digest,
            ),
            resumed={**res_now.counts(), "archive fragments": fragments},
            estimate=estimate, ceiling=ceiling, committed_before=committed,
        )
        if announce is not None:
            announce([line for line in summary.lines() if line not in shown])
        pairs = [
            (trial_from_row(r), res_now.requests[r["trial_id"]]) for r in res_now.dispatched
        ]
        finished = None
        if not pairs and not res_now.settles:  # nothing to send or settle: write nothing
            states, committed_after = read_only(
                study, lambda conn: (state_counts(conn, test), committed_usd(conn))
            ) or ({}, committed)
            paused = read_only(study, lambda conn: paused_reason(conn, test))
            if now.finish is not None:
                finished = asyncio.run(_finish_only(study, now.finish, writer_spy))
        else:
            budget = ctx_now.budget(test, ceiling)
            states, committed_after, paused, finished = asyncio.run(
                _dispatch_resume(study, ctx_now.cfg, test, pairs, res_now.collect, raters_now,
                                 writer_spy, budget, new_ceiling, ctx_now.max_retries,
                                 res_now.settles, ctx_now.cfg.session.retry, now.finish)
            )
    return dataclasses.replace(
        summary, states=states, committed=committed_after, paused=paused, finished=finished
    )


async def _finish_only(
    study: Path, finish: Callable[[sqlite3.Connection], object], writer_spy: Spy | None
) -> object:
    async with start_writer(study, spy=writer_spy) as writer:
        return await writer.do("finish", finish)


async def _dispatch_resume(
    study: Path,
    cfg: StudyConfig,
    test: str,
    pairs: list[tuple[Trial, TrialRequest]],
    collect: dict[str, tuple[int, str]],
    raters: dict[str, Rater],
    writer_spy: Spy | None,
    budget: Budget,
    new_ceiling: Decimal | None,
    max_retries: int,
    settles: list[tuple[str, int, str]],
    retry: RetryPolicy,
    finish: Callable[[sqlite3.Connection], object] | None = None,
) -> tuple[dict[str, int], Decimal, str | None, Any]:
    async with start_writer(study, spy=writer_spy) as writer:
        if new_ceiling is not None:
            await writer.do(
                "set_ceiling", lambda conn: set_ceiling(conn, new_ceiling, test, "resume")
            )
        for tid, attempt, state in settles:  # story 1.10: settled from the board, no dispatch
            await writer.do(
                "settle",
                functools.partial(settle, trial_id=tid, attempt=attempt, state=state),
                tid, attempt,
            )
        paused = None
        if pairs:
            try:
                paused = await dispatch(
                    study, pairs, raters, writer=writer, seed=cfg.seed,
                    concurrency=cfg.concurrency, collect=collect, budget=budget,
                    max_retries=max_retries, retry=retry,
                )
            finally:
                await close_raters(raters.values())
        if paused is None:  # the pause is cleared only once the resume did not pause again
            await writer.do("set_paused", lambda conn: set_paused(conn, test, None))
        states, committed = await writer.do(
            "state_counts", lambda conn: (state_counts(conn, test), committed_usd(conn))
        )
        finished = await writer.do("finish", finish) if finish is not None else None
        return states, committed, paused, finished
