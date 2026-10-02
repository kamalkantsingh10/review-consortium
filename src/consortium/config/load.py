"""The only module that reads YAML: Study files into typed config models.

Every failure is a ``ConsortiumError``: ``config_invalid`` with
``"<field>: <reason>"`` and the file path relative to the Study folder, or
``unknown_instrument`` when an Instrument name does not resolve.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from consortium.config.models import (
    NARS_CONSTRUCT,
    NARS_SUBSCALES,
    CardWording,
    InstrumentDef,
    PricesConfig,
    StudyConfig,
    TestConfig,
)
from consortium.core.errors import ConsortiumError
from consortium.core.personas import QUOTA_ATTRIBUTES, Persona

log = logging.getLogger(__name__)

STUDY_FILE = "study.yaml"
PRICES_FILE = "prices.yaml"
USER_INSTRUMENTS_DIR = "instruments"
_BUILTIN_PACKAGE = "consortium.instruments"
_TEMPLATES_PACKAGE = "consortium.templates"
CARD_WORDING_FILE = "persona_card/wording.yaml"
PERSONAS_DIR = "panel/personas"
PERSONAS_INDEX = f"{PERSONAS_DIR}/index.json"
FIDELITY_BFI = "fidelity_bfi10"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_]*$")

# --------------------------------------------------------------------------- YAML reading


class _UniqueKeyLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that refuses duplicate mapping keys instead of keeping the last."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    # Keys written in this mapping; keys pulled in by a ``<<`` merge may be overridden.
    explicit = {id(k) for k, _ in node.value if k.tag != "tag:yaml.org,2002:merge"}
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    written: set[Any] = set()
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            hash(key)
        except TypeError as err:
            raise yaml.constructor.ConstructorError(
                None, None, "mapping keys must be plain values, not lists or mappings",
                key_node.start_mark,
            ) from err
        if id(key_node) in explicit:
            if key in written:
                line = key_node.start_mark.line + 1
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r} (line {line})", key_node.start_mark
                )
            written.add(key)
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _safe_load(text: str) -> Any:
    # yaml.safe_load semantics (SafeLoader), plus duplicate-key rejection.
    return yaml.load(text, Loader=_UniqueKeyLoader)


def _yaml_reason(err: yaml.YAMLError) -> str:
    mark = getattr(err, "problem_mark", None)
    problem = getattr(err, "problem", None) or str(err)
    if mark is not None:
        return f"invalid YAML at line {mark.line + 1}, column {mark.column + 1}: {problem}"
    return f"invalid YAML: {problem}"


def _invalid(rel: str, reason: str) -> ConsortiumError:
    return ConsortiumError("config_invalid", reason, path=rel)


def _parse[M: BaseModel](text: str, rel: str, model: type[M]) -> M:
    """YAML text -> validated ``model``; every failure becomes ``config_invalid``."""
    try:
        data = _safe_load(text)
    except yaml.YAMLError as err:
        raise _invalid(rel, _yaml_reason(err)) from err
    if data is None:
        raise _invalid(rel, "file is empty")
    if not isinstance(data, dict):
        raise _invalid(rel, f"top level must be a mapping, got {type(data).__name__}")
    try:
        return model.model_validate(data)
    except ValidationError as err:
        raise _invalid(rel, _validation_reason(err)) from err


def _validation_reason(err: ValidationError) -> str:
    parts = []
    for item in err.errors(include_url=False):
        field = ".".join(str(p) for p in item["loc"] if p != "[key]")
        msg = str(item["msg"]).removeprefix("Value error, ")
        msg = msg[:1].lower() + msg[1:]
        parts.append(f"{field}: {msg}" if field else msg)
    return "; ".join(parts)


def _read_file(path: Path, rel: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise _invalid(rel, "file not found") from err
    except (OSError, UnicodeDecodeError) as err:
        raise _invalid(rel, f"cannot read file: {err}") from err


def _rel(path: Path, study_dir: Path) -> str:
    try:
        return Path(os.path.relpath(path.resolve(), study_dir.resolve())).as_posix()
    except ValueError:  # different drive on Windows
        return str(path)


# --------------------------------------------------------------------------- Instruments


def _builtin_dir() -> Traversable:
    return resources.files(_BUILTIN_PACKAGE)


def _builtin_file(name: str) -> Traversable | None:
    entry = _builtin_dir() / f"{name}.yaml"
    return entry if entry.is_file() else None


def _user_file(study_dir: Path, name: str) -> Path | None:
    path = Path(study_dir) / USER_INSTRUMENTS_DIR / f"{name}.yaml"
    return path if path.is_file() else None


def builtin_instrument_names() -> list[str]:
    """Names of the Instruments shipped with the package."""
    return sorted(
        e.name.removesuffix(".yaml")
        for e in _builtin_dir().iterdir()
        if e.name.endswith(".yaml") and e.is_file()
    )


def _check_no_shadowing(study_dir: Path) -> None:
    folder = Path(study_dir) / USER_INSTRUMENTS_DIR
    if not folder.is_dir():
        return
    builtins = set(builtin_instrument_names())
    for path in sorted(folder.glob("*.yaml")):
        name = path.stem
        if name in builtins:
            raise _invalid(
                f"{USER_INSTRUMENTS_DIR}/{path.name}",
                f"name: {name!r} is a built-in Instrument; rename the user Instrument",
            )


def _resolvable(study_dir: Path, name: str) -> bool:
    return bool(_NAME.match(name)) and (
        _user_file(study_dir, name) is not None or _builtin_file(name) is not None
    )


def _load_instrument(
    study_dir: Path, name: str, ref_path: str | None = None, field: str | None = None
) -> InstrumentDef:
    user = _user_file(study_dir, name)
    if user is not None:
        rel = f"{USER_INSTRUMENTS_DIR}/{name}.yaml"
        text = _read_file(user, rel)
    else:
        builtin = _builtin_file(name)
        if builtin is None:
            raise ConsortiumError(
                "unknown_instrument", f"{field or 'name'}: {name!r} not found", path=ref_path
            )
        rel = f"<built-in>/instruments/{name}.yaml"
        text = builtin.read_text(encoding="utf-8")
    instrument = _parse(text, rel, InstrumentDef)
    if instrument.name != name:
        raise _invalid(rel, f"name: {instrument.name!r} must match the file name {name!r}")
    return instrument


# --------------------------------------------------------------------------- loaders


def load_study(study_dir: Path | str) -> StudyConfig:
    """Load and validate ``<study>/study.yaml``.

    Also checks that every enabled Instrument name resolves (user folder, then
    built-in) and that no user Instrument shadows a built-in.
    """
    study_dir = Path(study_dir)
    text = _read_file(study_dir / STUDY_FILE, STUDY_FILE)
    cfg = _parse(text, STUDY_FILE, StudyConfig)
    _check_no_shadowing(study_dir)
    for i, name in enumerate(cfg.instruments):
        if not _resolvable(study_dir, name):
            raise ConsortiumError(
                "unknown_instrument", f"instruments.{i}: {name!r} not found", path=STUDY_FILE
            )
    return cfg


def load_instruments(study_dir: Path | str, cfg: StudyConfig) -> dict[str, InstrumentDef]:
    """Every Instrument enabled in ``cfg.instruments``, by name, in declared order."""
    study_dir = Path(study_dir)
    _check_no_shadowing(study_dir)
    out: dict[str, InstrumentDef] = {}
    for i, name in enumerate(cfg.instruments):
        if not _resolvable(study_dir, name):
            raise ConsortiumError(
                "unknown_instrument", f"instruments.{i}: {name!r} not found", path=STUDY_FILE
            )
        out[name] = _load_instrument(study_dir, name, STUDY_FILE, f"instruments.{i}")
    return out


def load_test(
    path: Path | str, study_dir: Path | str | None = None, *, cfg: StudyConfig | None = None
) -> TestConfig:
    """Load and validate a Test YAML file.

    ``study_dir`` defaults to the parent of the file's folder (``<study>/tests/x.yaml``).
    Checks that every Instrument resolves and is enabled in ``study.yaml``, and that
    every listed Model id exists. Logs ``draft_instrument: <name>`` to stderr for each
    draft Instrument the Test lists. ``cfg`` is the already loaded ``study.yaml``
    (loaded here when omitted).
    """
    path = Path(path)
    study = Path(study_dir) if study_dir is not None else path.resolve().parent.parent
    rel = _rel(path, study)
    test = _parse(_read_file(path, rel), rel, TestConfig)
    if test.test != path.stem:
        raise _invalid(rel, f"test: {test.test!r} must match the file name {path.stem!r}")

    if cfg is None:
        cfg = load_study(study)
    for i, name in enumerate(test.instruments):
        if not _resolvable(study, name):
            raise ConsortiumError(
                "unknown_instrument", f"instruments.{i}: {name!r} not found", path=rel
            )
        if name not in cfg.instruments:
            if _is_self_report(study, name):
                raise _not_allowed(rel, i, name)
            raise ConsortiumError(
                "unknown_instrument",
                f"instruments.{i}: {name!r} is not enabled in {STUDY_FILE} instruments",
                path=rel,
            )
    known = {m.id for m in cfg.models}
    for i, model_id in enumerate(test.models or []):
        if model_id not in known:
            raise _invalid(rel, f"models.{i}: {model_id!r} is not a Model id in {STUDY_FILE}")

    instruments = {
        name: _load_instrument(study, name, rel, f"instruments.{i}")
        for i, name in enumerate(test.instruments)
    }
    for i, (name, instrument) in enumerate(instruments.items()):
        if instrument.self_report:
            raise _not_allowed(rel, i, name)
    for i, example in enumerate(test.practice):
        where = f"practice.{i}"
        instrument = instruments.get(example.instrument)
        if instrument is None:
            raise ConsortiumError(
                "unknown_instrument",
                f"{where}.instrument: {example.instrument!r} is not one of the Test's instruments",
                path=rel,
            )
        want = 2 if instrument.pairwise else 1
        if len(example.clips) != want:
            raise _invalid(
                rel, f"{where}.clips: {example.instrument} needs exactly {want} clip(s)"
            )
        problem = instrument.answer_problem(example.answer)
        if problem:
            raise _invalid(rel, f"{where}.answer.{problem}")
    _check_checks(test, instruments, rel)

    for name, instrument in instruments.items():
        if instrument.draft:
            log.warning("draft_instrument: %s", name)
    return test


def _check_checks(test: TestConfig, instruments: dict[str, InstrumentDef], rel: str) -> None:
    """Perception checks (story 3.2; ``kind: screening`` only, enforced by the model).

    The Instrument is one of the Test's and the Item one of its Items (a pairwise Item for
    a pairwise Instrument); the Clips are among the Test's ``clips``. A pairwise Instrument
    needs 2 Clips; a pair check (pairwise, or a single-Clip Instrument given 2 Clips) needs
    ``expected`` to be one of its Clips (and, single-Clip, a Likert Item of at least 3
    points, compared strictly); a low-level check (1 Clip) needs a non-free-text Item and
    ``expected`` a valid answer to it. The same
    check (instrument, item, Clip set) may appear only once: a repeat is a duplicate, a
    different ``expected`` a contradiction. Else ``config_invalid``.
    """
    targets = set(test.clips)
    seen: dict[tuple, tuple[int, Any]] = {}
    for i, check in enumerate(test.checks):
        where = f"checks.{i}"
        instrument = instruments.get(check.instrument)
        if instrument is None:
            raise _invalid(
                rel, f"{where}.instrument: {check.instrument!r} is not one of the Test's "
                "instruments",
            )
        item = next((it for it in instrument.items if it.id == check.item), None)
        if item is None:
            raise _invalid(
                rel, f"{where}.item: {check.item!r} is not an item of {check.instrument}"
            )
        if instrument.pairwise and item.type != "pairwise":
            raise _invalid(
                rel, f"{where}.item: pairwise Instrument {check.instrument} needs a pairwise "
                f"item, not {item.type}",
            )
        for j, clip in enumerate(check.clips):
            if clip not in targets:
                raise _invalid(
                    rel, f"{where}.clips.{j}: {clip} is not one of the Test's clips"
                )
        if instrument.pairwise and len(check.clips) != 2:
            raise _invalid(
                rel, f"{where}.clips: pairwise Instrument {check.instrument} needs 2 clips"
            )
        if len(check.clips) == 2:
            if check.expected not in check.clips:
                raise _invalid(
                    rel, f"{where}.expected: must be one of the check's clips "
                    f"({', '.join(check.clips)})",
                )
            if not instrument.pairwise and item.type != "likert":
                raise _invalid(
                    rel, f"{where}.item: a 2-clip check of a single-clip Instrument needs a "
                    f"likert item, not {item.type}",
                )
            if not instrument.pairwise and (item.points or 0) < 3:
                raise _invalid(
                    rel, f"{where}.item: a 2-clip check needs a likert item of at least 3 "
                    f"points ({check.item} has {item.points}: ties are expected)",
                )
        else:
            if item.type == "free_text":
                raise _invalid(
                    rel, f"{where}.item: {check.item} is a free_text item; a check needs a "
                    "likert or pairwise item",
                )
            problem = item.value_problem(check.expected, f"{where}.expected")
            if problem:
                raise _invalid(rel, problem)
        key = (check.instrument, check.item, frozenset(check.clips))
        if key in seen:
            first, expected = seen[key]
            what = "duplicates" if expected == check.expected else "contradicts"
            raise _invalid(
                rel, f"{where}: {what} checks.{first} (same instrument, item and clips)"
            )
        seen[key] = (i, check.expected)


def _is_self_report(study: Path, name: str) -> bool:
    try:
        return _load_instrument(study, name).self_report
    except ConsortiumError:
        return False


def _not_allowed(rel: str, i: int, name: str) -> ConsortiumError:
    return ConsortiumError(
        "instrument_not_allowed",
        f"instruments.{i}: {name!r} is a self-report screening Instrument; it is used only "
        "by consortium screen personas",
        path=rel,
    )


def load_fidelity_instruments(
    study_dir: Path | str, cfg: StudyConfig, *, warn_draft: bool = True
) -> list[InstrumentDef]:
    """The Persona-fidelity Instruments (story 3.1): BFI-10, then the NARS Instrument
    selected by ``screening.nars_instrument`` when the frame has NARS bands.

    Resolved like any Instrument (user folder first, then built-in), but not gated by
    ``study.yaml`` ``instruments``. The NARS Instrument must be ``self_report`` with every
    key of construct ``nars`` and at least one of the subscales s1, s2, s3 keyed
    (``config_invalid``, field ``screening.nars_instrument``; ``unknown_instrument`` when
    it does not resolve). Logs ``draft_instrument: <name>`` for a draft one unless
    ``warn_draft`` is false (``open`` only needs the fidelity stamp, story 3.3).
    """
    study = Path(study_dir)
    _check_no_shadowing(study)
    out = [_load_instrument(study, FIDELITY_BFI)]
    if cfg.personas.nars_bands:
        name = cfg.screening.nars_instrument
        field = "screening.nars_instrument"
        if not _resolvable(study, name):
            raise ConsortiumError(
                "unknown_instrument", f"{field}: {name!r} not found", path=STUDY_FILE
            )
        try:
            nars = _load_instrument(study, name, STUDY_FILE, field)
        except ConsortiumError as err:
            if err.code != "config_invalid":
                raise
            raise _invalid(STUDY_FILE, f"{field}: {name!r} ({err.path}): {err.message}") from err
        problem = _nars_problem(nars)
        if problem:
            raise _invalid(STUDY_FILE, f"{field}: {name!r} {problem}")
        shared = sorted({i.id for i in nars.items} & {i.id for i in out[0].items})
        if shared:
            raise _invalid(
                STUDY_FILE,
                f"{field}: {name!r} reuses item id(s) of {FIDELITY_BFI}: {', '.join(shared)}",
            )
        out.append(nars)
    for instrument in out:
        if not instrument.self_report:  # a built-in edited into an invalid state
            raise _invalid(STUDY_FILE, f"{instrument.name!r} is not a self_report Instrument")
        if instrument.draft and warn_draft:
            log.warning("draft_instrument: %s", instrument.name)
    return out


def _nars_problem(instrument: InstrumentDef) -> str | None:
    if not instrument.self_report:
        return "must be a self_report Instrument"
    keys = instrument.keys or {}
    for item in instrument.items:
        key = keys.get(item.id)
        if key is None:
            return f"item {item.id} has no key"
        if key.construct != NARS_CONSTRUCT:
            return f"keys.{item.id}: construct must be nars, not {key.construct}"
    if not any(k.subscale in NARS_SUBSCALES for k in keys.values()):
        return "must key at least one of the subscales s1, s2, s3"
    return None


def peek_test(path: Path | str) -> dict[str, Any]:
    """The raw top-level mapping of a Test file, unvalidated; ``{}`` if it cannot be read.

    For checks that must run before schema validation (``push test``: the name
    rule and the pairing plan). ``load_test`` reports every read or YAML error.
    """
    try:
        data = _safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def load_prices(study_dir: Path | str) -> PricesConfig:
    """Load and validate ``<study>/prices.yaml``; its Model ids must equal ``study.yaml``'s."""
    study_dir = Path(study_dir)
    prices = _parse(_read_file(study_dir / PRICES_FILE, PRICES_FILE), PRICES_FILE, PricesConfig)
    study_ids = [m.id for m in load_study(study_dir).models]
    for model_id in study_ids:
        if model_id not in prices.models:
            raise _invalid(
                PRICES_FILE, f"models.{model_id}: field required (a Model in {STUDY_FILE})"
            )
    for model_id in prices.models:
        if model_id not in study_ids:
            raise _invalid(PRICES_FILE, f"models.{model_id}: not a Model id in {STUDY_FILE}")
    return prices


