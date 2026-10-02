"""Thin Typer driving adapter over the stages."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from typer.core import TyperGroup

from consortium.core.errors import ConsortiumError
from consortium.stages import personas as personas_stage
from consortium.stages.export import export_test
from consortium.stages.init import init_study
from consortium.stages.open import open_test, usd
from consortium.stages.push import push_clip, push_test
from consortium.stages.screen import (
    ScreenSummary,
    abandon_models,
    abandon_personas,
    screen_models,
    screen_personas,
)
from consortium.stages.status import format_table, status


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
    resume: Annotated[
        bool, typer.Option("--resume", help="Continue a stopped Run of TEST at Trial level.")
    ] = False,
    study: StudyOption = Path("."),
) -> None:
    """Plan, render and run TEST's Trials (--dry-run: print counts and the cost estimate,
    change nothing; --resume: continue a stopped or paused Run without re-sending completed
    Trials; --ceiling: set the Study's cost ceiling in USD)."""
    announced: list[str] = []
    # Warnings (unscreened_pilot) are held until the command gets past its refusals: shown
    # just before the confirmation prompt, else with the summary under the lease (--yes),
    # so a refusal is always the first stderr line.
    warnings: list[str] = []

    def flush() -> None:
        for line in warnings:
            typer.echo(line, err=True)
        warnings.clear()

    def announce(lines: list[str]) -> None:  # printed before confirmation and dispatch
        if announced:
            flush()  # the second announcement comes under the lease, before dispatch
        for line in lines:
            typer.echo(line)
        announced.extend(lines)

    def confirm(prompt: str) -> bool:
        if _stdin_is_tty():
            flush()
        return _confirm(prompt)

    summary = open_test(
        study, test, dry_run=dry_run, yes=yes, ceiling=ceiling, resume=resume,
        confirm=confirm, announce=announce, warn=warnings.append,
    )
    flush()
    pending = list(announced)
    for line in summary.lines():
        if line in pending:
            pending.remove(line)
        else:
            typer.echo(line)
    if summary.dry_run:
        return
    if (
        summary.ceiling is not None
        and summary.committed is not None
        and summary.committed > summary.ceiling
    ):
        typer.echo(
            f"ceiling_overshoot: committed {usd(summary.committed)} > ceiling "
            f"{usd(summary.ceiling)} (actual cost exceeded the estimate)",
            err=True,
        )
    if summary.not_valid:
        typer.echo(f"warning: {summary.not_valid} Trials did not end valid", err=True)
    if summary.paused == "ceiling":
        raise ConsortiumError(
            "ceiling_reached",
            f"Run paused at the ceiling; continue with "
            f"`consortium open {test} --resume --ceiling <higher USD>`",
        )


screen_app = typer.Typer(
    help="Screen the Panel before it rates (Persona fidelity, Model perception)."
)
app.add_typer(screen_app, name="screen")


@screen_app.command("personas")
def screen_personas_cmd(
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
    ceiling: Annotated[
        str | None, typer.Option("--ceiling", help="Cost ceiling in USD, e.g. 5.00.")
    ] = None,
    resume: Annotated[
        bool, typer.Option("--resume", help="Continue the open fidelity screening run.")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Plan and render the next run, print counts and the "
                     "cost estimate, write nothing."),
    ] = False,
    abandon: Annotated[
        bool,
        typer.Option("--abandon", help="Mark the open fidelity run abandoned (no results; "
                     "its Trials and Archive are kept)."),
    ] = False,
    study: StudyOption = Path("."),
) -> None:
    """Run a Persona-fidelity screening run s<n>: every Agent answers the clip-less BFI-10
    (and NARS) self-report Instruments; each Agent passes when its answers match its
    Persona card (thresholds.persona_fidelity_min). Prints the run summary and the
    pass/fail counts per Model."""
    if sum((resume, dry_run, abandon)) > 1:
        raise ConsortiumError(
            "bad_option", "--resume, --dry-run and --abandon cannot be combined"
        )
    if abandon:
        typer.echo(f"screening run: {abandon_personas(study)} (fidelity) abandoned")
        return
    if dry_run:
        dry = screen_personas(study, ceiling=ceiling, dry_run=True)
        for line in [*dry.run.lines(), *dry.result_lines()]:
            typer.echo(line)
        return
    announced: list[str] = []

    def announce(lines: list[str]) -> None:  # printed before confirmation and dispatch
        for line in lines:
            typer.echo(line)
        announced.extend(lines)

    summary = screen_personas(
        study, yes=yes, ceiling=ceiling, resume=resume, confirm=_confirm, announce=announce,
    )
    _screen_output(summary, announced, "consortium screen personas --resume")


