"""Use case: generate the seeded Persona Panel into ``panel/personas/``.

Writes one behaviour-only card per Persona (``p<n>.md``), ``index.json`` (the
structured labels as canonical JSON) and ``meta.json`` (provenance). The set is
built in a temporary folder under ``panel/`` and renamed into place, so a failure
leaves no partial Panel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from consortium.board.db import DB_FILE, read_only
from consortium.board.screening import any_results
from consortium.board.trials import any_panel_trials
from consortium.config.load import (
    PERSONAS_DIR,
    card_wording_sha256,
    load_card_wording,
    load_study,
)
from consortium.config.models import StudyConfig
from consortium.config.panel_files import (
    INDEX_FILE,
    META_FILE,
    exists,
    fsync_dir,
    move_into_place,
    sweep_stale,
    write_file,
)
from consortium.core.errors import ConsortiumError
from consortium.core.perception import NEUTRAL_PERSONA_ID
from consortium.core.personas import (
    GENERATOR_VERSION,
    Persona,
    design_profiles,
    generate_personas,
    render_card,
)

log = logging.getLogger(__name__)

def canonical_index(personas: list[Persona]) -> bytes:
    """Canonical JSON (sorted keys, UTF-8, no whitespace) of the Persona list."""
    data = [p.model_dump(mode="json") for p in personas]
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _meta(cfg: StudyConfig) -> bytes:
    frame = json.dumps(
        cfg.personas.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    big_five = cfg.personas.big_five
    _, design = design_profiles(big_five.fraction, big_five.replicates)
    meta = {
        "design": design.as_json(),
        "frame_sha256": hashlib.sha256(frame).hexdigest(),
        "generator_version": GENERATOR_VERSION,
        "seed": cfg.seed,
        "wording_sha256": card_wording_sha256(),
    }
    return json.dumps(meta, sort_keys=True, separators=(",", ":")).encode("utf-8")


def generate(study_dir: Path | str, force: bool = False) -> list[Persona]:
    """Generate the Panel; returns the Personas written.

    Refuses with ``panel_in_use`` (with or without ``force``) when ``board.db`` holds
    any Trial: a new Panel would re-label the Personas those Trials reference. Then
    refuses with ``panel_exists`` when ``panel/personas`` is non-empty, unless
    ``force``, in which case the old set is replaced. Nothing is written on refusal;
    ``board.db`` is only read (no lease, no migration).
    """
    study_dir = Path(study_dir)
    cfg = load_study(study_dir)
    wording = load_card_wording(cfg)
    target = study_dir / PERSONAS_DIR
    panel = target.parent
    _refuse_if_in_use(study_dir)
    sweep_stale(target)
    if exists(target) and not force:
        raise _panel_exists()

    personas = generate_personas(cfg)
    cards = {f"{p.id}.md": render_card(p, wording).encode("utf-8") for p in personas}
    meta = _meta(cfg)
    try:
        panel.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".personas-new-", dir=panel))
        try:
            for name, data in cards.items():
                write_file(tmp / name, data)
            write_file(tmp / INDEX_FILE, canonical_index(personas))
            write_file(tmp / META_FILE, meta)
            os.chmod(tmp, 0o755)
            fsync_dir(tmp)
            _refuse_if_in_use(study_dir)  # an ``open`` may have planned Trials meanwhile
            if force:
                _swap_into_place(tmp, target, study_dir)
            else:
                _move_into_place(tmp, target)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        fsync_dir(panel)
    except OSError as err:
        raise ConsortiumError(
            "personas_failed", f"could not write {PERSONAS_DIR}: {err}", path=PERSONAS_DIR
        ) from err
    log.info("wrote %d personas to %s", len(personas), target)
    return personas


def _refuse_if_in_use(study_dir: Path) -> None:
    """``panel_in_use`` when ``board.db`` holds any Trial of a Panel Persona, which includes
    every fidelity screening Trial, or any fidelity result, which includes those a
    ``panel copy`` imported without Trials (story 3.4); any layout version, never
    migrated, no lease. Perception screening's neutral Persona ``p0`` and perception
    results do not count (story 3.2). ``board_unreadable`` / ``board_busy`` fail closed."""
    if read_only(study_dir, lambda conn: any_panel_trials(conn, NEUTRAL_PERSONA_ID)
                 or any_results(conn, "fidelity"), allow_older=True):
        raise ConsortiumError(
            "panel_in_use",
            "board.db already holds Trials or screening results that reference this Panel; "
            "regenerating would re-label them (start a new Study folder to change the Panel)",
            path=DB_FILE,
        )


def _panel_exists() -> ConsortiumError:
    return ConsortiumError(
        "panel_exists", f"{PERSONAS_DIR} already exists (use --force)", path=PERSONAS_DIR
    )


def _move_into_place(new: Path, target: Path) -> None:
    """Rename ``new`` to ``target`` without --force (``panel_exists`` when it is taken)."""
    try:
        move_into_place(new, target)
    except FileExistsError as err:
        raise _panel_exists() from err


def _swap_into_place(new: Path, target: Path, study_dir: Path) -> None:
    """--force: move the old ``target`` aside, rename ``new`` in, then delete the old copy.

    If the second rename fails the old Panel is renamed back; if that also fails,
    ``personas_failed`` names where the old Panel was preserved.
    """
    if not (target.exists() or target.is_symlink()):
        os.rename(new, target)
        return
    old = Path(tempfile.mkdtemp(prefix=".personas-old-", dir=target.parent))
    aside = old / "personas"
    os.rename(target, aside)
    try:
        os.rename(new, target)
    except OSError as err:
        try:
            os.rename(aside, target)
        except OSError as rollback:
            kept = os.path.relpath(aside, study_dir)
            raise ConsortiumError(
                "personas_failed",
                f"could not write {PERSONAS_DIR} ({err}) nor restore the old Panel "
                f"({rollback}); it is preserved at {kept}",
                path=kept,
            ) from rollback
        shutil.rmtree(old, ignore_errors=True)
        raise
    try:
        shutil.rmtree(old)
    except OSError as err:
        log.warning("could not remove the old Panel copy %s: %s", old, err)
