from __future__ import annotations

from pathlib import Path

import pytest

from supromen.auth import AuthError, login
from supromen.client import BookedClient
from supromen.config import Settings

from .fixtures import DASHBOARD_HTML, LOGIN_PAGE_HTML


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        base_url="https://booked.test",
        email=None,
        password=None,
        cookie_file=tmp_path / "cookies.txt",
    )


def test_login_success_persists_cookie(httpx_mock, tmp_path):
    httpx_mock.add_response(url="https://booked.test/Web/index.php", html=LOGIN_PAGE_HTML)
    httpx_mock.add_response(
        url="https://booked.test/Web/",
        method="POST",
        html=DASHBOARD_HTML,
        headers={"set-cookie": "PHPSESSID=session-value; Path=/Web"},
    )
    httpx_mock.add_response(url="https://booked.test/Web/dashboard.php", html=DASHBOARD_HTML)

    with BookedClient(_settings(tmp_path)) as client:
        status = login(client, "user@example.com", "pw")
        assert status.authenticated
        assert status.display_name == "Ben Goosman"
        path = client.save_cookies()
        assert path.is_file()
        assert "PHPSESSID" in path.read_text()


def test_login_rejects_bad_credentials(httpx_mock, tmp_path):
    httpx_mock.add_response(url="https://booked.test/Web/index.php", html=LOGIN_PAGE_HTML)
    # Booked re-renders the login page on failure.
    httpx_mock.add_response(url="https://booked.test/Web/", method="POST", html=LOGIN_PAGE_HTML)

    with BookedClient(_settings(tmp_path)) as client:
        with pytest.raises(AuthError):
            login(client, "user@example.com", "wrong")
