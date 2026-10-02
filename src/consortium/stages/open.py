"""Use case: open a registered Test. Story 1.6 delivers ``--dry-run`` only.

A dry run loads the Study config, the registered Test, its Instruments, the
Persona Panel and the Clip hashes; plans every Session and Trial; renders every
request (so a broken Instrument or missing Clip fails here, not mid-Run); and
returns counts plus a SHA-256 digest of every request. It writes no Study data:
``board.db`` is opened read-only and no Study file is created or changed.
Dispatch arrives in story 1.7.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from consortium.board.clips import list_clips
from consortium.board.db import read_only
from consortium.board.tests import get_test
from consortium.config.load import PERSONAS_DIR, load_instruments, load_personas, load_study
from consortium.config.load import load_test as load_test_file
from consortium.core.errors import ConsortiumError
from consortium.core.plan import Plan, plan_test
from consortium.core.render import TrialRequest, canonical_json, practice_for, render
from consortium.core.test_checks import check_media_limits, check_plan


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
) -> OpenSummary:
    """Plan and render every Trial of the registered Test ``test`` and return the counts.

    Only ``dry_run=True`` is available (story 1.6); otherwise ``run_unavailable``.
    ``ceiling`` is validated (``bad_ceiling``); ``yes`` and ``resume`` are ignored
    until stories 1.7-1.9. Raises ``unknown_test`` (not registered, or no
    ``board.db``), ``protocol_lock_unavailable`` (before any planning),
    ``test_exists`` (the registered file is missing or was edited),
    ``test_changed``, ``board_unreadable``, ``panel_missing`` / ``panel_invalid``,
    ``unknown_clip``, ``bad_pairing``, ``bad_practice``, ``media_limit_exceeded``,
    ``unknown_instrument`` and any config error.
    """
    parse_ceiling(ceiling)
    if not dry_run:
        raise ConsortiumError("run_unavailable", "dispatch arrives in story 1.7")
    plan, requests, instrument_order = plan_and_render(study_dir, test)
    digest = hashlib.sha256()
    for request in requests:  # rendered lazily, one at a time
        digest.update(canonical_json(request))
    return _summarize(plan, instrument_order, dry_run, digest.hexdigest())


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
    study = Path(study_dir)
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
    return plan, requests, list(test_cfg.instruments)
