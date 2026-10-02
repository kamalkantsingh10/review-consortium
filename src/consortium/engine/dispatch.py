"""The one execution path for every Model call (AD-6): ``dispatch``.

Per attempt, in this order, every board.db write and Archive append going
through the single writer (``board.writer``):

1. ``begin_attempt``: reserve the attempt's estimated cost (``core.cost.estimate``)
   in the ledger, increment ``attempt``, record the attempt and its seed, all in
   one transaction; refused (nothing written) when committed spend plus the
   reservation would exceed the ceiling (story 1.9). Clips are prepared
   (uploaded) only after a successful reservation;
2. ``append_request``: the rendered request goes to the Archive;
3. ``mark_sent``: the Trial is ``sent`` (Archive before state, AD-5);
4. ``rater.submit`` -> ``set_handle``;
5. ``rater.collect`` -> ``append_response``;
6. ``record_actual``: the attempt's actual cost from the returned usage (with no
   usage the reservation stands; an attempt with no ledger row, from before the
   ledger existed, gets one);
7. ``record_validation`` (story 1.10): an ``ok`` response is parsed and validated
   by ``core.validate.validate_response`` against the Trial's Instrument (the Items
   of its request); the attempt's ``valid``, ``invalid_reason`` and canonical
   answer JSON are recorded;
8. ``set_state``: a valid attempt -> ``valid``; an invalid one -> ``invalid`` once
   ``attempt > max_retries`` (no retries left), else the Trial stays ``sent`` and
   gets a new attempt through steps 1-8 (a new seed, the same request text, its
   own reservation; a refused reservation leaves it ``sent`` for resume). Any
   category other than ``ok`` -> ``failed`` (never retried, never validated).

Retries are sequential per Trial: attempt n+1 starts only after attempt n was
collected and found invalid. A Trial never gets more than ``1 + max_retries``
attempts: when it already has that many and would need another (on resume),
it is settled instead (``board.trials.settle_exhausted``): ``invalid`` if its
last attempt was recorded invalid, ``failed`` (category ``attempts_exhausted``)
if that attempt was abandoned unanswered, ``valid`` if it was recorded valid.

**Ceiling pause** (story 1.9): once a reservation is refused, or a recorded
actual cost pushes committed spend over the ceiling (the estimate was too low),
no new attempt starts (Trials still waiting are left as they are), attempts
already in flight are collected, and ``set_paused`` stores ``paused_reason =
'ceiling'`` on the Test (also when the Run then stops on an error);
``dispatch`` returns ``"ceiling"``. Committed spend is kept as a running total
on the writer (``board.ledger.Spend``).

Each ``(trial_id, attempt)`` is dispatched at most once. Clips are prepared
once per Rater (cached; a failed prepare is not cached). Concurrency is capped
by one ``asyncio.Semaphore`` per provider. ``submit`` and the storing of its
handle are shielded from cancellation, so a handle the provider issued is never
lost. This is the only module that calls ``submit`` and ``collect``.

If any Trial fails, the Run stops: the other Trials are cancelled and the first
``ConsortiumError`` is raised unchanged, any other error as ``run_failed``.
A cancelled Trial can be left ``planned`` with ``attempt >= 1`` (an attempt row
without ``sent_at``, possibly with an archived request) or ``sent``.

**Resume** (story 1.8): ``collect`` maps the ``trial_id`` of every ``sent`` Trial
whose latest attempt has a stored handle to ``(attempt, handle)``. Such a Trial
is collected at that same attempt (steps 5-8 only: no new attempt, no new
request line; an invalid answer then continues into retries). Every other
Trial passed in (``planned``, with any ``attempt``, or ``sent`` without a
handle) gets a new attempt through steps 1-8; attempt
numbers are never reused, and an attempt never marked ``sent`` is never
collected. A latest attempt already recorded invalid is not in ``collect``
(``board.trials.load_resumable``), so it gets a new attempt; one already
recorded valid is settled by the caller from the board and not passed in.
Terminal Trials must not be passed in.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from consortium.archive.jsonl import append_request, append_response
from consortium.board import ledger as board_ledger
from consortium.board import tests as board_tests
from consortium.board import trials as board_trials
from consortium.board.writer import Writer
from consortium.core import cost
from consortium.core.errors import ConsortiumError
from consortium.core.plan import Trial
from consortium.core.render import ClipRef, RequestItem, TrialRequest, canonical_json
from consortium.core.validate import INVALID_RESPONSE, validate_response
from consortium.raters.base import Handle, MediaRef, Rater, RaterCall, RaterResult


@dataclass(frozen=True)
class Budget:
    """What the engine needs to reserve, price and cap each attempt (story 1.9).

    ``models`` maps Model ID to its ``ModelConfig``; ``prices`` is the
    ``PricesConfig``; ``clip_seconds`` maps Clip ID to ``duration_s``;
    ``ceiling`` is the Study's ceiling in USD, ``None`` for an uncapped Run.
    """

    test: str
    models: Mapping[str, Any]
    prices: Any
    clip_seconds: Mapping[str, float]
    ceiling: Decimal | None
    free: bool = False  # zero-cost: every reservation and actual cost is 0 (tests only)

    @classmethod
    def zero(cls, test: str) -> Budget:
        """A zero-cost, uncapped Budget, for all-Fake zero-priced Runs only."""
        return cls(test=test, models={}, prices=None, clip_seconds={}, ceiling=None, free=True)

    def reservation(self, trial: Trial, request: TrialRequest) -> Decimal:
        if self.free:
            return Decimal(0)
        return cost.estimate(request, self.models[trial.model_id], self.prices,
                             self.clip_seconds)

    def actual(self, model_id: str, usage: Mapping[str, Any] | None) -> Decimal | None:
        if self.free:
            return Decimal(0)
        return cost.actual(usage, self.models[model_id], self.prices)


@dataclass(frozen=True)
class _InstrumentView:
    """The Instrument as its request carries it: its name and its Items."""

    name: str
    items: tuple[RequestItem, ...]


@dataclass(frozen=True)
class Outcome:
    """What one collected attempt means: its validation and the Trial's next step."""

    valid: bool | None  # None: not validated (category not ok)
    reason: str | None
    answer_json: str | None
    state: str | None  # the terminal state to set, None: stays sent and is retried


