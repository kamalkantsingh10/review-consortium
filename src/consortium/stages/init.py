"""Use case: create a new Study folder from the packaged template."""

from __future__ import annotations

import logging
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from consortium.core.errors import ConsortiumError

log = logging.getLogger(__name__)

_TEMPLATE_PACKAGE = "consortium.templates"
_TEMPLATE_DIR = "study"


def init_study(path: Path | str) -> Path:
    """Copy the Study template into ``path`` and return the created folder.

    ``path`` must not exist or must be an empty directory. Otherwise
    ``ConsortiumError("study_exists")`` is raised and nothing is written.
    """
    target = Path(path)
    try:
        if target.exists() and not target.is_dir():
            raise ConsortiumError(
                "study_exists",
                f"{target} exists and is not an empty directory",
                path=str(target),
            )
        if target.exists() and any(target.iterdir()):
            raise ConsortiumError("study_exists", f"{target} is not empty", path=str(target))

        template = resources.files(_TEMPLATE_PACKAGE) / _TEMPLATE_DIR
        target.mkdir(parents=True, exist_ok=True)
        _copy_tree(template, target)
    except OSError as err:
        raise ConsortiumError("study_create_failed", str(err), path=str(target)) from err
    log.info("initialized study at %s", target)
    return target


def _copy_tree(src: Traversable, dst: Path) -> None:
    for entry in src.iterdir():
        if entry.name == "__pycache__":
            continue
        out = dst / entry.name
        if entry.is_dir():
            out.mkdir()
            _copy_tree(entry, out)
        else:
            out.write_bytes(entry.read_bytes())
