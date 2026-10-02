"""Plan a Test into Sessions and Trials (pure: no I/O, only seeded randomness).

* **Agents** are every Persona (in Panel order) x every Model of the Test (in the
  Test's order, default all of ``study.yaml``); Agent ID ``p<n>-m<n>``.
* **Sessions**: one per Agent and Repeat ``1..N`` (N = effective
  ``session.repeats``), ID ``<test>/<agent>/r<repeat>``. Sessions are listed
  Persona-major, then Model, then Repeat.
* **Trials per Session**, canonical order: Instruments in the Test's order; for a
  single-Clip Instrument one Trial per target Clip (Test order); for a pairwise
  Instrument, every unordered pair of target Clips (``itertools.combinations``
  over the Test's Clip order), each as two Trials sharing
  ``pair_id = <instrument>:<clip_lo>:<clip_hi>`` (sorted IDs): ``position`` 1
  shows ``clip_lo`` first, ``position`` 2 shows ``clip_hi`` first.
* **Order**: the canonical list is shuffled with
  ``random.Random(derive_seed(study.seed, "order", session_id))`` (an explicit
  Fisher-Yates over ``getrandbits``, as for Personas), then numbered
  ``trial_index`` 1..n. ``trial_id = <session_id>/t<trial_index>``.
* **Self-report** Instruments (story 3.1, ``self_report: true``): one clip-less
  Trial per Session (``clip_ids = ()``), whatever the Test's Clips.
* **Prompt variant**: Repeat ``r`` uses the ``(r - 1) mod n``-th of the
  Instrument's ``prompt_variants`` in declared order.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from consortium.core.errors import ConsortiumError
from consortium.core.ids import session_id as make_session_id
from consortium.core.personas import fisher_yates
from consortium.core.seeds import derive_seed

ORDER_PURPOSE = "order"

# Structural views of the config models, so core never imports config.


class _Session(Protocol):
    repeats: int


class _Study(Protocol):
    seed: int


class _Test(Protocol):
    test: str
    kind: str
    instruments: list[str]
    clips: list[str]

    def model_ids(self, study: _Study) -> list[str]: ...

    def effective_session(self, study: _Study) -> _Session: ...


class _Instrument(Protocol):
    prompt_variants: Mapping[str, str]
    self_report: bool

    @property
    def pairwise(self) -> bool: ...


class _Persona(Protocol):
    @property
    def id(self) -> str: ...


@dataclass(frozen=True)
class Trial:
    """One fresh request to one Agent: an Instrument applied to one Clip or an ordered pair."""

    trial_id: str
    test: str
    session_id: str
    trial_index: int
    instrument: str
    clip_ids: tuple[str, ...]
    pair_id: str | None  # pairwise only
    position: int | None  # pairwise only: 1 = clip_lo first, 2 = clip_hi first
    prompt_variant: str
    order_seed: int
    repeat: int
    agent_id: str
    persona_id: str
    model_id: str

    @property
    def pairwise(self) -> bool:
        return self.pair_id is not None


@dataclass(frozen=True)
class Session:
    session_id: str
    test: str
    agent_id: str
    persona_id: str
    model_id: str
    repeat: int
    order_seed: int
    trials: tuple[Trial, ...]


@dataclass(frozen=True)
class Plan:
    test: str
    kind: str
    sessions: tuple[Session, ...]

    @property
    def trials(self) -> Iterator[Trial]:
        for session in self.sessions:
            yield from session.trials

    def __len__(self) -> int:
        return sum(len(s.trials) for s in self.sessions)


def agent_id(persona_id: str, model_id: str) -> str:
    """Agent ID ``p<n>-m<n>``."""
    return f"{persona_id}-{model_id}"


def trial_id(session_id: str, trial_index: int) -> str:
    """Trial ID ``<session_id>/t<trial_index>``."""
    return f"{session_id}/t{trial_index}"


def pair_id(instrument: str, a: str, b: str) -> str:
    """``<instrument>:<clip_lo>:<clip_hi>`` with the Clip IDs sorted."""
    lo, hi = sorted((a, b))
    return f"{instrument}:{lo}:{hi}"


def prompt_variant(instrument: _Instrument, repeat: int) -> str:
    """The variant name Repeat ``repeat`` (1-based) uses: rotation in declared order."""
    names = list(instrument.prompt_variants)
    return names[(repeat - 1) % len(names)]


# (instrument, clip_ids, pair_id, position)
_Shape = tuple[str, tuple[str, ...], str | None, int | None]


def canonical_trials(
    instrument_names: Sequence[str],
    clips: Sequence[str],
    instruments: Mapping[str, _Instrument],
) -> list[_Shape]:
    """The unshuffled Trial list of one Session (see the module docstring)."""
    out: list[_Shape] = []
    for name in instrument_names:
        if instruments[name].self_report:
            out.append((name, (), None, None))
        elif instruments[name].pairwise:
            for a, b in itertools.combinations(clips, 2):
                lo, hi = sorted((a, b))
                pid = pair_id(name, lo, hi)
                out.append((name, (lo, hi), pid, 1))
                out.append((name, (hi, lo), pid, 2))
        else:
            out.extend((name, (clip,), None, None) for clip in clips)
    return out


def plan_test(
    test: _Test,
    cfg: _Study,
    personas: Sequence[_Persona],
    instruments: Mapping[str, _Instrument],
    shapes: Sequence[_Shape] | None = None,
) -> Plan:
    """Every Session and Trial of ``test`` (a ``TestConfig``) for ``cfg`` (a ``StudyConfig``).

    ``instruments`` must hold every Instrument the Test lists (else
    ``unknown_instrument``). ``shapes`` (story 3.2: a perception run's
    ``core.perception.check_shapes``) replaces the canonical Trial list of each Session.
    Same inputs give the same Plan.
    """
    missing = [name for name in test.instruments if name not in instruments]
    if missing:
        raise ConsortiumError(
            "unknown_instrument", f"instrument(s) not loaded: {', '.join(missing)}"
        )
    repeats = test.effective_session(cfg).repeats
    if shapes is None:
        shapes = canonical_trials(test.instruments, test.clips, instruments)
    sessions: list[Session] = []
    for persona in personas:
        for model_id in test.model_ids(cfg):
            agent = agent_id(persona.id, model_id)
            for repeat in range(1, repeats + 1):
                sid = make_session_id(test.test, agent, repeat)
                seed = derive_seed(cfg.seed, ORDER_PURPOSE, sid)
                order = list(range(len(shapes)))
                fisher_yates(order, random.Random(seed))
                trials = []
                for index, k in enumerate(order, start=1):
                    name, clip_ids, pid, position = shapes[k]
                    trials.append(
                        Trial(
                            trial_id=trial_id(sid, index),
                            test=test.test,
                            session_id=sid,
                            trial_index=index,
                            instrument=name,
                            clip_ids=clip_ids,
                            pair_id=pid,
                            position=position,
                            prompt_variant=prompt_variant(instruments[name], repeat),
                            order_seed=seed,
                            repeat=repeat,
                            agent_id=agent,
                            persona_id=persona.id,
                            model_id=model_id,
                        )
                    )
                sessions.append(
                    Session(
                        sid, test.test, agent, persona.id, model_id, repeat, seed, tuple(trials)
                    )
                )
    return Plan(test.test, test.kind, tuple(sessions))
