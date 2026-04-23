from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from supromen import openings as openings_mod
from supromen.client import BookedClient
from supromen.config import Settings
from supromen.openings import find_openings
from supromen.schedule import Booking, EmbeddedResource, ScheduleView


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        base_url="https://booked.test",
        email=None,
        password=None,
        cookie_file=tmp_path / "cookies.txt",
    )


def _iso(d: date, hhmm: str) -> str:
    # Treat "hhmm" as America/New_York local and return a UTC "Z" ISO stamp.
    from datetime import datetime, timedelta

    try:
        from zoneinfo import ZoneInfo

        ny = ZoneInfo("America/New_York")
    except Exception:  # pragma: no cover
        ny = timezone(timedelta(hours=-4))
    h, m = hhmm.split(":")
    local = datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=ny)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _view(
    d: date, bookings: list[tuple[str, str, str]], resource_id: str = "2"
) -> ScheduleView:
    """Build a ScheduleView with local-time (start, end, label) bookings."""
    return ScheduleView(
        date=d.isoformat(),
        schedule_name="Test",
        date_label=None,
        resources=[],
        bookings=[
            Booking(
                reference_number=f"r{i}",
                title=label,
                start=_iso(d, s),
                end=_iso(d, e),
                resource_id=resource_id,
                resource_name="Room",
            )
            for i, (s, e, label) in enumerate(bookings)
        ],
        embedded_resources=[EmbeddedResource(resource_id=resource_id, name="Room")],
    )


def test_find_openings_subtracts_bookings_within_hours(monkeypatch, tmp_path):
    day = date(2026, 4, 21)

    per_day = {
        day: _view(day, [("13:00", "16:00", "A"), ("16:30", "19:30", "B")]),
    }

    def fake_fetch(client, target):
        return per_day[target]

    monkeypatch.setattr(openings_mod, "fetch_schedule", fake_fetch)

    # Freeze "now" to something before the opening window so the first day isn't clipped.
    fixed_now = datetime(2026, 4, 21, 0, 0, tzinfo=timezone.utc)

    with BookedClient(_settings(tmp_path)) as c:
        windows, name = find_openings(
            c,
            start=day,
            end=day,
            open_hour=8,
            close_hour=22,
            min_duration_hours=1,
            now=fixed_now,
        )

    assert name == "Room"
    # 8-13 (5h), 19:30-22 (2.5h). 16-16:30 (0.5h) is dropped by min_duration=1.
    assert [(w.start[11:16], w.end[11:16], w.duration_hours) for w in windows] == [
        ("08:00", "13:00", 5.0),
        ("19:30", "22:00", 2.5),
    ]


def test_find_openings_respects_min_duration(monkeypatch, tmp_path):
    day = date(2026, 4, 21)

    per_day = {
        day: _view(day, [("09:00", "21:30", "All day")]),
    }

    monkeypatch.setattr(openings_mod, "fetch_schedule", lambda c, t: per_day[t])

    with BookedClient(_settings(tmp_path)) as c:
        windows, _ = find_openings(
            c,
            start=day,
            end=day,
            open_hour=8,
            close_hour=22,
            min_duration_hours=2,
            now=datetime(2026, 4, 21, 0, 0, tzinfo=timezone.utc),
        )

    # 8-9 (1h) and 21:30-22 (0.5h) are both below 2h.
    assert windows == []


def test_find_openings_filters_by_resource_and_clips_past(monkeypatch, tmp_path):
    day = date(2026, 4, 21)

    view = ScheduleView(
        date=day.isoformat(),
        schedule_name="Test",
        date_label=None,
        resources=[],
        bookings=[
            Booking(
                reference_number="keep",
                title="Room2 booking",
                start=_iso(day, "18:00"),
                end=_iso(day, "20:00"),
                resource_id="2",
                resource_name="Room Two",
            ),
            Booking(
                reference_number="skip",
                title="Other room",
                start=_iso(day, "09:00"),
                end=_iso(day, "21:00"),
                resource_id="7",
                resource_name="Room Seven",
            ),
        ],
        embedded_resources=[
            EmbeddedResource(resource_id="2", name="Room Two"),
            EmbeddedResource(resource_id="7", name="Room Seven"),
        ],
    )

    monkeypatch.setattr(openings_mod, "fetch_schedule", lambda c, t: view)

    # "Now" = 15:00 local on day, which clips the 8am-15:00 portion away.
    from zoneinfo import ZoneInfo

    now = datetime(2026, 4, 21, 15, 0, tzinfo=ZoneInfo("America/New_York")).astimezone(
        timezone.utc
    )

    with BookedClient(_settings(tmp_path)) as c:
        windows, name = find_openings(
            c,
            start=day,
            end=day,
            open_hour=8,
            close_hour=22,
            min_duration_hours=0.5,
            resource_id="2",
            now=now,
        )

    assert name == "Room Two"
    # Clipped to [15:00, 22:00), minus 18:00-20:00 -> [15-18) and [20-22).
    assert [(w.start[11:16], w.end[11:16], w.duration_hours) for w in windows] == [
        ("15:00", "18:00", 3.0),
        ("20:00", "22:00", 2.0),
    ]
