"""Thin Typer driving adapter over the stages."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from typer.core import TyperGroup

from consortium.core.errors import ConsortiumError
from consortium.stages.init import init_study


class _ConsortiumGroup(TyperGroup):
    """Maps ConsortiumError from any command to ``code: message`` on stderr, exit 1."""

    def invoke(self, ctx: typer.Context) -> object:
        try:
            return super().invoke(ctx)
        except ConsortiumError as err:
            typer.echo(f"{err.code}: {err.message}", err=True)
            raise typer.Exit(code=1) from err


app = typer.Typer(
    name="consortium",
    cls=_ConsortiumGroup,
    help="Run blinded rating studies with LLM rater panels.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log debug output.")] = False,
) -> None:
    """Configure logging to stderr; data goes to stdout."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Folder to create; must be absent or empty.")],
) -> None:
    """Create a new Study folder from the Fake-rater template."""
    typer.echo(str(init_study(path)))
