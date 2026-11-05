"""Typer entrypoint for the `supromen` CLI."""

from __future__ import annotations

import json as jsonlib
import sys
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .auth import AuthError, check_session, login
from .client import BookedClient
from .config import ConfigError, Settings, load_settings
from .my_reservations import fetch_my_reservations
from .openings import find_openings
from .reserve import (
    ReserveError,
    check_slot,
    create_reservation,
    open_context,
    parse_slot,
)
from .schedule import ScheduleView, fetch_schedule

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="CLI for the Supromen Booked Scheduler instance.",
)
auth_app = typer.Typer(help="Authentication commands.", no_args_is_help=True)
schedule_app = typer.Typer(help="Schedule viewing commands.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")
app.add_typer(schedule_app, name="schedule")

console = Console()
err_console = Console(stderr=True)


def _settings(
    email: Optional[str],
    password: Optional[str],
    base_url: Optional[str],
    cookie_file: Optional[Path],
) -> Settings:
    return load_settings(
        email=email,
        password=password,
        base_url=base_url,
        cookie_file=cookie_file,
    )


def _fail(message: str, code: int = 1) -> None:
    err_console.print(f"[bold red]error:[/] {message}")
    raise typer.Exit(code=code)


@auth_app.command("login")
def auth_login(
    email: Optional[str] = typer.Option(None, envvar="SUPROMEN_EMAIL"),
    password: Optional[str] = typer.Option(
        None,
        envvar="SUPROMEN_PASSWORD",
        help="Password. Prefer env var or --password-stdin over passing on the CLI.",
    ),
    password_stdin: bool = typer.Option(False, "--password-stdin", help="Read password from stdin."),
    base_url: Optional[str] = typer.Option(None, envvar="SUPROMEN_BASE_URL"),
    cookie_file: Optional[Path] = typer.Option(None, envvar="SUPROMEN_COOKIE_FILE"),
) -> None:
    """Authenticate against Booked and persist the session cookie."""
    if password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    try:
        settings = _settings(email, password, base_url, cookie_file)
        email_val, password_val = settings.require_credentials()
    except ConfigError as e:
        _fail(str(e))

    with BookedClient(settings) as client:
        try:
            status = login(client, email_val, password_val)
        except AuthError as e:
            _fail(str(e))
        path = client.save_cookies()
    who = status.display_name or email_val
    console.print(f"[green]Logged in[/] as {who}. Session saved to {path}.")


@auth_app.command("status")
def auth_status(
    base_url: Optional[str] = typer.Option(None, envvar="SUPROMEN_BASE_URL"),
    cookie_file: Optional[Path] = typer.Option(None, envvar="SUPROMEN_COOKIE_FILE"),
) -> None:
    """Check whether the persisted session is still valid."""
    settings = _settings(None, None, base_url, cookie_file)
    with BookedClient(settings) as client:
        try:
            status = check_session(client)
        except AuthError as e:
            _fail(str(e))
    if status.authenticated:
        if status.display_name:
            console.print(f"[green]authenticated[/] as {status.display_name}")
        else:
            console.print("[green]authenticated[/] (session valid)")
    else:
        console.print(f"[yellow]not authenticated[/] ({status.detail or 'no session'})")
        raise typer.Exit(code=1)


@auth_app.command("logout")
def auth_logout(
    cookie_file: Optional[Path] = typer.Option(None, envvar="SUPROMEN_COOKIE_FILE"),
) -> None:
    """Remove the local cookie jar. Does not invalidate the server-side session."""
    settings = _settings(None, None, None, cookie_file)
    if settings.cookie_file.is_file():
        settings.cookie_file.unlink()
        console.print(f"Removed {settings.cookie_file}.")
    else:
        console.print(f"No cookie file at {settings.cookie_file}; nothing to do.")


def _parse_date(raw: str) -> date:
    # Accept YYYY-MM-DD or YYYY-M-D (Booked's own URLs use the latter).
    parts = raw.split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        try:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError as e:
            raise typer.BadParameter(f"Invalid date {raw!r}: {e}") from e
    raise typer.BadParameter(f"Unrecognized date: {raw!r}. Use YYYY-MM-DD.")


