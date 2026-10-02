"""Use case: generate the seeded Persona Panel into ``panel/personas/``.

Writes one behaviour-only card per Persona (``p<n>.md``), ``index.json`` (the
structured labels as canonical JSON) and ``meta.json`` (provenance). The set is
built in a temporary folder under ``panel/`` and renamed into place, so a failure
leaves no partial Panel.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from consortium.config.load import (
    PERSONAS_DIR,
    card_wording_sha256,
    load_card_wording,
    load_study,
)
from consortium.config.models import StudyConfig
from consortium.core.errors import ConsortiumError
from consortium.core.personas import (
    GENERATOR_VERSION,
    Persona,
    generate_personas,
    render_card,
)

log = logging.getLogger(__name__)

INDEX_FILE = "index.json"
META_FILE = "meta.json"


def canonical_index(personas: list[Persona]) -> bytes:
    """Canonical JSON (sorted keys, UTF-8, no whitespace) of the Persona list."""
    data = [p.model_dump(mode="json") for p in personas]
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _write(path: Path, data: bytes) -> None:
    with path.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _exists(target: Path) -> bool:
    if target.is_dir():
        return any(target.iterdir())
    return target.exists() or target.is_symlink()


def _sweep_stale(target: Path) -> None:
    """Remove ``.personas-*`` work folders left behind by a crashed run.

    ``.personas-old-*`` copies are kept while ``panel/personas`` is absent: one may
    hold the only copy of a Panel whose restore failed.
    """
    panel = target.parent
    if not panel.is_dir():
        return
    keep_old = not (target.exists() or target.is_symlink())
    for entry in panel.glob(".personas-*"):
        if keep_old and entry.name.startswith(".personas-old-"):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)


def _meta(cfg: StudyConfig) -> bytes:
    frame = json.dumps(
        cfg.personas.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    meta = {
        "frame_sha256": hashlib.sha256(frame).hexdigest(),
        "generator_version": GENERATOR_VERSION,
        "seed": cfg.seed,
        "wording_sha256": card_wording_sha256(),
    }
    return json.dumps(meta, sort_keys=True, separators=(",", ":")).encode("utf-8")


def generate(study_dir: Path | str, force: bool = False) -> list[Persona]:
    """Generate the Panel; returns the Personas written.

    Refuses with ``panel_exists`` when ``panel/personas`` is non-empty, unless ``force``,
    in which case the old set is replaced.
    """
    study_dir = Path(study_dir)
    cfg = load_study(study_dir)
    wording = load_card_wording(cfg)
    target = study_dir / PERSONAS_DIR
    panel = target.parent
    _sweep_stale(target)
    if _exists(target) and not force:
        raise _panel_exists()

    personas = generate_personas(cfg)
    cards = {f"{p.id}.md": render_card(p, wording).encode("utf-8") for p in personas}
    meta = _meta(cfg)
    try:
        panel.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".personas-new-", dir=panel))
        try:
            for name, data in cards.items():
                _write(tmp / name, data)
            _write(tmp / INDEX_FILE, canonical_index(personas))
            _write(tmp / META_FILE, meta)
            os.chmod(tmp, 0o755)
            _fsync_dir(tmp)
            if force:
                _swap_into_place(tmp, target, study_dir)
            else:
                _move_into_place(tmp, target)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        _fsync_dir(panel)
    except OSError as err:
        raise ConsortiumError(
            "personas_failed", f"could not write {PERSONAS_DIR}: {err}", path=PERSONAS_DIR
        ) from err
    log.info("wrote %d personas to %s", len(personas), target)
    return personas


def _panel_exists() -> ConsortiumError:
    return ConsortiumError(
        "panel_exists", f"{PERSONAS_DIR} already exists (use --force)", path=PERSONAS_DIR
    )


def _move_into_place(new: Path, target: Path) -> None:
    """Rename ``new`` to ``target`` without --force.

    POSIX ``rename`` replaces only an absent target or an empty directory, so a
    Panel that appeared since the first check is never overwritten.
    """
    try:
        os.rename(new, target)
    except OSError as err:
        if err.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR, errno.EISDIR):
            raise _panel_exists() from err
        raise


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
