---
title: 'Story 1.1 — Project scaffold and consortium init'
type: 'feature'
created: '2026-10-02'
status: 'done'
baseline_commit: '9d9c26bd5b9ad78a4af5d7afc5d19b6e88684dbc'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-1-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The repo has no code yet. Every later story needs an installable `consortium` CLI with the architecture's layout, error and output conventions, enforced import rules, and a Study folder to work in.

**Approach:**
- Scaffold a uv package (Python ≥ 3.12) with the AD-1 module layout and a thin Typer CLI.
- Add one `init` command that copies a Fake-rater Study template into a new folder.
- Enforce the dependency direction and the blinding boundary with tests from day one.
- Start `docs/INTERFACE.md`.

## Boundaries & Constraints

**Always:**
- **Layout (AD-1).** Package `consortium` under `src/`, with subpackages `core`, `engine`, `stages`, `raters`, `board`, `archive`, `media`, `config`, `instruments`, `templates`.
- **Dependency direction:**
  - `cli` → `stages` → (`engine`, `core`, `board`, `media`, `config`, `archive`)
  - `engine` → (`core`, `board`, `archive`, `raters`)
  - `raters`, `board` and `config` → `core`
  - `core` imports only stdlib and pydantic
  - stages never import one another
- **Blinding boundary (AD-2).** `consortium.board.blinding` exists, as a stub only. The only modules allowed to import it are `consortium.stages.push`, `consortium.stages.export` and `consortium.board` itself.
- **Errors.** There is one `ConsortiumError(code, message, path=None)` in `core`. The CLI catches it, prints `code: message` to stderr and exits with code 1.
- **Output.** Data goes to stdout. Logs go to stderr through stdlib `logging`. No telemetry.
- **Pinned dependencies:** Typer 0.27, Pydantic 2.13, PyYAML 6. Dev dependencies: pytest 9.1, ruff 0.16, import-linter 2.15.

**Never:**
- Config schema validation (that is Story 1.2).
- `board.db` or any SQL.
- Any network access.
- Network-dependent tests.
- Speculative abstractions or empty base classes beyond the stubs listed below.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| New folder | `consortium init ./s1`, where `./s1` doesn't exist | Creates `s1/` with `study.yaml`, `protocol.md`, `prices.yaml`, `tests/example.yaml`, and prints the created path to stdout. Exit code 0. | N/A |
| Empty folder | `./s1` exists and is empty | Same as above | N/A |
| Non-empty folder | `./s1` contains any file | Nothing changes | stderr `study_exists: <path> is not empty`, exit code 1 |
| Path is a file | `./s1` is a regular file | Nothing changes | stderr `study_exists: ...`, exit code 1 |
| Help | `consortium --help` | Lists `init`, exit code 0 | N/A |

</frozen-after-approval>

## Code Map

- The repo is greenfield: no Python code exists yet. Planning lives under `_doc/`, and BMAD tooling under `.claude/` and `_bmad/`. Do not touch any of these.
- The spine's structural seed is in `_doc/planning-artifacts/architecture/architecture-review-consortium-2026-10-02/ARCHITECTURE-SPINE.md`. Use it for the source tree and Study folder layout.

## Tasks & Acceptance

