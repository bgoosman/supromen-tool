"""Fetch and parse the Booked schedule view for a given date.

Booked 4.1.x loads the schedule shell from ``/Web/schedule.php?sd=YYYY-MM-DD``
and then populates the reservation grid via an AJAX **POST** to
``/Web/schedule.php?dr=reservations`` (the ``reservationLoadUrl`` from
``scheduleOpts``). The body is the hidden ``#fetchReservationsForm`` that ships
with the shell page and carries ``beginDate``, ``endDate``, ``scheduleId``,
``LAST_REFRESH``, ``CSRF_TOKEN``, and filter fields.

The response is JSON of the form::

    {"reservations":[{...PascalCase fields...}], "lastRefresh":"..."}

with ``StartDate`` / ``EndDate`` as Unix timestamps, ``StartDateString`` /
``StartTime`` for display, ``Label`` for the reservation title, and boolean
flags like ``IsPending``, ``IsPast``, ``IsOwner``, ``IsParticipant``.

Resources are listed in the shell page as ``reservationResources.push({...})``
JavaScript blocks; we scrape those so the CLI can show what room(s) exist even
when there are no reservations.

Some older installs inline an HTML slot grid instead. We still fall back to
parsing that when the response is not JSON.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx
from selectolax.parser import HTMLParser, Node

from .auth import AuthError, _looks_like_login_page
from .client import BookedClient

SLOT_STATE_CLASSES = {
    "reservable": "reservable",
    "reserved": "reserved",
    "mine": "mine",
    "participating": "participating",
    "pending": "pending",
    "past": "past",
    "restricted": "restricted",
    "unreservable": "unreservable",
}


@dataclass
class Slot:
    resource_id: str | None
    start: str | None
    end: str | None
    label: str
    states: list[str] = field(default_factory=list)
    reference_number: str | None = None


@dataclass
class ResourceRow:
    resource_id: str | None
    name: str
    slots: list[Slot] = field(default_factory=list)


@dataclass
class EmbeddedResource:
    """A resource row from ``reservationResources.push`` in the shell page."""

    resource_id: str
    name: str


@dataclass
class Booking:
    """One reservation from the ``dr=reservations`` JSON payload."""

    reference_number: str | None
    title: str | None
    start: str | None
    end: str | None
    start_date: str | None = None
    end_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    resource_id: str | None = None
    resource_name: str | None = None
    owner: str | None = None
    is_pending: bool | None = None
    is_past: bool | None = None
    is_owner: bool | None = None
    is_participant: bool | None = None


@dataclass
class ScheduleView:
    date: str
    schedule_name: str | None
    date_label: str | None
    resources: list[ResourceRow] = field(default_factory=list)
    bookings: list[Booking] = field(default_factory=list)
    embedded_resources: list[EmbeddedResource] = field(default_factory=list)
    last_refresh: str | None = None

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "schedule_name": self.schedule_name,
            "date_label": self.date_label,
            "last_refresh": self.last_refresh,
            "embedded_resources": [asdict(r) for r in self.embedded_resources],
            "bookings": [asdict(b) for b in self.bookings],
            "resources": [
                {**asdict(row), "slots": [asdict(s) for s in row.slots]}
                for row in self.resources
            ],
        }


def _format_date(d: date) -> str:
    # Booked accepts loose ISO-ish dates (schedule.php?sd=2026-3-29 works).
    return d.isoformat()


def _ensure_authenticated(html: str) -> None:
    if _looks_like_login_page(html):
        raise AuthError(
            "Not authenticated. Run `supromen auth login` first (or pass --email/--password)."
        )


def _extract_schedule_name(tree: HTMLParser) -> str | None:
    selected = tree.css_first('select#schedules option[selected]')
    if selected is not None:
        text = selected.text(strip=True)
        if text:
            return text
    # Fallback: page title often includes the schedule name.
    title = tree.css_first("title")
    if title:
        return title.text(strip=True) or None
    return None


def _extract_date_label(tree: HTMLParser) -> str | None:
    node = tree.css_first(".schedule-date-range") or tree.css_first(".page-header h1")
    return node.text(strip=True) if node else None


def _classes(node: Node) -> list[str]:
    raw = node.attributes.get("class") or ""
    return [c for c in raw.split() if c]


def _slot_states(classes: list[str]) -> list[str]:
    return [c for c in classes if c in SLOT_STATE_CLASSES]


def _is_json_response(text: str) -> bool:
    s = text.lstrip()
    return bool(s) and s[0] in "{["


def _extract_embedded_resources(html: str) -> list[EmbeddedResource]:
    """Parse ``reservationResources.push({ ... });`` blocks from the shell script."""
    out: list[EmbeddedResource] = []
    for m in re.finditer(r"reservationResources\.push\(\{([\s\S]*?)\}\);", html):
        block = m.group(1)
        id_m = re.search(r"\bid:\s*(\d+)", block)
        name_m = re.search(r'\bname:\s*"([^"]*)"', block)
        if id_m and name_m:
            out.append(EmbeddedResource(resource_id=id_m.group(1), name=name_m.group(1)))
    return out


def _first(d: dict, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _epoch_to_iso(value) -> str | None:
    try:
        ts = int(str(value))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _booking_from_api(obj: object, resource_map: dict[str, str] | None = None) -> Booking | None:
    if not isinstance(obj, dict):
        return None
    o = obj

    ref = _first(o, "ReferenceNumber", "referenceNumber", "reference_number")
    title = _first(o, "Label", "title", "summary", "Title")

    start_raw = _first(o, "StartDate", "startDate", "start", "startDateTime")
    end_raw = _first(o, "EndDate", "endDate", "end", "endDateTime")
    start_iso = _epoch_to_iso(start_raw) if start_raw is not None else None
    end_iso = _epoch_to_iso(end_raw) if end_raw is not None else None
    start_date = _first(o, "StartDateString", "startDate")
    end_date = _first(o, "EndDateString", "endDate")
    start_time = _first(o, "StartTime", "startTime")
    end_time = _first(o, "EndTime", "endTime")

    rid = _first(o, "ResourceId", "resourceId", "resource_id")
    rname = _first(o, "ResourceName", "resourceName", "resource")
    if not rname and rid is not None and resource_map:
        rname = resource_map.get(str(rid))

    owner_first = str(_first(o, "FirstName", "firstName", "first_name") or "").strip()
    owner_last = str(_first(o, "LastName", "lastName", "last_name") or "").strip()
    owner = (owner_first + " " + owner_last).strip() or _first(
        o, "OwnerName", "owner", "ownerName"
    )

    def _bool(k1, k2=None):
        v = o.get(k1, o.get(k2) if k2 else None)
        return bool(v) if v is not None else None

    return Booking(
        reference_number=str(ref) if ref is not None else None,
        title=str(title) if title is not None else None,
        start=start_iso or (str(start_raw) if start_raw is not None else None),
        end=end_iso or (str(end_raw) if end_raw is not None else None),
        start_date=str(start_date) if start_date is not None else None,
        end_date=str(end_date) if end_date is not None else None,
        start_time=str(start_time) if start_time is not None else None,
        end_time=str(end_time) if end_time is not None else None,
        resource_id=str(rid) if rid is not None else None,
        resource_name=str(rname) if rname is not None else None,
        owner=str(owner) if owner is not None else None,
        is_pending=_bool("IsPending", "isPending"),
        is_past=_bool("IsPast", "isPast"),
        is_owner=_bool("IsOwner", "isOwner"),
        is_participant=_bool("IsParticipant", "isParticipant"),
    )


def _parse_reservations_json(
    text: str, resource_map: dict[str, str] | None = None
) -> tuple[list[Booking], str | None]:
    data = json.loads(text)
    if isinstance(data, list):
        raw_list: list = data
        last: object = None
    elif isinstance(data, dict):
        raw_list_obj = data.get("reservations")
        raw_list = raw_list_obj if isinstance(raw_list_obj, list) else []
        last = data.get("lastRefresh") or data.get("last_refresh")
    else:
        return [], None

    bookings: list[Booking] = []
    for item in raw_list:
        if isinstance(item, dict) and item.get("IsBuffer") is True:
            continue
        b = _booking_from_api(item, resource_map=resource_map)
        if b is not None:
            bookings.append(b)
    last_s = str(last) if last is not None else None
    return bookings, last_s


def _parse_grid(html: str) -> list[ResourceRow]:
    """Parse the AJAX reservations grid into resource rows with slots."""
    tree = HTMLParser(html)
    rows: list[ResourceRow] = []

    # The grid renders each resource as a `<tr>` that has a `<th>` with the
    # resource name. Not every `<tr>` is a resource (some are group headers),
    # so key off the presence of a resource header cell.
    for tr in tree.css("tr"):
        header = tr.css_first("th.resource, th.resourceHeader, th")
        name_node = tr.css_first(".resourceName, th a, th span.resource-name")
        if name_node is None and header is None:
            continue

        cells = tr.css("td")
        if not cells:
            continue

        name = (name_node or header).text(strip=True) if (name_node or header) else ""
        resource_id = None
        if header is not None:
            resource_id = header.attributes.get("data-resource-id") or header.attributes.get("id")

        row = ResourceRow(resource_id=resource_id, name=name)
        for td in cells:
            classes = _classes(td)
            states = _slot_states(classes)
            if not states:
                continue
            attrs = td.attributes
            anchor = td.css_first("a")
            row.slots.append(
                Slot(
                    resource_id=attrs.get("data-resource-id") or resource_id,
                    start=attrs.get("data-start") or attrs.get("data-starttime"),
                    end=attrs.get("data-end") or attrs.get("data-endtime"),
                    label=(anchor.text(strip=True) if anchor else td.text(strip=True)),
                    states=states,
                    reference_number=attrs.get("data-reference-number"),
                )
            )
        if row.slots or row.name:
            rows.append(row)
    return rows


def _booking_overlaps(b: Booking, target_iso: str) -> bool:
    """Keep bookings whose day range includes ``target_iso`` (YYYY-MM-DD)."""
    start = b.start_date or (b.start or "")[:10]
    end = b.end_date or (b.end or "")[:10] or start
    if not start:
        return True
    return start <= target_iso <= (end or start)


def _extract_fetch_form(html: str) -> dict[str, str]:
    """Extract the ``#fetchReservationsForm`` hidden inputs from the shell HTML."""
    tree = HTMLParser(html)
    form = tree.css_first("form#fetchReservationsForm")
    fields: dict[str, str] = {}
    if form is None:
        return fields
    for inp in form.css("input"):
        name = inp.attributes.get("name")
        if not name:
            continue
        fields[name] = inp.attributes.get("value") or ""
    return fields