def outcome_for(
    trial: Trial, request: TrialRequest, result: RaterResult, attempt: int, max_retries: int
) -> Outcome:
    """Validate ``result`` (core) and decide: ``valid``, retry, ``invalid`` or ``failed``."""
    if result.category != "ok":
        return Outcome(None, None, None, "failed")
    try:
        parsed = validate_response(result.raw, _InstrumentView(trial.instrument, request.items))
    except ConsortiumError as err:
        if err.code != INVALID_RESPONSE:
            raise
        return Outcome(False, err.message, None, None if attempt <= max_retries else "invalid")
    return Outcome(True, None, canonical_json(parsed.answers).decode("utf-8"), "valid")


def _clips_of(request: TrialRequest) -> list[ClipRef]:
    seen: dict[str, ClipRef] = {}
    for example in request.practice:
        for clip in example.clips:
            seen.setdefault(clip.clip_id, clip)
    for clip in request.clips:
        seen.setdefault(clip.clip_id, clip)
    return list(seen.values())


class _Prepared:
    """``prepare`` once per Clip per Rater; concurrent callers share one call."""

    def __init__(self) -> None:
        self._tasks: dict[tuple[int, str], asyncio.Future[MediaRef]] = {}

    async def get(self, rater: Rater, clip: ClipRef) -> MediaRef:
        key = (id(rater), clip.clip_id)
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.ensure_future(rater.prepare(clip))
            self._tasks[key] = task
        try:
            return await asyncio.shield(task)
        except BaseException:
            failed = task.done() and (task.cancelled() or task.exception() is not None)
            if failed and self._tasks.get(key) is task:
                del self._tasks[key]  # a failed prepare is retried, not cached
            raise


def _one[T](items: list[T], what: str, provider: str) -> T:
    if not isinstance(items, list) or len(items) != 1:
        n = len(items) if isinstance(items, list) else type(items).__name__
        raise ConsortiumError(
            "adapter_error", f"{provider} Rater: {what} returned {n} results for 1 call"
        )
    return items[0]


def _first_error(group: BaseExceptionGroup) -> BaseException:
    leaves: list[BaseException] = []

    def walk(eg: BaseExceptionGroup) -> None:
        for e in eg.exceptions:
            if isinstance(e, BaseExceptionGroup):
                walk(e)
            else:
                leaves.append(e)

    walk(group)
    for e in leaves:
        if isinstance(e, ConsortiumError):
            return e
    return leaves[0]