**Execution:**
- [x] `pyproject.toml` -- Generate it with `uv init --package --name consortium` (uv's build backend; `requires-python >=3.12`). Then:
  - add the pinned dependencies and a dev group
  - set the console script `consortium = "consortium.cli:app"`
  - add the ruff config
  - add the import-linter config (root package `consortium`): a `layers` contract `cli : stages : engine : raters | board | archive | media | config : core`, and an `independence` contract over `consortium.stages`

  -- These are the AD-1 rules, made executable.
- [x] `.gitignore` -- Ignore `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `dist/`. -- Keeps the repo clean.
- [x] `src/consortium/{core,engine,stages,raters,board,archive,media,config,instruments,templates}/__init__.py` -- Empty packages, each with a one-line docstring stating its role. -- Establishes the layout.
- [x] `src/consortium/core/errors.py` -- `ConsortiumError(Exception)` with `code`, `message` and optional `path`. -- The single error type.
- [x] `src/consortium/board/blinding.py` -- A docstring-only stub stating the AD-2 rule. -- Gives the boundary a concrete target.
- [x] `src/consortium/templates/study/` -- The template files:
  - `study.yaml`: `seed`, and one Model `id: m1, provider: fake` (the schema arrives in 1.2)
  - `protocol.md`: a placeholder with section headings (the full template arrives in 4.1)
  - `prices.yaml`: the fake Model at price 0
  - `tests/example.yaml`: a pilot Test stub

  -- What `init` copies.
- [x] `src/consortium/stages/init.py` -- `init_study(path) -> Path`. Copies the template using `importlib.resources`. Raises `ConsortiumError("study_exists")` for a non-empty directory or a file, and writes nothing in that case. -- The use case.
- [x] `src/consortium/cli.py` -- The Typer `app` with an `init PATH` command. Configures logging to stderr, and maps `ConsortiumError` to stderr plus exit code 1. -- Thin driving adapter.
- [x] `docs/INTERFACE.md` -- Documents `init`, the Study folder layout (planned files marked "arrives in story X"), the error format, and the unit-of-analysis guidance (Agents sharing a Model are not independent; Agent, Persona, Model and Clip are crossed factors; no statistics in the tool). -- FR27, NFR10.
- [x] `tests/test_init.py` -- Covers every I/O matrix row through Typer's `CliRunner`.
- [x] `tests/test_architecture.py` --
  - runs `lint-imports` as a subprocess and asserts that it passes
  - walks the AST of every `.py` file under `src/consortium` and asserts that only the allowed modules import `consortium.board.blinding`
  - includes a negative test: a temporary module that imports `blinding` from a forbidden location is detected

  -- The rules must fail loudly.

**Acceptance Criteria:**
- Given a clean checkout, when `uv tool install .` is run and then `consortium --help`, then the CLI runs and lists `init`.
- Given the test suite, when `uv run pytest` runs, then all tests pass, and `uv run ruff check` is clean.
- Given an edit that makes `core` import `stages`, when the tests run, then `test_architecture` fails.

## Implementation Notes

- Typer 0.27 vendors its own click and does not depend on `click`; `cli.py` must not `import click` (it only worked in the dev venv because a dev tool pulls click in). `ConsortiumError` is mapped centrally by a `TyperGroup` subclass, so every future command inherits the `code: message` / exit 1 behaviour.
- The layers contract needs `containers = ["consortium"]`, so layer names stay short (`cli`, `stages`, ...).
- `[tool.pytest.ini_options] addopts` blocks the `launch_testing` / `launch_ros` pytest plugins: a sourced ROS `PYTHONPATH` on the dev machine autoloads them and they fail to import.
- `test_architecture` also has a negative test for the layers contract: a copy of the package where `core` imports `stages` must make `lint-imports` fail.
- Re-installing an unchanged version with `uv tool install --force .` can reuse a cached wheel; use `--reinstall` to pick up source changes.

## Spec Change Log

## Review Triage Log

| # | Source | Finding | Verdict | Evidence / route |
|---|---|---|---|---|
| 1 | VG, BH4 | No test for "missing parents created" | medium | Contract in INTERFACE.md, regression undetected. Patch: test added |
| 2 | VG, BH4 | `-v/--verbose` untested | low | Documented stderr/stdout split could break silently. Patch: test added |
| 3 | VG | Stage-independence contract has no failing-case test | medium | Contract could be deleted with all tests green. Patch: negative test |
| 4 | BH1, EC1-3 | OSError (dangling symlink, parent is a file, permission denied, disk full) gives a traceback, not `code: message` | medium | Reproduced by the edge-case hunter. Patch: wrap as `study_create_failed` |
| 5 | BH3 | File PATH reports "is not empty" | low | Wrong wording, direct correction. Patch |
| 6 | BH5 | Core's stdlib+pydantic rule is not enforced | medium | A later story could import yaml/typer in core undetected. Patch: forbidden contract |
| 7 | BH12 | .gitignore incomplete | low | Direct correction. Patch |
| 8 | VG-other, BH7, EC8 | Blinding check misses importlib and attribute access | low | Unlikely in practice, and the fix adds AST branches. Rejected |
| 9 | BH2, EC4 | Partial copy leaves a half-built folder | low | Needs a mid-copy I/O failure on a 4-file template; the fix adds a temp-dir dance. Rejected |
| 10 | EC5 | TOCTOU race on PATH | low | Single-operator local CLI. Rejected |
| 11 | BH6, EC10 | instruments/templates are in no layer | low | Both contain no code. Rejected |
| 12 | BH8, EC7 | lint-imports path fragile on Windows | low | Windows isn't a target (the lease uses fcntl later), and a missing binary already fails loudly. Rejected |
| 13 | BH9 | prices.yaml has no units | false | Story 1.2 defines the prices schema and rewrites the template by design |
| 14 | BH10 | README empty | false | Story 4.5 owns the README |
| 15 | BH11 | Path printed as typed | low | Cosmetic. Rejected |
| 16 | BH13 | ROS pytest workaround in pyproject | low | Harmless off this machine, and documented inline. Rejected |
| 17 | EC6 | Stray template files copied | false | The packaged template has only the four files plus `__pycache__`, which is already skipped |
| 18 | EC9 | Deep relative import gives a malformed target | false | Python itself rejects such imports, so no reachable source hits it |
| 19 | BH4 | Usage-error exit 2, no-args and wheel contents untested | low | Typer behaviour, and the uv tool install was verified manually. Rejected |

## Design Notes

- **Why a custom AST test for blinding.** import-linter's `forbidden` contract can't easily say "everyone except push and export", because those stage modules don't exist yet. The AST allow-list stays correct as stages are added.
- **Template location.** Templates live inside the package and are read with `importlib.resources`, so `uv tool install` users get them too.

## Verification

**Commands:**
- `uv sync && uv run pytest -q` -- expected: all pass
- `uv run ruff check src tests` -- expected: no findings
- `uv run lint-imports` -- expected: all contracts kept
- `uv tool install --force . && consortium init /tmp/rc-s1 && ls /tmp/rc-s1` -- expected: the four template entries
