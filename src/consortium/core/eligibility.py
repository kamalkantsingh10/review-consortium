"""The eligibility gate (story 3.3): which Agents of a Test may be planned (pure).

Inputs are the Test's Agents (Persona x Model), its Instruments, the result rows of every
complete screening run (``board.screening.complete_results``: superseded rows included,
fidelity and perception) and the current stamps (``core.hashes`` of the current
``study.yaml`` Models, the Test's Instrument definitions and the fidelity Instruments).

A result row *counts* only while its ``settings_hash`` equals its Model's current stamp
and its ``instrument_hash`` the current stamp of its Instrument (fidelity rows: the
fidelity stamp). For each key the **latest counting row** (highest run number) decides,
so a newer stale row never hides an older counting one; a key with rows but no counting
row is *stale*. Outcomes other than ``pass`` / ``fail`` are rejected (``ValueError``).

- **Self-report** Instruments (``self_report``) are never screened for perception: they
  are left out of the coverage and perception checks.
- **Coverage** (Story 3.2's rule, ``core.perception.covered_instruments``): a Test
  Instrument is covered when a counting perception row of it, of any Model, has
  ``pair_checks > 0``; stale rows never cover (``core.perception.coverage``). An
  uncovered Instrument with no pair-checked row at all is ``screening_coverage_missing``;
  one whose pair-checked rows are all stale is refused ``screening_stale`` instead (it
  was covered until a Model's settings or the Instrument changed).
- **Perception** per ``(model, instrument)``: ``pass`` / ``fail`` from the deciding row,
  else ``stale`` or ``missing``. A Model with a counting ``fail`` loses all its Agents
  with ``perception_fail`` (first failed Instrument in Test order); its stale keys (and
  its Agents' fidelity rows) are then ignored, since it is excluded whatever they say.
  Otherwise a stale key makes the Model's Agents ``screening_stale``, and a missing one
  ``perception_missing``.
- **Fidelity** per Agent (``instrument = 'fidelity'``): ``fidelity_fail`` /
  ``fidelity_missing``, or ``screening_stale``.
- **One reason per excluded Agent**: ``perception_fail`` > ``screening_stale`` >
  ``perception_missing`` > ``fidelity_*``.

``Eligibility.refusal`` gives the refusal ``open`` raises, in order: coverage
(``screening_coverage_missing``), stale (``screening_stale``), empty plan
(``no_eligible_agents``); so ``screening_stale`` is never a stored exclusion reason.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from consortium.core.errors import ConsortiumError
from consortium.core.ids import run_number
from consortium.core.perception import coverage, covered_instruments
from consortium.core.plan import agent_id as make_agent_id

FIDELITY = "fidelity"
PERCEPTION = "perception"
STALE = "screening_stale"
PERCEPTION_FAIL = "perception_fail"
PERCEPTION_MISSING = "perception_missing"
FIDELITY_FAIL = "fidelity_fail"
FIDELITY_MISSING = "fidelity_missing"
REASONS = (PERCEPTION_FAIL, STALE, PERCEPTION_MISSING, FIDELITY_FAIL, FIDELITY_MISSING)
OUTCOMES = ("pass", "fail")


@dataclass(frozen=True)
class Stamps:
    """The current stamps: ``settings_hash`` per Model of ``study.yaml`` (all of them:
    coverage counts any Model), ``instrument_hash`` per Test Instrument and the fidelity
    stamp (``core.hashes.fidelity_hash``)."""

    settings: Mapping[str, str]
    instruments: Mapping[str, str]
    fidelity: str


@dataclass(frozen=True)
class Exclusion:
    agent_id: str
    persona_id: str
    model_id: str
    instrument: str | None  # the Instrument it failed on; None for fidelity reasons
    reason: str


@dataclass(frozen=True)
class StaleKey:
    """A needed key whose rows are all stale, and why."""

    kind: str  # "fidelity" | "perception"
    model_id: str
    instrument: str  # "fidelity" for fidelity keys
    settings_changed: bool
    instrument_changed: bool


@dataclass(frozen=True)
class Eligibility:
    agents_ok: tuple[str, ...]
    excluded: tuple[Exclusion, ...]
    total: int = 0
    coverage_missing: tuple[str, ...] = ()
    stale: tuple[StaleKey, ...] = ()
    perception: Mapping[tuple[str, str], str] = field(default_factory=dict)  # outcome
    # (model, instrument or "fidelity") -> the run IDs whose rows decided
    deciding: Mapping[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)

    @property
    def runs(self) -> dict[str, tuple[str, ...]]:
        """``{kind: run IDs}`` of every deciding row, in run-number order."""
        out: dict[str, set[str]] = {FIDELITY: set(), PERCEPTION: set()}
        for (_, name), runs in self.deciding.items():
            out[FIDELITY if name == FIDELITY else PERCEPTION].update(runs)
        return {k: tuple(sorted(v, key=run_number)) for k, v in out.items()}

    def counts(self) -> dict[str, int]:
        """Excluded Agents per reason, in precedence order (non-zero only)."""
        found = Counter(e.reason for e in self.excluded)
        return {reason: found[reason] for reason in REASONS if found[reason]}

    def details(self, reason: str) -> dict[str, list[str]]:
        """``{model: instruments}`` behind a perception reason (outcome ``fail`` or
        ``missing``), Models and Instruments in first-seen order."""
        outcome = {PERCEPTION_FAIL: "fail", PERCEPTION_MISSING: "missing"}.get(reason)
        models = {e.model_id for e in self.excluded if e.reason == reason}
        out: dict[str, list[str]] = {}
        for (model, name), value in self.perception.items():
            if model in models and value == outcome:
                out.setdefault(model, []).append(name)
        return out

    def refusal(self) -> ConsortiumError | None:
        """The refusal ``open`` raises (coverage, then stale, then empty), or None."""
        if self.coverage_missing:
            return ConsortiumError(
                "screening_coverage_missing",
                "no current perception screening result with pair checks covers "
                f"{', '.join(self.coverage_missing)}; add pair checks for them to a "
                "screening Test and run consortium screen models <TEST>",
            )
        if self.stale:
            return ConsortiumError("screening_stale", stale_message(self.stale))
        if not self.agents_ok:
            counts = ", ".join(f"{k} {v}" for k, v in self.counts().items()) or "no Agents"
            return ConsortiumError(
                "no_eligible_agents", f"no Agent of the Test is eligible ({counts})"
            )
        return None


def stale_message(stale: Iterable[StaleKey]) -> str:
    """Names each Model whose settings and each Instrument whose definition changed, and
    the screening command(s) to re-run."""
    models: list[str] = []
    instruments: list[str] = []
    kinds: set[str] = set()
    for key in stale:
        kinds.add(key.kind)
        if key.settings_changed and key.model_id not in models:
            models.append(key.model_id)
        if key.instrument_changed:
            name = (
                "the fidelity Instruments or Persona card wording" if key.kind == FIDELITY
                else key.instrument
            )
            if name not in instruments:
                instruments.append(name)
    parts = [f"Model {m} (settings changed)" for m in models]
    parts += [f"Instrument {n} (definition changed)" for n in instruments]
    commands = []
    if FIDELITY in kinds:
        commands.append("consortium screen personas")
    if PERCEPTION in kinds:
        commands.append("consortium screen models <TEST>")
    return (
        f"screening results are stale for {', '.join(parts) or 'some keys'}; "
        f"re-run {' and '.join(commands)}"
    )


def _judge(
    rows: Sequence[Mapping[str, Any]], settings: str | None, stamp: str | None
) -> tuple[str, Mapping[str, Any] | None, bool, bool]:
    """``(outcome, deciding row, settings_changed, instrument_changed)`` of one key:
    outcome ``pass`` / ``fail`` (latest counting row), ``stale`` or ``missing``."""
    fresh = [
        r for r in rows
        if settings is not None and stamp is not None
        and r.get("settings_hash") == settings and r.get("instrument_hash") == stamp
    ]
    if fresh:
        row = max(fresh, key=lambda r: run_number(r.get("run_id", "")))
        return str(row["outcome"]), row, False, False
    if rows:
        return (
            "stale", None,
            any(r.get("settings_hash") != settings for r in rows),
            any(r.get("instrument_hash") != stamp for r in rows),
        )
    return "missing", None, False, False


def eligible(
    agents: Sequence[tuple[str, str]],
    instruments: Sequence[str],
    results: Iterable[Mapping[str, Any]],
    stamps: Stamps,
    *,
    self_report: Collection[str] = (),
) -> Eligibility:
    """The gate over ``agents`` (``(persona_id, model_id)``, plan order) for the Test
    Instruments ``instruments`` (see the module docstring; ``self_report`` names the
    Test's self-report Instruments). ``results`` are the rows of every complete run:
    fidelity rows have ``instrument == 'fidelity'`` and an ``agent_id``; perception rows
    have no ``agent_id``. Raises ``ValueError`` for an outcome other than pass / fail."""
    fidelity: dict[str, list[Mapping[str, Any]]] = {}
    perception: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    perception_rows: list[Mapping[str, Any]] = []
    for row in results:
        if row.get("outcome") not in OUTCOMES:
            raise ValueError(
                f"screening result of run {row.get('run_id')!r} has outcome "
                f"{row.get('outcome')!r}, not pass or fail"
            )
        if row.get("agent_id") is not None:
            if row.get("instrument") == FIDELITY:
                fidelity.setdefault(row["agent_id"], []).append(row)
            continue
        perception.setdefault((row["model_id"], row["instrument"]), []).append(row)
        perception_rows.append(row)

    judged = [n for n in instruments if n not in self_report]
    covered = covered_instruments(
        perception_rows,
        {n: stamps.instruments[n] for n in judged if n in stamps.instruments},
        stamps.settings,
    )
    stale: list[StaleKey] = []
    missing_list: list[str] = []
    for name in coverage({name: () for name in judged}, covered):
        pair_rows = [r for r in perception_rows
                     if r["instrument"] == name and (r.get("pair_checks") or 0) > 0]
        if not pair_rows:
            missing_list.append(name)
        for r in pair_rows:  # only stale rows have pair checks: refuse as stale
            stale.append(StaleKey(
                PERCEPTION, r["model_id"], name,
                r.get("settings_hash") != stamps.settings.get(r["model_id"]),
                r.get("instrument_hash") != stamps.instruments.get(name),
            ))
    missing = tuple(missing_list)
    deciding: dict[tuple[str, str], set[str]] = {}

    models = list(dict.fromkeys(m for _, m in agents))
    outcomes: dict[tuple[str, str], str] = {}
    model_reason: dict[str, tuple[str, str] | None] = {}
    for model in models:
        settings = stamps.settings.get(model)
        found: dict[str, str] = {}
        model_stale: list[StaleKey] = []
        for name in judged:
            outcome, row, s_changed, i_changed = _judge(
                perception.get((model, name), []), settings, stamps.instruments.get(name)
            )
            outcomes[(model, name)] = outcome
            found[name] = outcome
            if row is not None:
                deciding.setdefault((model, name), set()).add(str(row["run_id"]))
            if outcome == "stale":
                model_stale.append(StaleKey(PERCEPTION, model, name, s_changed, i_changed))
        reason = None
        for outcome, code in (("fail", PERCEPTION_FAIL), ("stale", STALE),
                              ("missing", PERCEPTION_MISSING)):
            first = next((n for n in judged if found[n] == outcome), None)
            if first is not None:
                reason = (code, first)
                break
        if reason is None or reason[0] != PERCEPTION_FAIL:
            stale.extend(model_stale)  # a Model excluded by a counting fail ignores them
        model_reason[model] = reason

    ok: list[str] = []
    excluded: list[Exclusion] = []
    for persona, model in agents:
        agent = make_agent_id(persona, model)
        perceived = model_reason[model]
        if perceived is not None and perceived[0] == PERCEPTION_FAIL:
            excluded.append(Exclusion(agent, persona, model, perceived[1], PERCEPTION_FAIL))
            continue
        outcome, row, s_changed, i_changed = _judge(
            fidelity.get(agent, []), stamps.settings.get(model), stamps.fidelity
        )
        if row is not None:
            deciding.setdefault((model, FIDELITY), set()).add(str(row["run_id"]))
        if outcome == "stale":
            stale.append(StaleKey(FIDELITY, model, FIDELITY, s_changed, i_changed))
        if outcome == "stale" or (perceived is not None and perceived[0] == STALE):
            reason = STALE
            instrument = perceived[1] if perceived is not None and perceived[0] == STALE else None
        elif perceived is not None:
            reason, instrument = perceived
        elif outcome == "fail":
            reason, instrument = FIDELITY_FAIL, None
        elif outcome == "missing":
            reason, instrument = FIDELITY_MISSING, None
        else:
            ok.append(agent)
            continue
        excluded.append(Exclusion(agent, persona, model, instrument, reason))

    return Eligibility(
        agents_ok=tuple(ok),
        excluded=tuple(excluded),
        total=len(agents),
        coverage_missing=missing,
        stale=tuple(stale),
        perception=outcomes,
        deciding={k: tuple(sorted(v, key=run_number)) for k, v in deciding.items()},
    )
