from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from supromen.auth import AuthError
from supromen.cli import app
from supromen.client import BookedClient
from supromen.config import Settings
from supromen.reserve import (
    ReserveError,
    Slot,
    check_slot,
    create_reservation,
    open_context,
    parse_slot,
)

from .fixtures import LOGIN_PAGE_HTML, SCHEDULE_SHELL_WITH_EMBED_HTML

BASE = "https://booked.test/Web"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

# Trimmed Booked 4.2.2 reservation page: the React app reads `const csrf`.
RESERVATION_PAGE_HTML = """<!doctype html><html><body>
<div id="react-root"></div>
<script>
    const path = 'https://booked.test/Web';
const csrf = "CSRF-PAGE";
const timezone = "America/New_York";
</script></body></html>
"""

# 12:00-15:00 America/New_York on 2026-11-11 (EST, UTC-5).
TEMPLATE = {
    "referenceNumber": None,
    "ownerId": 18,
    "resourceIds": [2],
    "title": None,
    "start": "2026-11-11T17:00:00+0000",
    "end": "2026-11-11T20:00:00+0000",
    "recurrence": {"type": "none", "interval": 1, "repeatDates": []},
}

SLOT = Slot(day=date(2026, 11, 11), start=time(12, 0), end=time(15, 0))

# Same slot far in the future, for CLI tests that run against the real clock.
FUTURE_TEMPLATE = {
    **TEMPLATE,
    "start": "2099-11-11T17:00:00+0000",
    "end": "2099-11-11T20:00:00+0000",
}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        base_url="https://booked.test",
        email=None,
        password=None,
        cookie_file=tmp_path / "cookies.txt",
    )


def _mock_context(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(rf"{re.escape(BASE)}/schedule\.php\?sd=.*"),
        html=SCHEDULE_SHELL_WITH_EMBED_HTML,
    )
    httpx_mock.add_response(
        url=re.compile(rf"{re.escape(BASE)}/reservation/\?.*"), html=RESERVATION_PAGE_HTML
    )


def _mock_available(httpx_mock, clash_ids=(), template=TEMPLATE) -> None:
    httpx_mock.add_response(
        url=re.compile(rf"{re.escape(BASE)}/api/resources\.php\?api=check-availability.*"),
        json={"data": {"ids": list(clash_ids)}},
    )
    if not clash_ids:
        httpx_mock.add_response(
            url=re.compile(rf"{re.escape(BASE)}/api/reservation\.php\?api=load.*"),
            json={"data": {"reservation": template}},
        )


def test_parse_slot():
    assert parse_slot("2026-11-11@12:00-15:00") == SLOT
    assert parse_slot("2026-11-1@9:30-11:00").hours == 1.5


@pytest.mark.parametrize(
    "raw", ["2026-11-11 12:00-15:00", "2026-11-11@15:00-12:00", "2026-13-01@12:00-13:00"]
)
def test_parse_slot_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        parse_slot(raw)


def test_open_context_resolves_ids_and_csrf(httpx_mock, tmp_path):
    _mock_context(httpx_mock)
    with BookedClient(_settings(tmp_path)) as client:
        ctx = open_context(client, day=SLOT.day)
    assert (ctx.resource_id, ctx.resource_name, ctx.schedule_id) == ("2", "Room A", "3")
    assert ctx.csrf == "CSRF-PAGE"


def test_open_context_detects_login_page(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url=re.compile(rf"{re.escape(BASE)}/schedule\.php\?sd=.*"), html=LOGIN_PAGE_HTML
    )
    with BookedClient(_settings(tmp_path)) as client:
        with pytest.raises(AuthError):
            open_context(client, day=SLOT.day)


def test_check_slot_available_sends_system_times(httpx_mock, tmp_path):
    _mock_context(httpx_mock)
    _mock_available(httpx_mock)
    with BookedClient(_settings(tmp_path)) as client:
        ctx = open_context(client, day=SLOT.day)
        check = check_slot(client, ctx, SLOT, now=NOW)

    assert check.available and check.template == TEMPLATE
    avail = next(r for r in httpx_mock.get_requests() if "check-availability" in str(r.url))
    assert avail.url.params["sd"] == "2026-11-11 12:00"
    assert avail.url.params["ed"] == "2026-11-11 15:00"
    assert avail.url.params["rid"] == "2"
    assert avail.headers["x-csrf-token"] == "CSRF-PAGE"


def test_check_slot_reports_clash(httpx_mock, tmp_path):
    _mock_context(httpx_mock)
    _mock_available(httpx_mock, clash_ids=[2])
    with BookedClient(_settings(tmp_path)) as client:
        ctx = open_context(client, day=SLOT.day)
        check = check_slot(client, ctx, SLOT, now=NOW)
    assert not check.available and check.problem == "already booked"