def fetch_schedule(client: BookedClient, target: date) -> ScheduleView:
    sd = _format_date(target)
    try:
        shell = client.http.get("/schedule.php", params={"sd": sd})
    except httpx.HTTPError as e:
        raise RuntimeError(f"Failed to fetch schedule shell for {sd}: {e}") from e
    if shell.status_code >= 400:
        raise RuntimeError(f"Schedule shell returned HTTP {shell.status_code}")
    _ensure_authenticated(shell.text)

    shell_tree = HTMLParser(shell.text)
    embedded = _extract_embedded_resources(shell.text)
    resource_map = {r.resource_id: r.name for r in embedded}

    form = _extract_fetch_form(shell.text)
    # Narrow the range to exactly one day; the shell form defaults to a week.
    next_day = (target + timedelta(days=1)).isoformat()
    form["beginDate"] = sd
    form["endDate"] = next_day

    try:
        grid = client.http.post(
            "/schedule.php",
            params={"dr": "reservations"},
            data=form,
        )
    except httpx.HTTPError as e:
        raise RuntimeError(f"Failed to fetch reservations for {sd}: {e}") from e
    if grid.status_code >= 400:
        raise RuntimeError(f"Reservations fetch returned HTTP {grid.status_code}")

    resources: list[ResourceRow] = []
    bookings: list[Booking] = []
    last_refresh: str | None = None

    if _is_json_response(grid.text):
        bookings, last_refresh = _parse_reservations_json(grid.text, resource_map=resource_map)
        bookings = [b for b in bookings if _booking_overlaps(b, sd)]
    else:
        resources = _parse_grid(grid.text)

    if not resources and not _is_json_response(grid.text):
        resources = _parse_grid(shell.text)

    view = ScheduleView(
        date=sd,
        schedule_name=_extract_schedule_name(shell_tree),
        date_label=_extract_date_label(shell_tree),
        resources=resources,
        bookings=bookings,
        embedded_resources=embedded,
        last_refresh=last_refresh,
    )

    return view
