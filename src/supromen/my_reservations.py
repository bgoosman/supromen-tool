"""Fetch the logged-in user's reservations for a date range.

Backed by ``POST /Web/search-reservations.php?action=search``, which Booked's
"Find a Reservation" UI uses. The POST body mirrors the hidden search form and
must carry the ``CSRF_TOKEN`` scraped from the shell page plus the caller's
numeric ``userId``.

Both values live as hidden inputs on ``/Web/search-reservations.php``::

    <input id="userId"     type="hidden" name='userId'     value="18"/>
    <input id="csrf_token" type="hidden" name='CSRF_TOKEN' value="..."/>

so a single GET yields everything needed to build the search POST.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

import httpx
from selectolax.parser import HTMLParser, Node

from .client import BookedClient
from .schedule import (
    Booking,
    ensure_authenticated,
    is_json_response,
    parse_reservations_json,
)


@dataclass
class MyReservationsView:
    user_id: str
    begin: str
    end: str
    bookings: list[Booking] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "begin": self.begin,
            "end": self.end,
            "bookings": [asdict(b) for b in self.bookings],
        }


_HIDDEN_USERID_RE = re.compile(
    r"""<input\b[^>]*\bname\s*=\s*['"]userId['"][^>]*\bvalue\s*=\s*['"](\d+)['"]""",
    re.IGNORECASE,
)


def _extract_hidden_value(html: str, name: str) -> str | None:
    """Pull the ``value`` of a hidden ``<input name=name>`` on the page.

    selectolax's CSS engine is used first; we fall back to a regex because
    Booked's templates mix single and double quotes (``name='userId'``).
    """
    tree = HTMLParser(html)
    for sel in (
        f'input[name="{name}"]',
        f"input[name='{name}']",
        f"input#{name}",
    ):
        node = tree.css_first(sel)
        if node is not None:
            val = node.attributes.get("value")
            if val:
                return val
    return None


def _extract_user_id(html: str) -> str | None:
    val = _extract_hidden_value(html, "userId")
    if val and val.isdigit():
        return val
    m = _HIDDEN_USERID_RE.search(html)
    return m.group(1) if m else None


