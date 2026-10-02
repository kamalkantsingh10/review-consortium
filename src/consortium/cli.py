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
from consortium.stages.push import push_clip


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


push_app = typer.Typer(help="Push Clips (and, from story 1.5, Tests) into the Study.")
app.add_typer(push_app, name="push")

StudyOption = Annotated[
    Path, typer.Option("--study", help="Study folder (default: the current directory).")
]


@push_app.command("clip")
def push_clip_cmd(
    file: Annotated[Path, typer.Argument(help="Video file with audio to ingest.")],
    condition: Annotated[
        list[str] | None,
        typer.Option(
            "--condition", "-c", help="factor=level; repeat for each factor. Omit for none."
        ),
    ] = None,
    study: StudyOption = Path("."),
) -> None:
    """Re-encode FILE blind into clips/<clip_id>.mp4 and print the new Clip ID."""
    typer.echo(push_clip(study, file, condition or []))
