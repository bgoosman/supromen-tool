# Convenience recipes for working on the supromen CLI.
#
# The `supromen` command is declared under [project.scripts] in pyproject.toml,
# so `uv tool install` is all we need to put it on PATH (by default at
# ~/.local/bin/supromen in an isolated uv-managed venv).

# Show available recipes.
default:
    @just --list

# Install the source-built `supromen` command into ~/.local/bin via uv tool.
install:
    uv tool install --force .
    @printf '\nInstalled: %s\n' "$(command -v supromen || echo '(not on PATH - ensure ~/.local/bin is in $PATH)')"

# Reinstall (force) after local code or dependency changes.
reinstall: install

# Same as reinstall; reflects any pyproject.toml changes.
upgrade: install

# Remove the globally installed `supromen` command.
uninstall:
    uv tool uninstall supromen

# Sync the local dev venv (.venv) with dev extras.
dev:
    uv sync --extra dev

# Run pytest.
test:
    uv run pytest

# Run ruff.
lint:
    uv run ruff check .

# Run release readiness checks.
check: lint test

# Build source and wheel distributions.
build:
    uv build

# Run ruff format.
fmt:
    uv run ruff format .
