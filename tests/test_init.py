"""consortium init: every row of the story 1.1 I/O matrix, through the CLI."""

from pathlib import Path

from typer.testing import CliRunner

from consortium.cli import app

runner = CliRunner()

TEMPLATE_FILES = {"study.yaml", "protocol.md", "prices.yaml", "tests/example.yaml"}


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_init_new_folder(tmp_path: Path) -> None:
    target = tmp_path / "s1"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == str(target)
    assert _files(target) == TEMPLATE_FILES
    assert "id: m1" in (target / "study.yaml").read_text()
    assert "provider: fake" in (target / "study.yaml").read_text()


def test_init_empty_folder(tmp_path: Path) -> None:
    target = tmp_path / "s1"
    target.mkdir()
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == str(target)
    assert _files(target) == TEMPLATE_FILES


def test_init_non_empty_folder(tmp_path: Path) -> None:
    target = tmp_path / "s1"
    target.mkdir()
    (target / "notes.txt").write_text("keep me")
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == f"study_exists: {target} is not empty"
    assert _files(target) == {"notes.txt"}
    assert (target / "notes.txt").read_text() == "keep me"


def test_init_path_is_file(tmp_path: Path) -> None:
    target = tmp_path / "s1"
    target.write_text("a file")
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == f"study_exists: {target} exists and is not an empty directory"
    assert target.is_file()
    assert target.read_text() == "a file"


def test_help_lists_init() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.stdout


def test_init_missing_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "s1"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.output
    assert _files(target) == TEMPLATE_FILES


def test_init_parent_is_file(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.write_text("a file")
    result = runner.invoke(app, ["init", str(parent / "s1")])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("study_create_failed: ")
    assert len(result.stderr.strip().splitlines()) == 1
    assert parent.read_text() == "a file"


def test_verbose_logs_to_stderr_only(tmp_path: Path) -> None:
    target = tmp_path / "s1"
    result = runner.invoke(app, ["-v", "init", str(target)])
    assert result.exit_code == 0, result.output
    assert result.stdout == f"{target}\n"
    assert "initialized study" in result.stderr


def test_without_verbose_no_info_log(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path / "s1")])
    assert result.exit_code == 0, result.output
    assert "initialized study" not in result.stderr
