"""Session login for Booked Scheduler's web front end."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from selectolax.parser import HTMLParser

from .client import BookedClient


class AuthError(RuntimeError):
    """Raised when login fails or the session is not authenticated."""


@dataclass(frozen=True)
class AuthStatus:
    authenticated: bool
    display_name: str | None = None
    detail: str | None = None


def _looks_like_login_page(html: str) -> bool:
    tree = HTMLParser(html)
    return tree.css_first('form#login') is not None or tree.css_first('input[name="password"]') is not None


def check_session(client: BookedClient) -> AuthStatus:
    """Verify the current session by requesting the dashboard and inspecting the response."""
    try:
        resp = client.http.get("/dashboard.php")
    except httpx.HTTPError as e:
        raise AuthError(f"Could not reach Booked at {client.settings.web_root}: {e}") from e

    if resp.status_code >= 500:
        raise AuthError(f"Booked returned HTTP {resp.status_code} checking session.")

    if _looks_like_login_page(resp.text):
        return AuthStatus(authenticated=False, detail="Session cookie is missing or expired.")

    tree = HTMLParser(resp.text)
    display_name = _extract_display_name(tree)
    return AuthStatus(authenticated=True, display_name=display_name)


def _extract_display_name(tree: HTMLParser) -> str | None:
    """Best-effort display name. Skip generic nav labels (e.g. \"Profile\")."""
    skip = {"profile", "my profile", "account"}

    for sel in (".user-name", "span.username", ".logged-in .name"):
        node = tree.css_first(sel)
        if node is None:
            continue
        text = node.text(strip=True)
        if text and text.lower() not in skip:
            return text

    for node in tree.css('a[href*="profile.php"]'):
        text = node.text(strip=True)
        if text and text.lower() not in skip:
            return text
    return None


def login(client: BookedClient, email: str, password: str, *, persist: bool = True) -> AuthStatus:
    """POST credentials to /Web/ and return authenticated status.

    Booked's login form posts to `/Web/` with fields:
      email, password, persistLogin (checkbox), captcha (hidden), resume (hidden), login (submit).
    """
    # Prime session: GET index.php so we have a PHPSESSID set on /Web.
    client.http.get("/index.php")

    data = {
        "email": email,
        "password": password,
        "captcha": "",
        "resume": "",
        "login": "submit",
    }
    if persist:
        data["persistLogin"] = "on"

    try:
        resp = client.http.post("/", data=data)
    except httpx.HTTPError as e:
        raise AuthError(f"Login request to {client.settings.web_root}/ failed: {e}") from e

    if resp.status_code >= 500:
        raise AuthError(f"Booked returned HTTP {resp.status_code} during login.")

    # After POST, Booked redirects; follow_redirects is on, so resp is the final page.
    if _looks_like_login_page(resp.text):
        raise AuthError(
            "Login rejected: returned to the login form. Check email/password "
            "(captcha may be required for this account)."
        )

    status = check_session(client)
    if not status.authenticated:
        raise AuthError(status.detail or "Login appeared to succeed but session is not authenticated.")
    return status
