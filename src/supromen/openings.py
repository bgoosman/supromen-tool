"""Find open blocks of time for a resource, constrained to opening hours.

For each day in ``[start, end]`` we:

1. Build the opening window ``[open_hour, close_hour)`` in the venue's local
   timezone and convert it to a UTC-epoch interval.
2. Pull reservations for that day via :func:`supromen.schedule.fetch_schedule`,
   optionally filtered to a single ``resource_id``.
3. Subtract the merged busy intervals from the opening window to produce free
   intervals, then keep the ones >= ``min_duration``.

Only reservations whose resource matches ``resource_id`` are treated as busy.
If ``resource_id`` is ``None`` we assume a single-resource schedule and use
every booking.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

from .client import BookedClient
from .schedule import Booking, fetch_schedule


@dataclass
class OpenWindow:
    """A contiguous free block, rendered in the caller-supplied timezone."""

    date: str
    start: str
    end: str
    duration_hours: float
    resource_id: str | None
    resource_name: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_iso_utc(value: str) -> datetime:
    """Parse the ISO-Z timestamps produced by :func:`schedule.Booking`."""
    s = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _booking_epoch_interval(b: Booking) -> tuple[int, int] | None:
    if not b.start or not b.end:
        return None
    try:
        s = int(_parse_iso_utc(b.start).timestamp())
        e = int(_parse_iso_utc(b.end).timestamp())
    except ValueError:
        return None
    if e <= s:
        return None
    return (s, e)


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[tuple[int, int]] = [ordered[0]]
    for s, e in ordered[1:]:
        ps, pe = merged[-1]
        if s <= pe:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def _subtract(window: tuple[int, int], busy: list[tuple[int, int]]) -> list[tuple[int, int]]:
    free: list[tuple[int, int]] = []
    cur, end = window
    for bs, be in busy:
        if be <= cur or bs >= end:
            continue
        if bs > cur:
            free.append((cur, min(bs, end)))
        cur = max(cur, be)
        if cur >= end:
            break
    if cur < end:
        free.append((cur, end))
    return free


def _resolve_tz(name: str):
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def _opening_window(day: date, open_hour: float, close_hour: float, tz) -> tuple[int, int]:
    """Return the UTC-epoch ``[open, close)`` span for ``day`` in ``tz``.

    Fractional hours (e.g. ``8.5``) are supported.
    """
    open_dt = datetime.combine(day, time(0, 0), tzinfo=tz) + timedelta(hours=open_hour)
    close_dt = datetime.combine(day, time(0, 0), tzinfo=tz) + timedelta(hours=close_hour)
    if close_dt <= open_dt:
        close_dt = open_dt
    return int(open_dt.timestamp()), int(close_dt.timestamp())


def find_openings(
    client: BookedClient,
    *,
    start: date,
    end: date,
    open_hour: float = 8.0,
    close_hour: float = 22.0,
    min_duration_hours: float = 1.0,
    resource_id: str | None = None,
    tz_name: str = "America/New_York",
    now: datetime | None = None,
) -> tuple[list[OpenWindow], str | None]:
    """Compute free blocks across ``[start, end]`` (inclusive).

    Returns ``(windows, resource_name)``. ``resource_name`` is the resolved
    name of the filtered resource, if any (for display).
    """
    if end < start:
        raise ValueError(f"end ({end}) is before start ({start})")

    tz = _resolve_tz(tz_name)
    min_seconds = int(round(min_duration_hours * 3600))
    now_epoch = int((now or datetime.now(timezone.utc)).timestamp())

    results: list[OpenWindow] = []
    resolved_name: str | None = None

    current = start
    while current <= end:
        view = fetch_schedule(client, current)

        if resource_id is None and len(view.embedded_resources) == 1:
            effective_rid = view.embedded_resources[0].resource_id
            resolved_name = resolved_name or view.embedded_resources[0].name
        else:
            effective_rid = resource_id
            if effective_rid is not None and resolved_name is None:
                for r in view.embedded_resources:
                    if r.resource_id == str(effective_rid):
                        resolved_name = r.name
                        break

        busy: list[tuple[int, int]] = []
        for b in view.bookings:
            if effective_rid is not None and str(b.resource_id) != str(effective_rid):
                continue
            if b.is_pending:
                # Pending reservations still hold the slot on most installs.
                pass
            iv = _booking_epoch_interval(b)
            if iv is not None:
                busy.append(iv)

        window = _opening_window(current, open_hour, close_hour, tz)
        # Clip the opening window to the future on the first day.
        if current == start and now_epoch > window[0]:
            window = (min(now_epoch, window[1]), window[1])
        if window[1] <= window[0]:
            current += timedelta(days=1)
            continue

        free = _subtract(window, _merge(busy))
        for s, e in free:
            if e - s < min_seconds:
                continue
            s_dt = datetime.fromtimestamp(s, tz=timezone.utc).astimezone(tz)
            e_dt = datetime.fromtimestamp(e, tz=timezone.utc).astimezone(tz)
            results.append(
                OpenWindow(
                    date=current.isoformat(),
                    start=s_dt.isoformat(timespec="minutes"),
                    end=e_dt.isoformat(timespec="minutes"),
                    duration_hours=round((e - s) / 3600, 2),
                    resource_id=str(effective_rid) if effective_rid is not None else None,
                    resource_name=resolved_name,
                )
            )

        current += timedelta(days=1)

    return results, resolved_name