def _fetch_search_shell(client: BookedClient) -> str:
    try:
        resp = client.http.get("/search-reservations.php")
    except httpx.HTTPError as e:
        raise RuntimeError(f"Failed to fetch search page: {e}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"Search page returned HTTP {resp.status_code}.")
    ensure_authenticated(resp.text)
    return resp.text


def get_current_user_id(client: BookedClient) -> str:
    """Scrape the authenticated user's numeric userId.

    Uses ``/Web/search-reservations.php``, which carries the current user's id
    as a hidden form input (``<input name='userId' value='18'/>``).
    """
    html = _fetch_search_shell(client)
    user_id = _extract_user_id(html)
    if not user_id:
        raise RuntimeError(
            "Could not determine userId from /Web/search-reservations.php. "
            "Please file a bug with your Booked version."
        )
    return user_id


def _extract_csrf_token(html: str) -> str | None:
    return _extract_hidden_value(html, "CSRF_TOKEN")


def _format_booked_datetime(d: date) -> str:
    # Matches the observed curl body: ``YYYY-MM-DD 00:00`` (URL-encoded as +).
    return f"{d.isoformat()} 00:00"


_RANGE_LIMIT_RE = re.compile(r"limited to\s+(\d+)\s+days", re.IGNORECASE)


def _node_text(node: Node | None) -> str:
    return node.text(strip=True) if node is not None else ""


def _parse_american_datetime(raw: str) -> tuple[str | None, str | None, str | None]:
    """Parse ``"3/3/26 6:00 PM"`` -> (iso_datetime, iso_date, time_str).

    If ``raw`` has no date component (e.g. ``"10:00 PM"``), all three are None.
    """
    raw = raw.strip()
    if not raw:
        return None, None, None
    for fmt in ("%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p"):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return dt.isoformat(timespec="minutes"), dt.date().isoformat(), dt.strftime("%I:%M %p").lstrip("0")
    return None, None, None


def _parse_time_only(raw: str) -> str | None:
    raw = raw.strip()
    for fmt in ("%I:%M %p",):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return dt.strftime("%I:%M %p").lstrip("0")
    return None


def _parse_search_html_results(html: str) -> list[Booking]:
    """Parse Booked's ``#reservation-list`` HTML fragment into Bookings.

    Each card looks like::

        <div class="row0 reservation" data-seriesId="134" data-refnum="B09CF688">
          ...
          <div class="resource search-reservation-data">Room Name</div>
          <div class="user search-reservation-data">Owner Name</div>
          <div class="referenceNumber search-reservation-data">B09CF688</div>
          <div class="date inline-block">3/3/26 6:00 PM</div>
          <div class="date inline-block">10:00 PM</div>
          <div class="duration search-reservation-data">4 hours</div>
          <div class="title search-reservation-data">Title</div>
          ...
        </div>
    """
    tree = HTMLParser(html)
    out: list[Booking] = []
    for card in tree.css("div.reservation[data-refnum]"):
        attrs = card.attributes
        ref = attrs.get("data-refnum")
        resource = _node_text(card.css_first(".resource.search-reservation-data"))
        owner = _node_text(card.css_first(".user.search-reservation-data"))
        title = _node_text(card.css_first(".title.search-reservation-data"))

        dates = [_node_text(n) for n in card.css(".date.inline-block")]
        start_iso = start_date = start_time = None
        end_iso = end_date = end_time = None
        if dates:
            start_iso, start_date, start_time = _parse_american_datetime(dates[0])
        if len(dates) > 1:
            end_iso, end_date, end_time = _parse_american_datetime(dates[1])
            if end_iso is None:
                # Same-day end ("10:00 PM") — reuse start date.
                t = _parse_time_only(dates[1])
                if t and start_date:
                    end_time = t
                    end_date = start_date
                    try:
                        end_iso = datetime.strptime(
                            f"{start_date} {t}", "%Y-%m-%d %I:%M %p"
                        ).isoformat(timespec="minutes")
                    except ValueError:
                        end_iso = None

        out.append(
            Booking(
                reference_number=ref,
                title=title or None,
                start=start_iso,
                end=end_iso,
                start_date=start_date,
                end_date=end_date,
                start_time=start_time,
                end_time=end_time,
                resource_id=None,
                resource_name=resource or None,
                owner=owner or None,
            )
        )
    return out


def fetch_my_reservations(
    client: BookedClient,
    begin: date,
    end: date,
    *,
    user_id: str | None = None,
) -> MyReservationsView:
    """Return the current user's reservations in ``[begin, end]``."""
    html = _fetch_search_shell(client)

    if user_id is None:
        user_id = _extract_user_id(html)
        if not user_id:
            raise RuntimeError(
                "Could not determine userId from /Web/search-reservations.php. "
                "Please file a bug with your Booked version."
            )

    csrf = _extract_csrf_token(html)
    if not csrf:
        raise RuntimeError("Could not find CSRF_TOKEN on the search page.")

    form = {
        "USER_LEVEL": "1",
        "userId": str(user_id),
        "reservationTitle": "",
        "reservationDescription": "",
        "referenceNumber": "",
        "AVAILABILITY_RANGE": "daterange",
        "beginDate": _format_booked_datetime(begin),
        "endDate": _format_booked_datetime(end),
        "CSRF_TOKEN": csrf,
    }

    try:
        resp = client.http.post(
            "/search-reservations.php",
            params={"action": "search"},
            data=form,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    except httpx.HTTPError as e:
        raise RuntimeError(f"Failed to search reservations: {e}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"Search returned HTTP {resp.status_code}.")

    range_limit = _RANGE_LIMIT_RE.search(resp.text)
    if range_limit and "alert-danger" in resp.text:
        raise RuntimeError(
            f"Booked rejected the search: range is limited to {range_limit.group(1)} "
            "days. Narrow --from/--to or use --upcoming with a smaller window."
        )

    bookings: list[Booking] = []
    if is_json_response(resp.text):
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            data = None
        # Some installs wrap rendered HTML in a JSON envelope, e.g. {"html": "..."}.
        if isinstance(data, dict) and isinstance(data.get("html"), str):
            bookings = _parse_search_html_results(data["html"])
        else:
            bookings, _ = parse_reservations_json(resp.text)
    else:
        bookings = _parse_search_html_results(resp.text)

    return MyReservationsView(
        user_id=str(user_id),
        begin=begin.isoformat(),
        end=end.isoformat(),
        bookings=bookings,
    )
