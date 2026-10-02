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


def load_test(path: Path | str, study_dir: Path | str | None = None) -> TestConfig:
    """Load and validate a Test YAML file.

    ``study_dir`` defaults to the parent of the file's folder (``<study>/tests/x.yaml``).
    Checks that every Instrument resolves and is enabled in ``study.yaml``, and that
    every listed Model id exists. Logs ``draft_instrument: <name>`` to stderr for each
    draft Instrument the Test lists.
    """
    path = Path(path)
    study = Path(study_dir) if study_dir is not None else path.resolve().parent.parent
    rel = _rel(path, study)
    test = _parse(_read_file(path, rel), rel, TestConfig)
    if test.test != path.stem:
        raise _invalid(rel, f"test: {test.test!r} must match the file name {path.stem!r}")

    cfg = load_study(study)
    for i, name in enumerate(test.instruments):
        if not _resolvable(study, name):
            raise ConsortiumError(
                "unknown_instrument", f"instruments.{i}: {name!r} not found", path=rel
            )
        if name not in cfg.instruments:
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

    for name, instrument in instruments.items():
        if instrument.draft:
            log.warning("draft_instrument: %s", name)
    return test


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
