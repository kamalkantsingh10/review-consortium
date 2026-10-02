"""Thin Typer driving adapter over the stages."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from typer.core import TyperGroup

from consortium.core.errors import ConsortiumError
from consortium.stages import personas as personas_stage
from consortium.stages.init import init_study
from consortium.stages.open import open_test
from consortium.stages.push import push_clip, push_test


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


push_app = typer.Typer(help="Push Clips and Tests into the Study.")
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


@push_app.command("test")
def push_test_cmd(
    file: Annotated[Path, typer.Argument(help="Test YAML file (test: <name>).")],
    study: StudyOption = Path("."),
) -> None:
    """Validate FILE, register it as tests/<name>.yaml and print the Test name."""
    typer.echo(push_test(study, file))


personas_app = typer.Typer(help="Generate the seeded Persona Panel.")
app.add_typer(personas_app, name="personas")


@personas_app.command("generate")
def personas_generate_cmd(
    study: StudyOption = Path("."),
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing panel/personas.")
    ] = False,
) -> None:
    """Write panel/personas/p<n>.md cards and index.json from study.yaml's seed and frame."""
    personas = personas_stage.generate(study, force=force)
    typer.echo(f"{len(personas)} personas -> {personas_stage.PERSONAS_DIR}")


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _confirm(prompt: str) -> bool:
    """Ask on the terminal (default no); ``confirmation_required`` when stdin is not a TTY."""
    if not _stdin_is_tty():
        raise ConsortiumError(
            "confirmation_required", "stdin is not a terminal; pass --yes to confirm"
        )
    try:
        return typer.confirm(prompt, default=False, err=True)
    except typer.Abort:  # EOF or Ctrl-C at the prompt
        typer.echo("", err=True)
        return False


@app.command("open")
def open_cmd(
    test: Annotated[str, typer.Argument(help="Name of a registered Test.")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Plan and render every Trial, print counts, write nothing."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
    ceiling: Annotated[
        str | None, typer.Option("--ceiling", help="Cost ceiling in USD, e.g. 5.00.")
    ] = None,
    resume: Annotated[bool, typer.Option("--resume", help="Resume an open Test.")] = False,
    study: StudyOption = Path("."),
) -> None:
    """Plan, render and run TEST's Trials (with --dry-run: print counts, change nothing)."""
    announced: list[str] = []

    def announce(lines: list[str]) -> None:  # printed before dispatch starts
        for line in lines:
            typer.echo(line)
        announced.extend(lines)

    summary = open_test(
        study, test, dry_run=dry_run, yes=yes, ceiling=ceiling, resume=resume,
        confirm=_confirm, announce=announce,
    )
    for line in summary.lines()[len(announced):]:
        typer.echo(line)
    if summary.not_valid:
        typer.echo(f"warning: {summary.not_valid} Trials did not end valid", err=True)