def _screen_output(summary: ScreenSummary, announced: list[str], resume_cmd: str) -> None:
    """The runner summary (minus announced lines), the results and the warnings."""
    run = summary.run
    pending = list(announced)
    for line in run.lines():
        if line in pending:
            pending.remove(line)
        else:
            typer.echo(line)
    for line in summary.result_lines():
        typer.echo(line)
    if run.ceiling is not None and run.committed is not None and run.committed > run.ceiling:
        typer.echo(
            f"ceiling_overshoot: committed {usd(run.committed)} > ceiling "
            f"{usd(run.ceiling)} (actual cost exceeded the estimate)",
            err=True,
        )
    if run.not_valid:
        typer.echo(f"warning: {run.not_valid} Trials did not end valid", err=True)
    if run.paused == "ceiling" and not summary.complete:
        raise ConsortiumError(
            "ceiling_reached",
            "Screening run paused at the ceiling; continue with "
            f"`{resume_cmd} --ceiling <higher USD>`",
        )


@screen_app.command("models")
def screen_models_cmd(
    test: Annotated[str, typer.Argument(help="Name of a registered kind: screening Test.")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
    ceiling: Annotated[
        str | None, typer.Option("--ceiling", help="Cost ceiling in USD, e.g. 5.00.")
    ] = None,
    resume: Annotated[
        bool, typer.Option("--resume", help="Continue the open perception run of TEST.")
    ] = False,
    abandon: Annotated[
        bool,
        typer.Option("--abandon", help="Mark the open perception run of TEST abandoned (no "
                     "results; its Trials and Archive are kept)."),
    ] = False,
    study: StudyOption = Path("."),
) -> None:
    """Run a perception screening run s<n> of the screening Test TEST: only the Trials its
    checks need, on every Model of TEST, answered by the neutral Persona p0. Each Model x
    Instrument passes when its pass ratio is at least thresholds.perception_min. Prints
    the run summary, the results and the coverage gaps (also coverage_gap on stderr)."""
    if resume and abandon:
        raise ConsortiumError("bad_option", "--resume and --abandon cannot be combined")
    if abandon:
        typer.echo(f"screening run: {abandon_models(study, test)} (perception) abandoned")
        return
    announced: list[str] = []

    def announce(lines: list[str]) -> None:  # printed before confirmation and dispatch
        for line in lines:
            typer.echo(line)
        announced.extend(lines)

    summary = screen_models(
        study, test, yes=yes, ceiling=ceiling, resume=resume, confirm=_confirm,
        announce=announce, warn=lambda line: typer.echo(line, err=True),
    )
    _screen_output(summary, announced, f"consortium screen models {test} --resume")


@app.command("status")
def status_cmd(
    test: Annotated[
        str | None, typer.Argument(help="Show only this registered Test (default: all).")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print {rows, committed, ceiling, state} as JSON.")
    ] = False,
    study: StudyOption = Path("."),
) -> None:
    """Show Trial counts by state, retries, invalid rate and cost per Test, Model and Agent,
    plus committed spend, the ceiling and the paused state. Read-only; safe during a Run."""
    report = status(study, test)
    if as_json:
        typer.echo(json.dumps(report.to_json(), indent=2))
    else:
        typer.echo(format_table(report.rows, report.footer()))


@app.command("export")
def export_cmd(
    test: Annotated[str, typer.Argument(help="Name of a registered, finished Test.")],
    study: StudyOption = Path("."),
) -> None:
    """Write exports/TEST.csv: one row per Item per Trial, Conditions joined from
    blinding_key.csv. Refuses while any Trial is planned or sent. Read-only; no lease."""
    typer.echo(str(export_test(study, test)))