def test_check_slot_rejects_adjusted_times(httpx_mock, tmp_path):
    _mock_context(httpx_mock)
    _mock_available(httpx_mock, template={**TEMPLATE, "end": "2026-11-11T19:00:00+0000"})
    with BookedClient(_settings(tmp_path)) as client:
        ctx = open_context(client, day=SLOT.day)
        check = check_slot(client, ctx, SLOT, now=NOW)
    assert not check.available and "adjusted" in check.problem


def test_check_slot_skips_past_slots_without_calling_api(httpx_mock, tmp_path):
    _mock_context(httpx_mock)
    later = datetime(2026, 11, 12, tzinfo=timezone.utc)
    with BookedClient(_settings(tmp_path)) as client:
        ctx = open_context(client, day=SLOT.day)
        check = check_slot(client, ctx, SLOT, now=later)
    assert check.problem == "in the past"
    assert not any("/api/" in str(r.url) for r in httpx_mock.get_requests())


def test_create_reservation_posts_multipart_request(httpx_mock, tmp_path):
    _mock_context(httpx_mock)
    _mock_available(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE}/api/reservation.php?action=create",
        method="POST",
        json={"data": {"success": True, "referenceNumber": "ABC123", "errors": []}},
        headers={"X-Csrf-Token": "CSRF-ROTATED"},
    )
    with BookedClient(_settings(tmp_path)) as client:
        ctx = open_context(client, day=SLOT.day)
        check = check_slot(client, ctx, SLOT, now=NOW)
        result = create_reservation(client, ctx, check, title="Ben")

    assert result.success and result.reference_number == "ABC123"
    assert ctx.csrf == "CSRF-ROTATED"

    post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    assert post.headers["content-type"].startswith("multipart/form-data")
    assert post.headers["x-csrf-token"] == "CSRF-PAGE"
    body = post.content.decode()
    assert 'name="CSRF_TOKEN"\r\n\r\nCSRF-PAGE' in body
    assert 'name="BROWSER_TIMEZONE"\r\n\r\nAmerica/New_York' in body
    raw = body.split('name="request"\r\n\r\n', 1)[1].split("\r\n--", 1)[0]
    request = json.loads(raw)
    assert request["updateScope"] == "full"
    assert request["reservation"]["title"] == "Ben"
    assert request["reservation"]["start"] == TEMPLATE["start"]


def test_create_reservation_surfaces_errors(httpx_mock, tmp_path):
    _mock_context(httpx_mock)
    _mock_available(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE}/api/reservation.php?action=create",
        method="POST",
        json={"data": {"success": False, "errors": ["Conflicts with an existing reservation"]}},
    )
    with BookedClient(_settings(tmp_path)) as client:
        ctx = open_context(client, day=SLOT.day)
        result = create_reservation(client, ctx, check_slot(client, ctx, SLOT, now=NOW))
    assert not result.success
    assert result.errors == ["Conflicts with an existing reservation"]


def test_create_reservation_refuses_unchecked_slot(httpx_mock, tmp_path):
    _mock_context(httpx_mock)
    _mock_available(httpx_mock, clash_ids=[2])
    with BookedClient(_settings(tmp_path)) as client:
        ctx = open_context(client, day=SLOT.day)
        check = check_slot(client, ctx, SLOT, now=NOW)
        with pytest.raises(ReserveError):
            create_reservation(client, ctx, check)


def _cli_env(tmp_path: Path) -> dict[str, str]:
    return {"SUPROMEN_BASE_URL": "https://booked.test", "SUPROMEN_COOKIE_FILE": str(tmp_path / "c.txt")}


def test_cli_reserve_without_yes_never_posts(httpx_mock, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # keep the repo .env out of the test
    _mock_context(httpx_mock)
    _mock_available(httpx_mock, template=FUTURE_TEMPLATE)
    result = CliRunner().invoke(
        app, ["schedule", "reserve", "2099-11-11@12:00-15:00", "--json"], env=_cli_env(tmp_path)
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["slots"][0]["available"] is True
    assert payload["results"] == []
    assert not any(r.method == "POST" for r in httpx_mock.get_requests())


def test_cli_reserve_yes_books_nothing_when_a_slot_is_blocked(httpx_mock, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _mock_context(httpx_mock)
    _mock_available(httpx_mock, clash_ids=[2])
    result = CliRunner().invoke(
        app, ["schedule", "reserve", "2099-11-11@12:00-15:00", "--yes"], env=_cli_env(tmp_path)
    )
    assert result.exit_code == 1
    assert "nothing was booked" in result.output
    assert not any(r.method == "POST" for r in httpx_mock.get_requests())