def _render_view(view: ScheduleView) -> None:
    header = view.schedule_name or "Schedule"
    if view.date_label:
        header = f"{header} - {view.date_label}"
    console.print(f"[bold]{header}[/]  ({view.date})")
    if view.last_refresh:
        console.print(f"[dim]Last refresh: {view.last_refresh}[/]")

    if view.embedded_resources:
        names = ", ".join(r.name for r in view.embedded_resources)
        console.print(f"[dim]Resources:[/] {names}")

    if view.bookings:
        bt = Table(title="Reservations", show_lines=False)
        bt.add_column("Date")
        bt.add_column("Start")
        bt.add_column("End")
        bt.add_column("Resource")
        bt.add_column("Label", overflow="fold")
        bt.add_column("Ref")
        for b in view.bookings:
            rlabel = b.resource_name or b.resource_id or "-"
            bt.add_row(
                b.start_date or (b.start or "")[:10] or "-",
                b.start_time or b.start or "-",
                b.end_time or b.end or "-",
                rlabel,
                b.title or "",
                b.reference_number or "-",
            )
        console.print(bt)

    if view.resources:
        for row in view.resources:
            table = Table(title=row.name or row.resource_id or "Resource", show_lines=False)
            table.add_column("Start")
            table.add_column("End")
            table.add_column("State")
            table.add_column("Label", overflow="fold")
            for slot in row.slots:
                table.add_row(
                    slot.start or "-",
                    slot.end or "-",
                    ",".join(slot.states) or "-",
                    slot.label or "",
                )
            console.print(table)

    if not view.bookings and not view.resources and not view.embedded_resources:
        console.print("[dim]No resources or slots parsed from the schedule page.[/]")


