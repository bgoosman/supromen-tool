from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from supromen.auth import AuthError
from supromen.client import BookedClient
from supromen.config import Settings
from supromen.schedule import fetch_schedule

from .fixtures import (
    LOGIN_PAGE_HTML,
    SCHEDULE_GRID_HTML,
    SCHEDULE_GRID_JSON,
    SCHEDULE_SHELL_HTML,
    SCHEDULE_SHELL_WITH_EMBED_HTML,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        base_url="https://booked.test",
        email=None,
        password=None,
        cookie_file=tmp_path / "cookies.txt",
    )


def test_fetch_schedule_parses_html_slot_grid(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url="https://booked.test/Web/schedule.php?sd=2026-03-29", html=SCHEDULE_SHELL_HTML
    )
    httpx_mock.add_response(
        url="https://booked.test/Web/schedule.php?dr=reservations",
        method="POST",
        html=SCHEDULE_GRID_HTML,
    )

    with BookedClient(_settings(tmp_path)) as client:
        view = fetch_schedule(client, date(2026, 3, 29))

    assert view.date == "2026-03-29"
    assert view.schedule_name == "Primary Schedule"
    assert len(view.resources) == 2

    squat = view.resources[0]
    assert squat.name == "Squat Rack"
    assert squat.resource_id == "7"
    assert [s.states for s in squat.slots] == [["past"], ["reservable"], ["reserved", "mine"]]
    assert squat.slots[2].reference_number == "abc123"
    assert squat.slots[1].start == "2026-03-29T09:00:00"

    platform = view.resources[1]
    assert platform.name == "Platform A"
    assert [s.states for s in platform.slots] == [["reservable"], ["restricted"]]


def test_fetch_schedule_posts_form_and_parses_json(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url="https://booked.test/Web/schedule.php?sd=2026-03-29",
        html=SCHEDULE_SHELL_WITH_EMBED_HTML,
    )
    httpx_mock.add_response(
        url="https://booked.test/Web/schedule.php?dr=reservations",
        method="POST",
        content=SCHEDULE_GRID_JSON.encode(),
        headers={"content-type": "application/json"},
    )

    with BookedClient(_settings(tmp_path)) as client:
        view = fetch_schedule(client, date(2026, 3, 29))

    posted = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert len(posted) == 1
    body = posted[0].content.decode()
    assert "beginDate=2026-03-29" in body
    assert "endDate=2026-03-30" in body
    assert "scheduleId=3" in body
    assert "CSRF_TOKEN=TOKEN123" in body

    assert view.resources == []
    assert len(view.embedded_resources) == 1
    assert view.embedded_resources[0].name == "Room A"
    assert view.embedded_resources[0].resource_id == "2"

    assert len(view.bookings) == 1
    b = view.bookings[0]
    assert b.reference_number == "rn1"
    assert b.title == "Jam"
    assert b.resource_id == "2"
    assert b.resource_name == "Room A"
    assert b.start_date == "2026-03-29"
    assert b.start_time == "10:00 AM"
    assert b.end_time == "11:00 AM"
    assert b.start == "2026-03-29T14:00:00Z"
    assert b.end == "2026-03-29T15:00:00Z"
    assert b.is_owner is True
    assert b.is_pending is False
    assert view.last_refresh == "2026-04-18T12:00:00+0000"


def test_fetch_schedule_detects_expired_session(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url="https://booked.test/Web/schedule.php?sd=2026-03-29", html=LOGIN_PAGE_HTML
    )

    with BookedClient(_settings(tmp_path)) as client:
        with pytest.raises(AuthError):
            fetch_schedule(client, date(2026, 3, 29))
