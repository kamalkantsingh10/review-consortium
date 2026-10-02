"""The one execution path for every Model call (AD-6): ``dispatch``.

Per attempt, in this order, every board.db write and Archive append going
through the single writer (``board.writer``):

1. ``begin_attempt``: increment ``attempt``, record the attempt and its seed;
2. ``append_request``: the rendered request goes to the Archive;
3. ``mark_sent``: the Trial is ``sent`` (Archive before state, AD-5);
4. ``rater.submit`` -> ``set_handle``;
5. ``rater.collect`` -> ``append_response``;
6. ``set_state``: ``ok`` -> ``valid``, any other category -> ``failed``.

Each ``(trial_id, attempt)`` is dispatched at most once. Clips are prepared
once per Rater (cached; a failed prepare is not cached). Concurrency is capped
by one ``asyncio.Semaphore`` per provider. ``submit`` and the storing of its
handle are shielded from cancellation, so a handle the provider issued is never
lost. This is the only module that calls ``submit`` and ``collect``.

If any Trial fails, the Run stops: the other Trials are cancelled and the first
``ConsortiumError`` is raised unchanged, any other error as ``run_failed``.
A cancelled Trial can be left ``planned`` with ``attempt >= 1`` (an attempt row
without ``sent_at``, possibly with an archived request) or ``sent``; resume
(story 1.8) re-dispatches the former with a new attempt.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Mapping, Sequence
from pathlib import Path

from consortium.archive.jsonl import append_request, append_response
from consortium.board import trials as board_trials
from consortium.board.writer import Writer
from consortium.core.errors import ConsortiumError
from consortium.core.plan import Trial
from consortium.core.render import ClipRef, TrialRequest, canonical_json
from consortium.raters.base import Handle, MediaRef, Rater, RaterCall, RaterResult


def state_for(result: RaterResult) -> str:
    """Terminal state of an attempt (story 1.7; validation replaces this in 1.10)."""
    return "valid" if result.category == "ok" else "failed"


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
) -> None:
    """Send every ``(Trial, TrialRequest)`` through its Model's Rater.

    ``seed`` is ``study.seed`` (attempt seeds derive from it); ``concurrency`` is
    the per-provider cap (``StudyConfig.concurrency``). The Trials must already be
    stored ``planned``. Any error stops the Run (see the module docstring):
    ``bad_concurrency`` for ``concurrency < 1``, ``provider_unavailable`` for a
    Model with no Rater, ``adapter_error``, ``run_failed`` or the adapter's own
    ``ConsortiumError``.
    """
    study = Path(study_dir)
    if concurrency < 1:
        raise ConsortiumError(
            "bad_concurrency", f"concurrency must be at least 1, not {concurrency}"
        )
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

    async def run_one(trial: Trial, request: TrialRequest) -> None:
        rater = rater_by_model[trial.model_id]
        tid = trial.trial_id
        async with semaphores[rater.provider]:
            media = tuple([await prepared.get(rater, c) for c in _clips_of(request)])
            attempt, attempt_seed = await writer.do(
                "begin_attempt",
                functools.partial(board_trials.begin_attempt, trial_id=tid, study_seed=seed),
                tid,
            )
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
            inner.add_done_callback(shielded.discard)
            handle = await asyncio.shield(inner)
            result = _one(await rater.collect([handle]), "collect", rater.provider)
            await writer.do(
                "append_response",
                lambda _conn: append_response(
                    study, trial_id=tid, attempt=attempt,
                    request_sha256=request_record["request_sha256"],
                    raw=result.raw, usage=result.usage,
                    model_build=result.model_build, category=result.category,
                ),
                tid, attempt,
            )
            await writer.do(
                "set_state",
                functools.partial(
                    board_trials.set_state, trial_id=tid, attempt=attempt,
                    state=state_for(result), category=result.category,
                ),
                tid, attempt,
            )

    try:
        async with asyncio.TaskGroup() as group:
            for trial, request in trials:
                group.create_task(run_one(trial, request))
    except BaseExceptionGroup as group_error:
        err = _first_error(group_error)
        if isinstance(err, ConsortiumError) or not isinstance(err, Exception):
            raise err from None
        raise ConsortiumError("run_failed", f"{type(err).__name__}: {err}") from err
    finally:
        # Let shielded submits finish storing their handles while the writer still runs.
        if shielded:
            await asyncio.gather(*shielded, return_exceptions=True)
