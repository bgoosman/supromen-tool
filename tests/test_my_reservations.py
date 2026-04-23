from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from supromen.auth import AuthError
from supromen.client import BookedClient
from supromen.config import Settings
from supromen.my_reservations import fetch_my_reservations, get_current_user_id

from .fixtures import LOGIN_PAGE_HTML


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        base_url="https://booked.test",
        email=None,
        password=None,
        cookie_file=tmp_path / "cookies.txt",
    )


# Matches the Booked 4.1.5 markup exactly: single-quoted `name='userId'`, and a
# separate hidden CSRF_TOKEN input on the same page.
SEARCH_PAGE_HTML = """<!doctype html>
<html><body>
  <form id="search-reservations-form">
    <input id="userId" type="hidden" name='userId' value="18"/>
    <input type="hidden" name='endDate' id="formattedEndDate"/>
    <input type="hidden" name='beginDate' id="formattedBeginDate"/>
    <input type="hidden" id="csrf_token" name="CSRF_TOKEN" value="CSRF-ABC" class="csrf_token"/>
  </form>
</body></html>
"""

SEARCH_PAGE_WITHOUT_USERID_HTML = """<!doctype html>
<html><body>
  <form id="search-reservations-form">
    <input type="hidden" name="CSRF_TOKEN" value="CSRF-ABC"/>
  </form>
</body></html>
"""

# Matches the real ``#reservation-list`` card fragment returned by
# Booked 4.1.5 (content-type text/html).
SEARCH_RESULTS_HTML = """
<div class="reservation-search-result-count">2 Reservations Found</div>
<div id="reservation-list">
  <div class="row0 reservation" data-seriesId="134" data-refnum="B09CF688">
    <div class="row">
      <div class="col-xs-12 col-md-3">
        <div class="search-reservation-title">Resource</div>
        <div class="resource search-reservation-data">SUPR OMEN Rehearsal Space</div>
        <div class="search-reservation-title">Owner</div>
        <div class="user search-reservation-data">Benjamin Goosman</div>
        <div class="search-reservation-title">Reference Number</div>
        <div class="referenceNumber search-reservation-data">B09CF688</div>
      </div>
      <div class="col-xs-12 col-md-3">
        <div class="search-reservation-title">Duration</div>
        <div>
          <div class="date inline-block">3/3/26 6:00 PM</div>
          -
          <div class="date inline-block">10:00 PM</div>
          <div class="duration search-reservation-data">4 hours</div>
        </div>
      </div>
      <div class="col-xs-12 col-md-5">
        <div class="search-reservation-title">Title</div>
        <div class="title search-reservation-data">Ben G</div>
      </div>
    </div>
  </div>
  <div class="row1 reservation" data-seriesId="116" data-refnum="F8281A48">
    <div class="row">
      <div class="col-xs-12 col-md-3">
        <div class="resource search-reservation-data">SUPR OMEN Rehearsal Space</div>
        <div class="user search-reservation-data">Benjamin Goosman</div>
      </div>
      <div class="col-xs-12 col-md-3">
        <div>
          <div class="date inline-block">3/10/26 6:00 PM</div>
          -
          <div class="date inline-block">3/11/26 8:00 PM</div>
        </div>
      </div>
      <div class="col-xs-12 col-md-5">
        <div class="title search-reservation-data">Multi-day</div>
      </div>
    </div>
  </div>
</div>
"""

RANGE_LIMIT_HTML = '<div class="alert alert-danger">Search range is limited to 180 days</div>'

SEARCH_RESULTS_JSON = (
    '{"reservations":[{"ReferenceNumber":"rn7","Label":"Lift",'
    '"StartDate":"1774792800","EndDate":"1774796400",'
    '"StartDateString":"2026-04-23","EndDateString":"2026-04-23",'
    '"StartTime":"10:00 AM","EndTime":"11:00 AM",'
    '"ResourceId":3,"ResourceName":"Squat Rack","IsOwner":true,"IsBuffer":false}],'
    '"lastRefresh":"2026-04-23T12:00:00+0000"}'
)


def test_get_current_user_id_from_search_page(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url="https://booked.test/Web/search-reservations.php", html=SEARCH_PAGE_HTML
    )
    with BookedClient(_settings(tmp_path)) as client:
        assert get_current_user_id(client) == "18"


def test_get_current_user_id_missing_raises(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url="https://booked.test/Web/search-reservations.php",
        html=SEARCH_PAGE_WITHOUT_USERID_HTML,
    )
    with BookedClient(_settings(tmp_path)) as client:
        with pytest.raises(RuntimeError, match="Could not determine userId"):
            get_current_user_id(client)


