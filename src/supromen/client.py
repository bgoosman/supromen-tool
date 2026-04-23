"""HTTP client with persistent cookie jar for Booked Scheduler sessions."""

from __future__ import annotations

from http.cookiejar import LWPCookieJar
from pathlib import Path

import httpx

from .config import Settings

USER_AGENT = "supromen-cli/0.1 (+https://github.com/bgoosman)"


class BookedClient:
    """Thin wrapper around httpx.Client that persists cookies to disk."""

    def __init__(self, settings: Settings, *, timeout: float = 30.0) -> None:
        self.settings = settings
        self._jar = LWPCookieJar(filename=str(settings.cookie_file))
        if settings.cookie_file.is_file():
            try:
                self._jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                # Corrupted jar shouldn't block a fresh login.
                pass

        self.http = httpx.Client(
            base_url=settings.web_root,
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            cookies=httpx.Cookies(self._jar),
        )

    def save_cookies(self) -> Path:
        self.settings.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        jar = self.http.cookies.jar
        # httpx exposes a CookieJar under .jar; copy its contents into our LWPCookieJar
        # so we can write in Netscape-compatible LWP format.
        self._jar.clear()
        for cookie in jar:
            self._jar.set_cookie(cookie)
        self._jar.save(ignore_discard=True, ignore_expires=True)
        return self.settings.cookie_file

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "BookedClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