@schedule_app.command("show")
def schedule_show(
    target: str = typer.Option(
        date.today().isoformat(),
        "--date",
        "-d",
        help="Schedule date (YYYY-MM-DD). Defaults to today.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    base_url: Optional[str] = typer.Option(None, envvar="SUPROMEN_BASE_URL"),
    cookie_file: Optional[Path] = typer.Option(None, envvar="SUPROMEN_COOKIE_FILE"),
) -> None:
    """Show the schedule for a date using the persisted session."""
    when = _parse_date(target)
    settings = _settings(None, None, base_url, cookie_file)
    with BookedClient(settings) as client:
        try:
            view = fetch_schedule(client, when)
        except AuthError as e:
            _fail(f"{e}")
        except RuntimeError as e:
            _fail(str(e))

    if json_out:
        typer.echo(jsonlib.dumps(view.to_dict(), indent=2))
    else:
        _render_view(view)


def _end_of_month(d: date) -> date:
    return date(d.year, d.month, monthrange(d.year, d.month)[1])


@schedule_app.command("openings")
def schedule_openings(
    from_date: Optional[str] = typer.Option(
        None, "--from", help="Start date (YYYY-MM-DD). Defaults to today."
    ),
    to_date: Optional[str] = typer.Option(
        None, "--to", help="End date (YYYY-MM-DD, inclusive). Defaults to end of the start month."
    ),
    duration: float = typer.Option(
        1.0, "--duration", "-n", min=0.0, help="Minimum block size in hours."
    ),
    total_hours: Optional[float] = typer.Option(
        None,
        "--total",
        "-t",
        help="Stop listing once this many cumulative hours of openings are shown.",
    ),
    open_hour: float = typer.Option(8.0, "--open", help="Opening hour (local, 24h). Default 8 (8am)."),
    close_hour: float = typer.Option(22.0, "--close", help="Closing hour (local, 24h). Default 22 (10pm)."),
    resource_id: Optional[str] = typer.Option(
        None,
        "--resource-id",
        "-r",
        help="Resource ID to filter on. Defaults to the sole resource if there is only one.",
    ),
    tz_name: str = typer.Option(
        "America/New_York", "--tz", help="IANA timezone for opening hours and display."
    ),
    json_out: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    base_url: Optional[str] = typer.Option(None, envvar="SUPROMEN_BASE_URL"),
    cookie_file: Optional[Path] = typer.Option(None, envvar="SUPROMEN_COOKIE_FILE"),
) -> None:
    """Find open blocks of time within the venue's opening hours."""
    start = _parse_date(from_date) if from_date else date.today()
    end = _parse_date(to_date) if to_date else _end_of_month(start)
    if end < start:
        _fail(f"--to ({end}) is before --from ({start}).")

    settings = _settings(None, None, base_url, cookie_file)
    with BookedClient(settings) as client:
        try:
            windows, resource_name = find_openings(
                client,
                start=start,
                end=end,
                open_hour=open_hour,
                close_hour=close_hour,
                min_duration_hours=duration,
                resource_id=resource_id,
                tz_name=tz_name,
            )
        except AuthError as e:
            _fail(str(e))
        except (RuntimeError, ValueError) as e:
            _fail(str(e))

    if total_hours is not None:
        selected: list = []
        accum = 0.0
        for w in windows:
            selected.append(w)
            accum += w.duration_hours
            if accum >= total_hours:
                break
        windows = selected

    total = sum(w.duration_hours for w in windows)

    if json_out:
        payload = {
            "range": {"from": start.isoformat(), "to": end.isoformat()},
            "tz": tz_name,
            "open_hour": open_hour,
            "close_hour": close_hour,
            "min_duration_hours": duration,
            "resource_id": resource_id,
            "resource_name": resource_name,
            "total_hours": round(total, 2),
            "windows": [w.to_dict() for w in windows],
        }
        typer.echo(jsonlib.dumps(payload, indent=2))
        return

    header = "Openings"
    if resource_name:
        header = f"{header} - {resource_name}"
    def _fmt_hour(h: float) -> str:
        hh = int(h)
        mm = int(round((h - hh) * 60))
        return f"{hh:02d}:{mm:02d}"

    header = (
        f"{header}  ({start} .. {end}, "
        f"{_fmt_hour(open_hour)}-{_fmt_hour(close_hour)} {tz_name})"
    )
    console.print(f"[bold]{header}[/]")

    if not windows:
        console.print("[yellow]No openings found with those constraints.[/]")
        return

    table = Table(show_lines=False)
    table.add_column("Date")
    table.add_column("Day")
    table.add_column("Start")
    table.add_column("End")
    table.add_column("Hours", justify="right")
    for w in windows:
        # ISO strings look like "2026-04-21T19:30-04:00"; strip the date and offset for display.
        s_hm = w.start.split("T", 1)[1][:5]
        e_hm = w.end.split("T", 1)[1][:5]
        day_name = date.fromisoformat(w.date).strftime("%a")
        table.add_row(w.date, day_name, s_hm, e_hm, f"{w.duration_hours:.2f}")
    console.print(table)
    console.print(f"[dim]Total listed:[/] {total:.2f} h across {len(windows)} window(s).")


@schedule_app.command("mine")
def schedule_mine(
    from_date: Optional[str] = typer.Option(
        None, "--from", help="Start date (YYYY-MM-DD). Defaults to today."
    ),
    to_date: Optional[str] = typer.Option(
        None, "--to", help="End date (YYYY-MM-DD, inclusive). Defaults to end of the start month."
    ),
    upcoming_days: Optional[int] = typer.Option(
        None,
        "--upcoming",
        min=0,
        help="Shortcut: list from today through today+N days (overrides --from/--to).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    base_url: Optional[str] = typer.Option(None, envvar="SUPROMEN_BASE_URL"),
    cookie_file: Optional[Path] = typer.Option(None, envvar="SUPROMEN_COOKIE_FILE"),
) -> None:
    """List the logged-in user's reservations in a date range."""
    if upcoming_days is not None:
        start = date.today()
        end = start + timedelta(days=upcoming_days)
    else:
        start = _parse_date(from_date) if from_date else date.today()
        end = _parse_date(to_date) if to_date else _end_of_month(start)
    if end < start:
        _fail(f"--to ({end}) is before --from ({start}).")

    settings = _settings(None, None, base_url, cookie_file)
    with BookedClient(settings) as client:
        try:
            view = fetch_my_reservations(client, start, end)
        except AuthError as e:
            _fail(str(e))
        except RuntimeError as e:
            _fail(str(e))

    if json_out:
        typer.echo(jsonlib.dumps(view.to_dict(), indent=2))
        return

    header = f"My reservations  ({view.begin} .. {view.end}, userId={view.user_id})"
    console.print(f"[bold]{header}[/]")

    if not view.bookings:
        console.print("[yellow]No reservations in that range.[/]")
        return

    table = Table(show_lines=False)
    table.add_column("Date")
    table.add_column("Start")
    table.add_column("End")
    table.add_column("Resource")
    table.add_column("Label", overflow="fold")
    table.add_column("Ref")
    for b in view.bookings:
        rlabel = b.resource_name or b.resource_id or "-"
        table.add_row(
            b.start_date or (b.start or "")[:10] or "-",
            b.start_time or b.start or "-",
            b.end_time or b.end or "-",
            rlabel,
            b.title or "",
            b.reference_number or "-",
        )
    console.print(table)


@schedule_app.command("reserve")
def schedule_reserve(
    slots: list[str] = typer.Argument(
        ...,
        metavar="SLOT...",
        help="One or more slots as YYYY-MM-DD@HH:MM-HH:MM (24h, local), e.g. 2026-11-11@12:00-15:00.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Actually book. Without this flag nothing is reserved."
    ),
    title: Optional[str] = typer.Option(None, "--title", help="Reservation title shown on the schedule."),
    resource_id: Optional[str] = typer.Option(
        None,
        "--resource-id",
        "-r",
        help="Resource ID to book. Defaults to the sole resource if there is only one.",
    ),
    tz_name: str = typer.Option(
        "America/New_York", "--tz", help="IANA timezone the slot times are written in."
    ),
    json_out: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    base_url: Optional[str] = typer.Option(None, envvar="SUPROMEN_BASE_URL"),
    cookie_file: Optional[Path] = typer.Option(None, envvar="SUPROMEN_COOKIE_FILE"),
) -> None:
    """Preview, then book, one or more reservations.

    Without --yes this is a dry run: each slot is checked for availability and
    validated against the server, and nothing is booked. With --yes every slot
    must pass the check first; if any fails, nothing is booked.
    """
    try:
        parsed = sorted({parse_slot(raw) for raw in slots}, key=lambda s: (s.day, s.start))
    except ValueError as e:
        _fail(str(e))

    settings = _settings(None, None, base_url, cookie_file)
    with BookedClient(settings) as client:
        try:
            ctx = open_context(client, day=parsed[0].day, resource_id=resource_id, tz_name=tz_name)
            checks = [check_slot(client, ctx, slot) for slot in parsed]
        except (AuthError, ReserveError) as e:
            _fail(str(e))

        blocked = [c for c in checks if not c.available]
        total = sum(c.slot.hours for c in checks)
        results = []
        if yes and not blocked:
            for c in checks:
                try:
                    result = create_reservation(client, ctx, c, title=title)
                except (AuthError, ReserveError) as e:
                    _fail(f"{c.slot.system_start}: {e}")
                results.append(result)
                if not result.success:
                    break

    if json_out:
        payload = {
            "resource_id": ctx.resource_id,
            "resource_name": ctx.resource_name,
            "dry_run": not yes,
            "total_hours": round(total, 2),
            "slots": [c.to_dict() for c in checks],
            "results": [r.to_dict() for r in results],
        }
        typer.echo(jsonlib.dumps(payload, indent=2))
        if (yes and blocked) or any(not r.success for r in results):
            raise typer.Exit(code=1)
        return

    mode = "Booking" if yes else "Preview (dry run)"
    console.print(f"[bold]{mode} - {ctx.resource_name or 'resource ' + ctx.resource_id}[/]")
    booked = {r.slot: r for r in results}
    table = Table(show_lines=False)
    table.add_column("Date")
    table.add_column("Day")
    table.add_column("Start")
    table.add_column("End")
    table.add_column("Hours", justify="right")
    table.add_column("Status")
    for c in checks:
        r = booked.get(c.slot)
        if r is not None and r.success:
            status = f"[green]booked {r.reference_number or ''}[/]".strip()
            if r.requires_approval:
                status += " [yellow](pending approval)[/]"
        elif r is not None:
            status = f"[red]failed: {'; '.join(r.errors) or 'unknown error'}[/]"
        elif c.available:
            status = "[green]available[/]" if not yes else "[dim]not attempted[/]"
        else:
            status = f"[red]{c.problem}[/]"
        table.add_row(
            c.slot.day.isoformat(),
            c.slot.day.strftime("%a"),
            c.slot.start.strftime("%H:%M"),
            c.slot.end.strftime("%H:%M"),
            f"{c.slot.hours:.2f}",
            status,
        )
    console.print(table)
    console.print(f"[dim]Total:[/] {total:.2f} h across {len(checks)} slot(s).")

    if blocked:
        console.print(
            f"[red]{len(blocked)} slot(s) can't be booked.[/] Remove or change them"
            + (" - nothing was booked." if yes else ".")
        )
        if yes:
            raise typer.Exit(code=1)
    elif not yes:
        console.print("[yellow]Nothing booked.[/] Re-run with --yes to reserve these slots.")
    elif any(not r.success for r in results):
        console.print("[red]Stopped at the first failed booking.[/] Earlier slots above stay booked.")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