def test_get_current_user_id_detects_login_page(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url="https://booked.test/Web/search-reservations.php", html=LOGIN_PAGE_HTML
    )
    with BookedClient(_settings(tmp_path)) as client:
        with pytest.raises(AuthError):
            get_current_user_id(client)


def test_fetch_my_reservations_posts_csrf_and_userid(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url="https://booked.test/Web/search-reservations.php", html=SEARCH_PAGE_HTML
    )
    httpx_mock.add_response(
        url="https://booked.test/Web/search-reservations.php?action=search",
        method="POST",
        content=SEARCH_RESULTS_JSON.encode(),
        headers={"content-type": "application/json"},
    )

    with BookedClient(_settings(tmp_path)) as client:
        view = fetch_my_reservations(client, date(2026, 4, 23), date(2026, 5, 23))

    posted = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert len(posted) == 1
    req = posted[0]
    body = parse_qs(req.content.decode())
    assert body["userId"] == ["18"]
    assert body["AVAILABILITY_RANGE"] == ["daterange"]
    assert body["beginDate"] == ["2026-04-23 00:00"]
    assert body["endDate"] == ["2026-05-23 00:00"]
    assert body["CSRF_TOKEN"] == ["CSRF-ABC"]
    assert body["USER_LEVEL"] == ["1"]
    assert req.headers.get("x-requested-with") == "XMLHttpRequest"

    # Single GET — we do NOT fetch the dashboard for auto-detection anymore.
    urls = [str(r.url) for r in httpx_mock.get_requests()]
    assert not any("dashboard.php" in u for u in urls)

    assert view.user_id == "18"
    assert view.begin == "2026-04-23"
    assert view.end == "2026-05-23"
    assert len(view.bookings) == 1
    b = view.bookings[0]
    assert b.reference_number == "rn7"
    assert b.title == "Lift"
    assert b.resource_name == "Squat Rack"
    assert b.is_owner is True


def test_fetch_my_reservations_accepts_explicit_user_id(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url="https://booked.test/Web/search-reservations.php", html=SEARCH_PAGE_HTML
    )
    httpx_mock.add_response(
        url="https://booked.test/Web/search-reservations.php?action=search",
        method="POST",
        content=b"[]",
        headers={"content-type": "application/json"},
    )

    with BookedClient(_settings(tmp_path)) as client:
        view = fetch_my_reservations(client, date(2026, 4, 23), date(2026, 4, 24), user_id="99")

    assert view.user_id == "99"
    assert view.bookings == []


def test_fetch_my_reservations_parses_html_cards(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url="https://booked.test/Web/search-reservations.php", html=SEARCH_PAGE_HTML
    )
    httpx_mock.add_response(
        url="https://booked.test/Web/search-reservations.php?action=search",
        method="POST",
        html=SEARCH_RESULTS_HTML,
    )

    with BookedClient(_settings(tmp_path)) as client:
        view = fetch_my_reservations(client, date(2026, 3, 1), date(2026, 3, 31))

    assert len(view.bookings) == 2
    b1, b2 = view.bookings

    assert b1.reference_number == "B09CF688"
    assert b1.title == "Ben G"
    assert b1.resource_name == "SUPR OMEN Rehearsal Space"
    assert b1.owner == "Benjamin Goosman"
    assert b1.start_date == "2026-03-03"
    assert b1.start_time == "6:00 PM"
    # Same-day end: date copied from start, time-only parsed.
    assert b1.end_date == "2026-03-03"
    assert b1.end_time == "10:00 PM"
    assert b1.start == "2026-03-03T18:00"
    assert b1.end == "2026-03-03T22:00"

    assert b2.reference_number == "F8281A48"
    assert b2.title == "Multi-day"
    # Multi-day: both dates fully parsed.
    assert b2.start_date == "2026-03-10"
    assert b2.end_date == "2026-03-11"
    assert b2.end_time == "8:00 PM"


def test_fetch_my_reservations_range_limit_error(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url="https://booked.test/Web/search-reservations.php", html=SEARCH_PAGE_HTML
    )
    httpx_mock.add_response(
        url="https://booked.test/Web/search-reservations.php?action=search",
        method="POST",
        html=RANGE_LIMIT_HTML,
    )

    with BookedClient(_settings(tmp_path)) as client:
        with pytest.raises(RuntimeError, match="limited to 180 days"):
            fetch_my_reservations(client, date(2024, 1, 1), date(2026, 12, 31))


def test_fetch_my_reservations_login_redirect(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url="https://booked.test/Web/search-reservations.php", html=LOGIN_PAGE_HTML
    )

    with BookedClient(_settings(tmp_path)) as client:
        with pytest.raises(AuthError):
            fetch_my_reservations(client, date(2026, 4, 23), date(2026, 4, 24), user_id="18")
