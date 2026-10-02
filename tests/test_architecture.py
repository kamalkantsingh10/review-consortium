"""AD-1 and AD-2 made executable: import rules must fail loudly."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
PACKAGE = SRC / "consortium"

BLINDING = "consortium.board.blinding"
BLINDING_ALLOWED = ("consortium.stages.push", "consortium.stages.export", "consortium.board")


def _lint_imports(cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    exe = Path(sys.executable).with_name("lint-imports")
    return subprocess.run(
        [str(exe), "--no-cache"], cwd=cwd, env=env, capture_output=True, text=True
    )


def _module_name(path: Path, src_root: Path) -> str:
    parts = list(path.relative_to(src_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_modules(tree: ast.AST, module: str, is_package: bool) -> set[str]:
    """Every module name an AST imports, including ``from pkg import submodule`` forms."""
    found: set[str] = set()
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                base = ".".join(base_parts)
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            found.add(target)
            found.update(f"{target}.{alias.name}" for alias in node.names)
    return found


def _is_allowed(module: str) -> bool:
    return any(module == a or module.startswith(a + ".") for a in BLINDING_ALLOWED)


def blinding_violations(src_root: Path) -> list[str]:
    """Modules under ``src_root/consortium`` that import the blinding module but may not."""
    violations = []
    for path in sorted((src_root / "consortium").rglob("*.py")):
        module = _module_name(path, src_root)
        if _is_allowed(module):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        imported = _imported_modules(tree, module, path.name == "__init__.py")
        if any(m == BLINDING or m.startswith(BLINDING + ".") for m in imported):
            violations.append(module)
    return violations


def test_import_linter_contracts_pass() -> None:
    result = _lint_imports(REPO)
    assert result.returncode == 0, result.stdout + result.stderr


def test_import_linter_catches_core_importing_stages(tmp_path: Path) -> None:
    src = tmp_path / "src"
    shutil.copytree(PACKAGE, src / "consortium", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(REPO / "pyproject.toml", tmp_path / "pyproject.toml")
    (src / "consortium" / "core" / "bad.py").write_text("import consortium.stages.init\n")
    env = {**os.environ, "PYTHONPATH": str(src)}
    result = _lint_imports(tmp_path, env)
    assert result.returncode != 0, result.stdout
    assert "consortium.core.bad" in result.stdout


def test_import_linter_catches_stage_importing_stage(tmp_path: Path) -> None:
    src = tmp_path / "src"
    shutil.copytree(PACKAGE, src / "consortium", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(REPO / "pyproject.toml", tmp_path / "pyproject.toml")
    (src / "consortium" / "stages" / "status.py").write_text("import consortium.stages.init\n")
    env = {**os.environ, "PYTHONPATH": str(src)}
    result = _lint_imports(tmp_path, env)
    assert result.returncode != 0, result.stdout
    assert "consortium.stages.status" in result.stdout


def test_only_allowed_modules_import_blinding() -> None:
    assert (PACKAGE / "board" / "blinding.py").is_file()
    assert blinding_violations(SRC) == []


def test_blinding_check_detects_forbidden_import(tmp_path: Path) -> None:
    src = tmp_path / "src"
    shutil.copytree(PACKAGE, src / "consortium", ignore=shutil.ignore_patterns("__pycache__"))
    stages = src / "consortium" / "stages"
    (stages / "status.py").write_text("from consortium.board import blinding\n")
    (stages / "open.py").write_text("import consortium.board.blinding\n")
    (src / "consortium" / "engine" / "run.py").write_text("from ..board.blinding import x\n")
    (stages / "push.py").write_text("from consortium.board import blinding\n")
    (stages / "export.py").write_text("from consortium.board.blinding import read_key\n")
    new = set(blinding_violations(src)) - set(blinding_violations(SRC))
    assert new == {"consortium.engine.run", "consortium.stages.open", "consortium.stages.status"}
