"""Create reservations through Booked 4.2's JSON reservation API.

The 4.2 reservation page (``/Web/reservation/?rid=..&sid=..&rd=..``) is a React
app. It reads the page-level ``const csrf = "..."`` and talks to
``/Web/api/reservation.php``:

* ``GET  ?api=load&sd=..&ed=..&rid=..&sid=..&rd=..`` returns a ready-to-save
  reservation template (owner, resource, UTC start/end) for the requested slot.
* ``POST ?action=create`` takes multipart form data: ``request`` (JSON of
  ``{reservation, retryParameters, updateScope, payment}``), ``CSRF_TOKEN`` and
  ``BROWSER_TIMEZONE``, plus an ``X-Csrf-Token`` header. The JSON reply carries
  ``success``, ``referenceNumber`` and ``errors``.

Availability comes from ``GET /Web/api/resources.php?api=check-availability``,
which returns ``{"data": {"ids": [...]}}`` listing the resource ids that clash
with the requested span (empty means free).

Times are passed in Booked's "system" format (``YYYY-MM-DD HH:MM``) and are
interpreted in the user's profile timezone.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone

import httpx

from .client import BookedClient
from .openings import _resolve_tz
from .schedule import (
    _extract_embedded_resources,
    _extract_fetch_form,
    ensure_authenticated,
)

_SLOT_RE = re.compile(
    r"^(?P<day>\d{4}-\d{1,2}-\d{1,2})@(?P<start>\d{1,2}:\d{2})-(?P<end>\d{1,2}:\d{2})$"
)
_CSRF_RE = re.compile(r"""const\s+csrf\s*=\s*["']([^"']+)["']""")


class ReserveError(RuntimeError):
    """Raised when the reservation API rejects or cannot process a request."""


@dataclass(frozen=True)
class Slot:
    day: date
    start: time
    end: time

    @property
    def hours(self) -> float:
        s = datetime.combine(self.day, self.start)
        e = datetime.combine(self.day, self.end)
        return round((e - s).total_seconds() / 3600, 2)

    @property
    def system_start(self) -> str:
        return f"{self.day.isoformat()} {self.start.strftime('%H:%M')}"

    @property
    def system_end(self) -> str:
        return f"{self.day.isoformat()} {self.end.strftime('%H:%M')}"

    def to_dict(self) -> dict:
        return {
            "date": self.day.isoformat(),
            "start": self.start.strftime("%H:%M"),
            "end": self.end.strftime("%H:%M"),
            "hours": self.hours,
        }


def parse_slot(raw: str) -> Slot:
    """Parse ``YYYY-MM-DD@HH:MM-HH:MM`` (24h, local time) into a :class:`Slot`."""
    m = _SLOT_RE.match(raw.strip())
    if not m:
        raise ValueError(f"Unrecognized slot {raw!r}. Use YYYY-MM-DD@HH:MM-HH:MM, e.g. 2026-11-11@12:00-15:00.")
    y, mo, d = (int(p) for p in m.group("day").split("-"))
    try:
        day = date(y, mo, d)
        start = time(*(int(p) for p in m.group("start").split(":")))
        end = time(*(int(p) for p in m.group("end").split(":")))
    except ValueError as e:
        raise ValueError(f"Invalid slot {raw!r}: {e}") from e
    if end <= start:
        raise ValueError(f"Slot {raw!r} ends before it starts.")
    return Slot(day=day, start=start, end=end)


@dataclass
class ReservationContext:
    """Per-session values every reservation call needs."""

    resource_id: str
    resource_name: str | None
    schedule_id: str
    csrf: str
    tz_name: str = "America/New_York"


@dataclass
class SlotCheck:
    slot: Slot
    available: bool
    template: dict | None = None
    problem: str | None = None

    def to_dict(self) -> dict:
        return {**self.slot.to_dict(), "available": self.available, "problem": self.problem}


@dataclass
class ReserveResult:
    slot: Slot
    success: bool
    reference_number: str | None = None
    requires_approval: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            **self.slot.to_dict(),
            "success": self.success,
            "reference_number": self.reference_number,
            "requires_approval": self.requires_approval,
            "errors": self.errors,
        }


def _api_headers(ctx: ReservationContext, *, json_body: bool = True) -> dict[str, str]:
    headers = {"X-Csrf-Token": ctx.csrf, "Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _refresh_csrf(ctx: ReservationContext, resp: httpx.Response) -> None:
    # The React client swaps in any rotated token the API hands back.
    token = resp.headers.get("X-Csrf-Token")
    if token:
        ctx.csrf = token


def _json_data(resp: httpx.Response, what: str) -> dict:
    try:
        payload = resp.json()
    except ValueError as e:
        ensure_authenticated(resp.text)
        raise ReserveError(f"{what} returned non-JSON (HTTP {resp.status_code}).") from e
    if resp.status_code >= 400:
        errors = payload.get("errors") if isinstance(payload, dict) else None
        raise ReserveError(f"{what} returned HTTP {resp.status_code}: {errors or payload}")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ReserveError(f"{what} returned an unexpected payload.")
    return data


def open_context(
    client: BookedClient,
    *,
    day: date,
    resource_id: str | None = None,
    tz_name: str = "America/New_York",
) -> ReservationContext:
    """Resolve resource/schedule ids and scrape the reservation page CSRF token."""
    try:
        shell = client.http.get("/schedule.php", params={"sd": day.isoformat()})
    except httpx.HTTPError as e:
        raise ReserveError(f"Failed to fetch schedule shell: {e}") from e
    ensure_authenticated(shell.text)

    resources = _extract_embedded_resources(shell.text)
    schedule_id = _extract_fetch_form(shell.text).get("scheduleId")
    if not schedule_id:
        raise ReserveError("Could not find scheduleId on the schedule page.")

    if resource_id is None:
        if len(resources) != 1:
            names = ", ".join(f"{r.resource_id}={r.name}" for r in resources) or "none found"
            raise ReserveError(f"Pass --resource-id; this schedule has: {names}.")
        resource_id = resources[0].resource_id
    resource_name = next((r.name for r in resources if r.resource_id == str(resource_id)), None)

    try:
        page = client.http.get(
            "/reservation/",
            params={"rid": resource_id, "sid": schedule_id, "rd": day.isoformat()},
        )
    except httpx.HTTPError as e:
        raise ReserveError(f"Failed to fetch reservation page: {e}") from e
    ensure_authenticated(page.text)
    m = _CSRF_RE.search(page.text)
    if not m:
        raise ReserveError("Could not find the CSRF token on the reservation page.")

    return ReservationContext(
        resource_id=str(resource_id),
        resource_name=resource_name,
        schedule_id=str(schedule_id),
        csrf=m.group(1),
        tz_name=tz_name,
    )


def _template_matches(template: dict, slot: Slot, tz_name: str) -> bool:
    """True when the server's template covers exactly the requested local span."""
    tz = _resolve_tz(tz_name)

    def _local(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
        return dt.astimezone(tz).replace(tzinfo=None)

    want_start = datetime.combine(slot.day, slot.start)
    want_end = datetime.combine(slot.day, slot.end)
    return _local(template.get("start")) == want_start and _local(template.get("end")) == want_end


def check_slot(
    client: BookedClient,
    ctx: ReservationContext,
    slot: Slot,
    *,
    now: datetime | None = None,
) -> SlotCheck:
    """Read-only preflight: is the slot free, and does the server accept its times?"""
    start_at = datetime.combine(slot.day, slot.start, tzinfo=_resolve_tz(ctx.tz_name))
    if start_at <= (now or datetime.now(timezone.utc)):
        return SlotCheck(slot=slot, available=False, problem="in the past")

    try:
        avail = client.http.get(
            "/api/resources.php",
            params={
                "api": "check-availability",
                "rid": ctx.resource_id,
                "sd": slot.system_start,
                "ed": slot.system_end,
                "sid": ctx.schedule_id,
                "rn": "",
                "tz": ctx.tz_name,
            },
            headers=_api_headers(ctx),
        )
    except httpx.HTTPError as e:
        raise ReserveError(f"Availability check failed: {e}") from e
    _refresh_csrf(ctx, avail)
    clashes = {str(i) for i in _json_data(avail, "Availability check").get("ids") or []}
    if ctx.resource_id in clashes:
        return SlotCheck(slot=slot, available=False, problem="already booked")

    try:
        loaded = client.http.get(
            "/api/reservation.php",
            params={
                "api": "load",
                "sd": slot.system_start,
                "ed": slot.system_end,
                "sid": ctx.schedule_id,
                "rid": ctx.resource_id,
                "rd": slot.day.isoformat(),
                "rn": "",
                "pid": "",
                "srn": "",
                "tz": ctx.tz_name,
            },
            headers=_api_headers(ctx),
        )
    except httpx.HTTPError as e:
        raise ReserveError(f"Loading reservation template failed: {e}") from e
    _refresh_csrf(ctx, loaded)
    template = _json_data(loaded, "Reservation template").get("reservation")
    if not isinstance(template, dict):
        return SlotCheck(slot=slot, available=False, problem="server returned no template")
    if not _template_matches(template, slot, ctx.tz_name):
        return SlotCheck(
            slot=slot,
            available=False,
            template=template,
            problem=f"server adjusted times to {template.get('start')}..{template.get('end')}",
        )
    return SlotCheck(slot=slot, available=True, template=template)


def create_reservation(
    client: BookedClient,
    ctx: ReservationContext,
    check: SlotCheck,
    *,
    title: str | None = None,
) -> ReserveResult:
    """Book a slot that passed :func:`check_slot`. This writes to Booked."""
    if not check.available or check.template is None:
        raise ReserveError(f"Refusing to book unchecked or unavailable slot {check.slot.system_start}.")

    reservation = dict(check.template)
    if title:
        reservation["title"] = title
    request = {
        "reservation": reservation,
        "retryParameters": [],
        "updateScope": "full",
        "payment": None,
    }
    # Multipart, matching the browser's FormData; (None, value) sends plain fields.
    form = {
        "request": (None, json.dumps(request)),
        "CSRF_TOKEN": (None, ctx.csrf),
        "BROWSER_TIMEZONE": (None, ctx.tz_name),
    }
    try:
        resp = client.http.post(
            "/api/reservation.php",
            params={"action": "create"},
            files=form,
            headers=_api_headers(ctx, json_body=False),
        )
    except httpx.HTTPError as e:
        raise ReserveError(f"Create request failed: {e}") from e
    _refresh_csrf(ctx, resp)
    data = _json_data(resp, "Create reservation")

    errors = [str(e) for e in (data.get("errors") or [])]
    return ReserveResult(
        slot=check.slot,
        success=bool(data.get("success")),
        reference_number=data.get("referenceNumber"),
        requires_approval=bool(data.get("requiresApproval")),
        errors=errors,
    )