# --------------------------------------------------------------------------- Personas


def load_card_wording(cfg: StudyConfig | None = None) -> CardWording:
    """Load the approved Persona card wording from the package ``templates/``.

    With ``cfg``, also checks there is a NARS sentence for every band in the frame
    (``config_invalid``, field ``nars.<band>``) and a phrase for every quota level
    (field ``level_phrases.<attribute>.<level>``).
    """
    try:
        text = _card_wording_bytes().decode("utf-8")
    except UnicodeDecodeError as err:
        raise _invalid(_CARD_WORDING_REL, f"cannot read file: {err}") from err
    wording = _parse(text, _CARD_WORDING_REL, CardWording)
    if cfg is not None:
        for band in cfg.personas.nars_bands:
            if band not in wording.nars:
                raise _invalid(
                    _CARD_WORDING_REL, f"nars.{band}: no card sentence for this NARS band"
                )
        for attr in QUOTA_ATTRIBUTES:
            phrases = getattr(wording.level_phrases, attr)
            for level in getattr(cfg.personas.quotas, attr):
                if level not in phrases:
                    raise _invalid(
                        _CARD_WORDING_REL,
                        f"level_phrases.{attr}.{level}: no card phrase for this quota level",
                    )
    return wording


_CARD_WORDING_REL = f"<built-in>/templates/{CARD_WORDING_FILE}"


