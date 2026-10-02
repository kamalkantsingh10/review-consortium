"""Use case: open a registered Test: ``--dry-run`` (1.6), a Run (1.7) or ``--resume`` (1.8).

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

``--resume`` (story 1.8) continues a stopped Run without re-planning: it
re-renders every stored Trial from its row plus the Study folder, checks that
every archived request re-renders byte-identically, and sends only the
non-terminal Trials through the same ``engine.dispatch``.

Cost (story 1.9): every open prints the ``core.cost`` estimate of the Trials it
would send (``expected`` once each, ``worst_case`` with every retry) before
anything reaches a Rater. A Run or resume needs a ceiling (``--ceiling``, or
the latest one logged in ``board.db``) unless every Model it sends to is Fake;
a fresh Run refuses ``over_ceiling`` when committed spend plus ``expected``
exceeds it. ``--ceiling`` is logged (``set_ceiling``) only once the Run is
confirmed, under the lease. The engine reserves per attempt and pauses the Run
at the ceiling (``OpenSummary.paused == "ceiling"``); ``--resume`` clears the
pause and continues.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from consortium.archive.jsonl import REQUESTS_FILE, RESPONSES_FILE, read_lines
from consortium.board.clips import list_clips
from consortium.board.db import read_only
from consortium.board.lease import acquire_lease
from consortium.board.ledger import committed_usd, current_ceiling, set_ceiling
from consortium.board.tests import get_test, paused_reason, set_paused
from consortium.board.trials import (
    MODEL_PURPOSE,
    TERMINAL_STATES,
    attempt_seeds,
    count_trials,
    insert_plan,
    load_resumable,
    load_trials,
    state_counts,
    trial_from_row,
)
from consortium.board.writer import Spy, migrate, start_writer
from consortium.config.load import (
    PERSONAS_DIR,
    load_instruments,
    load_personas,
    load_prices,
    load_study,
)
from consortium.config.load import load_test as load_test_file
from consortium.config.models import InstrumentDef, PricesConfig, StudyConfig, TestConfig
from consortium.core.cost import PlanEstimate, estimate_plan, usd
from consortium.core.errors import ConsortiumError
from consortium.core.plan import Plan, Trial, plan_test
from consortium.core.render import TrialRequest, canonical_json, practice_for, render
from consortium.core.seeds import derive_seed
from consortium.core.test_checks import check_media_limits, check_plan
from consortium.engine.dispatch import Budget, dispatch
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
    resumed: dict[str, int] | None = None  # --resume: collect / new attempt / terminal counts
    estimate: PlanEstimate | None = None  # cost of the Trials this open sends (all for dry run)
    ceiling: Decimal | None = None  # the ceiling this open runs under; None = uncapped
    committed_before: Decimal | None = None  # Study-wide committed spend before this open
    committed: Decimal | None = None  # Study-wide committed spend after a Run
    paused: str | None = None  # "ceiling" when the Run paused (or, dry run, is paused)
    would_refuse: str | None = None  # dry run: the ceiling refusal a Run would raise

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
        ctx, plan, requests = _plan_and_render(study, test)
        digest = hashlib.sha256()

        def hashed() -> Iterator[TrialRequest]:
            for request in requests:  # rendered lazily, one at a time
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
        return dataclasses.replace(
            _summarize(plan, ctx.instrument_order, dry_run, digest.hexdigest()),
            estimate=estimate, ceiling=dry_ceiling, committed_before=committed,
            would_refuse=would_refuse, paused=read_only(study, lambda c: paused_reason(c, test)),
        )
    load = _load_context if resume else _plan_and_render
    try:
        prepared = load(study, test)
    except ConsortiumError as err:
        if err.code != "board_version_mismatch":
            raise
        with acquire_lease(study):  # a Run writes board.db, so it migrates an older layout
            migrate(study)
        prepared = load(study, test)
    if resume:
        ctx, _, _ = prepared
        return _resume(
            study, test, ctx, new_ceiling, yes=yes, confirm=confirm, announce=announce,
            writer_spy=writer_spy,
        )
    return _run(
        study, test, prepared, new_ceiling, yes=yes, confirm=confirm, announce=announce,
        writer_spy=writer_spy,
    )


@dataclass(frozen=True)
class _Resumable:
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
            if r["state"] == "sent" and r["handle"] is not None
        }

    def counts(self) -> dict[str, int]:
        collect = len(self.collect)
        return {
            "collect": collect,
            "new attempt": len(self.todo) - collect,
            "terminal": self.terminal,
        }

    def key(self) -> list[tuple]:
        return [(r["trial_id"], r["state"], r["attempt"], r["handle"]) for r in self.todo]


def _load_resumable(study: Path, test: str, ctx: _Context) -> _Resumable:
    def read(conn):
        return load_trials(conn, test), load_resumable(conn, test)

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
    return _Resumable(trials, requests, digest.hexdigest(), todo, terminal)


def _check_reissue(study: Path, ctx: _Context, test: str, res: _Resumable) -> tuple[int, int]:
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
    ctx, _, _ = _load_context(study, test)
    return _check_reissue(study, ctx, test, _load_resumable(study, test, ctx))[0]


def _resume_models(cfg: StudyConfig, test: str, res: _Resumable) -> list[str]:
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


def _providers(cfg: StudyConfig, model_ids: list[str]) -> list[str]:
    return list(dict.fromkeys(cfg.model_by_id(m).provider for m in model_ids))


def _ceiling_prompt(new_ceiling: Decimal | None) -> str:
    return "" if new_ceiling is None else f", ceiling {usd(new_ceiling)} USD (study-wide)"


def _resume(
    study: Path,
    test: str,
    ctx: _Context,
    new_ceiling: Decimal | None,
    *,
    yes: bool,
    confirm: Confirm | None,
    announce: Announce | None,
    writer_spy: Spy | None,
) -> OpenSummary:
    """``open --resume``: continue a stopped Run at Trial level (story 1.8).

    Never re-plans: every stored Trial is re-rendered from its row plus the Study
    folder. Terminal Trials are untouched; a ``sent`` Trial whose latest attempt
    has a handle is collected at that attempt; every other non-terminal Trial
    gets a new attempt. Before dispatch, under the lease, every archived request
    of the Test must re-render byte-identically (``reissue_mismatch``), so a
    collected attempt's response carries the archived ``request_sha256``.
    """
    res = _load_resumable(study, test, ctx)
    model_ids = _resume_models(ctx.cfg, test, res)
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
    raters_for(ctx.cfg, model_ids)  # provider_unavailable before confirmation
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
        try:
            if hashlib.sha256((study / ctx.rel).read_bytes()).hexdigest() != ctx.test_sha256:
                raise changed  # the file confirmed was the registered one (checked on load)
        except OSError as err:
            raise changed from err
        ctx_now, _, _ = _load_context(study, test)
        if ctx_now.test_sha256 != ctx.test_sha256:
            raise changed
        res_now = _load_resumable(study, test, ctx_now)
        models_now = _resume_models(ctx_now.cfg, test, res_now)
        raters_now = raters_for(ctx_now.cfg, models_now)
        if (
            res_now.digest != res.digest
            or res_now.key() != res.key()
            or _providers(ctx_now.cfg, models_now) != providers
        ):
            raise changed
        _, fragments = _check_reissue(study, ctx_now, test, res_now)
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
            (trial_from_row(r), res_now.requests[r["trial_id"]]) for r in res_now.todo
        ]
        if not pairs:  # nothing to send: write nothing
            states, committed_after = read_only(
                study, lambda conn: (state_counts(conn, test), committed_usd(conn))
            ) or ({}, committed)
            paused = read_only(study, lambda conn: paused_reason(conn, test))
        else:
            budget = ctx_now.budget(test, ceiling)
            states, committed_after, paused = asyncio.run(
                _dispatch_resume(study, ctx_now.cfg, test, pairs, res_now.collect, raters_now,
                                 writer_spy, budget, new_ceiling)
            )
    return dataclasses.replace(summary, states=states, committed=committed_after, paused=paused)


def _resume_estimate(ctx: _Context, res: _Resumable) -> PlanEstimate:
    """The estimate over the non-terminal Trials only."""
    todo = [trial_from_row(r) for r in res.todo]
    return ctx.estimate(todo, (res.requests[t.trial_id] for t in todo))


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
) -> tuple[dict[str, int], Decimal, str | None]:
    async with start_writer(study, spy=writer_spy) as writer:
        if new_ceiling is not None:
            await writer.do(
                "set_ceiling", lambda conn: set_ceiling(conn, new_ceiling, test, "resume")
            )
        paused = await dispatch(
            study, pairs, raters, writer=writer, seed=cfg.seed,
            concurrency=cfg.concurrency, collect=collect, budget=budget,
        )
        if paused is None:  # the pause is cleared only once the resume did not pause again
            await writer.do("set_paused", lambda conn: set_paused(conn, test, None))
        states, committed = await writer.do(
            "state_counts", lambda conn: (state_counts(conn, test), committed_usd(conn))
        )
        return states, committed, paused


def raters_for(cfg: StudyConfig, model_ids: list[str]) -> dict[str, Rater]:
    """``model_id -> Rater``. Only ``fake`` exists so far: one ``FakeRater`` per Model,
    reporting that Model's ``fake`` usage (they share the provider's semaphore)."""
    out: dict[str, Rater] = {}
    for model_id in model_ids:
        model = cfg.model_by_id(model_id)
        if model.provider != "fake":
            raise ConsortiumError(
                "provider_unavailable",
                f"model {model_id}: provider {model.provider!r} has no adapter yet (Epic 2)",
            )
        fake = model.fake_settings
        out[model_id] = FakeRater(input_tokens=fake.input_tokens, output_tokens=fake.output_tokens)
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


def _run(
    study: Path,
    test: str,
    prepared: _Prepared,
    new_ceiling: Decimal | None,
    *,
    yes: bool,
    confirm: Confirm | None,
    announce: Announce | None,
    writer_spy: Spy | None,
) -> OpenSummary:
    ctx, plan, requests = prepared
    cfg, instrument_order, rel, test_sha256 = (
        ctx.cfg, ctx.instrument_order, ctx.rel, ctx.test_sha256
    )
    pairs, digest = _render_all(plan, requests)
    model_ids = list(dict.fromkeys(s.model_id for s in plan.sessions))
    providers = _providers(cfg, model_ids)
    _refuse_if_open(study, test)
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
    raters_for(cfg, model_ids)  # provider_unavailable before confirmation
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
        _refuse_if_open(study, test)
        changed = ConsortiumError(
            "test_changed", "the Test or its requests changed since the confirmation", path=rel
        )
        try:
            if hashlib.sha256((study / rel).read_bytes()).hexdigest() != test_sha256:
                raise changed
        except OSError as err:
            raise changed from err
        ctx_now, plan2, requests2 = _plan_and_render(study, test)
        pairs2, digest_now = _render_all(plan2, requests2)
        raters_now = raters_for(ctx_now.cfg, model_ids)
        if (
            ctx_now.test_sha256 != test_sha256
            or digest_now != digest
            or _providers(ctx_now.cfg, model_ids) != providers
            or ctx_now.estimate(plan2.trials, (r for _, r in pairs2)) != estimate
        ):
            raise changed
        before = (ceiling, committed)
        ceiling, committed = _resolve_ceiling(study, new_ceiling)
        _enforce_ceiling(ceiling, committed, new_ceiling, ctx_now.uncapped_ok(model_ids),
                         estimate.expected, fresh=True)
        if (ceiling, committed) != before:
            raise changed
        summary = dataclasses.replace(
            _summarize(plan, instrument_order, False, digest),
            estimate=estimate, ceiling=ceiling, committed_before=committed,
        )
        if announce is not None:
            announce([line for line in summary.lines() if line not in shown])
        states, committed_after, paused = asyncio.run(
            _dispatch_all(study, cfg, plan, pairs, raters_now, writer_spy,
                          ctx_now.budget(test, ceiling), new_ceiling)
        )
    return dataclasses.replace(summary, states=states, committed=committed_after, paused=paused)


async def _dispatch_all(
    study: Path,
    cfg: StudyConfig,
    plan: Plan,
    pairs: list[tuple[Trial, TrialRequest]],
    raters: dict[str, Rater],
    writer_spy: Spy | None,
    budget: Budget,
    new_ceiling: Decimal | None,
) -> tuple[dict[str, int], Decimal, str | None]:
    async with start_writer(study, spy=writer_spy) as writer:
        await writer.do("insert_plan", lambda conn: insert_plan(conn, plan.test, plan.trials))
        if new_ceiling is not None:
            await writer.do(
                "set_ceiling", lambda conn: set_ceiling(conn, new_ceiling, plan.test, "run")
            )
        paused = await dispatch(
            study, pairs, raters, writer=writer, seed=cfg.seed, concurrency=cfg.concurrency,
            budget=budget,
        )
        states, committed = await writer.do(
            "state_counts", lambda conn: (state_counts(conn, plan.test), committed_usd(conn))
        )
        return states, committed, paused


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
    ctx, plan, requests = _plan_and_render(Path(study_dir), test)
    return plan, requests, ctx.instrument_order


# render context, plan, lazily rendered requests (plan order)
_Prepared = tuple["_Context", Plan, Iterator[TrialRequest]]


@dataclass(frozen=True)
class _Context:
    """Everything ``core.render`` needs from the Study folder for one registered Test."""

    cfg: StudyConfig
    kind: str
    instrument_order: list[str]
    rel: str  # the registered Test file
    test_sha256: str
    cards: dict[str, str]
    instruments: dict[str, InstrumentDef]
    practice: dict[str, list]
    clip_sha256: dict[str, str]
    prices: PricesConfig
    clip_seconds: dict[str, float]
    max_retries: int  # the Test's effective session.max_retries

    def estimate(
        self, trials: Sequence[Trial], requests: Iterator[TrialRequest]
    ) -> PlanEstimate:
        """``core.cost.estimate_plan`` over ``trials`` and their requests (same order)."""
        return estimate_plan(
            trials, requests, self.cfg, self.prices, self.clip_seconds,
            max_retries=self.max_retries,
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


def _load_context(study: Path, test: str) -> tuple[_Context, TestConfig, list]:
    """The registered Test's checks and render inputs (see ``open_test`` for the errors)."""
    cfg = load_study(study)
    prices = load_prices(study)
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
    ctx = _Context(
        cfg=cfg,
        kind=test_cfg.kind,
        instrument_order=list(test_cfg.instruments),
        rel=rel,
        test_sha256=row["sha256"],
        cards={p.id: _read_card(study, p.id) for p in personas},
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


def _plan_and_render(study: Path, test: str) -> _Prepared:
    ctx, test_cfg, personas = _load_context(study, test)
    plan = plan_test(test_cfg, ctx.cfg, personas, ctx.instruments)
    requests = (ctx.render(trial) for trial in plan.trials)
    return ctx, plan, requests
