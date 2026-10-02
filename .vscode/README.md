# Editor configuration

`settings.json` and `extensions.json` are shared and committed on purpose: they
keep the editor's ruff behaviour identical to the checks
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs. Every other file
under `.vscode/` stays git-ignored (see [`.gitignore`](../.gitignore)).

## What CI runs

```bash
ruff check   src tests scripts
ruff format --check src tests scripts
```

Rule selection (`select = ["E", "F", "W", "I", "UP", "B", "C4"]`) and the
`src/dankagu/grpc/generated` exclusion both come from
[`pyproject.toml`](../pyproject.toml). Nothing is duplicated here, so there is a
single source of truth: `ruff.configuration` points the extension at that file.

| Editor setting | Matches CI how |
|---|---|
| `ruff.lint.run: onSave` | Equivalent to `ruff check` over the same trees |
| `ruff.format.enable` + `formatOnSave` | Equivalent to `ruff format --check` |
| `source.fixAll.ruff` / `source.organizeImports.ruff` | Applies the same fixes as `ruff check --fix` |

## One-time setup

The extension runs the `ruff` executable found in the selected interpreter's
environment, so the dev extra must be installed:

```bash
uv sync --extra dev
```

If `ruff` is missing from the environment, the extension falls back to its
bundled binary, which can be a *different version* from the one
`uv.lock` pins — and a version difference is exactly how a lint error reaches CI
that the editor never showed. Install the dev extra rather than relying on the
fallback.

## Platform note

`python.defaultInterpreterPath` is set to
`${workspaceFolder}/.venv/Scripts/python.exe`, which is the Windows layout.
On Linux/macOS change `Scripts` to `bin`. It is a hint only: once the Python
extension has a selected interpreter, that selection wins. Set it per-machine
with `"python.defaultInterpreterPath"` in your *user* settings if you work
across platforms, or use **Python: Select Interpreter** from the command
palette.

## Prefer the same commands locally

To reproduce CI exactly without relying on the editor:

```bash
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest -q
```

`pytest` requires the protocol schemas to be present, so run
`uv run python scripts/compile_protos.py --fetch` once first (or after any change
to the upstream schemas). Note that `pytest` imports the generated stubs, so a
missing `protos/` fails the suite at import time.