def _card_wording_bytes() -> bytes:
    entry = resources.files(_TEMPLATES_PACKAGE).joinpath(*CARD_WORDING_FILE.split("/"))
    try:
        return entry.read_bytes()
    except OSError as err:
        raise _invalid(_CARD_WORDING_REL, f"cannot read file: {err}") from err


def card_wording_sha256() -> str:
    """SHA-256 (lowercase hex) of the packaged ``wording.yaml`` bytes, for provenance."""
    return hashlib.sha256(_card_wording_bytes()).hexdigest()


def read_card(study_dir: Path | str, persona_id: str) -> str:
    """The stored card text of ``persona_id`` exactly as written (``panel_invalid``)."""
    rel = f"{PERSONAS_DIR}/{persona_id}.md"
    try:
        return (Path(study_dir) / rel).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ConsortiumError("panel_invalid", f"cannot read {rel}: {err}", path=rel) from err


def load_personas(study_dir: Path | str) -> list[Persona]:
    """The generated Persona pool from ``panel/personas/index.json``, in Persona order.

    Raises ``panel_missing`` when the index is absent (run ``personas generate``),
    ``panel_invalid`` when it cannot be read as a list of Personas.
    """
    path = Path(study_dir) / PERSONAS_INDEX
    try:
        raw = path.read_bytes()
    except FileNotFoundError as err:
        raise ConsortiumError(
            "panel_missing",
            f"{PERSONAS_INDEX} not found (run consortium personas generate)",
            path=PERSONAS_INDEX,
        ) from err
    except OSError as err:
        raise ConsortiumError("panel_invalid", f"cannot read: {err}", path=PERSONAS_INDEX) from err
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, list) or not data:
            raise ValueError("must be a non-empty JSON list of Personas")
        personas = [Persona.model_validate(item) for item in data]
        ids = [p.id for p in personas]
        if ids != [f"p{i}" for i in range(1, len(ids) + 1)]:
            raise ValueError("Persona ids must be p1 ... pN, unique and in order")
        if len({p.nars is None for p in personas}) > 1:
            raise ValueError("nars must be null for every Persona or for none")
    except ValueError as err:  # includes ValidationError and UnicodeDecodeError
        raise ConsortiumError(
            "panel_invalid", f"{PERSONAS_INDEX}: {err}", path=PERSONAS_INDEX
        ) from err
    for p in personas:
        if not (Path(study_dir) / PERSONAS_DIR / f"{p.id}.md").is_file():
            raise ConsortiumError(
                "panel_invalid", f"{PERSONAS_DIR}/{p.id}.md is missing", path=PERSONAS_INDEX
            )
    return personas