async def dispatch(
    study_dir: Path | str,
    trials: Sequence[tuple[Trial, TrialRequest]],
    rater_by_model: Mapping[str, Rater],
    *,
    writer: Writer,
    seed: int,
    concurrency: int,
    collect: Mapping[str, tuple[int, str]] | None = None,
    budget: Budget,
    max_retries: int,
) -> str | None:
    """Send every ``(Trial, TrialRequest)`` through its Model's Rater.

    ``seed`` is ``study.seed`` (attempt seeds derive from it); ``concurrency`` is
    the per-provider cap (``StudyConfig.concurrency``). The Trials must already be
    stored. ``collect`` (resume) maps ``trial_id`` to the ``(attempt, handle JSON
    text)`` of a ``sent`` attempt to collect instead of re-sending; its response
    line carries the SHA-256 of ``request``, which the caller has checked equals
    the archived one. ``budget`` prices and caps every attempt
    (``Budget.zero`` for an uncapped zero-cost Run). ``max_retries`` is the Test's
    effective ``session.max_retries``: an invalid answer is retried while
    ``attempt <= max_retries``. Returns ``"ceiling"`` when the Run
    paused at the ceiling, else ``None``. Any error stops the Run (see the module docstring):
    ``bad_concurrency`` for ``concurrency < 1``, ``provider_unavailable`` for a
    Model with no Rater, ``bad_max_retries`` for ``max_retries < 0``,
    ``adapter_error``, ``run_failed`` or the adapter's own ``ConsortiumError``.
    """
    study = Path(study_dir)
    if concurrency < 1:
        raise ConsortiumError(
            "bad_concurrency", f"concurrency must be at least 1, not {concurrency}"
        )
    if max_retries < 0:
        raise ConsortiumError(
            "bad_max_retries", f"max_retries must be at least 0, not {max_retries}"
        )
    max_attempts = 1 + max_retries
    missing = sorted({t.model_id for t, _ in trials} - set(rater_by_model))
    if missing:
        raise ConsortiumError(
            "provider_unavailable", f"no Rater for model(s) {', '.join(missing)}"
        )
    semaphores: dict[str, asyncio.Semaphore] = {}
    for rater in rater_by_model.values():
        semaphores.setdefault(rater.provider, asyncio.Semaphore(concurrency))
    prepared = _Prepared()
    shielded: set[asyncio.Future] = set()
    paused: list[str] = []  # set once a reservation is refused: no new attempt starts
    spend = board_ledger.Spend()  # the writer's running committed total
    ceiling = budget.ceiling

    def _settle(task: asyncio.Future) -> None:
        shielded.discard(task)
        if not task.cancelled():
            task.exception()  # retrieved: the awaiting task reports it, or it was cancelled

    async def run_one(trial: Trial, request: TrialRequest) -> None:
        """New attempts for ``trial`` until it is settled, paused or out of retries."""
        while await attempt_once(trial, request):
            pass

    async def attempt_once(trial: Trial, request: TrialRequest) -> bool:
        """One new attempt; True when its answer was invalid and it is to be retried."""
        rater = rater_by_model[trial.model_id]
        tid = trial.trial_id
        async with semaphores[rater.provider]:
            if paused:
                return False
            usd = budget.reservation(trial, request)

            def reserve(conn):  # runs on the writer: no attempt starts after a refusal
                if paused:
                    return None
                if board_trials.settle_exhausted(conn, tid, max_attempts):
                    return None  # no attempts left: the Trial is now invalid
                began = board_trials.begin_attempt(
                    conn, trial_id=tid, study_seed=seed, usd=usd, ceiling=ceiling, spend=spend
                )
                if began is None:  # the reservation would cross the ceiling: pause
                    paused.append("ceiling")
                return began

            began = await writer.do("begin_attempt", reserve, tid)
            if began is None:
                return False
            attempt, attempt_seed = began
            media = tuple([await prepared.get(rater, c) for c in _clips_of(request)])
            request_record = await writer.do(
                "append_request",
                lambda _conn: append_request(
                    study, trial_id=tid, attempt=attempt, seed=attempt_seed,
                    model_id=trial.model_id, request=request,
                ),
                tid, attempt,
            )
            await writer.do(
                "mark_sent",
                functools.partial(board_trials.mark_sent, trial_id=tid, attempt=attempt),
                tid, attempt,
            )
            call = RaterCall(tid, attempt, attempt_seed, request, media)

            async def submit_and_store() -> Handle:
                handle = _one(await rater.submit([call]), "submit", rater.provider)
                handle_text = canonical_json(handle).decode("utf-8")
                await writer.do(
                    "set_handle",
                    functools.partial(
                        board_trials.set_handle, trial_id=tid, attempt=attempt,
                        handle=handle_text,
                    ),
                    tid, attempt,
                )
                return handle

            inner = asyncio.ensure_future(submit_and_store())
            shielded.add(inner)
            inner.add_done_callback(_settle)
            handle = await asyncio.shield(inner)
            return await finish(rater, trial, request, attempt, handle,
                                request_record["request_sha256"])

    async def finish(
        rater: Rater, trial: Trial, request: TrialRequest, attempt: int, handle: Handle,
        request_sha256: str,
    ) -> bool:
        """Collect, archive, price and validate one attempt; True when it is to be retried."""
        tid = trial.trial_id
        result = _one(await rater.collect([handle]), "collect", rater.provider)
        await writer.do(
            "append_response",
            lambda _conn: append_response(
                study, trial_id=tid, attempt=attempt, request_sha256=request_sha256,
                raw=result.raw, usage=result.usage,
                model_build=result.model_build, category=result.category,
            ),
            tid, attempt,
        )
        spent = budget.actual(trial.model_id, result.usage)
        reserved = budget.reservation(trial, request)

        def record(conn):  # on the writer: an overshoot pauses before the next reservation
            board_ledger.record_actual(
                conn, trial_id=tid, attempt=attempt, model_id=trial.model_id, usd=spent,
                reserved=reserved, spend=spend,
            )
            if ceiling is not None and spend.committed(conn) > ceiling and not paused:
                paused.append("ceiling")

        await writer.do("record_actual", record, tid, attempt)
        outcome = outcome_for(trial, request, result, attempt, max_retries)
        if outcome.valid is not None:
            await writer.do(
                "record_validation",
                functools.partial(
                    board_trials.record_validation, trial_id=tid, attempt=attempt,
                    valid=outcome.valid, reason=outcome.reason,
                    answer_json=outcome.answer_json, category=result.category,
                ),
                tid, attempt,
            )
        if outcome.state is None:
            return True  # stays sent: a new attempt follows
        await writer.do(
            "set_state",
            functools.partial(
                board_trials.set_state, trial_id=tid, attempt=attempt,
                state=outcome.state, category=result.category,
            ),
            tid, attempt,
        )
        return False

    async def collect_one(trial: Trial, request: TrialRequest, attempt: int, handle: str) -> None:
        """Resume a ``sent`` attempt with a handle: collect it, never re-send it."""
        rater = rater_by_model[trial.model_id]
        request_sha256 = hashlib.sha256(canonical_json(request)).hexdigest()
        try:
            parsed = json.loads(handle)
        except (TypeError, ValueError):
            parsed = None
        if not isinstance(parsed, dict):
            raise ConsortiumError(
                "adapter_error", f"stored handle unreadable for {trial.trial_id}"
            )
        async with semaphores[rater.provider]:
            retry = await finish(rater, trial, request, attempt, parsed, request_sha256)
        if retry:
            await run_one(trial, request)

    try:
        async with asyncio.TaskGroup() as group:
            for trial, request in trials:
                resumed = collect.get(trial.trial_id) if collect else None
                if resumed is None:
                    group.create_task(run_one(trial, request))
                else:
                    group.create_task(collect_one(trial, request, *resumed))
    except BaseExceptionGroup as group_error:
        err = _first_error(group_error)
        if isinstance(err, ConsortiumError) or not isinstance(err, Exception):
            raise err from None
        raise ConsortiumError("run_failed", f"{type(err).__name__}: {err}") from err
    finally:
        # Let shielded submits finish storing their handles while the writer still runs.
        if shielded:
            await asyncio.gather(*shielded, return_exceptions=True)
        if paused:  # stored even when the Run then stopped on an error
            await writer.do(
                "set_paused",
                functools.partial(board_tests.set_paused, test=budget.test, reason="ceiling"),
            )
    return "ceiling" if paused else None
