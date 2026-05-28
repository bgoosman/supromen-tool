# supromen

A small CLI for the Supromen [Booked Scheduler](https://www.bookedscheduler.com/) instance at
`https://supromen.bookedscheduler.io`.

The site's public REST API (`/Web/Services`) is currently disabled on this host, so the CLI
authenticates through the normal web login and fetches the schedule page plus its AJAX
reservation grid. That internal contract is narrow but stable across Booked 4.1.x.

## Quick start

Requires Python 3.9 or newer and **[uv](https://docs.astral.sh/uv/)**.

```bash
git clone git@github.com:bgoosman/supromen-tool.git
cd supromen-tool

# Build/install the supromen command from this source checkout.
uv tool install --force .
supromen --help

# Configure credentials, then log in once to create the local cookie jar.
cp .env.example .env
$EDITOR .env
supromen auth login
```

The installed `supromen` command is source-built through Python packaging and uv. It is not a
standalone native executable; rerun `uv tool install --force .` after changing dependencies,
bumping the version, or pulling source updates you want reflected in the command on your `PATH`.

## Install

This project uses uv for environments and installs; see the
[installation guide](https://docs.astral.sh/uv/getting-started/installation/).

From the repository root:

```bash
uv sync --extra dev
```

That creates `.venv`, installs `supromen` in editable mode, and pulls runtime plus dev
dependencies (pytest, etc.). The committed [`uv.lock`](uv.lock) pins transitive versions for
reproducible installs.

Run the CLI without activating the venv:

```bash
uv run supromen --help
```

If you prefer an activated shell, use `source .venv/bin/activate` (Windows:
`.venv\Scripts\activate`) and run `supromen` directly.

### Install the `supromen` command on PATH

For day-to-day use, install the CLI as a source-built tool managed by uv. This
drops a `supromen` executable into `~/.local/bin` (make sure that's on your
`PATH`) backed by an isolated venv:

```bash
just install
# or directly:
uv tool install --force .
```

After editing dependencies or bumping the version, rerun `just install` (alias:
`just upgrade`) to refresh the installed command. To remove it:

```bash
just uninstall
```

## Configure

Copy the example file and fill in your credentials. Do not commit real values.

```bash
cp .env.example .env
$EDITOR .env
```

Recognized variables:

| Variable | Purpose |
|---|---|
| `SUPROMEN_BASE_URL` | Site root. Defaults to `https://supromen.bookedscheduler.io`. |
| `SUPROMEN_EMAIL` | Login email. |
| `SUPROMEN_PASSWORD` | Password. Prefer `--password-stdin` over exporting plaintext. |
| `SUPROMEN_COOKIE_FILE` | Where to persist the session cookie. Defaults to `~/.supromen/cookies.txt`. |

## Use

```bash
# Log in once; the session cookie is written to ~/.supromen/cookies.txt.
uv run supromen auth login

# Or read the password from stdin (does not touch your shell history).
printf '%s' "$MY_PASSWORD" | uv run supromen auth login --password-stdin

# Check that the saved session is still valid.
uv run supromen auth status

# Show the schedule for a given date (defaults to today).
uv run supromen schedule show --date 2026-03-29

# Same, as JSON for scripting.
uv run supromen schedule show --date 2026-03-29 --json

# List your own reservations (auto-detects userId from the session).
uv run supromen schedule mine --upcoming 30
uv run supromen schedule mine --from 2026-04-23 --to 2026-05-23 --json

# Drop the local cookie jar (does not invalidate the server session).
uv run supromen auth logout
```

## How it works

1. `GET /Web/index.php` to obtain a `PHPSESSID`.
2. `POST /Web/` with `email`, `password`, `persistLogin`, `captcha=""`, `resume=""`, `login=submit`.
3. Confirm authentication by fetching `/Web/dashboard.php` and checking the DOM.
4. For `schedule show`, fetch `schedule.php?sd=DATE` for the shell plus
   `schedule.php?dr=reservations&sd=DATE` (same URL the UI uses via `reservationLoadUrl`).
5. Parse the response: many hosts return **JSON** (`{"reservations":[...], "lastRefresh":...}`)
   instead of an HTML slot grid. In that case we list **reservations** from JSON and
   **resources** from `reservationResources.push({...})` blocks embedded in the shell script.
   If the grid response is **HTML**, we parse `<tr>` / `<td class="slot ...">` rows and slot
   state from CSS classes (`reservable`, `reserved`, `mine`, …) and `data-start` / `data-end`.

## Extending to bookings

The CLI is structured so reservation create/cancel/modify can be added later behind a
subcommand group. Those flows hit `/Web/reservation.php` and `/Web/ajax/*.php` endpoints
and require the CSRF token embedded in the schedule shell page. They are intentionally
out of scope for v1.

## Development

```bash
just dev     # uv sync --extra dev
just test    # uv run pytest
just lint    # uv run ruff check .
just check   # run lint and tests
just build   # uv build
just fmt     # uv run ruff format .
```

Run `just` with no arguments to list every recipe.

Tests use [`pytest-httpx`](https://pypi.org/project/pytest-httpx/) to mock the Booked
endpoints; no network traffic or real credentials are required.

After editing dependencies in [`pyproject.toml`](pyproject.toml), refresh the lockfile with:

```bash
uv lock
```
